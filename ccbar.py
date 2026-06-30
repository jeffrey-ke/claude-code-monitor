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
MAX_OUT = 200               # hard cap on emitted length — a guard against an absurd snapshot,
                            # never hit by 2 short segments; protects tmux's format parser

# Match ccdash's blocked styling (STATE_STYLE["blocked"] == "bold yellow") for consistency.
ALERT = "#[fg=yellow,bold]"
# Softer "your turn" tier — distinct from blocked's bold yellow. tmux has no `purple`, so
# magenta is the closest named color to ccdash's AWAIT_STYLE="purple" / AWAIT_GLYPH="◆".
TURN = "#[fg=magenta]"
TURN_GLYPH = "◆"
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


def _awaiting(s):
    """Idle and handed back to you (not yet acked/dismissed) ⇒ ccdash's 'your turn' tier.
    Mirrors ccdash._awaiting so the two consumers agree on the tier definition."""
    return (s.get("state") == "idle"
            and not s.get("acknowledged") and not s.get("dismissed"))


def _segment(rows, glyph, style):
    """`<style><glyph> <title><RESET>[<DIM> +N more]` for a non-empty needs-you list, naming
    the session ccdash floats to the top of that tier (ascending age); '' if empty."""
    if not rows:
        return ""
    rows = sorted(rows, key=lambda s: s["age_s"] if s.get("age_s") is not None else 1 << 30)
    title = _esc(str(rows[0].get("title") or "?")[:TITLE_MAX])   # str() — a corrupt snapshot
    out = f"{style}{glyph} {title}{RESET}"                       # may hand us a non-string
    if len(rows) > 1:
        out += f"{DIM} +{len(rows) - 1} more{RESET}"
    return out


def render(sessions, stale):
    if stale:
        # A dead daemon must not masquerade as "all clear"; flag it faintly instead.
        return f"{DIM}⚠{RESET}"
    # Two tiers, mirroring ccdash: blocked = "needs you" hard stop; awaiting = "your turn".
    # Both drop the ones already responded-to or hidden (auto-clear on new provider activity).
    # Skip non-dict rows defensively — a corrupt snapshot must never crash the bar.
    rows = [s for s in sessions if isinstance(s, dict)]
    blocked = [s for s in rows
               if s.get("state") == "blocked"
               and not s.get("acknowledged") and not s.get("dismissed")]
    awaiting = [s for s in rows if _awaiting(s)]
    segs = [_segment(blocked, "⛔", ALERT), _segment(awaiting, TURN_GLYPH, TURN)]
    out = "   ".join(seg for seg in segs if seg)   # 3 spaces between blocked (loud) + turn (soft)
    if len(out) > MAX_OUT:
        # Clamp an absurd snapshot; re-append RESET so a cut never leaves a dangling #[...].
        out = out[:MAX_OUT] + RESET
    return out


def main():
    try:
        sessions, stale = _load()
        sys.stdout.write(render(sessions, stale))
    except Exception:
        sys.stdout.write("")   # never let a bug brick tmux's status-right


if __name__ == "__main__":
    main()
