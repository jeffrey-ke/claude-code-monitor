#!/usr/bin/env python3
"""
claude_status.py
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

STATUS_FILE   = Path.home() / ".claude" / "run" / "status"
POLL_INTERVAL = 2.0


# ── Process tree ─────────────────────────────────────────────────────────────

def _ppid(pid: str) -> str | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return line.split()[1]
    except OSError:
        return None


def _comm(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _find_pane_pid(claude_pid: str, pane_pids: set[str]) -> str | None:
    """
    Walk up from claude_pid until we find a pid that tmux reports as a pane.
    Stop if we hit the tmux server process itself (not a pane).
    """
    pid = claude_pid
    for _ in range(10):
        if pid in pane_pids:
            return pid
        if _comm(pid).startswith("tmux"):
            return None
        parent = _ppid(pid)
        if parent is None or parent in ("0", "1"):
            return None
        pid = parent
    return None


# ── Session files ─────────────────────────────────────────────────────────────

def _load_live_sessions() -> dict[str, dict]:
    """
    Returns {claude_pid: session_data} for sessions whose process is alive.
    """
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.exists():
        return {}
    result = {}
    for f in sessions_dir.iterdir():
        pid = f.stem
        if not Path(f"/proc/{pid}").exists():
            continue
        try:
            data = json.loads(f.read_text())
            result[pid] = {"name": data.get("name", "")}
        except (json.JSONDecodeError, OSError):
            continue
    return result


# ── Classification ────────────────────────────────────────────────────────────

def _classify_pane(text: str) -> str:
    if "esc to cancel" in text.lower():
        return "blocked"
    if re.search(r'↑\s*\d+\.?\d*k?\s*tokens', text):
        return "working"
    if '[ctx:' in text.lower() or re.search(r'\d+\.?\d*k?/\d+', text):
        return "idle"
    return "working"


# ── Backend ───────────────────────────────────────────────────────────────────

def get_sessions_tmux() -> dict[str, dict]:
    session_files = _load_live_sessions()
    if not session_files:
        return {}

    fmt = "#{pane_pid}\t#{pane_id}\t#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}"
    raw = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", fmt],
        capture_output=True, text=True
    )
    if raw.returncode != 0:
        return {}

    panes = {}
    for line in raw.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            panes[parts[0]] = (parts[1], parts[2], parts[3])

    pane_pids = set(panes.keys())

    sessions = {}
    for claude_pid, data in session_files.items():
        pane_pid = _find_pane_pid(claude_pid, pane_pids)
        if pane_pid is None:
            continue

        pane_id, cwd, target = panes[pane_pid]
        content = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True, text=True
        ).stdout

        sessions[claude_pid] = {
            "state":  _classify_pane(content),
            "target": target,
            "cwd":    cwd,
            "name":   data.get("name", ""),
        }

    return sessions


get_sessions = get_sessions_tmux


# ── Output ────────────────────────────────────────────────────────────────────

def write_status(sessions: dict[str, dict]) -> None:
    home = str(Path.home())
    lines = ["TARGET       STATE    NAME                      CWD"]
    for sid, s in sessions.items():
        cwd    = s["cwd"].replace(home, "~")[-36:]
        name   = (s.get("name") or "")[:24]
        target = s.get("target", "?")
        lines.append(f"{target:<12} {s['state']:<8} {name:<25} {cwd}")
    if not sessions:
        lines.append("(no sessions detected)")

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, STATUS_FILE)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Writing to {STATUS_FILE}")
    while True:
        sessions = get_sessions()
        write_status(sessions)
        print(
            f"[{time.strftime('%H:%M:%S')}] {len(sessions)} session(s): "
            + ", ".join(
                f"{v['target']}({v.get('name') or '-'})={v['state']}"
                for sid, v in sessions.items()
            )
        )
        time.sleep(POLL_INTERVAL)
