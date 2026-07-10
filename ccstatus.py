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
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

HOME = Path.home()
RUN = HOME / ".claude" / "run"
STATE_DIR = RUN / "state"
# --serve output paths are env-overridable so a host with a small/quota'd $HOME can write the
# snapshot elsewhere (e.g. scratch/project space). Both are tiny, atomically-overwritten
# snapshots (not append logs) — see the header. Consumers on the *same* box read these defaults,
# so only relocate on a host whose snapshot is fetched remotely (ccremote points at the new path).
STATUS_FILE = Path(os.environ.get("CCSTATUS_STATUS_FILE", str(RUN / "status")))
STATUS_JSON = Path(os.environ.get("CCSTATUS_STATUS_JSON", str(RUN / "status.json")))
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
# Remote monitoring: a sibling syncer (ccremote.py) SSH-mirrors each remote host's
# run/status.json into run/remote/<host>.json; consumers fold those in via
# load_remote_sessions(). The provider itself stays strictly machine-local.
# ($CCMONITOR_REMOTE_DIR mirrors ccremote's override — isolated worktree/test runs.)
REMOTE_DIR = Path(os.environ.get("CCMONITOR_REMOTE_DIR", str(RUN / "remote")))
# Freshness floors for read_verdict. Both scale UP from the sidecar's own self-description
# (interval_s/ssh_timeout_s for the syncer, remote_write_cadence_s for the content), so a
# slow-but-configured-that-way chain never reads as dead; the floors only catch a sidecar
# too old/sparse to speak for itself.
REMOTE_STALE_S = 15            # sidecar older than max(this, 3·interval+timeout) ⇒ syncer dead
REMOTE_CONTENT_STALE_S = 30    # remote file older (remote clock) than max(this, 5·cadence) ⇒
                               # the remote's provider froze while `ssh cat` kept re-mirroring
                               # it fresh ("phantom fresh" — the mirror mtime can't see this)
HEALTH_SUFFIX = ".health"      # ccremote's per-host sidecar (non-.json: the *.json mirror
                               # globs here and in ccbar can never mistake it for a mirror)
REMOTES_FILE = Path(os.environ.get("CCMONITOR_REMOTES", str(RUN / "ccmonitor-remotes")))

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
    turn_complete: bool = False # the transcript's newest conversational entry is a turn-end
                                # marker: Claude finished and handed back, even when lingering
                                # background shells/agents pin the CLI status at busy/shell
                                # (the yazi bug) — see _turn_complete_step
    focused: bool = False       # an attached tmux client is viewing this pane right now
                                # (pane+window active, session attached) — a fact; consumers
                                # own the policy (suppress ◆ / --serve auto-ack). Fail-open
                                # False: no tmux / old snapshot ⇒ alert as before
    host: str = ""              # "" = local; set to the SSH host by load_remote_sessions()
                                # for a session mirrored from another machine (view-only)


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


def _proc_identity_ok(comm, cmdline, proc_uid, my_uid):
    """Does this /proc entry still look like the claude process a session record points at?
    True iff it's ours (uid) and claude-ish — comm starts with `claude` or `node` (the CLI
    runs on node, so comm may show the runtime), or `claude` appears anywhere in the
    cmdline. Pure — the truth table is testable without a /proc."""
    if proc_uid != my_uid:
        return False
    if (comm or "").lower().startswith(("claude", "node")):
        return True
    return "claude" in (cmdline or "").lower()


def _alive(pid):
    """A session's process is alive iff /proc/<pid> exists AND still passes the identity
    check. Bare existence bred orange ghosts on multi-login-node hosts (bridges2): `claude
    agents --json` reads $HOME-shared state while /proc is per-node, so a closed session's
    pid — recycled by any unrelated process on a busy node — kept its last state (blocked/
    busy) frozen forever. False-dead beats false-blocked: a genuinely cross-node session
    shows `dead` (grey, non-alerting), never a phantom "needs you".

    pid None stays vouched — a blocked background session has no live process by design;
    the agents list is its witness. Fail-open nuance: an entry we can't even stat counts
    alive (a permission hiccup must not kill a live session), but a different uid's pid is
    dead — that IS the recycling case."""
    if pid is None:
        return True
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return False
    try:
        proc_uid = proc.stat().st_uid
    except OSError:
        return True
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        cmdline = ""
    return _proc_identity_ok(_comm(pid), cmdline, proc_uid, os.getuid())


# ── Source readers (all fail-open) ───────────────────────────────────────────

