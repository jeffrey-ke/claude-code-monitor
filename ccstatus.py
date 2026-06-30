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
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

HOME = Path.home()
RUN = HOME / ".claude" / "run"
STATE_DIR = RUN / "state"
STATUS_FILE = RUN / "status"
STATUS_JSON = RUN / "status.json"   # full-fidelity Session[] snapshot for status-bar consumers
ACK_DIR = RUN / "ack"            # ~/.claude/run/ack/<sid>        (touch = responded-to)
DISMISS_DIR = RUN / "dismissed"  # ~/.claude/run/dismissed/<sid>  (touch = hidden in ccdash)
ROSTER = HOME / ".claude" / "daemon" / "roster.json"
PROJECTS = HOME / ".claude" / "projects"
# --serve self-heal: a long-lived daemon never reloads source, so it can silently keep
# running pre-fix logic for as long as it stays up. Captured at import time so the
# comparison is "changed since this process loaded it" — see _restart_if_source_changed.
_SELF_PATH = Path(__file__).resolve()
_SELF_MTIME = _SELF_PATH.stat().st_mtime if _SELF_PATH.exists() else None
# Shared ignore list for the consumers (ccdash + ccbar): one regex per line, '#' comments.
# The PROVIDER never filters on it — get_sessions() always emits the full Session[] so the
# notch / --serve stay complete; only the display consumers drop ignored rows.
IGNORE_FILE = Path(os.environ.get("CCMONITOR_IGNORE", str(RUN / "ccmonitor-ignore")))

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
    title: str           # haiku name → Claude name → short_id
    synopsis: str        # understanding → roster seed.intent → first user prompt → ""
    cwd: str
    cwd_short: str        # $HOME → ~
    tmux_target: "str | None"   # e.g. "train:1.0"
    pane_id: "str | None"       # e.g. "%363" — robust switch-client target
    age_s: "int | None"         # now − statusUpdatedAt
    model: "str | None" = None
    ctx_pct: "float | None" = None
    understanding: str = ""     # richer moving-average state behind the synopsis
    waiting_for: str = ""       # what a waiting/blocked session needs (e.g. "permission prompt")
    acknowledged: bool = False  # responded-to; auto-clears on new activity
    dismissed: bool = False     # hidden in ccdash; auto-clears on new activity
    engaged: bool = False       # transcript has ≥1 assistant turn (Claude has actually
                                # responded); gates the consumer "your turn" tier so a fresh
                                # idle session (never answered) isn't mistaken for a hand-back


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


def transcript_path(cwd, sid):
    """Public seam: the .jsonl transcript Path for a session (consumers e.g. ccdash reader)."""
    return _transcript_path(cwd, sid)


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


