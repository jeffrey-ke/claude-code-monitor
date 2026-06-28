#!/usr/bin/env python3
"""
ccstatus.py — provider: one normalized status record per live Claude Code session.

This is the single source of truth consumed by ccdash (the TUI) and any other tool.
It gathers from the supported `claude agents --json` API and enriches each record with
age (~/.claude/sessions/<pid>.json), synopsis (AI cache → roster seed → first prompt),
and tmux pane location (tmux list-panes + /proc parent-walk), then emits a stable
`Session` record.

CLI:
  ccstatus                    bare TSV, blocked-first, header   (pipe-friendly default)
  ccstatus --json             Session[] as JSON  → ccdash + other consumers
  ccstatus --no-header        omit the TSV header (for awk/cut)
  ccstatus --state blocked    filter by state; exit 1 if no rows match (script-gateable)
  ccstatus --watch[=SECS]     repaint every SECS (default 1.5)
  ccstatus --serve[=SECS]     daemon: write ~/.claude/run/status (claude-island format)

Every enrichment is fail-open: a missing/garbled source yields None for that field,
never an exception (same philosophy as ccmonitor-statusline.py).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

HOME = Path.home()
RUN = HOME / ".claude" / "run"
STATE_DIR = RUN / "state"
STATUS_FILE = RUN / "status"
ROSTER = HOME / ".claude" / "daemon" / "roster.json"
PROJECTS = HOME / ".claude" / "projects"

# Sort/priority: actionable states float to the top (k9s convention).
STATE_ORDER = {"blocked": 0, "busy": 1, "shell": 2, "idle": 3, "dead": 4}
# Map our finer states onto the three the claude-island notch UI parses.
SERVE_STATE = {"blocked": "blocked", "busy": "working", "shell": "working",
               "idle": "idle", "dead": "idle"}
SYNOPSIS_MAX = 140


@dataclass
class Session:
    """The provider contract. Serialized verbatim by `--json`."""
    session_id: str
    short_id: str
    pid: "int | None"
    kind: str            # interactive | background
    state: str           # blocked | busy | shell | idle | dead
    title: str           # Claude-generated name, else short_id
    synopsis: str        # AI cache → roster seed.intent → first user prompt → ""
    cwd: str
    cwd_short: str        # $HOME → ~
    tmux_target: "str | None"   # e.g. "train:1.0"
    pane_id: "str | None"       # e.g. "%363" — robust switch-client target
    age_s: "int | None"         # now − statusUpdatedAt
    model: "str | None" = None
    ctx_pct: "float | None" = None


# ── Subprocess helpers ───────────────────────────────────────────────────────

def _run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _claude_agents():
    """The supported session list. Returns [] on any failure."""
    out = _run(["claude", "agents", "--json"], timeout=10)
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


# ── Process tree (lifted from claude_status.py) ──────────────────────────────

def _ppid(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return line.split()[1]
    except OSError:
        return None


def _comm(pid):
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def _find_pane_pid(claude_pid, pane_pids):
    """Walk up from claude_pid until we hit a pid tmux reports as a pane."""
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


def _alive(pid):
    # Background sessions may have no live process; the agents list still vouches
    # for them, so only a present-but-vanished pid counts as dead.
    if pid is None:
        return True
    return Path(f"/proc/{pid}").exists()


# ── Source readers (all fail-open) ───────────────────────────────────────────

def _tmux_panes():
    """{pane_pid: (pane_id, target)} for every pane on the server."""
    fmt = "#{pane_pid}\t#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"
    panes = {}
    for line in _run(["tmux", "list-panes", "-a", "-F", fmt]).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            panes[parts[0]] = (parts[1], parts[2])
    return panes


def _roster_seeds():
    """{sessionId: launch intent} — free synopsis for background sessions."""
    try:
        d = json.loads(ROSTER.read_text())
    except (OSError, ValueError):
        return {}
    seeds = {}
    for w in (d.get("workers") or {}).values():
        sid = w.get("sessionId")
        intent = ((w.get("dispatch") or {}).get("seed") or {}).get("intent")
        if sid and intent:
            seeds[sid] = intent
    return seeds


def _project_dir(cwd):
    """Claude Code's project-dir encoding: replace /, ., and _ with -."""
    return cwd.replace("/", "-").replace(".", "-").replace("_", "-")


