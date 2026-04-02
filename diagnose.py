#!/usr/bin/env python3
"""
diagnose.py

Run this before the monitor to verify it can see your sessions.
Dumps raw pane captures and shows exactly what the classifier sees.

Usage:
    python diagnose.py
"""

import json
import re
import subprocess
from pathlib import Path


def run(*args) -> str:
    r = subprocess.run(list(args), capture_output=True, text=True)
    return r.stdout.strip()


def main():
    # ── 1. List all panes ────────────────────────────────────────────────────
    fmt = "#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}"
    raw = run("tmux", "list-panes", "-a", "-F", fmt)

    if not raw:
        print("ERROR: tmux returned nothing. Are you inside a tmux session?")
        print("  Try: tmux ls")
        return

    panes = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            panes.append({
                "pane_id": parts[0],
                "pid":     parts[1],
                "cwd":     parts[2],
                "label":   parts[3],
            })

    print(f"Found {len(panes)} pane(s) total\n")

    # ── 2. For each pane, show what we see ───────────────────────────────────
    for p in panes:
        print(f"{'─'*60}")
        print(f"Pane:  {p['label']}  (pid {p['pid']})")
        print(f"CWD:   {p['cwd']}")

        # Session ID resolution
        session_id = _resolve_session_id(p["pid"])
        if session_id:
            print(f"SessionID: {session_id}")
        else:
            session_path = Path.home() / ".claude" / "sessions" / f"{p['pid']}.json"
            if session_path.exists():
                print(f"Session file exists but couldn't parse: {session_path}")
                print(f"  Contents: {session_path.read_text()[:200]}")
            else:
                print(f"No session file at: {session_path}")
                print(f"  → This pane is not a Claude Code session (or PID mismatch)")

        # Capture pane — show the last 5 lines where the status bar lives
        content = run("tmux", "capture-pane", "-p", "-t", p["pane_id"])
        last_lines = [l for l in content.splitlines() if l.strip()][-5:]
        print(f"Last 5 non-empty lines:")
        for line in last_lines:
            print(f"  {repr(line)}")

        # Show classification result
        state = _classify_pane(content)
        print(f"Classified as: {state!r}")
        if state == "working" and session_id:
            print("  ↑ Has session ID but couldn't classify — check patterns below")
            _debug_patterns(content)

        print()

    # ── 3. Check ~/.claude/sessions/ directory ───────────────────────────────
    sessions_dir = Path.home() / ".claude" / "sessions"
    print(f"{'─'*60}")
    print(f"~/.claude/sessions/ contents:")
    if sessions_dir.exists():
        files = list(sessions_dir.iterdir())
        if not files:
            print("  (empty)")
        for f in files[:10]:  # cap at 10
            try:
                data = json.loads(f.read_text())
                print(f"  {f.name}: {data}")
            except Exception as e:
                print(f"  {f.name}: ERROR {e}")
    else:
        print(f"  Directory does not exist: {sessions_dir}")
        print("  This is the main thing to fix — Claude Code should be writing here.")


def _resolve_session_id(pid: str) -> str | None:
    path = Path.home() / ".claude" / "sessions" / f"{pid}.json"
    try:
        data = json.loads(path.read_text())
        # Try both known key names
        return data.get("sessionId") or data.get("session_id")
    except (OSError, json.JSONDecodeError):
        return None


def _classify_pane(text: str) -> str:
    if "esc to cancel" in text.lower():
        return "blocked"
    if re.search(r'↑\s*\d+\.?\d*k?\s*tokens', text):
        return "working"
    if '[ctx:' in text.lower() or re.search(r'\d+\.?\d*k?/\d+', text):
        return "idle"
    return "working"


def _debug_patterns(text: str) -> None:
    """Show why each pattern did or didn't match."""
    lower = text.lower()
    checks = [
        ("esc to interrupt",        "esc to interrupt" in lower),
        ("esc to cancel",           "esc to cancel" in lower),
        (r"\[ctx:\s*\d+%\]",        bool(re.search(r'\[ctx:\s*\d+%\]', lower))),
        (r"\d+\.?\d*k?/\d+",        bool(re.search(r'\d+\.?\d*k?/\d+', lower))),
    ]
    print("  Pattern match results:")
    for pattern, matched in checks:
        mark = "✓" if matched else "✗"
        print(f"    {mark} {pattern!r}")


if __name__ == "__main__":
    main()
