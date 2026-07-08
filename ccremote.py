#!/usr/bin/env python3
"""ccremote — SSH syncer: mirror remote hosts' Claude-session status onto this machine.

`claude agents --json` (and therefore `ccstatus.get_sessions()`) is strictly machine-local,
so a session running on another box is invisible to this machine's ccdash/ccbar. This daemon
closes that gap the same way the claude-island Mac app does (see
`claude-island/remote-ssh-stages.md`): it *pulls a file over SSH*. For each configured host it
runs `ssh <host> cat ~/.claude/run/status.json` — the full-fidelity `Session[]` snapshot that
the remote's own `ccstatus.py --serve` already writes — and drops it verbatim at
`~/.claude/run/remote/<host>.json`. The consumers fold those mirrors in via
`ccstatus.load_remote_sessions()`; each mirror's mtime is the liveness clock, so a host or
syncer that stops updating goes stale and its rows are dropped (no schema translation needed —
the snapshot is already the portable `Session` contract).

Beside each mirror it writes a `<host>.health` sidecar every tick (success or failure) — the
v2 schema carries a *verdict* computed here, once, so the consumers read state instead of
re-deriving it:

  {"v": 2, "synced_at": …, "state": "ok|degraded|down", "ok": …, "error": …,
   "consecutive_failures": …, "bad_since": …, "interval_s": …, "ssh_timeout_s": …,
   "remote_status_age_s": …, "remote_mtimes": […], "remote_write_cadence_s": …,
   "sessions": …}

`state` applies the hysteresis ladder (one slow tick = "degraded", quiet-ish; confirmed
outage = "down", loud) so a single 10s ssh stall can't paint the bar red. The age is
measured with a sentinel header in the same ssh exec — `date +%s` and the file's mtime both
on the *remote's* clock, so it's skew-free — which catches the phantom-fresh failure where
the remote's `ccstatus --serve` died but `ssh cat` keeps happily re-mirroring the frozen
file. The ring of distinct remote mtimes yields `remote_write_cadence_s` (median write
interval), letting consumers scale the frozen-content threshold to how fast that host
actually writes. `error` classifies the failure ('auth' = control master gone, rerun
ccremote-up.sh; 'timeout'; 'unreachable'; 'no-file'; 'garbled'; 'write-failed') so the
consumers can warn loudly instead of rows just silently vanishing.

Config (the headless interface): `~/.claude/run/ccmonitor-remotes` (override `$CCMONITOR_REMOTES`),
one `host` or `user@host` per line, `#` starts a comment, blank lines skipped. Missing file ⇒
no remotes. Mirrors the `ccmonitor-ignore` file idiom. A line may add a second whitespace-
separated field — the snapshot path on that host — for a remote that writes status.json outside
`~/.claude/run/` (small/quota'd $HOME); it defaults to `$CCMONITOR_REMOTE_STATUS` or
`~/.claude/run/status.json`. That host must serve to the matching path, e.g.
`CCSTATUS_STATUS_JSON=/scratch/me/status.json python3 ccstatus.py --serve`.

  # ~/.claude/run/ccmonitor-remotes
  psc-login                                   # default ~/.claude/run/status.json
  me@psc  /ocean/projects/xxx/me/run/status.json   # host writes its snapshot on scratch

CLI:
  ccremote                one-shot: sync every host once, print what was written  (debug)
  ccremote --serve[=SECS]  daemon: re-sync every SECS (default 3.0)

Stdlib-only, atomic writes (tmp + os.replace), fail-open per host — an unreachable host or a
garbled response leaves that host's previous mirror untouched and never crashes the loop.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home()
RUN = HOME / ".claude" / "run"
# Local mirror dir (override $CCMONITOR_REMOTE_DIR — lets a worktree/test run a fully
# isolated syncer + consumers without touching the live daemon's files).
REMOTE_DIR = Path(os.environ.get("CCMONITOR_REMOTE_DIR", str(RUN / "remote")))
REMOTES_FILE = Path(os.environ.get("CCMONITOR_REMOTES", str(RUN / "ccmonitor-remotes")))
# Default path of the snapshot on the *remote* host (ssh expands ~). Override globally with
# $CCMONITOR_REMOTE_STATUS, or per-host with a second whitespace-separated field in the hosts
# file — for a remote whose $HOME is small/quota'd and writes its snapshot elsewhere (that host
# must run `CCSTATUS_STATUS_JSON=<path> ccstatus.py --serve` so it writes to the same place).
REMOTE_STATUS = os.environ.get("CCMONITOR_REMOTE_STATUS", "~/.claude/run/status.json")
REMOTE_ACK = "~/.claude/run/ack"               # marker dirs on the remote (fixed; not the
REMOTE_DISMISS = "~/.claude/run/dismissed"     # relocatable status.json path) — ssh expands ~
_SID_OK = re.compile(r"[A-Za-z0-9._-]+")       # a session id is a UUID-ish token; reject anything
                                               # else before it reaches a remote shell command
SSH_TIMEOUT = 20               # subprocess wall-clock ceiling per host. Healthy-psc execs were
                               # measured completing after 12.8s during a slow episode (busy login
                               # node) — the old 8s read every such stall as a failure. 20 turns
                               # slow ticks into slow successes; it only needs to bound a genuinely
                               # wedged exec, since sync_once runs hosts in parallel (a slow host
                               # never delays the others) and TCP-level failures still die at
                               # ConnectTimeout=5.

# Hysteresis ladder constants — one slow/failed tick is a blip, not an outage. Consumers only
# go red on state=="down"; "degraded" keeps rows rendered and stays quiet(ish).
DOWN_AFTER_FAILS = 3           # 3 consecutive failed ticks confirm an outage …
DOWN_AFTER_BAD_S = 25.0        # … or a failure streak this old, whichever comes first
AUTH_DOWN_AFTER_FAILS = 2      # auth can't self-heal (password host, dead master) — confirm
                               # once to rule out a transient misread, then get loud fast
CADENCE_RING = 8               # distinct remote mtimes kept for the write-cadence estimate

# ControlMaster/ControlPersist reuse one TCP+auth connection across polls, so a 3s cadence is
# cheap (no fresh handshake each tick). The control socket lives under the remote dir. The
# persist window is the same env var ccremote-up.sh honors, so a master ccremote auto-creates
# (key-auth host) lives exactly as long as one ccremote-up.sh opened interactively.
_CTL = REMOTE_DIR / ".ssh-%r@%h:%p"
CONTROL_PERSIST = os.environ.get("CCREMOTE_CONTROL_PERSIST", "12h")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ControlMaster=auto", "-o", f"ControlPersist={CONTROL_PERSIST}",
            "-o", f"ControlPath={_CTL}"]

HEALTH_SUFFIX = ".health"      # per-host sidecar beside the mirror — deliberately NOT .json,
                               # so the consumers' run/remote/*.json mirror globs never see it
# The fetch prepends this sentinel header (`#cc# <now> <mtime>`, both from the *remote's*
# clock) so content freshness is measured end-to-end with zero cross-host skew — a frozen
# remote provider stays visible even though `ssh cat` keeps succeeding and the local mirror
# mtime stays fresh.
_AGE_HDR = "#cc# "

# stderr evidence for classify_ssh_failure. Substring matches against ssh's own messages —
# keep them specific: remote login noise (psc's .bashrc prints to stderr) rides along in the
# same stream, so a loose pattern would misclassify a healthy fetch.
_AUTH_PATTERNS = ("permission denied", "authentication", "host key verification")
_UNREACHABLE_PATTERNS = ("could not resolve", "timed out", "connection refused",
                         "no route", "network is unreachable")

# --serve self-heal (same idiom as ccstatus.py): a long-lived daemon never reloads its own
# source, so capture the mtime at import and re-exec in place if the file changes on disk.
_SELF_PATH = Path(__file__).resolve()
_SELF_MTIME = _SELF_PATH.stat().st_mtime if _SELF_PATH.exists() else None


def load_hosts(path=None):
    """Parse the config file into [(host, remote_status_path), …]. One entry per line:
    `host` or `user@host`, optionally followed by whitespace + the remote snapshot path (for a
    host that writes status.json outside the default `~/.claude/run/`); the path defaults to
    REMOTE_STATUS. '#' starts a comment, blank lines skipped. Fail-open — a missing/unreadable
    file yields []. Order + de-dup preserved (first spelling of a host wins)."""
    path = Path(path) if path is not None else REMOTES_FILE
    hosts, seen = [], set()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return hosts
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)             # host [remote_path]
        host = parts[0]
        remote = parts[1].strip() if len(parts) > 1 else REMOTE_STATUS
        if host not in seen:
            seen.add(host)
            hosts.append((host, remote))
    return hosts


def _host_file(host):
    """Local mirror path for a host. Sanitize so `user@host` / IPs are safe filenames; the
    stem is what load_remote_sessions() surfaces as Session.host."""
    safe = "".join(c if (c.isalnum() or c in "._@-") else "_" for c in host)
    return REMOTE_DIR / f"{safe}.json"


def _health_file(host):
    return _host_file(host).with_suffix(HEALTH_SUFFIX)


def _master_gone(host):
    """True if the shared ControlMaster for `host` is down. BatchMode can't answer a password
    prompt, so a dead master on a password-auth host means every fetch fails until the user
    re-runs ccremote-up.sh — the one failure worth naming distinctly."""
    try:
        r = subprocess.run(["ssh", "-O", "check", "-o", f"ControlPath={_CTL}", host],
                           capture_output=True, text=True, timeout=SSH_TIMEOUT)
        return r.returncode != 0
    except (OSError, subprocess.SubprocessError):
        return True


def build_fetch_cmd(remote_status):
    """The one remote shell line: sentinel header (`#cc# <now> <mtime>`, both remote-clock),
    then the file. The path stays unquoted (as a plain `cat` would be) so `~` expands;
    hosts-file paths are whitespace-free by format. `stat -f %m` is the BSD/mac fallback."""
    return (f'echo "{_AGE_HDR}$(date +%s) '
            f'$(stat -c %Y {remote_status} 2>/dev/null || stat -f %m {remote_status} 2>/dev/null)"; '
            f'cat {remote_status}')


def parse_sentinel(stdout):
    """Split a fetch's stdout into (payload, age_s, remote_mtime). The header line is
    `#cc# <now> <mtime>` with both stamps from the remote's clock; a missing or unparseable
    header fails open to (payload, None, None), and a negative age (remote clock jitter or a
    write racing the stat) clamps to 0."""
    if not stdout.startswith(_AGE_HDR):
        return stdout, None, None
    head, _, payload = stdout.partition("\n")
    parts = head[len(_AGE_HDR):].split()
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        now_r, mtime = int(parts[0]), int(parts[1])
        return payload, float(max(0, now_r - mtime)), mtime
    return payload, None, None


def classify_ssh_failure(rc, stderr, master_gone):
    """Error class for a *finished* ssh exec: None (success) | 'auth' | 'unreachable' |
    'no-file'. rc 255 means ssh itself failed before running the command: stderr patterns are
    the primary evidence (BatchMode on a password host says "Permission denied"); on
    ambiguity, `master_gone()` — a callable, because the probe costs a subprocess — breaks
    the tie, since a dead master on a password host is the one failure that needs a human.
    Any other non-zero rc means the remote shell ran but `cat` failed → 'no-file'."""
    if rc == 0:
        return None
    if rc != 255:
        return "no-file"
    err = (stderr or "").lower()
    if any(p in err for p in _AUTH_PATTERNS):
        return "auth"
    if any(p in err for p in _UNREACHABLE_PATTERNS):
        return "unreachable"
    return "auth" if master_gone() else "unreachable"


def parse_snapshot(text):
    """The mirrored Session[] list from a payload, or None for anything else (None input,
    non-JSON, non-list) — the caller maps None to 'garbled'."""
    if text is None:
        return None
    try:
        recs = json.loads(text)
    except ValueError:
        return None
    return recs if isinstance(recs, list) else None


def atomic_write_json(path, obj):
    """tmp + os.replace under the target's own name (the `.tmp` twin never matches the
    consumers' `*.json` globs). True on success, False fail-open — a full disk or bad
    permissions must never crash the sync loop."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj))
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def read_prev_health(host):
    """The host's previous sidecar dict, {} fail-open — feeds make_health's streak/ring."""
    try:
        prev = json.loads(_health_file(host).read_text())
        return prev if isinstance(prev, dict) else {}
    except (OSError, ValueError):
        return {}


