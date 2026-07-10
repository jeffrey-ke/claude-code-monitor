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
import re
import sys
import time
from pathlib import Path

STATUS_JSON = Path.home() / ".claude" / "run" / "status.json"
# Remote sessions mirrored from other machines by ccremote.py (one <host>.json per host, each
# a verbatim Session[] snapshot). Read alongside the local snapshot; a mirror older than
# STALE_AFTER_S is a dead syncer/host, so its rows are dropped (mirrors ccstatus loader).
REMOTE_DIR = Path(os.environ.get("CCMONITOR_REMOTE_DIR",
                                 str(Path.home() / ".claude" / "run" / "remote")))
# Same ignore file ccdash/ccstatus use; mirrored here (ccbar stays stdlib-only, no provider
# import) so an ignored session never alerts in the bar either. One regex/line, '#' comments.
IGNORE_FILE = Path(os.environ.get("CCMONITOR_IGNORE",
                                  str(Path.home() / ".claude" / "run" / "ccmonitor-ignore")))
# The remote-hosts intent declaration ccremote syncs from; a listed host with broken sync
# gets a red ⚠<host> warning (comment it out to mute). Mirrored stdlib-only from ccstatus.
REMOTES_FILE = Path(os.environ.get("CCMONITOR_REMOTES",
                                   str(Path.home() / ".claude" / "run" / "ccmonitor-remotes")))
STALE_AFTER_S = 15          # serve writes every ~2s; older than this ⇒ daemon is likely dead
# ccremote's per-host sidecar (non-.json so the mirror glob below never sees it) + the remote
# file's own age (remote clock) beyond which its provider is frozen — mirrors ccstatus.
HEALTH_SUFFIX = ".health"
REMOTE_CONTENT_STALE_S = 30
WARN_MAX_HOSTS = 2          # host names shown in the ⚠ segment before collapsing to +N
TITLE_MAX = 28
MAX_OUT = 200               # hard cap on emitted length — a guard against an absurd snapshot,
                            # never hit by 2 short segments; protects tmux's format parser

# Match ccdash's blocked styling (STATE_STYLE["blocked"] == "bold yellow") for consistency.
ALERT = "#[fg=yellow,bold]"
# Softer "your turn" tier — distinct from blocked's bold yellow. tmux has no `purple`, so
# magenta is the closest named color to ccdash's AWAIT_STYLE="purple" / AWAIT_GLYPH="◆".
TURN = "#[fg=magenta]"
TURN_GLYPH = "◆"
WARN = "#[fg=red]"          # broken remote sync — louder than the dim local-stale ⚠
DIM = "#[fg=colour244]"
SECTION_SEP = "#[fg=colour240]│#[default]"   # divides the local section from the remote section
RESET = "#[default]"


def _esc(text):
    """tmux re-parses #(...) output as a format string, so a literal '#' must be doubled."""
    return text.replace("#", "##")


def _num(v):
    """float(v) for a real number, else 0.0 — sidecar fields are foreign input."""
    return float(v) if isinstance(v, (int, float)) else 0.0


def _read_verdict(health, sidecar_age):
    """Reduce a host's sidecar + its file age to one verdict — the stdlib mirror of
    ccstatus.read_verdict (kept diff-ably identical; no provider import). Rows render for
    {ok, degraded}; the red ⚠ fires for {missing, stale-mirror, stale-content, down}.
    Thresholds scale to the sidecar's own self-description (syncer pace, remote write
    cadence) so a slow-but-alive chain never reads as dead."""
    if not isinstance(health, dict):
        return "missing"
    if sidecar_age is None or sidecar_age > max(
            STALE_AFTER_S, 3 * _num(health.get("interval_s")) + _num(health.get("ssh_timeout_s"))):
        return "stale-mirror"
    age = health.get("remote_status_age_s")
    if isinstance(age, (int, float)) and age > max(
            REMOTE_CONTENT_STALE_S, 5 * _num(health.get("remote_write_cadence_s"))):
        return "stale-content"
    state = health.get("state")
    if state in ("ok", "degraded", "down"):
        return state
    return "ok" if health.get("ok") else "down"    # v1 sidecar: no hysteresis to pass through


