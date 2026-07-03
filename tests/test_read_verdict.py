"""The consumer read rule. One verdict table runs against BOTH copies — ccstatus.read_verdict
and ccbar._read_verdict — so the deliberate stdlib duplication is provably in lockstep."""
import json
import time

import pytest

import ccbar
import ccstatus
from ccstatus import load_remote_health, load_remote_sessions

V2_OK = {"v": 2, "state": "ok", "ok": True, "error": None, "consecutive_failures": 0,
         "bad_since": None, "interval_s": 3.0, "ssh_timeout_s": 20.0,
         "remote_status_age_s": 1.0, "remote_write_cadence_s": 2.5, "sessions": 1}

# (case-name, health, sidecar_age, expected)
VERDICTS = [
    ("healthy", V2_OK, 2.0, "ok"),
    ("no sidecar", None, None, "missing"),
    ("garbled sidecar", "not-a-dict", 1.0, "missing"),
    ("degraded passes through", {**V2_OK, "state": "degraded", "ok": False,
                                 "error": "timeout", "remote_status_age_s": None}, 2.0,
     "degraded"),
    ("down passes through", {**V2_OK, "state": "down", "ok": False,
                             "error": "unreachable", "remote_status_age_s": None}, 2.0, "down"),
    # stale-mirror threshold = max(15, 3*interval + timeout); defaults here ⇒ 29s
    ("sidecar just fresh", V2_OK, 29.0, "ok"),
    ("sidecar just stale", V2_OK, 29.1, "stale-mirror"),
    ("stat failed", V2_OK, None, "stale-mirror"),
    # slow syncer declares itself: interval 10 + timeout 20 ⇒ 50s allowed
    ("slow syncer self-declared", {**V2_OK, "interval_s": 10.0}, 40.0, "ok"),
    # fields missing ⇒ 15s floor
    ("threshold floor", {"state": "ok", "ok": True}, 14.0, "ok"),
    ("threshold floor exceeded", {"state": "ok", "ok": True}, 16.0, "stale-mirror"),
    # stale-content threshold = max(30, 5*cadence)
    ("content just fresh", {**V2_OK, "remote_status_age_s": 30.0}, 2.0, "ok"),
    ("content frozen", {**V2_OK, "remote_status_age_s": 31.0}, 2.0, "stale-content"),
    ("slow-writer host allowed", {**V2_OK, "remote_status_age_s": 55.0,
                                  "remote_write_cadence_s": 12.0}, 2.0, "ok"),
    ("slow-writer host frozen", {**V2_OK, "remote_status_age_s": 61.0,
                                 "remote_write_cadence_s": 12.0}, 2.0, "stale-content"),
    # stale-mirror outranks a frozen-content reading (a dead syncer's age data is old news)
    ("mirror-staleness outranks", {**V2_OK, "remote_status_age_s": 99.0}, 60.0, "stale-mirror"),
    # v1 sidecar (no `state`): ok maps to ok, failure to down (no hysteresis to pass through)
    ("v1 ok", {"synced_at": 1.0, "ok": True, "error": None, "consecutive_failures": 0,
               "remote_status_age_s": 1.0, "sessions": 2}, 2.0, "ok"),
    ("v1 failure", {"synced_at": 1.0, "ok": False, "error": "unreachable",
                    "consecutive_failures": 1, "remote_status_age_s": None,
                    "sessions": None}, 2.0, "down"),
]


@pytest.mark.parametrize("fn", [ccstatus.read_verdict, ccbar._read_verdict],
                         ids=["ccstatus", "ccbar"])
@pytest.mark.parametrize("name,health,age,expected", VERDICTS,
                         ids=[v[0] for v in VERDICTS])
def test_verdict_table_both_copies(fn, name, health, age, expected):
    assert fn(health, age) == expected


# ------------------------------------------------- loaders over a tmp remote dir

REC = {"session_id": "abc-123", "short_id": "abc", "pid": 1, "kind": "interactive",
       "state": "busy", "title": "t", "synopsis": "", "cwd": "/x", "cwd_short": "~/x",
       "tmux_target": None, "pane_id": "%1", "age_s": 3}


