"""Unit tests for ccremote's pure primitives — the parse/classify/hysteresis logic that used
to be fused into _fetch/_write_health and untestable without a live ssh."""
import json

import ccremote
from ccremote import (atomic_write_json, classify_ssh_failure, load_hosts, make_health,
                      parse_sentinel, parse_snapshot)


def mh(prev, *, ok, error=None, age_s=None, remote_mtime=None, nsessions=None,
       now=1000.0, interval_s=3.0):
    return make_health(prev, ok=ok, error=error, age_s=age_s, remote_mtime=remote_mtime,
                       nsessions=nsessions, now=now, interval_s=interval_s)


# ---------------------------------------------------------------- parse_sentinel

def test_sentinel_happy_path():
    payload, age, mtime = parse_sentinel("#cc# 1000 998\n[{}]")
    assert payload == "[{}]" and age == 2.0 and mtime == 998


def test_sentinel_missing_header_fails_open():
    assert parse_sentinel('[{"a":1}]') == ('[{"a":1}]', None, None)


def test_sentinel_non_digit_stat_output():
    # `stat` failed on the remote → header is `#cc# 1000 ` (empty mtime field)
    assert parse_sentinel("#cc# 1000 \n[]") == ("[]", None, None)
    assert parse_sentinel("#cc# 1000 nope\n[]") == ("[]", None, None)


def test_sentinel_negative_age_clamps_to_zero():
    payload, age, mtime = parse_sentinel("#cc# 998 1000\n[]")
    assert payload == "[]" and age == 0.0 and mtime == 1000


def test_sentinel_header_only_no_payload():
    assert parse_sentinel("#cc# 1000 998") == ("", 2.0, 998)


# ---------------------------------------------------------- classify_ssh_failure

def never(*_):
    raise AssertionError("master_gone must not be probed when stderr is conclusive")


def test_classify_success():
    assert classify_ssh_failure(0, "anything", never) is None


def test_classify_auth_patterns():
    assert classify_ssh_failure(255, "jke2@psc: Permission denied (password).", never) == "auth"
    assert classify_ssh_failure(255, "Host key verification failed.", never) == "auth"


def test_classify_unreachable_patterns():
    assert classify_ssh_failure(255, "ssh: connect: Connection timed out", never) == "unreachable"
    assert classify_ssh_failure(255, "Could not resolve hostname x", never) == "unreachable"


def test_classify_ambiguous_255_probes_master():
    assert classify_ssh_failure(255, "something odd", lambda: True) == "auth"
    assert classify_ssh_failure(255, "something odd", lambda: False) == "unreachable"
    assert classify_ssh_failure(255, None, lambda: False) == "unreachable"


def test_classify_nonzero_rc_is_no_file():
    assert classify_ssh_failure(1, "cat: no such file", never) == "no-file"


# ----------------------------------------------------------------- parse_snapshot

def test_parse_snapshot():
    assert parse_snapshot('[{"session_id": "x"}]') == [{"session_id": "x"}]
    assert parse_snapshot("not json") is None
    assert parse_snapshot('{"a": 1}') is None      # non-list → garbled
    assert parse_snapshot(None) is None


# ------------------------------------------------------------------- make_health

def test_first_failure_is_degraded_not_down():
    h = mh({}, ok=False, error="timeout")
    assert h["state"] == "degraded" and h["consecutive_failures"] == 1
    assert h["bad_since"] == 1000.0


def test_third_failure_is_down():
    h1 = mh({}, ok=False, error="timeout", now=1000.0)
    h2 = mh(h1, ok=False, error="timeout", now=1003.0)
    h3 = mh(h2, ok=False, error="timeout", now=1006.0)
    assert h1["state"] == h2["state"] == "degraded"
    assert h3["state"] == "down" and h3["consecutive_failures"] == 3
    assert h3["bad_since"] == 1000.0               # streak start sticks


def test_old_streak_is_down_even_at_two_fails():
    h1 = mh({}, ok=False, error="unreachable", now=1000.0)
    h2 = mh(h1, ok=False, error="unreachable", now=1000.0 + ccremote.DOWN_AFTER_BAD_S)
    assert h2["consecutive_failures"] == 2 and h2["state"] == "down"


def test_auth_goes_down_on_second_tick():
    h1 = mh({}, ok=False, error="auth", now=1000.0)
    h2 = mh(h1, ok=False, error="auth", now=1003.0)
    assert h1["state"] == "degraded" and h2["state"] == "down"