def _sidecar_verdict(jf, now):
    """_read_verdict for a mirror's sidecar; None when there is no sidecar at all (the
    caller then falls back to the mirror-mtime rule) — mirrors ccstatus._sidecar_verdict."""
    hf = jf.with_suffix(HEALTH_SUFFIX)
    try:
        health = json.loads(hf.read_text())
        if not isinstance(health, dict):
            health = None
    except (OSError, ValueError):
        health = None
    if health is None and not hf.exists():
        return None
    try:
        sidecar_age = now - hf.stat().st_mtime
    except OSError:
        sidecar_age = None
    return _read_verdict(health, sidecar_age)


def _load_remote():
    """Fresh remote sessions from run/remote/*.json (fail-open to []). Rows render only
    while the host's sidecar verdict is ok or degraded (a degraded blip keeps rows); a
    missing/stale/down source can't alert forever — the ⚠<host> warning covers it instead.
    A mirror with no sidecar at all falls back to the mtime rule."""
    now = time.time()
    out = []
    try:
        files = sorted(REMOTE_DIR.glob("*.json"))
    except OSError:
        return out
    for jf in files:
        try:
            verdict = _sidecar_verdict(jf, now)
            if verdict is None:                # no sidecar — mirror mtime is all we have
                if now - jf.stat().st_mtime > STALE_AFTER_S:
                    continue
            elif verdict not in ("ok", "degraded"):
                continue
            recs = json.loads(jf.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(recs, list):
            for r in recs:                     # tag the source host (the remote wrote host="")
                if isinstance(r, dict):        # so the segment can label it `host:<title>`
                    r["host"] = jf.stem
            out.extend(recs)
    return out


def _load():
    """Return (sessions, stale). `sessions` = local snapshot + fresh remote mirrors; `stale`
    tracks the *local* daemon only. Local and remote are read independently — a missing or
    corrupt local snapshot flags stale (a dead daemon must not look like all-quiet) and must
    never suppress the remote sections, which have their own sync chain."""
    local, stale = [], False
    try:
        age = time.time() - STATUS_JSON.stat().st_mtime
        rows = json.loads(STATUS_JSON.read_text())
        local = rows if isinstance(rows, list) else []
        stale = age > STALE_AFTER_S
    except (OSError, ValueError):
        stale = True
    return local + _load_remote(), stale


def _unhealthy_hosts():
    """Hosts listed in ccmonitor-remotes whose verdict is red — {missing, stale-mirror,
    stale-content, down}; a degraded blip is deliberately NOT here (hysteresis: red is
    reserved for a confirmed outage). Order preserved; mirrors ccstatus.load_remote_health
    reduced to a name list; commenting the host out of the file is the mute. Fail-open
    to []."""
    now, seen, bad = time.time(), set(), []
    try:
        lines = REMOTES_FILE.read_text().splitlines()
    except OSError:
        return bad
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        host = line.split(None, 1)[0]
        stem = "".join(c if (c.isalnum() or c in "._@-") else "_" for c in host)
        if stem in seen:
            continue
        seen.add(stem)
        verdict = _sidecar_verdict(REMOTE_DIR / f"{stem}.json", now)
        if verdict not in ("ok", "degraded"):  # None (no sidecar at all) is red: the host
            bad.append(stem)                   # is *declared* here, so silence = missing
    return bad


def _awaiting(s):
    """The ◆ type 2 (response-complete) tier: Claude's turn is over — state idle, OR
    busy/shell with turn_complete (lingering background shells/agents pin the CLI status
    after a hand-back: the yazi bug) — engaged (Claude has responded), not
    acked/dismissed, and not the pane you're watching right now (`focused` — seen =
    handled). Mirrors ccstatus.awaiting (lockstep table in tests/test_your_turn.py); the
    `engaged` gate suppresses a fresh never-answered idle session. Old snapshots lack
    keys → falsy → no false ◆ (engaged/turn_complete) and no false suppression
    (focused)."""
    state = s.get("state")
    return bool((state == "idle"
                 or (state in ("busy", "shell") and s.get("turn_complete")))
                and s.get("engaged")
                and not s.get("acknowledged") and not s.get("dismissed")
                and not s.get("focused"))


def _load_ignore():
    """Compiled ignore regexes (mirrors ccstatus.load_ignore_patterns); fail-open to []."""
    pats = []
    try:
        lines = IGNORE_FILE.read_text().splitlines()
    except OSError:
        return pats
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            pats.append(re.compile(line, re.IGNORECASE))
        except re.error:
            pass
    return pats


def _ignored(s, patterns):
    """True if any pattern matches the row's kind/title/cwd (mirrors ccstatus.is_ignored)."""
    return any(p.search(str(s.get(f, "") or ""))
               for p in patterns for f in ("kind", "title", "cwd_short"))


def _segment(rows, glyph, style):
    """`<style><glyph> <title><RESET>[<DIM> +N more]` for a non-empty needs-you list, naming
    the session ccdash floats to the top of that tier (ascending age); '' if empty."""
    if not rows:
        return ""
    rows = sorted(rows, key=lambda s: s["age_s"] if s.get("age_s") is not None else 1 << 30)
    name = str(rows[0].get("title") or "?")                      # str() — a corrupt snapshot
    if rows[0].get("host"):                                      # may hand us a non-string
        name = f"{rows[0]['host']}:{name}"                       # remote: label `host:<title>`
    title = _esc(name[:TITLE_MAX])
    out = f"{style}{glyph} {title}{RESET}"
    if len(rows) > 1:
        out += f"{DIM} +{len(rows) - 1} more{RESET}"
    return out


def _two_tier(rows):
    """The blocked (⛔) + your-turn (◆) segments for one section (local or a remote host), joined
    by 3 spaces; '' if the section has nothing needing you. Drops acked/dismissed as before."""
    blocked = [s for s in rows
               if s.get("state") == "blocked"
               and not s.get("acknowledged") and not s.get("dismissed")]
    awaiting = [s for s in rows if _awaiting(s)]
    segs = [_segment(blocked, "⛔", ALERT), _segment(awaiting, TURN_GLYPH, TURN)]
    return "   ".join(seg for seg in segs if seg)   # 3 spaces between blocked (loud) + turn (soft)


def render(sessions, stale, unhealthy=()):
    # Two tiers, mirroring ccdash: blocked = "needs you" hard stop; awaiting = "your turn".
    # Both drop the ones already responded-to or hidden (auto-clear on new provider activity).
    # Skip non-dict rows defensively — a corrupt snapshot must never crash the bar.
    rows = [s for s in sessions if isinstance(s, dict)]
    patterns = _load_ignore()                       # honor the same ignore file as ccdash
    if patterns:
        rows = [s for s in rows if not _ignored(s, patterns)]
    # Local and remote (mirrored from another host) sessions get their own section, so at a glance
    # you can tell where an alert lives. Each section shows the same two tiers.
    local = [s for s in rows if not s.get("host")]
    remote = [s for s in rows if s.get("host")]
    if stale:
        # A dead local daemon must not masquerade as "all clear" — flag it faintly — but its
        # rows are untrusted, and the remote sections (own sync chain) still render.
        local = []
    sections = [_two_tier(local), _two_tier(remote)]
    body = f"   {SECTION_SEP}   ".join(sec for sec in sections if sec)  # divider between two
    parts = []                                                          # non-empty sections
    if stale:
        parts.append(f"{DIM}⚠{RESET}")
    if body:
        parts.append(body)
    if unhealthy:
        # Broken remote sync is loud (red), never silent-vanishing rows: ⚠psc,gpu2 +1
        names = ",".join(_esc(str(h))[:TITLE_MAX] for h in unhealthy[:WARN_MAX_HOSTS])
        more = f" +{len(unhealthy) - WARN_MAX_HOSTS}" if len(unhealthy) > WARN_MAX_HOSTS else ""
        parts.append(f"{WARN}⚠{names}{more}{RESET}")
    out = "   ".join(parts)
    if len(out) > MAX_OUT:
        # Clamp an absurd snapshot; re-append RESET so a cut never leaves a dangling #[...].
        out = out[:MAX_OUT] + RESET
    return out


def main():
    try:
        sessions, stale = _load()
        sys.stdout.write(render(sessions, stale, _unhealthy_hosts()))
    except Exception:
        sys.stdout.write("")   # never let a bug brick tmux's status-right


if __name__ == "__main__":
    main()
