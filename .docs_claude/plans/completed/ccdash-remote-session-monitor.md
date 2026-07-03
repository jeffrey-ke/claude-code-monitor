# ccdash remote session monitor — mirror a remote host's `status.json` over SSH

Status: **completed** — view-only first cut, verified live against PSC (Bridges-2). Remote
interactivity deferred (see out-of-scope). After-approval follow-ups also landed: `host:<name>`
row labels in both consumers, env-overridable `--serve` output paths + per-host remote paths,
and the `ccremote-up.sh` launcher for password/2FA hosts.

**Follow-up (ack-over-SSH + remote sections):** acking a remote row now **pushes to the host**
(`ccremote.remote_ack` → `ssh <host> touch/rm ~/.claude/run/ack/<sid>`, `sid` shell-guarded) instead
of writing a local marker the mirror would clobber, so the remote's own ccstatus recomputes
`acknowledged` (single source of truth, accurate transcript auto-clear). And remote sessions now get
their **own section** in both consumers: a `── remote ──` heading (inert `_Section` row) in ccdash,
and a `│`-divided section in ccbar. Verified live on PSC (ack push/clear round-trip via
`ssh psc ls ~/.claude/run/ack/`, injection guard, both section layouts, headless ccdash mount).
Dismiss-over-SSH stays out of scope (dismiss is a local "hide from my view" concept).

## Goal

Let this machine's Python monitoring stack (`ccdash` TUI + `ccbar` tmux segment) also show
Claude Code sessions running on *other* hosts — lightweight, no new daemon on the remote
beyond the `ccstatus --serve` that's already the standard setup.

## Problem

`claude agents --json` — the sole session enumerator behind `ccstatus.get_sessions()` — is
strictly **machine-local**: it resolves pids against local `/proc`, correlates with local
`tmux`, and reads local `~/.claude/...`. So a session on another box is invisible here. The
user's instinct ("a file here that tracks the remote's `claude.json`") is right in *shape* but
wrong in *file*: `~/.claude.json` is global config/telemetry, not live session state (the repo
never reads it). The live per-machine feed is **`~/.claude/run/status.json`** — the
full-fidelity `Session[]` snapshot that every host's `ccstatus.py --serve` already writes, and
it's already portable, self-describing JSON.

## Approach

Reuse the claude-island Mac app's proven idiom (`claude-island/remote-ssh-stages.md`, Stage 1:
`ssh host cat ~/.claude/run/status`) but **Python-side**, preserving the provider/consumer
discipline of the ignore list — the provider stays local and never reaches off-box; a
dedicated syncer produces files; consumers merge them.

```
Remote host:  ccstatus.py --serve → ~/.claude/run/status.json   (Session[], already portable)
                                            │  ssh host cat …
Local box:    ccremote.py --serve  → ~/.claude/run/remote/<host>.json   (verbatim mirror)
                                            │  ccstatus.load_remote_sessions()
              ccdash: get_sessions() + load_remote_sessions()   ← merged, remote rows `host:<name>`
              ccbar:  status.json  +  remote/*.json              ← merged, same two-tier alert
```

- **Zero schema translation** — the mirror *is* the `Session` contract; the loader rebuilds
  `Session(**rec)` tolerantly (ignores unknown/missing keys → version-robust).
- **Liveness = the mirror's mtime.** A mirror older than ~15s (dead syncer or host) is dropped
  by both consumers, so a downed host never shows phantom-live rows or alerts forever.
