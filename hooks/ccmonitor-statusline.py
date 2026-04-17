#!/usr/bin/env python3
"""
ccmonitor-statusline.py — Claude Code statusline that also surfaces
subscription rate-limit data to claude-island.

Claude Code passes JSON on stdin; for Claude.ai Pro/Max users, after the first
API response in a session, it includes `rate_limits.{five_hour,seven_day}`.
This script captures those values to ~/.claude/run/usage.json and forwards a
TCP event to the Mac bridge if one is configured.

Fail open: any error exits 0 with empty stdout so the user's Claude Code
session is never broken by a monitor bug.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_send import send_event  # noqa: E402

USAGE_FILE = Path.home() / ".claude" / "run" / "usage.json"


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    rate_limits = data.get("rate_limits") or {}
    five = rate_limits.get("five_hour") or {}
    seven = rate_limits.get("seven_day") or {}

    five_pct = five.get("used_percentage")
    five_reset = five.get("resets_at")
    if five_pct is None or five_reset is None:
        sys.exit(0)

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

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
