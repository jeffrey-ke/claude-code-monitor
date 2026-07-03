# Remote-sync health: end-to-end freshness + loud failure surfacing

**Status: COMPLETE (all stages); verified except a live ccdash visual smoke —
see "Remaining" at the bottom.** Approved plan copy:
`~/.claude/plans/see-latest-plan-on-declarative-donut.md`.

## Why (incident, 2026-07-01 night)

A psc session asked an AskUserQuestion and no local alert appeared for >30 min. Root
cause: the sync chain (SSH master + `ccremote --serve`) wasn't up when the question
arrived — master socket created 21:16, syncer started 21:41 — and the design silently
drops stale mirrors, so "sync broken" rendered exactly like "all quiet". Note: psc uses
**password auth (not 2FA)**; BatchMode ssh can't answer a password prompt, so a dead
ControlMaster = silent failure forever until `ccremote-up.sh` is re-run.

Decision (user-approved): keep the pull/mirror state-snapshot model, **no broker/queue**
(state-not-events; SSH is already reliable transport; what was missing is content
freshness + loudness). Warn-forever policy; `ccmonitor-remotes` is the intent
declaration, commenting a host out is the mute. Pull-only (no push layer).

## What was built

### ccremote.py
- `SSH_OPTS` ControlPersist now `$CCREMOTE_CONTROL_PERSIST` (default 12h — matches
  ccremote-up.sh; was hardcoded 30s mismatch).
- `_fetch(host, path)` → `(payload|None, remote_age_s|None, error|None)`. One ssh exec:
  `echo "#cc# $(date +%s) $(stat -c %Y P 2>/dev/null || stat -f %m P 2>/dev/null)"; cat P`
  → `remote_age_s = now−mtime` **both on the remote's clock** (skew-free; BSD stat
  fallback; unparseable header → age None). Detects "phantom-fresh": remote provider dead
  but `ssh cat` keeps refreshing the local mirror mtime.
- Error classification on rc=255: stderr patterns first (`permission denied`/
  `authentication`/`host key verification` → `auth`; `could not resolve`/`timed out`/
  `connection refused`/`no route`/`network is unreachable` → `unreachable`), tie broken by
  `_master_gone()` (`ssh -O check`). rc≠0 non-255 → `no-file`; unparsable/non-list
  payload → `garbled`.