def make_health(prev, *, ok, error, age_s, remote_mtime, nsessions, now, interval_s):
    """Build the sidecar-v2 dict for one tick. Pure — no I/O, no clock (`now` is an argument)
    — so the hysteresis ladder is unit-testable:

      success        → state "ok", streak reset, bad_since cleared.
      failure        → streak = prev+1, bad_since sticks to the streak's first tick; then
                       "down" once the streak hits DOWN_AFTER_FAILS ticks or DOWN_AFTER_BAD_S
                       seconds (AUTH_DOWN_AFTER_FAILS for 'auth' — it can't self-heal),
                       else "degraded".

    The ring of distinct remote-clock mtimes lives in the sidecar itself (not daemon memory),
    so it survives restarts and one-shots. `remote_write_cadence_s` (median delta between
    ring entries) is the *observed refresh granularity* — the true remote write cadence when
    fetches keep up, inflated toward the fetch interval when they don't. Either way it's the
    right scale for the consumers' frozen-content threshold: an alarm can't meaningfully be
    tighter than how often we actually observe the file advance."""
    prev = prev if isinstance(prev, dict) else {}
    if ok:
        fails, bad_since, state = 0, None, "ok"
    else:
        try:
            fails = int(prev.get("consecutive_failures") or 0) + 1
        except (TypeError, ValueError):
            fails = 1
        prev_bad = prev.get("bad_since")
        bad_since = prev_bad if isinstance(prev_bad, (int, float)) else now
        down_fails = AUTH_DOWN_AFTER_FAILS if error == "auth" else DOWN_AFTER_FAILS
        if fails >= down_fails or now - bad_since >= DOWN_AFTER_BAD_S:
            state = "down"
        else:
            state = "degraded"
    ring = prev.get("remote_mtimes")
    ring = [m for m in ring if isinstance(m, (int, float))] if isinstance(ring, list) else []
    if isinstance(remote_mtime, (int, float)) and (not ring or remote_mtime != ring[-1]):
        ring = (ring + [remote_mtime])[-CADENCE_RING:]
    deltas = [b - a for a, b in zip(ring, ring[1:]) if b > a]
    cadence = float(statistics.median(deltas)) if deltas else None
    return {"v": 2, "synced_at": now, "state": state, "ok": ok, "error": error,
            "consecutive_failures": fails, "bad_since": bad_since,
            "interval_s": interval_s, "ssh_timeout_s": SSH_TIMEOUT,
            "remote_status_age_s": age_s, "remote_mtimes": ring,
            "remote_write_cadence_s": cadence, "sessions": nsessions}