def _write_host(dir_, host, recs, health):
    (dir_ / f"{host}.json").write_text(json.dumps(recs))
    if health is not None:
        (dir_ / f"{host}.health").write_text(json.dumps(health))


def test_degraded_keeps_rows_down_drops(tmp_path):
    now = time.time()
    _write_host(tmp_path, "flaky", [REC],
                {**V2_OK, "state": "degraded", "ok": False, "error": "timeout",
                 "synced_at": now, "remote_status_age_s": None})
    _write_host(tmp_path, "gone", [REC],
                {**V2_OK, "state": "down", "ok": False, "error": "unreachable",
                 "synced_at": now, "remote_status_age_s": None})
    got = load_remote_sessions(remote_dir=tmp_path, now=now)
    assert [s.host for s in got] == ["flaky"]
    assert got[0].session_id == "abc-123"


def test_no_sidecar_falls_back_to_mtime(tmp_path):
    now = time.time()
    (tmp_path / "bare.json").write_text(json.dumps([REC]))
    assert [s.host for s in load_remote_sessions(remote_dir=tmp_path, now=now)] == ["bare"]
    assert load_remote_sessions(remote_dir=tmp_path, now=now + 16) == []


def test_tolerant_rebuild_regression(tmp_path):
    now = time.time()
    _write_host(tmp_path, "h", [{"session_id": "only-id", "state": "idle"},
                                {"no_id": True}], {**V2_OK, "synced_at": now})
    got = load_remote_sessions(remote_dir=tmp_path, now=now)
    assert len(got) == 1 and got[0].session_id == "only-id" and got[0].host == "h"


def test_load_remote_health_verdicts(tmp_path, monkeypatch):
    now = time.time()
    remotes = tmp_path / "remotes"
    remotes.write_text("healthy\nblipping\ndead\nneverran\n")
    monkeypatch.setattr(ccstatus, "REMOTES_FILE", remotes)
    _write_host(tmp_path, "healthy", [REC], {**V2_OK, "synced_at": now})
    _write_host(tmp_path, "blipping", [REC],
                {**V2_OK, "state": "degraded", "ok": False, "error": "timeout",
                 "bad_since": now - 12, "synced_at": now, "remote_status_age_s": None})
    _write_host(tmp_path, "dead", [REC],
                {**V2_OK, "state": "down", "ok": False, "error": "auth",
                 "bad_since": now - 300, "synced_at": now, "remote_status_age_s": None})
    h = load_remote_health(remote_dir=tmp_path, now=now)
    assert h["healthy"]["state"] == "ok"
    assert h["blipping"]["state"] == "degraded" and h["blipping"]["error"] == "timeout"
    assert 11 < h["blipping"]["age_s"] < 13          # now − bad_since
    assert h["dead"]["state"] == "down" and h["dead"]["error"] == "auth"
    assert h["neverran"]["state"] == "missing"


def test_ccbar_unhealthy_hosts_red_set_only(tmp_path, monkeypatch):
    now = time.time()
    remotes = tmp_path / "remotes"
    remotes.write_text("healthy\nblipping\ndead\nneverran\n")
    monkeypatch.setattr(ccbar, "REMOTES_FILE", remotes)
    monkeypatch.setattr(ccbar, "REMOTE_DIR", tmp_path)
    _write_host(tmp_path, "healthy", [REC], {**V2_OK, "synced_at": now})
    _write_host(tmp_path, "blipping", [REC],
                {**V2_OK, "state": "degraded", "ok": False, "error": "timeout",
                 "synced_at": now, "remote_status_age_s": None})
    _write_host(tmp_path, "dead", [REC],
                {**V2_OK, "state": "down", "ok": False, "error": "auth",
                 "synced_at": now, "remote_status_age_s": None})
    # degraded is NOT red (hysteresis); down + never-ran are
    assert ccbar._unhealthy_hosts() == ["dead", "neverran"]
