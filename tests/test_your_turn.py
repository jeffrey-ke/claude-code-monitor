"""The ◆ "your turn" tier. One hand-back table runs against BOTH copies — ccstatus.awaiting
and ccbar._awaiting — so the deliberate stdlib duplication is provably in lockstep. Also
covers the pane-focus parser (the `focused` fact) and the --serve focused auto-ack policy
("seen = handled"), including the guard that ⛔ blocked rows are never auto-acked (ccbar
drops acknowledged blocked rows, so a wrong ack would silently kill the loud tier)."""
import json

import pytest

import ccbar
import ccstatus
from ccstatus import (Session, _SESSION_DEFAULTS, _auto_ack_focused, _parse_panes,
                      _transcript_usage, _turn_complete_step, awaiting, handed_back)


# ── _parse_panes: focused = pane_active ∧ window_active ∧ session_attached>0 ──

def _line(pid="100", pane="%1", pane_on="1", win_on="1", att="1", target="main:0.0"):
    return "\t".join([pid, pane, pane_on, win_on, att, target])


# (case-name, line, expected focused)
PANES = [
    ("viewed pane", _line(), True),
    ("inactive pane of viewed window", _line(pane_on="0"), False),
    ("active pane of a background window", _line(win_on="0"), False),
    ("detached session's active pane", _line(att="0"), False),
    ("two clients attached", _line(att="2"), True),
    ("garbage attach count", _line(att="x"), False),   # fail-open: never suppress on junk
]


@pytest.mark.parametrize("name,line,focused", PANES, ids=[p[0] for p in PANES])
def test_parse_panes_focus_flags(name, line, focused):
    assert _parse_panes(line) == {"100": ("%1", "main:0.0", focused)}


def test_parse_panes_malformed_and_empty():
    assert _parse_panes("") == {}
    assert _parse_panes("100\t%1\t1\t1") == {}            # short line skipped
    assert _parse_panes("not a pane line at all") == {}


def test_parse_panes_tab_in_session_name():
    # The free-text target is last + maxsplit, so an embedded tab can't shift the flags.
    out = _parse_panes(_line(target="odd\tname:0.0"))
    assert out == {"100": ("%1", "odd\tname:0.0", True)}


# ── awaiting: the ◆ predicate, one table against both copies ─────────────────

def _sess(rec):
    """Build a Session the way load_remote_sessions does — the tolerant constructor —
    so the missing-key cases double as the old-snapshot/old-mirror schema regression."""
    return Session(**{**_SESSION_DEFAULTS, **rec})


HANDBACK = {"state": "idle", "engaged": True}
# The yazi bug shape: turn over (transcript turn-end markers) but lingering background
# shells/agents pin the CLI status at busy/shell — still a type 2 (◆) hand-back.
TURNDONE = {"state": "busy", "engaged": True, "turn_complete": True}

# (case-name, record, expected ◆)
CASES = [
    ("plain hand-back", HANDBACK, True),
    ("focused hand-back (watching live)", {**HANDBACK, "focused": True}, False),
    ("acked", {**HANDBACK, "acknowledged": True}, False),
    ("dismissed", {**HANDBACK, "dismissed": True}, False),
    ("never answered (not engaged)", {"state": "idle", "engaged": False}, False),
    ("blocked is the other tier", {"state": "blocked", "engaged": True}, False),
    ("busy mid-turn", {"state": "busy", "engaged": True}, False),
    # old status.json / old remote mirror: no `focused` key at all ⇒ alert as before
    ("no focused key (old schema)", dict(HANDBACK), True),
    ("busy + turn_complete (the yazi bug)", TURNDONE, True),
    ("shell + turn_complete", {**TURNDONE, "state": "shell"}, True),
    ("busy + turn_complete, watching live", {**TURNDONE, "focused": True}, False),
    ("busy + turn_complete, acked", {**TURNDONE, "acknowledged": True}, False),
    # ⛔ stays structurally exempt: turn_complete never softens/acks the loud tier
    ("blocked + turn_complete stays type 1", {**TURNDONE, "state": "blocked"}, False),
    ("idle needs no turn_complete", {**HANDBACK, "turn_complete": False}, True),
]


@pytest.mark.parametrize("fn", [lambda rec: awaiting(_sess(rec)), ccbar._awaiting],
                         ids=["ccstatus", "ccbar"])
@pytest.mark.parametrize("name,rec,expected", CASES, ids=[c[0] for c in CASES])
def test_awaiting_table_both_copies(fn, name, rec, expected):
    assert fn(rec) is expected


def test_focused_handback_still_handed_back():
    # The contract ccdash's jump-ack and --serve's auto-ack rely on: a focused hand-back
    # is suppressed from ◆ (awaiting False) but still actionable (handed_back True).
    s = _sess({**HANDBACK, "focused": True})
    assert handed_back(s) is True
    assert awaiting(s) is False


# ── turn_complete: the transcript turn-end signal (the yazi bug) ──────────────

TS = "2026-07-10T14:21:16.212Z"

ASSISTANT = {"type": "assistant", "timestamp": TS,
             "message": {"role": "assistant", "model": "m",
                         "content": [{"type": "text", "text": "done — want me to?"}],
                         "usage": {"input_tokens": 10}}}
TURN_END = [{"type": "system", "subtype": "stop_hook_summary", "timestamp": TS},
            {"type": "system", "subtype": "turn_duration", "timestamp": TS}]
