# Remote-sync hysteresis + ghost sessions: kill the flickering ⚠psc and the orange ghosts

Status: **implemented** on branch `remote-sync-hysteresis` (worktree), pending merge back to
`ccdash`. Approved plan copy: `~/.claude/plans/read-the-latest-plan-glittery-locket.md`.
Remaining live items at the bottom.

## Why

Two flakiness classes in the remote-monitoring chain (psc `ccstatus --serve` → `ccremote.py`
SSH mirror → ccbar/ccdash):

1. **Flickering red `⚠psc` while psc was fine.** Live sampling caught single-tick
   `{"ok": false, "error": "unreachable", "consecutive_failures": 1}` sidecar blips while a
   parallel ssh over the same master streamed fine. Root cause chain: occasional ssh execs
   stall past the old `SSH_TIMEOUT = 8` (a *successful* `ssh psc echo` was measured at
   **12.8s** during a busy-login-node episode) → `TimeoutExpired` → swallowed by a broad
   `except` → misclassified `"unreachable"` → and **no consumer debounced**, so one bad tick
   went red immediately. Failed ticks also stretched the sidecar gap to ~11s, brushing the
   15s stale-mirror threshold — a second false-positive class.
2. **Orange ghost rows for closed psc sessions.** `_alive()` was a bare `/proc/<pid>`
   existence check. bridges2 has multiple round-robin login nodes sharing `$HOME` on /jet
   Lustre: `claude agents --json` reads shared state but `/proc` is per-node, so a closed
   session's pid — recycled by any unrelated process — kept its last state (`blocked`/`busy`)
   frozen forever: a permanent phantom "needs you". Plus a UX flake: the remote-ack round
   trip (~5–10s) left the row orange after `a`.

A reusable-parts review (3 independent agents) also converged on **policy triplication** as
the dominant defect: hosts grammar ×3, stem sanitizer ×3, freshness gate ×2, and a
*divergent* health-verdict ladder ×2 (ccstatus cascade vs ccbar squashed boolean).

## Design

### Verdict as data (sidecar v2)

`ccremote.make_health` — pure, unit-tested — computes the verdict **once, writer-side** and
writes it into the `.health` sidecar; consumers read state instead of re-deriving policy:

```json
{"v": 2, "synced_at": …, "state": "ok|degraded|down", "ok": …, "error": …,
 "consecutive_failures": …, "bad_since": …, "interval_s": 3.0, "ssh_timeout_s": 20,
 "remote_status_age_s": …, "remote_mtimes": [≤8], "remote_write_cadence_s": …, "sessions": …}
```