_PANE_FMT = ("#{pane_pid}\t#{pane_id}\t#{pane_active}\t#{window_active}"
             "\t#{session_attached}\t#{session_name}:#{window_index}.#{pane_index}")


def _parse_panes(output):
    """{pane_pid: (pane_id, target, focused)} from `list-panes -a -F _PANE_FMT` output.
    focused = an attached client is looking at this pane RIGHT NOW: active pane of its
    window AND the window is the session's current one AND ≥1 client attached. The
    free-text target is last + maxsplit so a tab in a session name can't shift fields.
    Garbage flags read unfocused — fail-open toward alerting, never toward suppression."""
    panes = {}
    for line in output.splitlines():
        parts = line.split("\t", 5)
        if len(parts) != 6:
            continue
        pid, pane_id, pane_on, win_on, attached, target = parts
        focused = (pane_on == "1" and win_on == "1"
                   and attached.isdigit() and int(attached) > 0)
        panes[pid] = (pane_id, target, focused)
    return panes


def _tmux_panes():
    """{pane_pid: (pane_id, target, focused)} for every pane on the server."""
    return _parse_panes(_run(["tmux", "list-panes", "-a", "-F", _PANE_FMT]))


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
    """(model, ctx_pct, engaged, last_turn_ts, turn_complete) from the transcript tail.
    Best-effort, tail-only.

    `engaged` is True once any assistant turn is seen — i.e. Claude has produced output at
    least once. A genuine "your turn" hand-back always ends with an assistant turn (so it's in
    the tail); a brand-new / never-answered session has none, which is how consumers tell the
    two idle cases apart.

    `last_turn_ts` is the epoch of the newest *timestamped* entry (a real user/assistant/system
    turn). Mutable metadata records Claude Code rewrites in place — `ai-title`, `mode`,
    `file-history-snapshot`, … — carry no `timestamp`, so they don't advance it. This makes it
    the true "last activity" clock, unlike the file's st_mtime which a metadata-only rewrite
    bumps to "now" without any new turn.

    `turn_complete` is the newest conversational entry being a turn-end marker — the
    per-entry rule lives in _turn_complete_step. Same single tail read, no extra I/O.
    """
    try:
        size = jsonl_path.stat().st_size
        with open(jsonl_path, "rb") as f:
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None, None, False, None, False
    model = ctx = last_ts = turn_complete = None
    engaged = False
    for line in reversed(tail.splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if turn_complete is None:
            turn_complete = _turn_complete_step(e)       # newest conversational entry decides
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
        if engaged and last_ts is not None and turn_complete is not None:
            break
    return model, ctx, engaged, last_ts, bool(turn_complete)


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


TURN_END_SUBTYPES = ("turn_duration", "stop_hook_summary")   # system entries closing a turn


def _turn_complete_step(e):
    """One vote in the newest→oldest transcript walk: is the turn over?

    Returns True/False when this entry decides it, None to keep walking. The turn is
    complete ⇔ the newest *conversational* entry is a turn-end marker (system
    turn_duration / stop_hook_summary — CLI ≥2.1.x writes both after the final assistant
    message). No vote (skip): untimestamped mutable metadata (custom-title, agent-name,
    file-history-snapshot…), sidechain entries (background-agent chatter),
    system/local_command (a /rename or /model run after hand-back must not mask it), and
    non-genuine user entries (tool_results, <system-reminder>/caveat wrappers). Anything
    else means the turn is live: a genuine user prompt (Claude is about to run), an
    assistant entry (mid-generation), or an unrecognized type — fail toward False, i.e.
    no alert, exactly today's behavior on an old CLI without turn-end markers.

    Known accepted imperfection: a parent auto-resuming after a background task completes
    can read True for the few seconds before its first assistant entry lands — a brief
    false ◆ that self-corrects next tick."""
    if _parse_ts(e.get("timestamp")) is None or e.get("isSidechain"):
        return None
    t = e.get("type")
    if t == "system":
        sub = e.get("subtype")
        return None if sub == "local_command" else sub in TURN_END_SUBTYPES
    if t == "user" and not _is_genuine_user(e, _entry_text(e)):
        return None
    return False


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


def touch_marker(dir_, sid):
    """Set an ack/dismiss touch-marker (mtime = now ⇒ active until the transcript advances
    past it — see _marker_active). The write side of the marker pair; shared by ccdash's
    keys and --serve's focused auto-ack."""
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / sid).touch()