def _fetch(host, remote_status=REMOTE_STATUS):
    """One ssh round-trip → (payload, remote_age_s, remote_mtime, error). Thin sequence over
    the primitives: build the command, run it, classify a failure, parse the sentinel. A
    TimeoutExpired is its own class — a stalled exec on a healthy host must not read as the
    host being unreachable (that misclassification was the flapping-⚠ bug)."""
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, host, build_fetch_cmd(remote_status)],
                           capture_output=True, text=True, timeout=SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, None, None, "timeout"
    except (OSError, subprocess.SubprocessError):
        return None, None, None, "unreachable"
    err = classify_ssh_failure(r.returncode, r.stderr, lambda: _master_gone(host))
    if err is not None:
        return None, None, None, err
    payload, age, mtime = parse_sentinel(r.stdout)
    return payload, age, mtime, None


def sync_host(host, remote_status=REMOTE_STATUS, interval_s=None, now=None):
    """Fetch one host, atomically write its mirror, and always write its `.health` sidecar.
    Returns the health dict. On failure the previous *mirror* is left in place (its mtime
    ages out on its own) — only the sidecar carries the bad news. The mirror is never gated
    on the remote file's age either: ccremote reports freshness, consumers decide what's too
    stale (mechanism here, policy there)."""
    now = time.time() if now is None else now
    payload, age, mtime, err = _fetch(host, remote_status)
    recs = parse_snapshot(payload) if err is None else None
    if err is None and recs is None:
        err = "garbled"
    if recs is not None and not atomic_write_json(_host_file(host), recs):
        recs, err = None, "write-failed"       # local disk trouble — still a failed tick
    health = make_health(read_prev_health(host), ok=recs is not None, error=err,
                         age_s=age, remote_mtime=mtime,
                         nsessions=len(recs) if recs is not None else None,
                         now=now, interval_s=interval_s)
    atomic_write_json(_health_file(host), health)   # advisory — never fatal
    return health


