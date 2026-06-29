#!/usr/bin/env python3
"""
ccsend.py — deliver an input into a running Claude Code session.

Headless-first: the representation of an input is a small JSON **file on disk**, so the
ccdash TUI is just one writer and `ccsend` (or any other program) is another. Delivery is a
single tmux `send-keys` seam — the only reliable way to drive an attached session (the Mac
app's ToolApprovalHandler does exactly this in production). Two payload kinds:

    {"type": "text", "text": "...",            "ts": <epoch>}   # send literal text, then Enter
    {"type": "keys", "keys": ["Down","Space"], "ts": <epoch>}   # send named tmux keys in order

`text` covers a free message, a single-choice menu pick ("1"), a plan accept ("1"), a
permission ("1"/"n"). `keys` drives arbitrary menus (multi-select: ["Down","Space","Enter"]).

Everything is fail-open (ssh-bridge-bugs.md #4): never deliver something you didn't parse,
never crash on a tmux error, never blind-retry a partial send.

CLI:
  ccsend <sid|prefix> "message text"        enqueue a text input + deliver it
  ccsend <sid|prefix> --keys Down Space Enter   enqueue named keys + deliver
  ccsend --drain                             deliver everything pending (no TUI needed)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Reuse the provider for session/pane resolution (stdlib-only seam).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ccstatus import get_sessions  # noqa: E402

OUTBOX = Path.home() / ".claude" / "run" / "outbox"   # outbox/<sid>/<uniq>.json
# Named keys we forward verbatim to `tmux send-keys` (case-sensitive tmux key names);
# anything else is sent as literal characters via `-l`.
KEY_OK = {"Enter", "Up", "Down", "Left", "Right", "Space", "Tab", "BTab", "Escape", "BSpace",
          "Home", "End", "PageUp", "PageDown", "DC", "IC",
          "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"}
STALE_S = 3600   # drop an undeliverable file only once its session is gone AND it's this old


# ── the contract: a per-session outbox queue any program can write ────────────

def enqueue(sid, payload):
    """Atomically drop one payload file into the session's outbox. Fail-open → bool."""
    d = OUTBOX / sid
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")   # unique ⇒ writers never clobber
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, d / (Path(tmp).stem + ".json"))
        return True
    except OSError:
        return False


# ── delivery: the ONE tmux send-keys seam ────────────────────────────────────

def _send(pane, *args):
    """tmux send-keys -t <pane> <args…>. Returns True on success, never raises."""
    try:
        r = subprocess.run(["tmux", "send-keys", "-t", pane, *args],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def deliver(session, payload):
    """Send one payload into `session`'s tmux pane. Returns (ok: bool, reason: str)."""
    pane = getattr(session, "pane_id", None)
    if not pane:
        return False, "no tmux pane (background or unmapped session)"
    kind = payload.get("type")
    if kind == "text":
        text = payload.get("text", "")
        # `-l --` ⇒ literal keys, and `--` stops a leading "-" glyph being read as a flag.
        if text and not _send(pane, "-l", "--", text):
            return False, "send-keys failed"
        if not _send(pane, "Enter"):                    # Enter as a separate key (proven pattern)
            return False, "send-keys Enter failed"
        return True, "sent"
    if kind == "keys":
        for k in payload.get("keys", []):
            ok = _send(pane, k) if k in KEY_OK else _send(pane, "-l", "--", str(k))
            if not ok:
                return False, f"send-keys {k!r} failed"
        return True, "sent"
    return False, f"unknown payload type {kind!r}"


# ── drain: deliver pending files (called by ccdash's tick, the CLI, anyone) ───

def _safe_unlink(p):
    try:
        p.unlink()
    except OSError:
        pass


def drain(sessions=None):
    """Deliver every pending outbox file; unlink on success, keep on failure (visible/retried).
    Returns the count delivered. Corrupt files are dropped; long-stale orphans are reaped."""
    if not OUTBOX.exists():
        return 0
    sessions = sessions if sessions is not None else get_sessions()
    by_id = {s.session_id: s for s in sessions}
    now = time.time()
    sent = 0
    for sid_dir in sorted(OUTBOX.glob("*")):
        if not sid_dir.is_dir():
            continue
        s = by_id.get(sid_dir.name)
        for f in sorted(sid_dir.glob("*.json"), key=lambda p: _mtime(p)):
            try:
                payload = json.loads(f.read_text())
            except (OSError, ValueError):
                _safe_unlink(f)                          # corrupt → drop (fail-open)
                continue
            if s and deliver(s, payload)[0]:
                _safe_unlink(f)
                sent += 1
            elif not s and (now - _mtime(f)) > STALE_S:
                _safe_unlink(f)                          # session long gone → reap the orphan
            # else: session present-but-undeliverable, or recent orphan → leave for retry
        try:                                             # tidy empty queue dirs
            sid_dir.rmdir()
        except OSError:
            pass
    return sent


def _mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ── CLI ───────────────────────────────────────────────────────────────────────

def _resolve(target, sessions):
    """Resolve a full sid, an unambiguous sid prefix, or a short_id to one Session."""
    for s in sessions:
        if s.session_id == target:
            return s
    pref = [s for s in sessions if s.session_id.startswith(target) or s.short_id == target]
    return pref[0] if len(pref) == 1 else None


def main():
    ap = argparse.ArgumentParser(description="Send an input into a Claude Code session.")
    ap.add_argument("target", nargs="?", help="session id or unique prefix")
    ap.add_argument("text", nargs="?", help="message / keystrokes to send (text payload)")
    ap.add_argument("--keys", nargs="+", metavar="KEY",
                    help="send named tmux keys instead of text (e.g. Down Space Enter)")
    ap.add_argument("--drain", action="store_true",
                    help="deliver all pending outbox files, then exit")
    args = ap.parse_args()

    if args.drain and not args.target:
        print(f"delivered {drain()} pending input(s)")
        return
    if not args.target:
        ap.error("give a session id/prefix (or --drain)")

    sessions = get_sessions()
    s = _resolve(args.target, sessions)
    if not s:
        print(f"no unique session matching {args.target!r}", file=sys.stderr)
        sys.exit(1)

    if args.keys:
        payload = {"type": "keys", "keys": args.keys, "ts": time.time()}
    elif args.text is not None:
        payload = {"type": "text", "text": args.text, "ts": time.time()}
    else:
        ap.error("give message text or --keys")

    enqueue(s.session_id, payload)
    n = drain([s])
    if n:
        print(f"sent → {s.short_id}  {s.title}")
    else:
        print(f"queued for {s.short_id} — not delivered (no pane? background session)",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