def _sort_key(s):
    return (STATE_ORDER.get(s.state, 5), s.age_s if s.age_s is not None else 1 << 30)


# ── "Your turn" tier (consumer-side alert policy; ccdash imports it, ccbar mirrors) ──

def handed_back(s):
    """The raw type 2 (◆ response-complete) condition: Claude's turn is over, engaged
    (Claude actually responded), not yet acked/dismissed. "Turn is over" = state idle,
    OR busy/shell with turn_complete — lingering background shells/agents pin the CLI
    status at busy/shell after a hand-back (the yazi bug), so the transcript's turn-end
    markers are the truth there. What ccdash's jump-ack and --serve's focused auto-ack
    act on. blocked (⛔ type 1) stays structurally exempt from both."""
    return ((s.state == "idle" or (s.state in ("busy", "shell") and s.turn_complete))
            and s.engaged and not s.acknowledged and not s.dismissed)


def awaiting(s):
    """What consumers alert ◆ on: a hand-back you are NOT already watching live.
    focused defaults False (old snapshot / no tmux) ⇒ fail-open to alerting."""
    return handed_back(s) and not s.focused


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


# ── Remote sessions (consumer-side; mirrored by ccremote.py, not gathered here) ─

_SESSION_FIELDS = {f.name for f in fields(Session)}
# Tolerant reconstruction for mirrored records: a remote running an older/newer schema may
# lack fields that have no dataclass default — fill them by type instead of dropping the row
# (ccbar's dict reads already tolerate this; ccdash shouldn't be stricter). Only a record
# with no session_id is skipped: identity is the one thing ack/dedup can't fake.
_SESSION_DEFAULTS = {"session_id": "", "short_id": "", "pid": None, "kind": "interactive",
                     "state": "idle", "title": "", "synopsis": "", "cwd": "", "cwd_short": "",
                     "tmux_target": None, "pane_id": None, "age_s": None}