def sync_once(hosts, interval_s=None):
    """Sync every host once, in parallel (per-host ControlPath sockets — no contention), so
    one slow host can't stretch the whole tick past the stale-mirror window for the others.
    Returns {host: health-dict} in hosts-file order. `hosts` is load_hosts()'s
    [(host, remote_path), …]."""
    if not hosts:
        return {}

    def _one(host, remote):
        try:
            return sync_host(host, remote, interval_s=interval_s)
        except Exception:                       # fail-open per host — never kill the tick
            return make_health(read_prev_health(host), ok=False, error="garbled",
                               age_s=None, remote_mtime=None, nsessions=None,
                               now=time.time(), interval_s=interval_s)

    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
        futures = {host: ex.submit(_one, host, remote) for host, remote in hosts}
    return {host: fut.result() for host, fut in futures.items()}


def build_mark_cmd(dir_, sid, on):
    """The remote shell command that sets/clears a touch-file marker `<dir_>/<sid>`. Pure —
    the caller has already validated `sid` against _SID_OK."""
    return (f"mkdir -p {dir_} && touch {dir_}/{sid}" if on
            else f"rm -f {dir_}/{sid}")


def _remote_mark(host, sid, dir_, on):
    """Set (on=True) or clear (on=False) a touch-file marker for a session on a *remote* host,
    over SSH, reusing the shared master (no re-auth). ccdash calls this instead of touching the
    local marker dir for a mirrored row, so the remote's own ccstatus recomputes the flag and it
    flows back through the next mirror. Returns True on success, False fail-open (bad sid / ssh
    error). `sid` is validated to a UUID-ish token before it reaches the remote shell."""
    if not _SID_OK.fullmatch(sid or ""):
        return False
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, host, build_mark_cmd(dir_, sid, on)],
                           capture_output=True, text=True, timeout=SSH_TIMEOUT)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def remote_ack(host, sid, on=True):
    """Set/clear the responded-to marker for a session at its source host."""
    return _remote_mark(host, sid, REMOTE_ACK, on)


