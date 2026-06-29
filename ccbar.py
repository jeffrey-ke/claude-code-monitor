#!/usr/bin/env python3
"""ccbar — Consumer #2 of the ccstatus Session contract: a tmux status-line segment.

Reads the ~/.claude/run/status.json snapshot written by `ccstatus.py --serve` and prints
a *quiet-until-needed* alert: nothing while no session needs you, and `⛔ <title> +N more`
when one or more sessions are blocked/waiting. Designed to be spawned every `status-interval`
seconds from tmux `status-right` via `#(python3 .../ccbar.py)`, so it must stay cheap and
dependency-free (stdlib only) — it only reads one small cached file, never the live provider.

Output is consumed inside a tmux format, so it emits `#[...]` colour directives (which tmux
re-interprets) and escapes any literal `#` in dynamic text to `##`.
"""
import json
import os
import sys
import time
from pathlib import Path

STATUS_JSON = Path.home() / ".claude" / "run" / "status.json"
STALE_AFTER_S = 15          # serve writes every ~2s; older than this ⇒ daemon is likely dead
TITLE_MAX = 28

# Match ccdash's blocked styling (STATE_STYLE["blocked"] == "bold yellow") for consistency.
ALERT = "#[fg=yellow,bold]"
DIM = "#[fg=colour244]"
RESET = "#[default]"


def _esc(text):
    """tmux re-parses #(...) output as a format string, so a literal '#' must be doubled."""
    return text.replace("#", "##")


def _load():
    """Return (sessions, stale). Fail-open: any error ⇒ ([], False) so the bar stays quiet."""
    try:
        age = time.time() - STATUS_JSON.stat().st_mtime
        sessions = json.loads(STATUS_JSON.read_text())
        return (sessions if isinstance(sessions, list) else []), age > STALE_AFTER_S
    except (OSError, ValueError):
        return [], False


def render(sessions, stale):
    if stale:
        # A dead daemon must not masquerade as "all clear"; flag it faintly instead.
        return f"{DIM}⚠{RESET}"
    # "Needs you" = blocked, minus the ones already responded-to or hidden (both auto-clear
    # on new activity in the provider). Mirror ccdash's blocked-row selection.
    blocked = [s for s in sessions
               if s.get("state") == "blocked"
               and not s.get("acknowledged") and not s.get("dismissed")]
    if not blocked:
        return ""
    # Name the same session ccdash floats to the top: blocked, then ascending age.
    blocked.sort(key=lambda s: s["age_s"] if s.get("age_s") is not None else 1 << 30)
    title = _esc((blocked[0].get("title") or "?")[:TITLE_MAX])
    out = f"{ALERT}⛔ {title}{RESET}"
    if len(blocked) > 1:
        out += f"{DIM} +{len(blocked) - 1} more{RESET}"
    return out


def main():
    sessions, stale = _load()
    sys.stdout.write(render(sessions, stale))


if __name__ == "__main__":
    main()
