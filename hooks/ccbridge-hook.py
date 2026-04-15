#!/usr/bin/env python3
"""
ccbridge-hook.py — Remote bridge hook for Claude Island

Sends session state to Claude Island on a Mac via TCP (over SSH reverse tunnel).
For PermissionRequest: blocks waiting for user decision; auto-denies on failure.
Also writes state files for claude_status.py compatibility.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

BRIDGE_PORT_FILE = Path.home() / ".claude" / "run" / "bridge_port"
STATE_DIR = Path.home() / ".claude" / "run" / "state"
TIMEOUT_SECONDS = 300  # 5 minutes for permission decisions


# ── Transport ────────────────────────────────────────────────────────────────

def _read_bridge_port():
    """Read the bridge port from the convention file. Returns None if unavailable."""
    try:
        return int(BRIDGE_PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def send_event(state):
    """Send event to Claude Island via TCP. Returns response dict or None.

    This is the single transport function — swap AF_INET for AF_UNIX here
    to change the transport for the entire hook.
    """
    port = _read_bridge_port()
    if port is None:
        return None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(("127.0.0.1", port))
        sock.sendall(json.dumps(state).encode())

        # For permission requests, wait for response
        if state.get("status") == "waiting_for_approval":
            response = sock.recv(4096)
            sock.close()
            if response:
                return json.loads(response.decode())
        else:
            sock.close()

        return None
    except (socket.error, OSError, json.JSONDecodeError):
        return None


# ── State file (claude_status.py compat) ─────────────────────────────────────

def _event_to_simple_state(event):
    """Map hook event name to simple three-state for claude_status.py."""
    if event in ("PreToolUse", "PostToolUse", "SubagentStart", "UserPromptSubmit"):
        return "working"
    if event in ("Stop", "SubagentStop", "SessionStart"):
        return "idle"
    if event == "PermissionRequest":
        return "blocked"
    return None


def _write_state_file(session_id, simple_state, cwd):
    """Write state file for claude_status.py compatibility."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f"{session_id}.tmp"
    out = STATE_DIR / session_id
    data = json.dumps({
        "state": simple_state,
        "pid": str(os.getppid()),
        "session_id": session_id,
        "cwd": cwd,
        "ts": str(int(time.time())),
    })
    tmp.write_text(data)
    os.replace(tmp, out)


# ── TTY detection ────────────────────────────────────────────────────────────

def get_tty():
    """Get the TTY of the Claude process (parent)."""
    import subprocess
    ppid = os.getppid()
    try:
        result = subprocess.run(
            ["ps", "-p", str(ppid), "-o", "tty="],
            capture_output=True, text=True, timeout=2,
        )
        tty = result.stdout.strip()
        if tty and tty not in ("??", "-"):
            if not tty.startswith("/dev/"):
                tty = "/dev/" + tty
            return tty
    except Exception:
        pass
    for fd in (sys.stdin, sys.stdout):
        try:
            return os.ttyname(fd.fileno())
        except (OSError, AttributeError):
            pass
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(1)

    session_id = data.get("session_id", "unknown")
    event = data.get("hook_event_name", "")
    cwd = data.get("cwd", "")
    tool_input = data.get("tool_input", {})

    # Write simple state file for claude_status.py
    simple_state = _event_to_simple_state(event)
    if simple_state:
        _write_state_file(session_id, simple_state, cwd)

    # Build state object for Claude Island
    claude_pid = os.getppid()
    tty = get_tty()

    state = {
        "session_id": session_id,
        "cwd": cwd,
        "event": event,
        "pid": claude_pid,
        "tty": tty,
    }

    # Map events to status
    if event == "UserPromptSubmit":
        state["status"] = "processing"

    elif event == "PreToolUse":
        state["status"] = "running_tool"
        state["tool"] = data.get("tool_name")
        state["tool_input"] = tool_input
        tool_use_id = data.get("tool_use_id")
        if tool_use_id:
            state["tool_use_id"] = tool_use_id

    elif event == "PostToolUse":
        state["status"] = "processing"
        state["tool"] = data.get("tool_name")
        state["tool_input"] = tool_input
        tool_use_id = data.get("tool_use_id")
        if tool_use_id:
            state["tool_use_id"] = tool_use_id

    elif event == "PermissionRequest":
        state["status"] = "waiting_for_approval"
        state["tool"] = data.get("tool_name")
        state["tool_input"] = tool_input

        # Send to Mac and wait for decision
        response = send_event(state)

        if response:
            decision = response.get("decision", "ask")
            reason = response.get("reason", "")

            if decision == "allow":
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {"behavior": "allow"},
                    }
                }))
                sys.exit(0)

            elif decision == "deny":
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {
                            "behavior": "deny",
                            "message": reason or "Denied by user via Claude Island (remote)",
                        },
                    }
                }))
                sys.exit(0)

        # No response from bridge — either:
        # 1. Couldn't connect (bridge not running) — fall through to local UI
        # 2. Connected but got no response (user approved locally, socket closed) — fall through
        # In both cases, exit cleanly and let Claude Code handle it normally.
        sys.exit(0)

    elif event == "Notification":
        notification_type = data.get("notification_type")
        if notification_type == "permission_prompt":
            sys.exit(0)
        elif notification_type == "idle_prompt":
            state["status"] = "waiting_for_input"
        else:
            state["status"] = "notification"
        state["notification_type"] = notification_type
        state["message"] = data.get("message")

    elif event == "Stop":
        state["status"] = "waiting_for_input"

    elif event == "SubagentStop":
        state["status"] = "waiting_for_input"

    elif event == "SessionStart":
        state["status"] = "waiting_for_input"

    elif event == "SessionEnd":
        state["status"] = "ended"

    elif event == "PreCompact":
        state["status"] = "compacting"

    else:
        state["status"] = "unknown"

    # Send to Mac (fire and forget for non-permission events)
    send_event(state)


if __name__ == "__main__":
    main()
