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

Beside each mirror it writes a `<host>.health` sidecar every tick (success or failure):
`{synced_at, ok, error, consecutive_failures, remote_status_age_s, sessions}`. The age is
measured with a sentinel header in the same ssh exec — `date +%s` and the file's mtime both
on the *remote's* clock, so it's skew-free — which catches the phantom-fresh failure where
the remote's `ccstatus --serve` died but `ssh cat` keeps happily re-mirroring the frozen
file. `error` classifies the failure ('auth' = control master gone, rerun ccremote-up.sh;
'unreachable'; 'no-file'; 'garbled') so the consumers can warn loudly instead of rows just
silently vanishing.

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
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
RUN = HOME / ".claude" / "run"
REMOTE_DIR = RUN / "remote"
REMOTES_FILE = Path(os.environ.get("CCMONITOR_REMOTES", str(RUN / "ccmonitor-remotes")))
# Default path of the snapshot on the *remote* host (ssh expands ~). Override globally with
# $CCMONITOR_REMOTE_STATUS, or per-host with a second whitespace-separated field in the hosts
# file — for a remote whose $HOME is small/quota'd and writes its snapshot elsewhere (that host
# must run `CCSTATUS_STATUS_JSON=<path> ccstatus.py --serve` so it writes to the same place).
REMOTE_STATUS = os.environ.get("CCMONITOR_REMOTE_STATUS", "~/.claude/run/status.json")
REMOTE_ACK = "~/.claude/run/ack"               # ack dir on the remote (fixed; not the relocatable
                                               # status.json path) — ssh expands ~
_SID_OK = re.compile(r"[A-Za-z0-9._-]+")       # a session id is a UUID-ish token; reject anything
                                               # else before it reaches a remote shell command
SSH_TIMEOUT = 8                                # subprocess wall-clock ceiling per host

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


def _fetch(host, remote_status=REMOTE_STATUS):
    """Return (payload, remote_age_s, error): the remote status.json text, the file's age
    measured entirely on the *remote's* clock (skew-free; None if unmeasurable), and a short
    error class ('auth' | 'unreachable' | 'no-file') when the payload is None. One ssh
    round-trip serves both — a sentinel header line, then the file. The path stays unquoted
    (as the plain `cat` was) so `~` expands; hosts-file paths are whitespace-free by format.
    `stat -f %m` is the BSD/mac fallback; an unparseable header just yields age None."""
    cmd = (f'echo "{_AGE_HDR}$(date +%s) '
           f'$(stat -c %Y {remote_status} 2>/dev/null || stat -f %m {remote_status} 2>/dev/null)"; '
           f'cat {remote_status}')
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, host, cmd],
                           capture_output=True, text=True, timeout=SSH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None, None, "unreachable"
    if r.returncode == 255:                     # ssh itself failed — never reached the command
        # stderr is the primary evidence (BatchMode on a password host says "Permission
        # denied"); the master probe breaks the tie for anything ambiguous.
        err = (r.stderr or "").lower()
        if any(p in err for p in ("permission denied", "authentication",
                                  "host key verification")):
            return None, None, "auth"
        if any(p in err for p in ("could not resolve", "timed out", "connection refused",
                                  "no route", "network is unreachable")):
            return None, None, "unreachable"
        return None, None, ("auth" if _master_gone(host) else "unreachable")
    if r.returncode != 0:                       # remote shell ran but `cat` failed
        return None, None, "no-file"
    out, age = r.stdout, None
    if out.startswith(_AGE_HDR):
        head, _, out = out.partition("\n")
        parts = head[len(_AGE_HDR):].split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            age = float(max(0, int(parts[0]) - int(parts[1])))
    return out, age, None


def _health_file(host):
    return _host_file(host).with_suffix(HEALTH_SUFFIX)


def _write_health(host, ok, error, age, nsessions):
    """Atomically write the host's `.health` sidecar (every tick, success or failure) and
    return the dict. `consecutive_failures` carries over from the previous sidecar (read
    fail-open) so consumers and the transition log can tell a blip from an outage."""
    hf = _health_file(host)
    prev_fail = 0
    if not ok:
        try:
            prev_fail = int(json.loads(hf.read_text()).get("consecutive_failures", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    health = {"synced_at": time.time(), "ok": ok, "error": error,
              "consecutive_failures": 0 if ok else prev_fail + 1,
              "remote_status_age_s": age, "sessions": nsessions}
    try:
        REMOTE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = hf.with_suffix(hf.suffix + ".tmp")
        tmp.write_text(json.dumps(health))
        os.replace(tmp, hf)
    except OSError:
        pass                                    # health is advisory — never fatal
    return health


def sync_host(host, remote_status=REMOTE_STATUS):
    """Fetch one host, atomically write its mirror, and always write its `.health` sidecar.
    Returns the health dict. On failure the previous *mirror* is left in place (its mtime
    ages out on its own) — only the sidecar carries the bad news. The mirror is never gated
    on the remote file's age either: ccremote reports freshness, consumers decide what's too
    stale (mechanism here, policy there)."""
    out, age, err = _fetch(host, remote_status)
    recs = None
    if err is None and out is not None:
        try:
            recs = json.loads(out)
        except ValueError:
            recs = None
        if not isinstance(recs, list):
            recs, err = None, "garbled"
    if recs is None:
        return _write_health(host, False, err or "garbled", age, None)
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    dst = _host_file(host)
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(recs))
    os.replace(tmp, dst)
    return _write_health(host, True, None, age, len(recs))


def sync_once(hosts):
    """Sync every host once; return {host: health-dict}. `hosts` is load_hosts()'s
    [(host, remote_path), …]."""
    return {host: sync_host(host, remote) for host, remote in hosts}


def remote_ack(host, sid, on=True):
    """Set (on=True) or clear (on=False) the responded-to marker for a session on a *remote* host,
    over SSH, reusing the shared master (no re-auth). ccdash calls this instead of touching the
    local ack dir for a mirrored row, so the remote's own ccstatus recomputes `acknowledged` and it
    flows back through the next mirror. Returns True on success, False fail-open (bad sid / ssh
    error). `sid` is validated to a UUID-ish token before it reaches the remote shell."""
    if not _SID_OK.fullmatch(sid or ""):
        return False
    cmd = (f"mkdir -p {REMOTE_ACK} && touch {REMOTE_ACK}/{sid}" if on
           else f"rm -f {REMOTE_ACK}/{sid}")
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, host, cmd],
                           capture_output=True, text=True, timeout=SSH_TIMEOUT)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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
    prev = {}                           # host → (ok, error): log state *transitions*, not ticks
    while True:
        _restart_if_source_changed()
        try:
            # hosts re-read each tick ⇒ edits take effect live
            for host, health in sync_once(load_hosts()).items():
                now, was = (health["ok"], health["error"]), prev.get(host)
                if now == was:
                    continue
                prev[host] = now
                if health["ok"]:
                    if was is not None:              # first healthy tick isn't news
                        print(f"ccremote serve: {host}: recovered "
                              f"({health['sessions']} session(s))", file=sys.stderr)
                else:
                    hint = " — rerun ccremote-up.sh" if health["error"] == "auth" else ""
                    print(f"ccremote serve: {host}: {health['error']}{hint}", file=sys.stderr)
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
            print(f"{host}\tFAIL {health['error']}{hint} (x{health['consecutive_failures']})")


if __name__ == "__main__":
    main()