def remote_dismiss(host, sid, on=True):
    """Set/clear the dismissed (hidden-row) marker for a session at its source host."""
    return _remote_mark(host, sid, REMOTE_DISMISS, on)


def _health_key(health):
    """What counts as 'the same situation' for transition logging: state + error class."""
    return (health.get("state"), health.get("error"))


def _transition_msg(host, prev_key, health):
    """The --serve stderr line for this tick, or None when nothing changed. Keyed on
    (state, error) so degraded→down and an error-class change both log while steady states
    stay silent; the auth remedy text lives here (single source for the serve log)."""
    if _health_key(health) == prev_key:
        return None
    if health.get("state") == "ok":
        if prev_key is None:
            return None                          # first healthy tick isn't news
        return f"ccremote serve: {host}: recovered ({health.get('sessions')} session(s))"
    hint = " — rerun ccremote-up.sh" if health.get("error") == "auth" else ""
    return f"ccremote serve: {host}: {health.get('state')} ({health.get('error')}){hint}"


def _restart_if_source_changed():
    """Re-exec in place if ccremote.py's own source changed since load (mirrors ccstatus.py)."""
    if _SELF_MTIME is None:
        return
    try:
        current = _SELF_PATH.stat().st_mtime
    except OSError:
        return
    if current != _SELF_MTIME:
        print(f"ccremote --serve: source changed ({_SELF_PATH}), restarting in place",
              file=sys.stderr)
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _serve(interval):
    print(f"ccremote --serve: mirroring {REMOTES_FILE} → {REMOTE_DIR}/<host>.json every "
          f"{interval}s", file=sys.stderr)
    prev = {}                           # host → (state, error): log transitions, not ticks
    while True:
        _restart_if_source_changed()
        try:
            # hosts re-read each tick ⇒ edits take effect live
            for host, health in sync_once(load_hosts(), interval_s=interval).items():
                msg = _transition_msg(host, prev.get(host), health)
                prev[host] = _health_key(health)
                if msg:
                    print(msg, file=sys.stderr)
        except Exception as e:          # never let the daemon die on a transient error
            print(f"ccremote serve: {e}", file=sys.stderr)
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="Mirror remote Claude-session status over SSH.")
    ap.add_argument("--serve", nargs="?", const=3.0, type=float, default=None,
                    metavar="SECS", help="daemon: re-sync every SECS (default 3.0)")
    args = ap.parse_args()

    if args.serve is not None:
        _serve(args.serve)
        return

    hosts = load_hosts()
    if not hosts:
        print(f"(no remote hosts configured — add one per line to {REMOTES_FILE})",
              file=sys.stderr)
        return
    for host, health in sync_once(hosts).items():
        if health["ok"]:
            age = health["remote_status_age_s"]
            age_txt = f", content {age:.0f}s old" if age is not None else ""
            print(f"{host}\t{health['sessions']} session(s){age_txt} → {_host_file(host)}")
        else:
            hint = " — rerun ccremote-up.sh" if health["error"] == "auth" else ""
            print(f"{host}\t{health['state'].upper()} {health['error']}{hint} "
                  f"(x{health['consecutive_failures']})")


if __name__ == "__main__":
    main()
