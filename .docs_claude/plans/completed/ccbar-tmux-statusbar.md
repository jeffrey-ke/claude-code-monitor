# ccbar — passive tmux status-bar alert for blocked sessions (COMPLETED)

## Context

The only passive view of Claude sessions was the `prefix G` ccdash popup, which is **modal**
— while it's up you can't work in your window. tmux has no non-modal floating overlay
([tmux#1842], unimplemented in 3.4), so the genuinely-passive, always-visible surface is the
**status line**. This replaces the `tmux-dotbar` plugin with a hand-rolled bar that keeps a
left-aligned window list and adds a **quiet-until-needed "needs you" segment** on the right.

A third consumer of the `ccstatus.py` provider contract (after `ccdash.py` and pipes), built
to honour the provider/consumer split: the provider only persists its normalized `Session[]`;
the consumer owns all display policy.

## What was built

- **Provider** (`ccstatus.py`): `--serve` now also writes `~/.claude/run/status.json` — a
  full-fidelity `Session[]` snapshot via the existing `asdict` serialization, atomic
  `tmp + os.replace`. Needed because the claude-island TSV (`run/status`) is *lossy*:
  `SERVE_STATE` collapses `busy`+`shell`→`working`.
- **Consumer** (`ccbar.py`, new): plain `python3`, stdlib-only so it spawns in ~30 ms each
  `status-interval`. Reads the snapshot (never the live provider — a fresh `ccstatus --json`
  per tick would hammer `claude agents --json`). Quiet-until-needed: empty unless a session is
  `blocked` and not acknowledged/dismissed, then `⛔ <title> +N more` (names the one ccdash
  floats to the top: blocked, ascending age). Faint `⚠` if the snapshot is stale (>15 s) so a
  dead daemon never reads as "all clear". Escapes `#`→`##` (tmux re-parses `#()` output).
- **tmux** (`~/.tmux.conf`): removed `tmux-dotbar` (plugin + `@tmux-dotbar-*` + the F12
  re-runs); un-shadowed and reworked the existing status block to left-aligned windows +
  `status-right '#(python3 …/ccbar.py) #{@pane_name}'`, `status-interval 2`; F12 nested-dim
  reworked to a session-level `status-style` override that `set -u` reverts cleanly.

## Design decisions

- **`status.json` is the status-bar feed**, not the TSV — full state fidelity; cheap consumers
  read the cached snapshot instead of re-invoking the provider.
- **Quiet until needed** (chosen over counts-always / counts+name): bar is empty unless a
  session is blocked. `waiting_for` is already in the snapshot as a zero-cost future toggle
  (`⛔ web-api · permission`); kept minimal for now.
- **Aligned with the headless system** (`ccsend.py`, added in parallel on `ccdash`): the three
  compose without overlap — `ccsend` = headless input (outbox → send-keys), `ccdash` =
  interactive responder, `ccbar` = passive read-only notifier. ccbar deliberately does **not**
  drain the outbox or respond. `state == "blocked"` remains the right "needs you" signal;
  `pending_interaction()` is optional enrichment the bar doesn't need.

## Notable history

- **Replayed onto an advanced base.** Built on `9d842fc`; meanwhile `ccdash` advanced to
  `9af3d7a` (headless ccsend + new `ccstatus.py` transcript helpers). Replayed via
  `git rebase --onto 9af3d7a` — `ccstatus.py`/`ccbar.py` applied clean (additive, non-overlapping
  regions); only `CLAUDE.md` conflicted (both edited the module table) and was hand-merged to
  keep both sides.
- **Bug found in deploy: `$HOME` didn't expand.** `tmux-continuum` prepends its auto-save hook
  to `status-right` on load and backslash-escapes any `$`, so `$HOME` became `\$HOME` and tmux
  ran `python3 $HOME/…` literally → ccbar never executed → bar stayed empty. **Fix:** absolute
  path `/home/jeffk/repo/ccmonitor/ccbar.py` (no `$` to mangle; matches the daemon if-shell's
  hardcoded path; harmless on macOS where the file is absent). Also bumped
  `status-right-length` 40→100 so the alert isn't truncated.

## Verification (all passed)

- Provider `--serve` writes valid `status.json` (matches `--json`); the other agent's +100
  `ccstatus.py` lines (`transcript_path`/`turns_since_last_user`/`pending_interaction`) intact.
- ccbar render matrix: empty / 1 blocked / `+N more` / `#`-escape / busy-only-quiet /
  acknowledged+dismissed suppressed / stale `⚠`.
- `ccbar` does zero subprocess work (1 execve — just python; reads one file).
- Live: confirmed the bar renders `⛔ <title>` for a real blocked, non-dismissed session.

## Files

`ccstatus.py` (+`_write_status_json`, `STATUS_JSON`), `ccbar.py` (new), `CLAUDE.md`
(ccbar Consumer #2 row + decisions), `~/.tmux.conf` (dotbar swap; in the dotfiles repo).

[tmux#1842]: https://github.com/tmux/tmux/issues/1842