def test_recovery_resets_streak():
    bad = mh(mh({}, ok=False, error="timeout"), ok=False, error="timeout", now=1003.0)
    good = mh(bad, ok=True, nsessions=2, age_s=1.0, now=1006.0)
    assert good["state"] == "ok" and good["consecutive_failures"] == 0
    assert good["bad_since"] is None and good["sessions"] == 2
    # and a fresh failure after recovery starts a NEW streak
    again = mh(good, ok=False, error="timeout", now=1009.0)
    assert again["state"] == "degraded" and again["bad_since"] == 1009.0


def test_ring_appends_dedups_and_caps():
    h = mh({}, ok=True, remote_mtime=100)
    assert h["remote_mtimes"] == [100]
    h = mh(h, ok=True, remote_mtime=100)           # unchanged mtime (frozen content) → no append
    assert h["remote_mtimes"] == [100]
    for m in range(101, 120):
        h = mh(h, ok=True, remote_mtime=m)
    assert len(h["remote_mtimes"]) == ccremote.CADENCE_RING
    assert h["remote_mtimes"][-1] == 119


def test_cadence_is_median_of_deltas():
    h = {}
    for m in (100, 102, 104, 116):                 # deltas 2, 2, 12 → median 2
        h = mh(h, ok=True, remote_mtime=m)
    assert h["remote_write_cadence_s"] == 2.0
    assert mh({}, ok=True, remote_mtime=100)["remote_write_cadence_s"] is None


def test_v1_prev_sidecar_tolerated():
    v1 = {"synced_at": 999.0, "ok": False, "error": "unreachable",
          "consecutive_failures": 2, "remote_status_age_s": None, "sessions": None}
    h = mh(v1, ok=False, error="unreachable")
    assert h["consecutive_failures"] == 3 and h["state"] == "down"
    assert h["v"] == 2 and h["remote_mtimes"] == []
    junk = mh({"consecutive_failures": "lots", "remote_mtimes": "nope", "bad_since": "x"},
              ok=False, error="timeout")
    assert junk["consecutive_failures"] == 1 and junk["state"] == "degraded"
    assert junk["remote_mtimes"] == [] and junk["bad_since"] == 1000.0


def test_schema_carries_thresholds_for_consumers():
    h = mh({}, ok=True, age_s=1.2, nsessions=3, interval_s=3.0)
    assert h["interval_s"] == 3.0 and h["ssh_timeout_s"] == ccremote.SSH_TIMEOUT
    assert h["remote_status_age_s"] == 1.2 and h["synced_at"] == 1000.0


# ------------------------------------------------------------- atomic_write_json

def test_atomic_write_json_roundtrip(tmp_path):
    p = tmp_path / "sub" / "x.json"                # parent dir created on demand
    assert atomic_write_json(p, {"a": 1}) is True
    assert json.loads(p.read_text()) == {"a": 1}
    assert not list(tmp_path.glob("**/*.tmp"))     # tmp twin cleaned up by the replace


def test_atomic_write_json_unwritable_dir_fails_open(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert atomic_write_json(ro / "x.json", {"a": 1}) is False
    finally:
        ro.chmod(0o700)


# -------------------------------------------------------------------- load_hosts

def test_load_hosts_grammar(tmp_path):
    f = tmp_path / "remotes"
    f.write_text("# comment\n"
                 "psc  /jet/home/jke2/.claude/run/status.json   # trailing comment\n"
                 "\n"
                 "user@gpu2\n"
                 "psc  /elsewhere/status.json\n")   # duplicate host — first spelling wins
    hosts = load_hosts(f)
    assert hosts == [("psc", "/jet/home/jke2/.claude/run/status.json"),
                     ("user@gpu2", ccremote.REMOTE_STATUS)]
    assert load_hosts(tmp_path / "missing") == []


# ----------------------------------------------------------- remote markers

def test_build_mark_cmd():
    assert (ccremote.build_mark_cmd(ccremote.REMOTE_ACK, "abc-123", True)
            == "mkdir -p ~/.claude/run/ack && touch ~/.claude/run/ack/abc-123")
    assert (ccremote.build_mark_cmd(ccremote.REMOTE_DISMISS, "abc-123", False)
            == "rm -f ~/.claude/run/dismissed/abc-123")


def test_remote_mark_rejects_bad_sid():
    # An injection-shaped or empty sid must fail before any ssh is spawned.
    for sid in ("", None, "x; rm -rf /", "a b", "$(boom)"):
        assert ccremote.remote_ack("host", sid) is False
        assert ccremote.remote_dismiss("host", sid) is False