def _transcript_path(cwd, sid):
    return PROJECTS / _project_dir(cwd) / f"{sid}.jsonl"


def _looks_like_wrapper(text):
    """Slash-command / local-command / caveat envelopes — not a real first prompt."""
    t = text.lstrip()
    return t.startswith("<") or t.startswith("Caveat:")


def _first_user_prompt(jsonl_path):
    """First real human turn from the transcript head — instant synopsis fallback.

    Skips slash-command and local-command wrappers (which Claude Code records as
    user turns) so the fallback reads as intent, not machinery.
    """
    try:
        with open(jsonl_path, "rb") as f:
            head = f.read(64 * 1024).decode("utf-8", "replace")
    except OSError:
        return None
    for line in head.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "user":
            continue
        content = (e.get("message") or {}).get("content")
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    text = b["text"]
                    break
        if text and text.strip() and not _looks_like_wrapper(text):
            return text.strip()
    return None


def _transcript_usage(jsonl_path):
    """(model, ctx_pct) from the last assistant message. Best-effort, tail-only."""
    try:
        size = jsonl_path.stat().st_size
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None, None
    for line in reversed(tail.splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        model = msg.get("model")
        usage = msg.get("usage") or {}
        used = (usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0))
        window = 1_000_000 if (model and "[1m]" in model) else 200_000
        ctx = round(100 * used / window, 1) if used else None
        return model, ctx
    return None, None


# ── Normalization ────────────────────────────────────────────────────────────

def _normalize_state(rec, alive):
    """Collapse the agents-json status/state fields into one state.

    Interactive records carry `status` (busy|idle|shell) and no `state`;
    background records carry `state` (blocked|…) and sometimes `status`.
    Permission-blocked wins; a present-but-dead pid is `dead`.
    """
    if not alive:
        return "dead"
    if rec.get("state") == "blocked":
        return "blocked"
    status = rec.get("status")
    if status in ("busy", "shell", "idle"):
        return status
    state = rec.get("state")
    if state:
        return "blocked" if state == "blocked" else state
    return "idle"


def _oneline(text):
    if not text:
        return ""
    s = " ".join(text.split())
    return s[:SYNOPSIS_MAX - 1] + "…" if len(s) > SYNOPSIS_MAX else s


def _short_cwd(cwd):
    home = str(HOME)
    return ("~" + cwd[len(home):]) if cwd.startswith(home) else cwd


def _synopsis(sid, cwd, seeds):
    cache = STATE_DIR / f"{sid}.synopsis"
    try:
        txt = cache.read_text().strip()
        if txt:
            return _oneline(txt)
    except OSError:
        pass
    if sid in seeds:
        return _oneline(seeds[sid])
    jp = _transcript_path(cwd, sid)
    if jp.exists():
        fp = _first_user_prompt(jp)
        if fp:
            return _oneline(fp)
    return ""


def _sort_key(s):
    return (STATE_ORDER.get(s.state, 5), s.age_s if s.age_s is not None else 1 << 30)


# ── Assembly ─────────────────────────────────────────────────────────────────