- **Remote rows are view-only.** Their `pane_id` is the *remote's* tmux id — meaningless and
  possibly *colliding* with a local pane — so every local-tmux path (jump, compose, drive, pane
  capture, peek) guards on `s.host`. Remote rows are labeled `host:<name>` in both consumers (the
  ccdash TMUX column then shows the remote's own target in magenta, ccbar prefixes the segment).

## Decisions

- **Consumers:** both ccdash and ccbar (ccbar's change is a few lines; the codebase keeps the
  two in lock-step).
- **Fetch mode:** `ssh host cat status.json` (assume remote runs `--serve`), not `ssh host
  ccstatus --json` — the former avoids a per-poll `claude agents --json` spawn on the remote and
  matches the Mac idiom. SSH ControlPersist reuses one connection across the 3s poll cadence.
- **Config file** `~/.claude/run/ccmonitor-remotes` (override `$CCMONITOR_REMOTES`), one
  `host`/`user@host` per line, `#` comments — deliberately the same headless-file idiom as
  `ccmonitor-ignore`.
- **Separate `ccremote.py`** (not folded into `ccstatus --serve`) per the module-per-role
  philosophy; runs as a sibling daemon.

## Key changes

- `+ ccremote.py` — SSH syncer: `load_hosts()` (host + optional per-host remote-path, env
  `$CCMONITOR_REMOTE_STATUS` default), `_fetch()` (ssh cat, fail-open), `sync_host()` (parse +
  atomic tmp/replace to `run/remote/<host>.json`), `sync_once()`, `--serve[=SECS]` daemon with
  the `_restart_if_source_changed()` self-heal + a one-shot debug CLI. Stdlib-only.
  Follow-up: `remote_ack(host, sid, on)` — `ssh <host> touch/rm ~/.claude/run/ack/<sid>` (reuses
  the master; `_SID_OK` shell-guard) so ccdash can ack a remote row at its source.
- `+ ccremote-up.sh` — launcher for a password/2FA remote: ensure `run/remote/`, bring up the
  SSH master at ccremote's exact ControlPath once (long `ControlPersist`), then `exec ccremote
  --serve`. Idempotent (`ssh -O check`); hosts from args or `ccmonitor-remotes`.
- `~ ccstatus.py` — `Session.host: str = ""` field; `load_remote_sessions()` consumer-side
  loader (stale-drop by mtime, tolerant `Session(**rec)` rebuild, host tag); `REMOTE_DIR`,
  `REMOTE_STALE_S`; `from dataclasses import … fields`. `--serve` output paths env-overridable
  (`$CCSTATUS_STATUS_JSON` / `$CCSTATUS_STATUS_FILE`) for a small-`$HOME` host.
- `~ ccdash.py` — merge `load_remote_sessions()` in `load()`; label the title `host:<name>` and
  show the remote target in the TMUX column (`_cells`, magenta); gate
  `action_jump`/`action_compose`/`action_drive` on `s.host` with a view-only `notify`;
  `_update_peek` + `ReaderScreen` skip local pane capture for remote rows. Follow-up: `action_ack`
  pushes over SSH for remote rows (`import ccremote`); an inert `_Section("remote")` heading groups
  remote rows in `_apply` (partition + order each side), with `_cells`/`_selected`/cursor-restore
  header-aware. The ack push runs in a `@work(thread=True)` worker (`_push_remote_ack`) so the
  seconds-long ssh never blocks the event loop.
- `~ ccbar.py` — `_load_remote()` reads `run/remote/*.json` (stale-drop, tags `host`) and
  `_load()` concatenates it with the local snapshot; `_segment` labels a remote row
  `host:<title>`. Stdlib-only, still fail-open. The two-tier `⛔`/`◆` alert naturally includes
  remote blocked / your-turn sessions. Follow-up: `render()` splits local vs remote into their own
  sections (`_two_tier` per side) joined by a `SECTION_SEP` divider.
- `~ CLAUDE.md` — `ccremote.py` in the module index; `load_remote_sessions` on the ccstatus
  row; remote notes on the ccdash/ccbar rows; a "Remote monitoring = mirror the remote's
  status.json" design-decision bullet.

## Verification (done)

- `ccremote.sync_host` (ssh transport stubbed): valid snapshot → atomic mirror written;
  unreachable → `None`, previous mirror untouched; non-JSON → `None`.
- `load_remote_sessions()`: fresh mirror → host-tagged `Session`s; backdated mtime → 0 rows.
- `ccbar`: a mirror with a blocked + a your-turn remote session surfaces both `⛔` and `◆`
  tiers, merged with local rows.
- `ccdash` (headless `App.run_test`): mounts, merges the remote mirror, labels rows
  `host:<name>`, and gates jump/compose on remote rows without crashing.
- **Live smoke against PSC (Bridges-2) done:** PSC runs `ccstatus.py --serve` writing to
  `/jet/home/<user>/.claude/run/status.json`; the hosts file entry `psc <that path>` +
  `ccremote-up.sh` (SSH master opened once, password/2FA host) mirrors 3 live sessions to
  `run/remote/psc.json`; ccdash shows `psc:<name>` rows and ccbar labels a blocked/your-turn
  remote row `psc:<title>`.

## Relationship to existing remote work

This is a **different axis** from `[[ssh-bridge-plan]]` and `[[remote-messages-and-permissions]]`,
which are about the **Mac** notch app reaching remote Linux hosts (push over a reverse tunnel /
pull for the Swift UI). Here it's **this Linux box's Python consumers** reaching *another* host.
Both share the "the status file is the portable interface" insight.

## Out of scope (clean later stages)

- Remote interactivity (respond/approve/jump) via `ssh host tmux send-keys` — reuse the
  `ccsend` outbox, routing delivery over SSH when `s.host` is set (mirrors Mac stages 4/6).
- Auto-starting `ccstatus --serve` / deploying ccmonitor on a remote (assume standard setup).
- Remote pane captures in peek/reader via `ssh host tmux capture-pane` (Mac Stage 5).
