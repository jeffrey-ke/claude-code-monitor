#!/usr/bin/env python3
"""
ccbridge-hook.py — Remote bridge hook for Claude Island

Sends session state to Claude Island on a Mac via TCP (over SSH reverse tunnel).
For PermissionRequest: blocks waiting for user decision; auto-denies on failure.
Also writes state files for claude_status.py compatibility.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_send import send_event, _read_bridge_port  # noqa: E402

STATE_DIR = Path.home() / ".claude" / "run" / "state"

# Stop-hook JSONL freshness poll
STOP_POLL_CEILING_S = 2.0
STOP_POLL_INTERVAL_S = 0.1
MAX_ASSISTANT_TEXT_BYTES = 4096
TIMEOUT_PLACEHOLDER = (
    "(assistant message unavailable — see remote terminal for latest reply; "
    "JSONL flush timeout)"
)


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


# ── JSONL reader (for Stop hook assistant-message forwarding) ────────────────

def _project_dir(cwd):
    """Claude Code's project-dir naming: replace /, ., and _ with -."""
    return cwd.replace("/", "-").replace(".", "-").replace("_", "-")


def _read_last_assistant_entry(jsonl_path):
    """Reverse-scan JSONL tail for the last type=assistant entry with text.
    Returns (text, uuid) or (None, None). UUID is what identifies this entry
    uniquely across polls — timestamps are not enough because back-to-back
    turns can write entries seconds apart."""
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None, None

    for line in reversed(tail.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        uid = entry.get("uuid")
        for block in entry.get("message", {}).get("content", []):
            if block.get("type") == "text":
                return block.get("text"), uid
        # Assistant turn with no text block (e.g., tool-only): keep scanning
    return None, None


def _poll_fresh_assistant_message(session_id, cwd, hook_start_ts):
    """Block until a newly-written assistant entry appears in the JSONL, or
    until the 2s ceiling. "Fresh" is defined as a different uuid than whatever
    was the last assistant entry at hook-start time.

    Exploits Claude Code's synchronous hook execution to *impose* ordering:
    Claude Code is blocked on this hook, so polling here cannot race the writer.

    Returns (text, is_stale). On ceiling: (TIMEOUT_PLACEHOLDER, True).

    Why uuid-change rather than a timestamp threshold: rapid back-to-back turns
    can write entries seconds apart, so any wall-clock freshness window large
    enough to tolerate flush lag is also wide enough to let the previous turn's
    entry pass as "fresh". UUIDs are unique per entry — a different uuid is an
    unambiguous signal that the writer has produced a new entry.
    """
    jsonl_path = Path.home() / ".claude" / "projects" / _project_dir(cwd) / f"{session_id}.jsonl"

    _, prev_uuid = _read_last_assistant_entry(jsonl_path)

    deadline = hook_start_ts + STOP_POLL_CEILING_S
    while True:
        text, uid = _read_last_assistant_entry(jsonl_path)
        if text and uid and uid != prev_uuid:
            if len(text.encode("utf-8")) > MAX_ASSISTANT_TEXT_BYTES:
                text = text.encode("utf-8")[:MAX_ASSISTANT_TEXT_BYTES].decode("utf-8", errors="ignore") + "…"
            return text, False
        if time.time() >= deadline:
            return TIMEOUT_PLACEHOLDER, True
        time.sleep(STOP_POLL_INTERVAL_S)


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
        # Tells the Mac this event came from a remote host. Cannot be inferred
        # from peer IP because the SSH reverse tunnel makes remote connections
        # appear as 127.0.0.1 on the Mac side (same as Mac-local statusline).
        "is_remote": True,
    }

    # Map events to status
    if event == "UserPromptSubmit":
        state["status"] = "processing"
        prompt = data.get("prompt")
        if prompt:
            state["message"] = prompt
            state["message_role"] = "user"
        try:
            log_path = Path.home() / ".claude" / "run" / "ccbridge-hook.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            preview = (prompt or "")[:120].replace("\n", "\\n")
            with open(log_path, "a") as f:
                f.write(f"{time.time():.3f} UserPromptSubmit session={session_id[:8]} prompt_len={len(prompt or '')} preview={preview!r}\n")
        except OSError:
            pass

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
        # Only pay the JSONL poll cost when a bridge is actually listening.
        if _read_bridge_port() is not None:
            text, is_stale = _poll_fresh_assistant_message(session_id, cwd, time.time())
            if text:
                state["message"] = text
                state["message_role"] = "assistant"
                if is_stale:
                    state["message_stale"] = True

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
