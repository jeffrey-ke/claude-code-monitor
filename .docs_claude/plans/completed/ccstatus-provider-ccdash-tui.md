# Plan: `ccstatus` provider + `ccdash` tmux-popup TUI

**Status: COMPLETED 2026-06-28** (branch `ccdash`). As-built notes at the bottom.

## Context

You already run `ccmonitor` (`~/repo/ccmonitor`): hooks write per-session state to
`~/.claude/run/state/<sid>`, `claude_status.py` polls every 2s and writes a padded
plain-text `~/.claude/run/status` that a Mac notch UI fetches over SSH. Two things have
changed and one is missing:

1. **Claude Code shipped a native, supported session API** — `claude agents --json` returns
   `pid, sessionId, cwd, kind, name, status|state` per live session. This obsoletes the
   brittle `_classify_pane` tmux-screen-scraper (regex on `"esc to cancel"` / token counts)
   and the redundant `ccmonitor-hook.sh` state writer.
2. There is **no pipeable CLI** — status is only a 2s-stale, daemon-dependent, space-padded
   file. Nothing can `--json`/`jq`/`awk` it, and nothing works without the daemon up.
3. There is **no AI synopsis**. (Title is already free: `name` from `agents --json` is
   Claude-generated, e.g. "Review completed gligen training plan and checkpoint".)

**Goal:** split this into a clean **provider** (`ccstatus`, the single normalized source of
truth, the "information source under test") and its **first consumer** — a pretty Textual
**TUI dashboard** (`ccdash`) that runs in a `tmux display-popup`, color-codes state with
permission-blocked sorted to the top, previews the live pane, and on `Enter` **jumps to that
session's tmux pane**. The provider is designed so future consumers reuse it; the TUI is the
first usability test of its contract.

Decision locked: synopsis = **`claude -p` Haiku, async + cached** (uses existing Claude.ai
auth, no API key/SDK). A `ccdash` git branch already exists in the submodule.

## Architecture (mechanism / policy split)

```
claude agents --json ─┐
~/.claude/sessions/*  ─┤
roster.json           ─┼─►  ccstatus  ──►  Session[]  ──►  ccstatus --json ─► ccdash (TUI)
tmux list-panes/proc  ─┤    (provider/      (contract)      ccstatus (TSV)  ─► pipes/awk/jq
state/<sid>.synopsis  ─┘     normalize)                     ccstatus --serve─► ~/.claude/run/status (island)
```

Provider = gather + normalize + emit (no display opinions). Consumers = all display/sort/act
policy. Matches the repo's separate-mechanism/policy and explicit-selector conventions.

## 1. Provider — `ccstatus.py` (module + CLI)

**`Session` record = the contract** (`@dataclass`, serialized verbatim by `--json`):

| field | source | notes |
|---|---|---|
| `session_id`, `short_id` | `agents --json` | short = first 8 |
| `pid`, `kind` | `agents --json` | `interactive` \| `background` |
| `state` | normalized | `blocked` \| `busy` \| `shell` \| `idle` \| `dead` |
| `title` | `agents` `name` → `short_id` | already Claude-generated |
| `synopsis` | cache → `seed.intent` → first user prompt → `""` | see §2 |
| `cwd`, `cwd_short` | `agents` | `$HOME`→`~`, tail-truncated |
| `tmux_target`, `pane_id` | tmux + `/proc` walk | `"train:1.0"`, `"%363"`; `None` for bg/no-pane |
| `age_s` | `sessions/<pid>.json` `statusUpdatedAt` | `now − ts`; `None` if absent |
| `model?`, `ctx_pct?` | transcript tail | cheap, optional, best-effort |

