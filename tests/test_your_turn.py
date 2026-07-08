"""The ◆ "your turn" tier. One hand-back table runs against BOTH copies — ccstatus.awaiting
and ccbar._awaiting — so the deliberate stdlib duplication is provably in lockstep. Also
covers the pane-focus parser (the `focused` fact) and the --serve focused auto-ack policy
("seen = handled"), including the guard that ⛔ blocked rows are never auto-acked (ccbar
drops acknowledged blocked rows, so a wrong ack would silently kill the loud tier)."""
import pytest

import ccbar
import ccstatus
from ccstatus import (Session, _SESSION_DEFAULTS, _auto_ack_focused, _parse_panes,
                      awaiting, handed_back)


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

# (case-name, record, expected ◆)
CASES = [
    ("plain hand-back", HANDBACK, True),
    ("focused hand-back (watching live)", {**HANDBACK, "focused": True}, False),
    ("acked", {**HANDBACK, "acknowledged": True}, False),
    ("dismissed", {**HANDBACK, "dismissed": True}, False),
    ("never answered (not engaged)", {"state": "idle", "engaged": False}, False),
    ("blocked is the other tier", {"state": "blocked", "engaged": True}, False),
    ("busy", {"state": "busy", "engaged": True}, False),
    # old status.json / old remote mirror: no `focused` key at all ⇒ alert as before
    ("no focused key (old schema)", dict(HANDBACK), True),
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
