# Self-heal the `ccstatus --serve` daemon against stale source

**Status: RESOLVED 2026-06-30** (branch `ccdash`). Code change + live daemon restart.

## Context

ccdash showed the "Persistent splits mechanism validation" row (`dca61951-…`,
`~/repo/refseg-workspace`) as acknowledged (✓), but the tmux statusline (`ccbar.py`)
still surfaced it under the "your turn" `◆` alert — the statusline looked divorced
from the dashboard state.

Root cause (verified live, not just from docs): it was **not** a logic bug. The
`ccstatus.py --serve 2.0` daemon (PID `4076460`) had started at `11:36:36` today.
Commit `08933ed` ("ccstatus: derive age + ack-clear from last timestamped turn, not
file mtime" — see [[fix-phantom-mtime-reflag]]) landed at `13:46:01`, **over two hours
after the daemon started**. Python never reloads source for an already-running
interpreter, so that daemon kept executing the pre-fix bytecode and writing stale
`age_s`/`acknowledged` values into `~/.claude/run/status.json` indefinitely.

- `ccbar.py` only reads that cached snapshot (by design — stdlib-only, no provider
  import, re-spawned every tmux tick but never re-imports `ccstatus`). It inherited
  the daemon's stale view.
- `ccdash.py` is launched fresh per `display-popup` and calls `get_sessions()` live in
  a brand-new process, so it always sees current code — hence the divergence.
- Confirmed directly: a fresh `ccstatus.py --json` call returned `acknowledged: true`,
  `age_s: ~64000` (matching ccdash's ✓ and the real ~17.6h-old last timestamped
  transcript turn), while the live `status.json` on disk (written by the stale daemon)
  showed `acknowledged: false`, `age_s: 378` (raw-mtime-based — the exact phantom
  in-place JSONL metadata rewrite [[fix-phantom-mtime-reflag]] already diagnosed once).

This is the **second** time this exact failure mode has hit this repo —
[[faq-stale-serve-daemon-engaged]] documents an earlier incident (Jun 29) where the
same long-lived daemon outlived the `engaged` field landing, silently killing the
ccbar `◆` tier entirely. That postmortem's "possible hardening (not done)" section
flagged the daemon-staleness gap but didn't fix it. This time it gets fixed instead of
just diagnosed: `ccstatus.py` is the most actively-edited file in the repo, so a daemon
that never notices its own source changed will keep causing this class of bug.

## Approach

`ccstatus.py` now self-heals: it detects its own source file changed on disk and
re-execs in place, the same idiom `SSHTunnelManager`'s "stale tunnel cleanup" already
uses elsewhere in this codebase, and the same primitive `claude_status.py` already
uses (`os.execv` hand-off) for its own back-compat shim.

1. **Module-level baseline**, captured at import time (`ccstatus.py:42-46`, near the
   other path constants):
   ```python
   _SELF_PATH = Path(__file__).resolve()
   _SELF_MTIME = _SELF_PATH.stat().st_mtime if _SELF_PATH.exists() else None
   ```

2. **New helper** next to `_serve()`:
   ```python
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
   ```
   `[sys.executable] + sys.argv` preserves the original argv exactly (`--serve 2.0`
   survives as `--serve 2.0`, not the default interval).

3. **Call site in `_serve()`**, once per loop tick, before the existing try block:
   ```python
   while True:
       _restart_if_source_changed()
       try:
           ...
   ```

Design decisions: check every tick (a single `stat()` is negligible next to
`get_sessions()`'s own work); `os.execv` not `sys.exit()` (confirmed no supervisor
exists for this daemon anywhere — `setup.sh` only installs hooks — so exiting would
just kill it); only `ccstatus.py`'s own mtime matters (it has zero local sibling
imports; `ccbar.py`/`ccdash.py` are already re-spawned fresh per invocation so they
have no analogous staleness problem).

**Caveat surfaced during rollout:** this only protects *future* edits. A daemon
already running pre-fix code doesn't have `_restart_if_source_changed` loaded in
memory at all, so it can't self-heal into existence — it needs one manual bounce to
bootstrap the new behavior. That bounce was done live as part of this fix (see
Verification #3).

## Verification

1. Isolated test daemon: started `ccstatus.py --serve 2.0` standalone, `touch`ed
   `ccstatus.py`, confirmed within one tick (~2s) it logged
   `source changed (...), restarting in place` and resumed writing — **same PID**, argv
   still `--serve 2.0` (confirmed via `/proc/<pid>/cmdline` before/after).
2. `ast.parse` syntax check on the edited file.
3. Live production fix: killed the actual stale daemon (PID `4076460`, up since
   `11:36:36`, pre-dating both `08933ed` and this fix) and relaunched
   `nohup python3 ccstatus.py --serve 2.0 >> ~/.claude/run/ccserve.log 2>&1 & disown`
   from the same cwd/log target it was already using. Confirmed post-restart
   `run/status.json` flipped to `acknowledged: true` for `dca61951-…`, now matching
   ccdash. The new daemon carries the self-heal code, so future edits to `ccstatus.py`
   no longer require a manual restart.

## Files

- `ccstatus.py` — `_SELF_PATH`/`_SELF_MTIME` baseline (near the path constants),
  `_restart_if_source_changed()` helper, one call site in `_serve()`.
- `claude_status.py` — referenced only as the existing `os.execv` style precedent; not
  modified.
