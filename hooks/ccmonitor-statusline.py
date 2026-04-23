#!/usr/bin/env python3
"""
ccmonitor-statusline.py — Claude Code statusline that also surfaces
subscription rate-limit data to claude-island.

Two jobs:
 1. Render a one-line statusline (model, context left, cost, cwd) to stdout.
 2. For Claude.ai Pro/Max users, capture `rate_limits.{five_hour,seven_day}`
    to ~/.claude/run/usage.json and forward a TCP event to the Mac bridge.

Fail open: any error still emits a minimal statusline and exits 0 so the
user's Claude Code session is never broken by a monitor bug.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_send import send_event  # noqa: E402

USAGE_FILE = Path.home() / ".claude" / "run" / "usage.json"

RESET = "\x1b[0m"
CYAN = "\x1b[36m"
BLUE = "\x1b[34m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def _last_assistant_usage(transcript_path):
    """Return the `usage` dict from the most recent assistant message, or None.

    Reads only the tail of the file to keep this cheap on long sessions.
    """
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        read_bytes = min(size, 256 * 1024)
        with path.open("rb") as f:
            f.seek(size - read_bytes)
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") != "assistant":
            continue
        usage = (obj.get("message") or {}).get("usage")
        if usage:
            return usage
    return None


def _context_window(model_id):
    # "[1m]" suffix indicates the 1M-context variant; everything else is 200k.
    if model_id and "[1m]" in model_id:
        return 1_000_000
    return 200_000


def _pct_color(left_pct):
    if left_pct > 50:
        return GREEN
    if left_pct > 20:
        return YELLOW
    return RED


def _render_statusline(data):
    parts = []

    cwd = data.get("cwd") or ""
    if cwd:
        parts.append(f"{BLUE}{Path(cwd).name}{RESET}")

    model_name = (data.get("model") or {}).get("display_name") or "Claude"
    parts.append(f"{CYAN}{model_name}{RESET}")

    usage = _last_assistant_usage(data.get("transcript_path"))
    if usage:
        used = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        window = _context_window((data.get("model") or {}).get("id"))
        left = max(0, window - used)
        left_pct = (left / window) * 100 if window else 0
        color = _pct_color(left_pct)
        parts.append(f"{color}{left_pct:.0f}% left{RESET}")

    return " · ".join(parts)


def _forward_rate_limits(data):
    rate_limits = data.get("rate_limits") or {}
    five = rate_limits.get("five_hour") or {}
    seven = rate_limits.get("seven_day") or {}

    five_pct = five.get("used_percentage")
    five_reset = five.get("resets_at")
    if five_pct is None or five_reset is None:
        return

    payload = {
        "five_hour_used_pct": five_pct,
        "five_hour_resets_at": five_reset,
        "seven_day_used_pct": seven.get("used_percentage"),
        "seven_day_resets_at": seven.get("resets_at"),
        "updated_at": int(time.time()),
    }
    try:
        _atomic_write(USAGE_FILE, payload)
    except OSError:
        pass

    send_event({
        "event": "Usage",
        "five_hour_used_pct": five_pct,
        "five_hour_resets_at": five_reset,
        "seven_day_used_pct": seven.get("used_percentage"),
        "seven_day_resets_at": seven.get("resets_at"),
    })


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    try:
        line = _render_statusline(data)
    except Exception:
        line = ""
    if line:
        sys.stdout.write(line)

    try:
        _forward_rate_limits(data)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