**State normalization** (handles both schemas seen live — interactive has `status`
busy/idle/shell and no `state`; background has `state` blocked and a `status`):
`blocked` (from `state`) wins → else `status` (busy/shell/idle) → `dead` if `pid` set but
`/proc/<pid>` gone. Every enrichment is **fail-open**: a missing/garbled source yields `None`
for that field, never an exception (mirrors `ccmonitor-statusline.py`'s philosophy).

**Reused code:** lift the `/proc` parent-walk `_find_pane_pid` + tmux `list-panes` parsing
from `claude_status.py:35-50,89-129`; lift `_project_dir` (cwd→`projects/` dir encoding) and
the tail-reader from `ccbridge-hook.py:61-95`. Retire `_classify_pane`.

**CLI surface:**
```
ccstatus                      # bare TSV, header, blocked-first        (default = pipe-friendly)
ccstatus --json               # Session[] as JSON  → ccdash + future consumers
ccstatus --no-header --state blocked   # filters; exit 1 if zero rows (script-gateable)
ccstatus --watch[=1.5]        # stream (iotop -b style)
ccstatus --serve              # daemon: write ~/.claude/run/status in the CURRENT format
```
`--serve` keeps claude-island byte-compatible; `claude_status.py` is reduced to a shim that
calls `ccstatus --serve` (no deletion of the reproducible path).

## 2. Synopsis — `ccsynopsis.py` (async, cached; never blocks the UI)

- **Title** needs nothing — `agents --json` `name`.
- **Instant synopsis** (provider, zero-cost): first user prompt from the transcript head, or
  `roster.json` `dispatch.seed.intent` for background sessions. Shown immediately.
- **AI synopsis**: `claude -p "<=12-word summary of what this session is doing:\n<tail>"
  --model claude-haiku-4-5 --output-format text` → atomic-write `~/.claude/run/state/<sid>.synopsis`,
  keyed by transcript mtime (re-summarize only when stale; otherwise serve cache).
- **Trigger:** the existing **`Stop`** hook (turn-end, cheap) runs `ccsynopsis <sid>` detached
  (`&`), so the hook never waits on the model. `ccdash` reads the cache file and upgrades the
  row when it appears.
- **Build-time check:** confirm a headless `claude -p` summarizer does **not** appear in
  `claude agents --json`; if it does, tag it (sentinel env/cwd) and filter it in `ccstatus`.

## 3. Consumer #1 — `ccdash.py` (Textual, PEP-723 single file)

Run via `uv run --script ccdash.py`; inline deps `dependencies = ["textual>=0.6"]`
(pure-Python, fast after first cache). DataTable, updated **in place** (not recomposed).

- **Columns:** `●  TITLE  SYNOPSIS  CWD  AGE  TMUX`.
- **Sort:** `blocked → busy → shell → idle → dead`, then `age` — actionable rows on top (k9s).
- **Color:** blocked = **bold amber**, busy = cyan, shell = blue, idle = dim, dead = grey.
- **Refresh:** `set_interval(1.5, …)` worker shells `ccstatus --json`; diff rows in place.
- **Peek panel** (bottom, on `RowHighlighted`): `tmux capture-pane -pt <pane_id> -S -20`
  (live tail; transcript tail for bg). Read-only, no popup nesting — works on your tmux 3.4.
- **Footer bindings:** `enter`/`o` jump · `p` toggle peek · `/` filter · `s` cycle sort ·
  `r` refresh · `?` help · `q` quit. Mouse click selects (bonus; keyboard is primary).
- **Jump:** `tmux switch-client -t <pane_id>` then `app.exit()`. The popup is a pure overlay
  (creates no client), so `switch-client` retargets the **underlying** client — session+window
  +pane in one call via `%id` — and `-E` closes the popup, landing you in the real pane.
  Background sessions (no `pane_id`): toast "background session — no tmux pane" (attach flow
  deferred to a later pass).

## 4. tmux wiring (one line, added to `~/.tmux.conf` alongside your yazi/claude popups)

```tmux
bind-key G display-popup -E -w 90% -h 85% -e COLORTERM=truecolor \
  "uv run --script $HOME/repo/ccmonitor/ccdash.py"
```
`prefix G` = "goto claude". 90% width leaves room for the peek panel; `-e COLORTERM` forces
truecolor inside the popup.

## File layout (all in `~/repo/ccmonitor`, branch `ccdash`)

| file | role |
|---|---|
| `ccstatus.py` | **new** — provider: `Session`, `get_sessions()`, CLI (`--json/--watch/--serve/--state`) |
| `ccdash.py` | **new** — Textual TUI consumer (PEP-723) |
| `ccsynopsis.py` | **new** — `claude -p` Haiku summarizer → `state/<sid>.synopsis` |
| `claude_status.py` | **shrink** — shim → `ccstatus --serve` (back-compat for island) |
| `~/.claude/settings.json` | **edit** — add `ccsynopsis` to the existing `Stop` hook; drop redundant `ccmonitor-hook.sh` state write |
| `~/.tmux.conf` | **edit** — add the `prefix G` popup binding |
| `README.md` / `CLAUDE.md` | update: provider/consumer model, CLI usage |

Retired: `_classify_pane` screen-scraping; the duplicate state-file write (ccbridge keeps the
remote-permission bridge; ccmonitor-hook.sh's state role is gone).

## Verification (end-to-end)

1. **Provider:** `python ccstatus.py --json | jq` shows all live sessions with correct
   `state`/`title`/`tmux_target`/`pane_id`; cross-check against `claude agents --json` and the
   live `~/.claude/run/status`. `ccstatus --state blocked` exits non-zero when none are blocked.
2. **TSV/pipe:** `ccstatus --no-header | awk '{print $1,$2}'` is clean (tab-delimited).
3. **Synopsis:** trigger a `Stop` in a test session → `state/<sid>.synopsis` appears within a
   couple seconds; `ccstatus --json` shows it; confirm the summarizer isn't in `agents --json`.
4. **TUI:** `uv run --script ccdash.py` in a normal pane — table renders, colors correct,
   blocked on top, peek panel tails the highlighted pane, auto-refresh visible.
5. **Popup + jump:** bind `prefix G`, open the popup, highlight another session, press `Enter`
   → popup closes and the underlying client is now on that session's pane. Repeat for a
   same-session and a cross-session target; verify a background row shows the toast.
6. **Back-compat:** `ccstatus --serve` running → `~/.claude/run/status` is byte-identical in
   format to today's, so claude-island is unaffected.

## As-built notes (deviations discovered during implementation)

- **Age source changed: transcript `.jsonl` mtime, not `sessions/<pid>.json
  statusUpdatedAt`.** The sidecar was found hours-stale on older CLI versions
  (cliVersion 2.1.193 reported `shell`/47h-old while `agents --json` said `busy`).
  Transcript mtime is the reliable "last activity" signal across versions, so
  `_sessions_index()` was dropped entirely.
- **Synopsis context pollution fixed two ways.** First runs summarized sessions as
  *ccmonitor itself* because `claude -p` loaded the cwd's CLAUDE.md. Fix: run the
  summarizer from a neutral empty cwd (`~/.claude/run/synopsis-ctx`) **and** pass
  `--exclude-dynamic-system-prompt-sections`. Also restructured the prompt — transcript
  as delimited DATA first, instruction last, plus an `--append-system-prompt` summarizer
  role — because the model was continuing the dialogue instead of summarizing.
- **No recursion / no pollution from the summarizer.** Verified `claude -p` does not
  appear in `claude agents --json` (count unchanged 8→8) and the `CCSYNOPSIS_RUNNING`
  env guard prevents a summarizer-of-a-summarizer; the `ccmonitor-hook.sh` removal was
  deferred (left in place as harmless legacy rather than ripped from 6 events).
- **Synopsis model/latency:** `claude-haiku-4-5`, ~2–5s per call, detached on Stop so
  the hook returns in ~15ms.
- **TUI verification:** rendered live in tmux (blocked-first, peek, AI synopses, footer);
  `action_jump` unit-tested (interactive → `switch-client -t <pane_id>` + exit; background
  → toast). The threaded refresh worker does not pump under Textual's `run_test` harness,
  so the jump test drives `action_jump` directly rather than via `pilot.press`.
- **Files added:** `ccstatus.py`, `ccdash.py`, `ccsynopsis.py`; `claude_status.py`
  reduced to an `os.execv` shim. Live config: `Stop` hook + `prefix G` binding.
- **Left for interactive confirmation:** the popup→underlying-client jump (needs an
  attached client; mechanic confirmed by research + the user's own `tmux-tree-pick.sh`).