def _read_health(hf):
    """One ccremote `.health` sidecar, parsed fail-open; None if missing/garbled."""
    try:
        rec = json.loads(hf.read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _num(v):
    """float(v) for a real number, else 0.0 — sidecar fields are foreign input."""
    return float(v) if isinstance(v, (int, float)) else 0.0


def read_verdict(health, sidecar_age_s):
    """THE remote-trust rule, shared by every consumer (ccbar carries a stdlib mirror):
    reduce one host's sidecar + its file age to a single verdict. ccremote already computed
    the fetch-side judgment (`state`: ok | degraded | down, with hysteresis); what it cannot
    know is whether its own sidecar is still being written or whether the remote file has
    quietly frozen — the two read-side checks layered on top here:

      missing        — no/garbled sidecar: the syncer never ran (mtime backstop elsewhere)
      stale-mirror   — sidecar older than max(REMOTE_STALE_S, 3·interval_s + ssh_timeout_s):
                       the syncer died (threshold scales to the syncer's own declared pace,
                       so a slow tick or long ssh stall isn't read as death)
      stale-content  — remote file's own age (remote clock) beyond
                       max(REMOTE_CONTENT_STALE_S, 5·remote_write_cadence_s): the remote's
                       provider froze (threshold scales to how often that host actually
                       writes — a 12s-cadence Lustre host gets 60s, not 30)
      ok | degraded | down — the sidecar's own verdict, passed through (a v1 sidecar
                       without `state` maps ok→ok, else down)

    Rows stay rendered for {ok, degraded}; red warnings fire for
    {missing, stale-mirror, stale-content, down}. Pure — no I/O, no clock."""
    if not isinstance(health, dict):
        return "missing"
    if sidecar_age_s is None or sidecar_age_s > max(
            REMOTE_STALE_S, 3 * _num(health.get("interval_s")) + _num(health.get("ssh_timeout_s"))):
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
    """read_verdict for a mirror file's sidecar; None when there is no sidecar at all (then
    the caller falls back to the mirror-mtime rule — e.g. a hand-copied mirror)."""
    hf = jf.with_suffix(HEALTH_SUFFIX)
    health = _read_health(hf)
    if health is None and not hf.exists():
        return None
    try:
        sidecar_age = now - hf.stat().st_mtime
    except OSError:
        sidecar_age = None
    return read_verdict(health, sidecar_age)


def load_remote_sessions(remote_dir=None, stale_s=REMOTE_STALE_S, now=None):
    """Sessions mirrored from other machines: read every run/remote/<host>.json (each is a
    verbatim Session[] snapshot a remote's `ccstatus --serve` wrote, SSH-copied here by
    ccremote.py) and return them as Session objects tagged with `host` = the file stem.

    Rows render only while the host's sidecar verdict is ok or degraded (a degraded blip
    keeps rows — see read_verdict); missing/stale/down mirrors are unknowable and must not
    linger as phantom-live. A mirror with no sidecar at all falls back to the mtime rule
    (older than `stale_s` ⇒ dropped). Fail-open — a missing dir, unreadable/garbled file,
    or bad record yields nothing for that source, never an exception (same contract as
    get_sessions()). The provider never calls this; consumers (ccdash) merge it with
    get_sessions()."""
    remote_dir = Path(remote_dir) if remote_dir is not None else REMOTE_DIR
    now = now if now is not None else time.time()
    out = []
    try:
        files = sorted(remote_dir.glob("*.json"))
    except OSError:
        return out
    for jf in files:
        try:
            verdict = _sidecar_verdict(jf, now)
            if verdict is None:                    # no sidecar — mirror mtime is all we have
                if now - jf.stat().st_mtime > stale_s:
                    continue
            elif verdict not in ("ok", "degraded"):
                continue
            recs = json.loads(jf.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(recs, list):
            continue
        host = jf.stem
        for rec in recs:
            if not isinstance(rec, dict) or not rec.get("session_id"):
                continue
            try:
                s = Session(**{**_SESSION_DEFAULTS,
                               **{k: v for k, v in rec.items() if k in _SESSION_FIELDS}})
            except TypeError:
                continue        # still-unbuildable record ⇒ skip it, keep the rest
            s.host = host
            out.append(s)
    return out


def _remote_hosts():
    """Host names from ccmonitor-remotes (first field per line, '#' comments), sanitized to
    the mirror-file stems ccremote uses. Mirrors ccremote.load_hosts/_host_file — a deliberate
    ~10-line duplication (same idiom as ccbar's stdlib copies) so this consumer library never
    imports the network-touching syncer."""
    stems = []
    try:
        lines = REMOTES_FILE.read_text().splitlines()
    except OSError:
        return stems
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        host = line.split(None, 1)[0]
        stem = "".join(c if (c.isalnum() or c in "._@-") else "_" for c in host)
        if stem not in stems:
            stems.append(stem)
    return stems


def load_remote_health(remote_dir=None, now=None):
    """Per-host sync health for the consumers: {host: {"state", "error", "age_s", "since"}}
    covering exactly the hosts named in ccmonitor-remotes — the intent declaration: a listed
    host is *expected* healthy, and commenting it out is the mute (its leftover sidecar is
    then ignored, so it can't warn forever). Warn-forever while listed: no decay here.

    `state` is read_verdict's vocabulary — ok | degraded | down | missing | stale-mirror |
    stale-content — with `error` carrying ccremote's fetch classification (auth | timeout |
    unreachable | no-file | garbled | write-failed) alongside a degraded/down verdict
    ('auth' = control master gone; rerun ccremote-up.sh). `age_s`: how long things have been
    bad — the failure streak's age (bad_since) for degraded/down, the sidecar's age for
    stale-mirror, the remote content age for stale-content; None when unknowable.
    Fail-open per host, never raises."""
    remote_dir = Path(remote_dir) if remote_dir is not None else REMOTE_DIR
    now = now if now is not None else time.time()
    out = {}
    for host in _remote_hosts():
        hf = remote_dir / f"{host}{HEALTH_SUFFIX}"
        health = _read_health(hf)
        try:
            sidecar_age = now - hf.stat().st_mtime
        except OSError:
            sidecar_age = None
        verdict = read_verdict(health, sidecar_age)
        h = health or {}
        if verdict in ("degraded", "down"):
            bad = h.get("bad_since")
            if isinstance(bad, (int, float)):
                age = now - bad
            else:
                try:            # v1 sidecar: the mirror's mtime is the last successful sync
                    age = now - hf.with_suffix(".json").stat().st_mtime
                except OSError:
                    age = None
        elif verdict == "stale-mirror":
            age = sidecar_age
        else:                                      # ok / stale-content / missing
            age = h.get("remote_status_age_s")
            age = float(age) if isinstance(age, (int, float)) else None
        out[host] = {"state": verdict, "error": h.get("error"),
                     "age_s": age, "since": h.get("synced_at")}
    return out


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
        focused = False
        if pid is not None:
            ppid = _find_pane_pid(str(pid), pane_pids)
            if ppid:
                pane_id, tmux_target, focused = panes[ppid]

        cwd = rec.get("cwd") or ""
        # Age = seconds since the last real turn. We prefer the newest *timestamped* entry in
        # the transcript over the file's st_mtime: Claude Code rewrites the JSONL in place to
        # update metadata (ai-title, mode, file-history-snapshot, …), which bumps st_mtime to
        # "now" with no new turn — that phantom bump otherwise both fakes a fresh age and clears
        # an ack the user set earlier (mtime races past the marker), re-raising a settled "your
        # turn". The last timestamped turn ignores those rewrites; st_mtime is the fallback.
        # (sessions/<pid>.json statusUpdatedAt is unusable — older CLI versions leave it stale.)
        age = model = ctx = None
        engaged = turn_complete = False
        last_ts = None
        jp = _transcript_path(cwd, sid)
        jp_mtime = None
        try:
            jp_mtime = jp.stat().st_mtime
        except OSError:
            pass
        if jp.exists():
            model, ctx, engaged, last_ts, turn_complete = _transcript_usage(jp)
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
            turn_complete=turn_complete,
            focused=focused,
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


def _auto_ack_focused(sessions):
    """POLICY (seen = handled), deliberately in the daemon — the always-on actor, same
    precedent as _write_status_file's acknowledged⇒idle mapping for the notch. A hand-back
    that lands while its pane is focused gets the ordinary ack marker (so switching away
    later doesn't re-raise ◆; auto-clears on the next real turn via _marker_active) and the
    in-memory row is marked BEFORE the snapshots are written, so ccbar never sees a
    one-tick unacked focused hand-back. Blocked (⛔) rows are never touched — handed_back
    requires idle. Skips mirrored rows (their marker lives on their host). Idempotent:
    next tick acknowledged is already True. NOT in get_sessions — --json is a read API
    and must not mutate ack state as a side effect of a read."""
    for s in sessions:
        if s.focused and not s.host and handed_back(s):
            try:
                touch_marker(ACK_DIR, s.session_id)
            except OSError:
                continue        # fail-open: an unwritable marker just means ◆ later
            s.acknowledged = True


def _serve_lock():
    """Exclusive single-writer guard. Two --serve daemons silently race the same
    status.tmp → os.replace (the loser ENOENT-spams its log every tick — observed live
    for 10 days in ccserve.log). Flock run/serve.lock and keep the fd open for the
    process lifetime; a second daemon exits loudly naming the holder. The fd is
    close-on-exec, so the self-heal re-exec releases and immediately re-acquires it."""
    RUN.mkdir(parents=True, exist_ok=True)
    f = open(RUN / "serve.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        owner = f.read().strip() or "?"
        sys.exit(f"ccstatus --serve: pid {owner} already holds {RUN / 'serve.lock'} — "
                 "one snapshot writer only; kill it first or use CCSTATUS_STATUS_FILE/"
                 "CCSTATUS_STATUS_JSON for an isolated run")
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def _log_transitions(prev, sessions):
    """Transitions-only forensic log (ccremote's idiom — never per-tick): one stderr line
    when a session's (state, turn_complete, awaiting) triple changes, plus a line when a
    session vanishes. This is the trail that answers "why didn't the ◆ fire" after the
    fact. Returns the new {sid: triple} map; first sighting of a sid is silent."""
    now = time.strftime("%H:%M:%S")
    cur = {}
    for s in sessions:
        new = (s.state, s.turn_complete, awaiting(s))
        old = prev.get(s.session_id)
        if old is not None and old != new:
            print(f"{now} {s.short_id} {s.title}: {old[0]}→{new[0]} "
                  f"turn_complete={new[1]} awaiting={new[2]}", file=sys.stderr)
        cur[s.session_id] = new
    for sid, old in prev.items():
        if sid not in cur:
            print(f"{now} {sid[:8]}: {old[0]}→gone", file=sys.stderr)
    return cur


def _serve(interval):
    lock = _serve_lock()   # noqa: F841 — the open fd IS the single-writer guard
    print(f"ccstatus --serve: writing {STATUS_FILE} + {STATUS_JSON} every {interval}s",
          file=sys.stderr)
    prev = {}
    while True:
        _restart_if_source_changed()
        try:
            sessions = get_sessions()
            _auto_ack_focused(sessions)
            prev = _log_transitions(prev, sessions)   # after auto-ack: log what consumers see
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