def _parse_ts(ts):
    """ISO-8601 transcript timestamp (UTC 'Z') → epoch seconds, or None. Comparable to a
    marker file's absolute st_mtime."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _transcript_usage(jsonl_path):
    """(model, ctx_pct, engaged, last_turn_ts) from the transcript tail. Best-effort, tail-only.

    `engaged` is True once any assistant turn is seen — i.e. Claude has produced output at
    least once. A genuine "your turn" hand-back always ends with an assistant turn (so it's in
    the tail); a brand-new / never-answered session has none, which is how consumers tell the
    two idle cases apart.

    `last_turn_ts` is the epoch of the newest *timestamped* entry (a real user/assistant/system
    turn). Mutable metadata records Claude Code rewrites in place — `ai-title`, `mode`,
    `file-history-snapshot`, … — carry no `timestamp`, so they don't advance it. This makes it
    the true "last activity" clock, unlike the file's st_mtime which a metadata-only rewrite
    bumps to "now" without any new turn.
    """
    try:
        size = jsonl_path.stat().st_size
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None, None, False, None
    model = ctx = last_ts = None
    engaged = False
    for line in reversed(tail.splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if last_ts is None:
            last_ts = _parse_ts(e.get("timestamp"))      # newest timestamped turn wins
        if not engaged and e.get("type") == "assistant":
            msg = e.get("message") or {}
            model = msg.get("model")
            usage = msg.get("usage") or {}
            used = (usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
            window = 1_000_000 if (model and "[1m]" in model) else 200_000
            ctx = round(100 * used / window, 1) if used else None
            engaged = True
        if engaged and last_ts is not None:
            break
    return model, ctx, engaged, last_ts


def _entry_text(e):
    """Plain text of a user/assistant transcript entry, or None if it carries no prose
    (a tool_result-only user turn or a tool_use-only assistant turn yields None)."""
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return " ".join(parts) if parts else None
    return None


def _is_genuine_user(e, text):
    """A real human-typed turn: a user entry with prose that isn't a meta/sidechain/wrapper
    envelope. (tool_result turns already fail this — their `text` comes back None.)"""
    return (e.get("type") == "user" and not e.get("isMeta") and not e.get("isSidechain")
            and bool(text) and text.strip() and not _looks_like_wrapper(text))


def turns_since_last_user(jsonl_path, tail_bytes=512 * 1024, max_turns=40, turn_max=320):
    """Conversation turns [{'role','text'}] from the last genuine user message to the end —
    the window you read/respond against ("what has Claude done since I last spoke").

    Fail-open: if no user turn is found in the tail (a very long agent run, or an unreadable
    file), fall back to the last `max_turns` text turns (the prior last-N behaviour).
    """
    try:
        size = Path(jsonl_path).stat().st_size
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    turns = []           # (role, text)
    last_user = None     # index into `turns` of the last genuine human message
    for line in tail.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") not in ("user", "assistant"):
            continue
        text = _entry_text(e)
        if not text or not text.strip():
            continue
        if e.get("type") == "user":
            if not _is_genuine_user(e, text):
                continue                       # skip tool_result / meta / wrapper user turns
            last_user = len(turns)
        turns.append((e.get("type"), " ".join(text.split())[:turn_max]))
    window = turns[last_user:] if last_user is not None else turns[-max_turns:]
    return [{"role": r, "text": t} for r, t in window]


def pending_interaction(jsonl_path, tail_bytes=256 * 1024):
    """The AskUserQuestion / ExitPlanMode the agent is currently parked on, so the reader can
    render the real options/plan instead of raw pane text. Returns None if nothing's pending.

        {'kind': 'question', 'questions': [{question, header, multiSelect, options:[{label,description}]}]}
        {'kind': 'plan',     'plan': '<markdown>'}
    """
    try:
        size = Path(jsonl_path).stat().st_size
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        if t == "user":
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                return None                    # the question was already answered → nothing pending
            continue
        if t != "assistant":
            continue                           # skip mode/ai-title/attachment/etc. between turns
        for b in (e.get("message") or {}).get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = b.get("name"), (b.get("input") or {})
            if name == "AskUserQuestion":
                return {"kind": "question", "questions": inp.get("questions") or []}
            if name == "ExitPlanMode":
                return {"kind": "plan", "plan": inp.get("plan") or ""}
        return None                            # most recent assistant turn isn't a Q/plan
    return None


# ── Normalization ────────────────────────────────────────────────────────────

def _normalize_state(rec, alive):
    """Collapse the agents-json status/state fields into one state.

    Interactive records carry `status` (busy|idle|shell|waiting) and no `state`;
    background records carry `state` (blocked|done|…) and sometimes `status`.
    Anything waiting on the user collapses to `blocked` (the actionable top tier):
    a background `state=blocked`, OR an interactive `status=waiting` — which covers a
    permission prompt, an AskUserQuestion menu, AND a plan-approval menu (all three are
    reported identically as status=waiting, waitingFor="permission prompt"; verified live).
    A present-but-dead pid is `dead`.
    """
    if not alive:
        return "dead"
    if rec.get("state") == "blocked":
        return "blocked"
    status = rec.get("status")
    if status == "waiting":        # interactive: permission prompt / question / plan menu
        return "blocked"
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


def _read_state(sid, suffix):
    """Read one ~/.claude/run/state/<sid><suffix> cache file, fail-open."""
    try:
        t = (STATE_DIR / f"{sid}{suffix}").read_text().strip()
        return t or None
    except OSError:
        return None


def _synopsis(sid, cwd, seeds, understanding):
    # Running understanding is the richest summary; else the launch intent, else the
    # first human prompt. The short haiku title (.synopsis) feeds `title`, not here.
    if understanding:
        return _oneline(understanding)
    if sid in seeds:
        return _oneline(seeds[sid])
    jp = _transcript_path(cwd, sid)
    if jp.exists():
        fp = _first_user_prompt(jp)
        if fp:
            return _oneline(fp)
    return ""


def _marker_active(dir_, sid, transcript_mtime):
    """A touch-marker (ack/dismissed) counts only while it is newer than the last
    transcript activity, so it auto-clears the moment the session writes something new
    (mtime advances past the mark)."""
    try:
        mark = (dir_ / sid).stat().st_mtime
    except OSError:
        return False
    return transcript_mtime is None or mark >= transcript_mtime


def _sort_key(s):
    return (STATE_ORDER.get(s.state, 5), s.age_s if s.age_s is not None else 1 << 30)


# ── Ignore list (consumer-side display policy; the provider never applies it) ──

def load_ignore_patterns(path=None):
    """Compiled regexes from the ignore file: one pattern per line, '#' starts a comment,
    blank lines skipped. A malformed pattern (or a missing/unreadable file) is skipped
    fail-open. Consumed by ccdash (and mirrored, stdlib-only, in ccbar)."""
    path = Path(path) if path is not None else IGNORE_FILE
    pats = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return pats
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            pats.append(re.compile(line, re.IGNORECASE))
        except re.error:
            pass            # ignore a bad pattern rather than break the whole list
    return pats


def is_ignored(s, patterns):
    """True if any pattern matches the session's kind, title, OR cwd (each searched
    independently, so `^Smoke test` anchors the title, `background` hides that kind, and a
    repo name hides a whole tree)."""
    return any(p.search(field) for p in patterns
               for field in (s.kind, s.title, s.cwd_short))


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
        # Age = seconds since the last real turn. We prefer the newest *timestamped* entry in
        # the transcript over the file's st_mtime: Claude Code rewrites the JSONL in place to
        # update metadata (ai-title, mode, file-history-snapshot, …), which bumps st_mtime to
        # "now" with no new turn — that phantom bump otherwise both fakes a fresh age and clears
        # an ack the user set earlier (mtime races past the marker), re-raising a settled "your
        # turn". The last timestamped turn ignores those rewrites; st_mtime is the fallback.
        # (sessions/<pid>.json statusUpdatedAt is unusable — older CLI versions leave it stale.)
        age = model = ctx = None
        engaged = False
        last_ts = None
        jp = _transcript_path(cwd, sid)
        jp_mtime = None
        try:
            jp_mtime = jp.stat().st_mtime
        except OSError:
            pass
        if jp.exists():
            model, ctx, engaged, last_ts = _transcript_usage(jp)
        activity_mtime = last_ts if last_ts is not None else jp_mtime
        if activity_mtime is not None:
            age = int(now - activity_mtime)

        understanding = _read_state(sid, ".understanding") or ""

        out.append(Session(
            session_id=sid,
            short_id=sid[:8],
            pid=pid,
            kind=kind,
            state=_normalize_state(rec, alive),
            title=_read_state(sid, ".synopsis") or rec.get("name") or sid[:8],
            synopsis=_synopsis(sid, cwd, seeds, understanding),
            cwd=cwd,
            cwd_short=_short_cwd(cwd),
            tmux_target=tmux_target,
            pane_id=pane_id,
            age_s=age,
            model=model,
            ctx_pct=ctx,
            understanding=understanding,
            waiting_for=rec.get("waitingFor") or "",
            acknowledged=_marker_active(ACK_DIR, sid, activity_mtime),
            dismissed=_marker_active(DISMISS_DIR, sid, activity_mtime),
            engaged=engaged,
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
        if s.acknowledged and state == "blocked":   # responded-to ⇒ stop alerting the notch
            state = "idle"
        lines.append(f"{target:<14} {state:<8} {name:<25} {cwd}")
    if not sessions:
        lines.append("(no sessions detected)")
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, STATUS_FILE)


def _write_status_json(sessions):
    """Persist the raw Session[] contract (same as `--json`) for status-bar consumers
    that need full state fidelity the claude-island TSV loses via SERVE_STATE."""
    STATUS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([asdict(s) for s in sessions]))
    os.replace(tmp, STATUS_JSON)


def _restart_if_source_changed():
    """If ccstatus.py's own source has changed on disk since this process loaded it,
    re-exec in place so --serve picks up the new code without an external restart."""
    if _SELF_MTIME is None:
        return
    try:
        current = _SELF_PATH.stat().st_mtime
    except OSError:
        return  # fail-open: a transient stat() failure shouldn't kill the daemon
    if current != _SELF_MTIME:
        print(f"ccstatus --serve: source changed ({_SELF_PATH}), restarting in place",
              file=sys.stderr)
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _serve(interval):
    print(f"ccstatus --serve: writing {STATUS_FILE} + {STATUS_JSON} every {interval}s",
          file=sys.stderr)
    while True:
        _restart_if_source_changed()
        try:
            sessions = get_sessions()
            _write_status_file(sessions)
            _write_status_json(sessions)
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