# What Claude Code appends when you /rename (or run any local command) after the
# hand-back — observed verbatim in the yazi transcript; must not mask the turn end.
POST_HANDBACK_NOISE = [
    {"type": "custom-title", "customTitle": "yazi"},
    {"type": "agent-name", "agentName": "yazi"},
    {"type": "system", "subtype": "local_command", "timestamp": TS,
     "content": "<command-name>/rename</command-name>"},
    {"type": "system", "subtype": "local_command", "timestamp": TS,
     "content": "<local-command-stdout>Session renamed</local-command-stdout>"},
    {"type": "user", "timestamp": TS,
     "message": {"role": "user",
                 "content": "<system-reminder>renamed to yazi</system-reminder>"}},
]

# (case-name, entry, vote) — one entry's vote in the newest→oldest walk:
# True/False decides, None keeps walking.
VOTES = [
    ("turn_duration decides True", TURN_END[1], True),
    ("stop_hook_summary decides True", TURN_END[0], True),
    ("local_command skipped", POST_HANDBACK_NOISE[2], None),
    ("unknown system subtype = live", {"type": "system", "subtype": "compact_boundary",
                                       "timestamp": TS}, False),
    ("untimestamped metadata skipped", POST_HANDBACK_NOISE[0], None),
    ("sidechain skipped", {**ASSISTANT, "isSidechain": True}, None),
    ("assistant = mid-generation", ASSISTANT, False),
    ("genuine user = about to run", {"type": "user", "timestamp": TS,
                                     "message": {"role": "user", "content": "go"}}, False),
    ("tool_result user skipped", {"type": "user", "timestamp": TS,
                                  "message": {"role": "user", "content": [
                                      {"type": "tool_result", "content": "out"}]}}, None),
    ("wrapper user skipped", POST_HANDBACK_NOISE[4], None),
]


@pytest.mark.parametrize("name,entry,vote", VOTES, ids=[v[0] for v in VOTES])
def test_turn_complete_step_votes(name, entry, vote):
    assert _turn_complete_step(entry) is vote


def _jl(tmp_path, entries):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


def test_turn_complete_yazi_tail(tmp_path):
    # The live repro verbatim: final assistant text, turn-end markers, then rename noise.
    jp = _jl(tmp_path, [ASSISTANT] + TURN_END + POST_HANDBACK_NOISE)
    model, ctx, engaged, last_ts, done = _transcript_usage(jp)
    assert engaged is True and done is True


def test_turn_not_complete_after_new_prompt(tmp_path):
    prompt = {"type": "user", "timestamp": TS,
              "message": {"role": "user", "content": "next task please"}}
    jp = _jl(tmp_path, [ASSISTANT] + TURN_END + [prompt])
    assert _transcript_usage(jp)[4] is False


def test_turn_not_complete_mid_generation_or_old_cli(tmp_path):
    # A tail ending on an assistant entry is either mid-generation or an old CLI that
    # writes no turn-end markers — both must read False (exactly the pre-fix behavior).
    jp = _jl(tmp_path, [ASSISTANT])
    assert _transcript_usage(jp)[4] is False


def test_turn_complete_skips_trailing_sidechain(tmp_path):
    side = {**ASSISTANT, "isSidechain": True}
    jp = _jl(tmp_path, [ASSISTANT] + TURN_END + [side, side])
    assert _transcript_usage(jp)[4] is True


# ── _auto_ack_focused: seen = handled, ⛔ exempt, mirrors skipped ─────────────

@pytest.fixture
def ack_dir(tmp_path, monkeypatch):
    d = tmp_path / "ack"
    monkeypatch.setattr(ccstatus, "ACK_DIR", d)
    return d


def test_auto_ack_focused_handback(ack_dir):
    s = _sess({**HANDBACK, "session_id": "abc", "focused": True})
    _auto_ack_focused([s])
    assert s.acknowledged is True                 # in-memory, pre-snapshot: no one-tick ◆
    assert (ack_dir / "abc").exists()             # the ordinary sticky marker


def test_auto_ack_skips_unfocused(ack_dir):
    s = _sess({**HANDBACK, "session_id": "abc"})
    _auto_ack_focused([s])
    assert s.acknowledged is False
    assert not ack_dir.exists()


def test_auto_ack_focused_turn_complete_handback(ack_dir):
    # The yazi shape watched live: busy + turn_complete IS a hand-back, so focus acks it.
    s = _sess({**TURNDONE, "session_id": "abc", "focused": True})
    _auto_ack_focused([s])
    assert s.acknowledged is True
    assert (ack_dir / "abc").exists()


def test_auto_ack_never_touches_blocked(ack_dir):
    # ⛔ must keep alerting even when focused: ccbar's blocked filter drops acked rows,
    # so an auto-ack here would silently hide a permission prompt.
    s = _sess({"state": "blocked", "engaged": True, "session_id": "abc", "focused": True})
    _auto_ack_focused([s])
    assert s.acknowledged is False
    assert not ack_dir.exists()


def test_auto_ack_skips_remote_rows(ack_dir):
    # A mirrored row's marker lives on its host (remote_ack pushes it over SSH); a local
    # marker would be a silent no-op that also lied in the local snapshot.
    s = _sess({**HANDBACK, "session_id": "abc", "focused": True, "host": "psc"})
    _auto_ack_focused([s])
    assert s.acknowledged is False
    assert not ack_dir.exists()


def test_auto_ack_idempotent(ack_dir):
    import os
    s = _sess({**HANDBACK, "session_id": "abc", "focused": True})
    _auto_ack_focused([s])
    os.utime(ack_dir / "abc", (1.0, 1.0))         # backdate so a re-touch would be visible
    _auto_ack_focused([s])                        # acknowledged already True ⇒ no re-touch
    assert (ack_dir / "abc").stat().st_mtime == 1.0