- `sync_host` now returns a **health dict** and always writes sidecar
  `run/remote/<host>.health` (atomic; `HEALTH_SUFFIX=".health"`, deliberately non-.json so
  the consumers' `*.json` mirror globs never see it):
  `{synced_at, ok, error, consecutive_failures, remote_status_age_s, sessions}`.
  On failure the mirror is left untouched (ages out); sidecar carries the bad news.
  `sync_once` → `{host: health}` (contract change from `{host: count|None}`).
- `_serve` logs per-host health **transitions** only to stderr; one-shot `main()` prints
  `FAIL <error> — rerun ccremote-up.sh (xN)` / `N session(s), content Xs old`.

### ccstatus.py
- New constants: `REMOTE_CONTENT_STALE_S = 30`, `HEALTH_SUFFIX = ".health"`,
  `REMOTES_FILE` (env `$CCMONITOR_REMOTES`).
- `load_remote_sessions`: skips mirrors whose sidecar shows
  `remote_status_age_s > 30` (`_content_stale`); **tolerant reconstruction** via
  `_SESSION_DEFAULTS` (12 no-default fields get typed defaults; only records without
  `session_id` are dropped).
- New `load_remote_health()` → `{host: {"state","age_s","since"}}` for **exactly** the
  configured hosts (leftover sidecar of a commented-out host can't warn). States:
  `ok | missing | stale-mirror | stale-content | auth | unreachable | no-file | garbled`.
  Sidecar mtime > 15s ⇒ stale-mirror (syncer dead) outranks health content. `age_s` for
  fetch failures = time since mirror mtime (last successful sync).
- `_remote_hosts()`: ~10-line duplicate of ccremote's hosts parsing + stem sanitizing
  (deliberate — ccstatus never imports the network-touching module).

### ccbar.py
- Decoupled `_load()`: local snapshot + remote mirrors in separate trys; missing/corrupt
  local → `stale=True` (was `([],False)` silent) and remotes still load.
- `render(sessions, stale, unhealthy=())`: local-stale is now a **prepended dim ⚠
  segment** (was early-return that swallowed remote sections); local rows suppressed when
  stale, remote sections still render; **red `⚠host1,host2 +N` segment** (WARN=
  `#[fg=red]`, `WARN_MAX_HOSTS=2`) appended for unhealthy hosts.
- `_content_stale()` + frozen-mirror drop in `_load_remote()`; `_unhealthy_hosts()`
  (stdlib mirror of load_remote_health reduced to a name list). New constants mirror
  ccstatus: `HEALTH_SUFFIX`, `REMOTE_CONTENT_STALE_S=30`, `REMOTES_FILE`.

### ccdash.py
- Imports `load_remote_health`; `load()` passes it to `_apply(sessions, health)`.
- `_Section` gains `style` param (default `"bold grey58"`); `_cells` renders `s.label`
  verbatim now (heading is constructed as `_Section("── remote ──")` — the dashes moved
  into the constructor call).
- `_apply` appends bold-red inert rows `⚠ <host> — <msg> (<age>)` (`_HEALTH_MSG` map)
  after remote rows; remote section renders even with zero live remote rows if warns
  exist. `fmt_age(int(age))` (fmt_age renders floats badly).

### ccremote-up.sh / CLAUDE.md
- ccremote-up.sh: header comment documents dead-master → `auth` surfacing (no re-auth
  automation).
- CLAUDE.md updated: module rows for ccstatus/ccremote/ccremote-up.sh/ccdash/ccbar + the
  "Remote monitoring" key-decision bullet (two clocks; fail-open-but-loud; broker/queue
  rejected rationale).

## Verification done
- Live syncer (pid persists via os.execv self-heal) writes `.health` every ~3s; psc
  healthy, `remote_status_age_s` ≈ 1.0.
- Bogus host → `unreachable`, sidecar written, `load_remote_health` +
  `ccbar._unhealthy_hosts` flag it, bar renders red `⚠no-such-host…` (used env
  `CCMONITOR_REMOTES` + cleaned up sidecar after).
- `root@localhost` → `auth` classification works.
- Isolated dir: old sidecar → `stale-mirror`, absent → `missing`; frozen sidecar
  (`age 99`) → rows dropped in both ccstatus and ccbar loaders; tolerant reconstruction
  builds a row from `{session_id, state}` only, drops id-less.
- ccbar: local snapshot missing → `stale=True`, remote ⛔ still renders with dim ⚠.
- Healthy-path E2E: live bar showed `⛔ psc:real-lr-val` during a real psc question.
- `py_compile` all four modules OK; ccdash imports clean under
  `uv run --with textual --with rich`.

## Remaining
1. **ccdash live smoke** (only open item) — throwaway detached-tmux run was rejected;
   the user should open ccdash (`prefix G`) and confirm no crash + the remote section /
   health lines render. No known issues.
2. Syncer-death live test was skipped (not allowed to kill the user's live
   `ccremote --serve`) — covered by isolated-directory tests instead.
3. Stage 5 docs done: this file moved to `plans/completed/`, PLANS_TOC.md entries added
   (topics 1 + 4 and the chronological index, 2026-07-01). `.docs_claude/architecture.md`
   doesn't diagram the ccremote flow, so it was left alone.
4. Memory saved: psc uses password auth (`psc-uses-password-auth.md`).
5. Contract note if anything downstream breaks: `sync_once`/`sync_host` return health
   dicts now (old: count|None); only known consumer was ccremote's own `main()`/`_serve`.