def get_sessions():
    """The public seam. Returns sorted list[Session]."""
    recs = _claude_agents()
    seeds = _roster_seeds()
    panes = _tmux_panes()
    pane_pids = set(panes)
    now = time.time()

    out = []
    for rec in recs:
        sid = rec.get("sessionId") or ""
        if not sid:
            continue
        pid = rec.get("pid")
        kind = rec.get("kind") or "interactive"
        alive = _alive(pid)

        tmux_target = pane_id = None
        if pid is not None:
            ppid = _find_pane_pid(str(pid), pane_pids)
            if ppid:
                pane_id, tmux_target = panes[ppid]

        cwd = rec.get("cwd") or ""
        # Age = seconds since the transcript was last appended (i.e. last activity).
        # The transcript mtime is reliable across CLI versions; sessions/<pid>.json
        # statusUpdatedAt is not — older versions leave it hours stale.
        age = model = ctx = None
        jp = _transcript_path(cwd, sid)
        try:
            age = int(now - jp.stat().st_mtime)
        except OSError:
            pass
        if jp.exists():
            model, ctx = _transcript_usage(jp)

        out.append(Session(
            session_id=sid,
            short_id=sid[:8],
            pid=pid,
            kind=kind,
            state=_normalize_state(rec, alive),
            title=rec.get("name") or sid[:8],
            synopsis=_synopsis(sid, cwd, seeds),
            cwd=cwd,
            cwd_short=_short_cwd(cwd),
            tmux_target=tmux_target,
            pane_id=pane_id,
            age_s=age,
            model=model,
            ctx_pct=ctx,
        ))

    out.sort(key=_sort_key)
    return out


# ── Output ───────────────────────────────────────────────────────────────────

TSV_COLS = ["STATE", "KIND", "AGE_S", "TMUX", "SID", "TITLE", "CWD", "SYNOPSIS"]


def _tsv(sessions, header=True):
    rows = ["\t".join(TSV_COLS)] if header else []
    for s in sessions:
        rows.append("\t".join([
            s.state,
            s.kind,
            "" if s.age_s is None else str(s.age_s),
            s.tmux_target or "-",
            s.short_id,
            s.title,
            s.cwd_short,
            s.synopsis,
        ]))
    return "\n".join(rows)


def _emit(sessions, args):
    if args.json:
        print(json.dumps([asdict(s) for s in sessions], indent=2))
    else:
        print(_tsv(sessions, header=not args.no_header))


def _write_status_file(sessions):
    """Reproduce claude_status.py's status-file format for claude-island."""
    lines = ["TARGET         STATE    NAME                      CWD"]
    for s in sessions:
        cwd = s.cwd_short[-36:]
        name = (s.title if s.title != s.short_id else "")[:24]
        target = s.tmux_target or "?"
        state = SERVE_STATE.get(s.state, s.state)
        lines.append(f"{target:<14} {state:<8} {name:<25} {cwd}")
    if not sessions:
        lines.append("(no sessions detected)")
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, STATUS_FILE)


def _serve(interval):
    print(f"ccstatus --serve: writing {STATUS_FILE} every {interval}s", file=sys.stderr)
    while True:
        try:
            _write_status_file(get_sessions())
        except Exception as e:  # never let the daemon die on a transient read
            print(f"ccstatus serve: {e}", file=sys.stderr)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="Live status of all Claude Code sessions.")
    ap.add_argument("--json", action="store_true", help="emit Session[] as JSON")
    ap.add_argument("--no-header", action="store_true", help="omit TSV header")
    ap.add_argument("--state", choices=list(STATE_ORDER),
                    help="filter to one state; exit 1 if none match")
    ap.add_argument("--watch", nargs="?", const=1.5, type=float, default=None,
                    metavar="SECS", help="repaint every SECS (default 1.5)")
    ap.add_argument("--serve", nargs="?", const=2.0, type=float, default=None,
                    metavar="SECS", help="write ~/.claude/run/status every SECS")
    args = ap.parse_args()

    if args.serve is not None:
        _serve(args.serve)
        return

    def snapshot():
        s = get_sessions()
        return [x for x in s if x.state == args.state] if args.state else s

    if args.watch is not None:
        try:
            while True:
                sys.stdout.write("\x1b[2J\x1b[H")
                _emit(snapshot(), args)
                sys.stdout.flush()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            pass
        return

    sessions = snapshot()
    _emit(sessions, args)
    if args.state and not sessions:
        sys.exit(1)


if __name__ == "__main__":
    main()
