# Remote dismiss: push `d` to the source host (and clean the stale psc ghost)

Status: **implemented** on branch `ccdash`. Approved plan copy:
`~/.claude/plans/i-m-unable-to-dismiss-splendid-graham.md`.

## Why

A stale orange `psc:real-lr-val` row couldn't be dismissed from ccdash. Two independent facts:

1. **`d` on a remote row was a silent no-op.** `action_dismiss` had no `s.host` gate — it
   touched the *local* `~/.claude/run/dismissed/<sid>`, which nothing reads for mirrored rows:
   `load_remote_sessions` rebuilds `s.dismissed` from the mirror every tick, so the toast said
   "dismissed" and the row never hid. The `a`/ack path had already solved the identical problem
   (push the marker to the source over the shared SSH master + optimistic overlay); dismiss just
   never got the same treatment.
2. **The stale session itself** was a dead **daemon-backed background job** on psc that
   `claude agents --json` kept listing as `blocked`. Its footprint spanned three stores:
   `~/.claude/sessions/64212.json` (runtime status record), `~/.claude/jobs/6fd7ed0a/state.json`
   (the job store — **this is what the agents list is built from**; deleting only the sessions
   record changed nothing), and `~/.claude/daemon/roster.json` (worker pid 64066 + sockets in a
   dead login node's `/tmp`, all frozen at the job's start minute). `claude agents` has no
   remove command; `rm -rf ~/.claude/jobs/<short>` is the cleanup that actually drops it from
   the list (plus the sessions/<pid>.json record for tidiness). The psc `--serve` daemon — on
   current code *with* the identity-checked `_alive` — runs on a different login node where the
   recycled pid evidently passes `_proc_identity_ok` (recycled to another of the user's own
   claude/node processes: the residual hole a uid+comm check can't close), which is why the row
   held `blocked` instead of `dead`.

## What changed

- `ccremote.py`: `remote_ack`'s body extracted into shared `_remote_mark(host, sid, dir_, on)`
  plus the pure `build_mark_cmd(dir_, sid, on)` (repo primitive style, cf. `build_fetch_cmd`);
  `remote_ack` and the new `remote_dismiss` are thin wrappers over `REMOTE_ACK` /
  `REMOTE_DISMISS = ~/.claude/run/dismissed`.
- `ccdash.py`: `_ack_overlay` generalized to a field-keyed
  `_remote_overlay[(host, sid, field)]`, field ∈ {`acknowledged`, `dismissed`}, applied in
  `_apply` via getattr/setattr with the same dissolve rules (mirror agrees → pop; 20s TTL;
  push failure → revert + error toast). `action_ack` and `action_dismiss` share a
  `_mark_remote` helper; the threaded worker is now `_push_remote_mark` dispatching to
  `remote_ack`/`remote_dismiss`. Local paths unchanged. Nothing downstream changed — the
  hidden-row filter, `D` reveal, and strike styling already key off `s.dismissed`, which the
  mirror now carries (the remote's own ccstatus derives it from its `dismissed/` dir).
- Tests: `build_mark_cmd` (both dirs, on/off) + bad-sid rejection of
  `remote_ack`/`remote_dismiss` in `tests/test_ccremote.py`.

## Verified live

Pushed `remote_dismiss('psc', 6fd7ed0a…)` → marker landed in `/jet/home/jke2/.claude/run/dismissed/`,
psc's ccstatus recomputed `dismissed: true`, the mirror carried it back within one sync tick.
(Bonus: the stale bg session has no transcript, so `activity_mtime is None` and the marker sticks
permanently — exactly right for a ghost.) Then removed the stale job at the source
(`rm -rf ~/.claude/jobs/6fd7ed0a` — removing `sessions/64212.json` alone did nothing); the row
left the mirror entirely on the next sync tick.

## Key changes

- `+` `ccremote.build_mark_cmd` / `_remote_mark` / `remote_dismiss`; `REMOTE_DISMISS` dir const
- `~` `ccremote.remote_ack` → thin wrapper over `_remote_mark`
- `~` `ccdash._ack_overlay` → field-keyed `_remote_overlay`; `ACK_OVERLAY_TTL_S` → `OVERLAY_TTL_S`
- `~` `ccdash.action_dismiss` gates on `s.host` and pushes to the source (new `_mark_remote`,
  `_push_remote_ack` → `_push_remote_mark`)
- `+` tests: `test_build_mark_cmd`, `test_remote_mark_rejects_bad_sid`

## Out of scope (noted for later)

The residual `_alive` hole: a recycled pid landing on *another of the user's own claude/node
processes* defeats the uid+comm identity check. The `sessions/<pid>.json` record carries
`procStart` (proc starttime ticks), which would allow an exact start-time match — a possible
future hardening if orange ghosts recur.