Hysteresis ladder: failure → `degraded` (quiet-ish); `down` only at 3 consecutive fails, a
≥25s failure streak, or 2 fails for `auth` (can't self-heal — password host). Success resets.
`error` classes: auth | timeout | unreachable | no-file | garbled | write-failed
(`timeout` is new — a stalled exec on a healthy host must not read as unreachable).
`SSH_TIMEOUT` 8→**20** (slow ticks become slow successes; ConnectTimeout=5 still bounds TCP
failures). `sync_once` runs hosts in parallel (ThreadPoolExecutor) so one slow host can't
stretch the tick. The `remote_mtimes` ring (in the sidecar → survives restarts) yields
`remote_write_cadence_s` = median observed refresh granularity.

### One consumer read rule: `read_verdict(health, sidecar_age_s)`

The only judgments a writer can't make about itself, layered over the sidecar's own verdict:

1. no/garbled sidecar → `missing`
2. sidecar age > `max(15, 3·interval_s + ssh_timeout_s)` (defaults ⇒ 29s) → `stale-mirror`
3. `remote_status_age_s` > `max(30, 5·remote_write_cadence_s)` → `stale-content` (adaptive:
   a slow-writing host gets a proportionally lazier freeze alarm)
4. else the sidecar's `state` (v1 legacy: `ok`→ok, else down).

**Rows render iff verdict ∈ {ok, degraded}** — a blip no longer drops rows; the mirror-mtime
15s rule survives only as the no-sidecar backstop. **Red** (ccbar `⚠host`, ccdash bold-red
line) iff ∈ {missing, stale-mirror, stale-content, down}; `degraded` renders as a dim
"sync degraded: <err>" note in ccdash and nothing in ccbar. ccbar carries a diff-ably
identical stdlib `_read_verdict` mirror (the ignore-list idiom: the file is the interface);
`tests/test_read_verdict.py` runs one 18-case table against **both** copies.

### Ghost fix: identity-checked `_alive`

`_proc_identity_ok(comm, cmdline, proc_uid, my_uid)` — pure: alive iff same uid AND
(comm starts with `claude`/`node` OR `"claude"` in cmdline). `pid None` stays vouched (a
blocked background session has no live process by design). Unstatable entry ⇒ alive
(permission hiccup must not kill a live session); other-uid pid ⇒ dead (that IS the
recycling case). False-dead beats false-blocked: a genuinely cross-node session shows grey
`dead`, never a phantom alert (accepted trade-off).

### Optimistic ack overlay (ccdash)

`a` on a remote row overlays `acknowledged` instantly ({(host,sid): (on, expires)}, TTL 20s);
dissolves when the mirror agrees, at TTL, or on vanish; a failed ssh push reverts it (row
re-oranges) with the error toast.

### Also

`$CCMONITOR_REMOTE_DIR` overrides the mirror dir in ccremote/ccstatus/ccbar — isolated
worktree/test runs (NB: ControlPath sockets cap at 108 bytes; use a short dir and symlink the
live master socket in). `atomic_write_json` fixes `sync_host`'s previously-unwrapped mirror
write. `--serve` logs `(state, error)` transitions via `_transition_msg`.

## Key changes

- `~ ccremote.py` — pure primitives (`build_fetch_cmd`, `parse_sentinel`,
  `classify_ssh_failure`, `parse_snapshot`, `atomic_write_json`, `read_prev_health`,
  `make_health`, `_transition_msg`); sidecar v2 + ladder; `timeout`/`write-failed` classes;
  SSH_TIMEOUT 20; threaded `sync_once`; `$CCMONITOR_REMOTE_DIR`.
- `~ ccstatus.py` — `read_verdict` + `_sidecar_verdict`; `load_remote_health` →
  `{state, error, age_s, since}` (age = failure-streak age for degraded/down);
  `load_remote_sessions` verdict-gated; `_content_stale` deleted; identity-checked `_alive`
  + `_proc_identity_ok`.
- `~ ccbar.py` — stdlib `_read_verdict`/`_sidecar_verdict` mirrors; `_unhealthy_hosts` =
  red-verdict filter; `_content_stale` deleted.
- `~ ccdash.py` — `_health_line` (dim degraded / bold-red down keyed on verdict+error);
  `_ack_overlay` + `ACK_OVERLAY_TTL_S`.
- `+ tests/` — `conftest.py`, `test_ccremote.py` (23), `test_read_verdict.py` (41 incl.
  parametrization), `test_alive.py` (11). Run: `uv run --with pytest -m pytest tests/`.

## Verification

- Unit: 75 tests green; `py_compile` all modules; ccdash headless `run_test` mount OK.
- Live isolated one-shots against psc (`CCMONITOR_REMOTE_DIR=~/.claude/run/remote-iso`):
  healthy tick (2 sessions, content 1s), a real timeout blip → `degraded` (x1, x2) → `down`
  (x3), recovery reset, cadence ring filling — the ladder observed end-to-end on real
  infrastructure during a genuine psc slow episode.
- Soak (isolated syncer + 1s sampler + ccbar): see scratchpad `iso-soak.log`.
- Deployed `ccstatus.py` to psc (`/jet/home/jke2/project/claude-code-monitor`, md5-verified).

## Remaining

1. **psc serve daemon restart** — the running daemon likely predates the self-heal commit
   (4ba5c32), so it won't pick up the deployed source on its own; restarting it was
   policy-blocked from this session. After restart, verify ghosts: pids 64212/68992 in
   `run/remote/psc.json` must show `dead` (or vanish), no orange ghost rows in ccdash.
2. Merge `remote-sync-hysteresis` back to `ccdash`; restart the *local* `ccremote --serve`
   and `ccstatus --serve` (both self-heal on source mtime once merged files land).
3. ccdash live visual smoke (`prefix G`): remote section + dim degraded line rendering.
