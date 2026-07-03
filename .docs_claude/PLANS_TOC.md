# Plans Table of Contents

A browsable index of every planning/design doc under `.docs_claude/plans/`, organized two
ways: a **chronological index** (most-recent first, dated by git creation date) and **topic
sections** (each plan with a prose abstract + a "Key changes" list of created/modified/deleted
code). A plan may appear under several topics; it's abstracted once and the entry reused.

## Maintaining .docs_claude/PLANS_TOC.md
When a plan is added, copied, moved, renamed, or deleted:
1. Find it: `find -L . -path '*/.docs_claude/plans/*' -name '*.md'` (use `-L` / `rg --follow` — worktree copies under `.claude/worktrees/` are duplicates, exclude them).
2. Read its title + summary to judge purpose and the code section it touches.
3. Add an entry (`###` link + code-location line + 2–4 sentence abstract + "Key changes" `+`/`~`/`-` list)
   under EVERY matching topic. A plan MUST appear under ≥1 topic; never drop it; add a new `## Topic` if none fit.
4. On move/rename/delete, update or remove the existing entry/entries.
5. Keep topic order stable; group plans by recency within a topic.
6. Add it to the Chronological index too — date = git creation date
   (`git log --diff-filter=A --follow --format=%as -- <path> | tail -1`; uncommitted → today); re-sort most-recent first.

Legend for Key changes: `+` created · `~` modified · `-` deleted.

---

## Chronological index

- **2026-07-03** — [remote-sync-hysteresis-ghosts.md](plans/active/remote-sync-hysteresis-ghosts.md) `active`
- **2026-07-01** — [remote-sync-health.md](plans/completed/remote-sync-health.md) `completed`
- **2026-07-01** — [ccdash-remote-session-monitor.md](plans/completed/ccdash-remote-session-monitor.md) `completed`
- **2026-06-30** — [synopsis-no-anchor-on-low-confidence.md](plans/active/synopsis-no-anchor-on-low-confidence.md) `active` `bugfix`
- **2026-06-30** — [ccdash-peek-scroll-fix.md](plans/completed/ccdash-peek-scroll-fix.md) `completed` `bugfix`
- **2026-06-30** — [self-heal-serve-daemon.md](plans/completed/self-heal-serve-daemon.md) `completed` `bugfix`
- **2026-06-30** — [fix-phantom-mtime-reflag.md](plans/completed/fix-phantom-mtime-reflag.md) `completed` `bugfix`
- **2026-06-30** — [title-prefer-haiku-synopsis.md](plans/completed/title-prefer-haiku-synopsis.md) `completed`
- **2026-06-30** — [faq-stale-serve-daemon-engaged.md](plans/completed/faq-stale-serve-daemon-engaged.md) `faq` `error`
- **2026-06-29** — [ccdash-your-turn-engaged-fix.md](plans/completed/ccdash-your-turn-engaged-fix.md) `completed`
- **2026-06-29** — [ccmonitor-ignore-list.md](plans/completed/ccmonitor-ignore-list.md) `completed`
- **2026-06-29** — [ccdash-jump-autoack-your-turn.md](plans/completed/ccdash-jump-autoack-your-turn.md) `completed`
- **2026-06-29** — [ccbar-your-turn-tier.md](plans/completed/ccbar-your-turn-tier.md) `completed`
- **2026-06-29** — [ccdash-respond-summary-reader.md](plans/completed/ccdash-respond-summary-reader.md) `completed`
- **2026-06-29** — [ccbar-tmux-statusbar.md](plans/completed/ccbar-tmux-statusbar.md) `completed`
- **2026-06-28** — [ccstatus-provider-ccdash-tui.md](plans/completed/ccstatus-provider-ccdash-tui.md) `completed`
- **2026-04-17** — [ssh-bridge-plan.md](plans/completed/ssh-bridge-plan.md) `completed`
- **2026-04-17** — [remote-messages-and-permissions.md](plans/active/remote-messages-and-permissions.md) `active`
- **2026-04-17** — [fix-parallel-tool-approval-override.md](plans/completed/fix-parallel-tool-approval-override.md) `completed`
- **2026-04-17** — [usage-battery-bar.md](plans/completed/usage-battery-bar.md) `completed`
- **2026-04-17** — [usage-battery-mac-local.md](plans/completed/usage-battery-mac-local.md) `completed`
- **2026-04-17** — [notch-battery-permission-swap.md](plans/completed/notch-battery-permission-swap.md) `completed`

---

## 1. Status provider, dashboard & tmux integration (ccstatus / ccdash / ccbar)

The Python monitoring stack: the `ccstatus.py` provider (normalized `Session[]` source of
truth) and its consumers — the `ccdash` TUI popup and the `ccbar` tmux status segment — plus
their tmux wiring.

### [remote-sync-hysteresis-ghosts.md](plans/active/remote-sync-hysteresis-ghosts.md)
`plans/active/` · 2026-07-03 · `ccremote.py` / `ccstatus.py` / `ccdash.py` / `ccbar.py` / `tests/`
> Fixes the two remote-monitoring flakiness classes: the intermittently-flashing red `⚠psc`
> (single slow ssh ticks misclassified `unreachable` with zero debounce) and orange ghost
> rows for closed psc sessions (bare `/proc` pid check vs multi-login-node pid recycling).
> The verdict becomes *data*: ccremote's pure `make_health` ladder writes
> `ok|degraded|down` into a v2 sidecar (degraded = quiet-ish, rows kept; down = red after 3
> fails/25s), consumers collapse onto one shared `read_verdict` with thresholds scaled to
> the sidecar's self-description, `_alive` gains a uid+comm identity check, and ccdash acks
> remote rows optimistically. Adds the pytest suite (`tests/`). Also under topic 4.
>
> **Key changes:**
> - `~ ccremote.py` — pure primitives, sidecar v2 + hysteresis ladder, `timeout` class,
>   SSH_TIMEOUT 20, threaded `sync_once`, `$CCMONITOR_REMOTE_DIR`
> - `~ ccstatus.py` — `read_verdict` (shared trust rule), verdict-gated loaders,
>   identity-checked `_alive` (+`_proc_identity_ok`)
> - `~ ccbar.py` — stdlib `_read_verdict` mirror; degraded ≠ red; `~ ccdash.py` —
>   `_health_line` dim-degraded/red-down + optimistic `_ack_overlay`
> - `+ tests/` — test_ccremote / test_read_verdict (both copies) / test_alive

### [remote-sync-health.md](plans/completed/remote-sync-health.md)
`plans/completed/` · 2026-07-01 · `ccremote.py` / `ccstatus.py` / `ccdash.py` / `ccbar.py` / `ccremote-up.sh`
> Makes remote-sync failure *loud* in both consumers after a psc question silently never
> alerted: skew-free content-freshness measurement in the ssh fetch, per-host `.health`
> sidecars with a classified error taxonomy, a red warn-forever `⚠<host>` ccbar segment and
> bold-red `⚠ <host> — <why>` ccdash rows for every unhealthy `ccmonitor-remotes` host, plus
> two ccbar coupling fixes so a dead local daemon can't hide remote alerts. Abstracted under
> topic 4 (remote control); listed here for the ccstatus/ccdash/ccbar facet.
>
> **Key changes:**
> - `~ ccremote.py` — sentinel-header fetch + `.health` sidecar + error classification
> - `~ ccstatus.py` — content-stale drop, tolerant rebuild, `+ load_remote_health()`
> - `~ ccbar.py` — decoupled loads, red `⚠<host>` segment; `~ ccdash.py` — red health rows
> - `~ ccremote-up.sh` / `~ CLAUDE.md` — auth-recovery + design docs

### [ccdash-remote-session-monitor.md](plans/completed/ccdash-remote-session-monitor.md)
`plans/completed/` · 2026-07-01 · `ccremote.py` / `ccstatus.py` / `ccdash.py` / `ccbar.py`
> Extends the ccstatus/ccdash/ccbar stack to also surface sessions running on *other* machines.
> Adds `Session.host` and a consumer-side `load_remote_sessions()` to the provider (which itself
> stays strictly local), plus a new `ccremote.py` daemon that SSH-mirrors each configured host's
> `run/status.json` to `run/remote/<host>.json`. `ccdash` merges the mirrors (view-only `@host`
> rows, local-tmux actions gated), `ccbar` reads them alongside the local snapshot. Same
> provider-local / consumer-merge discipline as the ignore list; a stale mirror drops so a
> downed host never lingers. Abstracted under topic 4 (remote control); listed here for the
> ccstatus/ccdash/ccbar facet.
>
> **Key changes:**
> - `+ ccremote.py`; `~ ccstatus.py` (`Session.host`, `load_remote_sessions()`)
> - `~ ccdash.py` (merge + `@host` tag + gate local-tmux actions); `~ ccbar.py` (`_load_remote()`)
> - `~ CLAUDE.md` — module index + design-decision bullet

### [synopsis-no-anchor-on-low-confidence.md](plans/active/synopsis-no-anchor-on-low-confidence.md)
`plans/active/` · 2026-06-30
> Fixes a ccdash row showing a stuck placeholder-sounding title/synopsis
> (`(awaiting plan details)` / `(unable to determine — no prior context or plan files found)`)
> for a session that, by inspection time, had a full real transcript. Neither string is
> hardcoded — they're literal Haiku output from `ccsynopsis.py`, written when the early
> "recent activity" window was sparse/boilerplate. Because the EMA prompt is deliberately
> sticky ("keep the TITLE identical... prefer no change"), this junk first-impression never
> self-corrected. Compounded by [[title-prefer-haiku-synopsis]], which made titles always
> prefer the Haiku synopsis, so the junk surfaces directly instead of being masked. Fix: an
> explicit `UNKNOWN` sentinel Haiku emits on a no-signal cold start, normalized to `None` in
> `_parse()` so the existing empty-title guard skips the write — the EMA only ever anchors on
> genuine signal. See topic 2 for the synopsis-mechanism facet.
>
> **Key changes:**
> - `~ ccsynopsis.py` — `SYS`/`PROMPT_TEMPLATE` instruct Haiku to answer `UNKNOWN` for both
>   fields on a no-signal cold start instead of guessing a vague placeholder
> - `~ ccsynopsis.py` `_parse()` — normalize a sentinel hit to `None` per field
> - `+ CLAUDE.md` — "Low-confidence first impressions are never anchored" design-decision bullet

### [ccdash-peek-scroll-fix.md](plans/completed/ccdash-peek-scroll-fix.md)
`plans/completed/` · 2026-06-30
> Fixes the inline preview panel (`P`) clipping its own most useful content: a fixed-height
> non-scrolling `Static` drew the understanding header + a live 12-line pane tail, and
> whenever the (variable-height) header pushed the total past the box's 16 rows, the bottom
> of the render — the newest, most relevant pane-tail lines — got cut off instead of the
> header. Mirrors `DriveScreen`'s existing live-tail pattern: wraps the panel in a
> non-focusable `VerticalScroll` and scrolls to the end on every update, so the live tail is
> always what's visible and any clipping falls on older header content instead.
>
> **Key changes:**
> - `~ ccdash.py` — `compose()`: `self.peek` is now a `VerticalScroll(self.peek_body, ...)`, `can_focus = False`
> - `+ ccdash.py` — `_set_peek()` helper (update body + `scroll_end(animate=False)`)
> - `~ ccdash.py` — `_update_peek()` / `_peek_capture()` route through `_set_peek`

### [self-heal-serve-daemon.md](plans/completed/self-heal-serve-daemon.md)
`plans/completed/` · 2026-06-30
> Fixes the same "ccdash shows ✓, ccbar still alerts" symptom as a structural cause: the
> long-lived `ccstatus.py --serve` daemon never reloads its source, so an edit landing
> while it's running (e.g. [[fix-phantom-mtime-reflag]] itself) strands it on stale
> bytecode indefinitely, serving wrong `age_s`/`acknowledged` into `run/status.json`
> forever — exactly the gap [[faq-stale-serve-daemon-engaged]] diagnosed once but left
> unfixed. The daemon now detects its own source mtime changed and re-execs in place
> each tick, the same idiom as `SSHTunnelManager`'s stale-tunnel cleanup.
>
> **Key changes:**
> - `+ ccstatus.py` — `_SELF_PATH`/`_SELF_MTIME` module-level baseline (import-time)
> - `+ ccstatus.py` — `_restart_if_source_changed()` (`os.execv` in place, preserves `sys.argv`)
> - `~ ccstatus.py` — `_serve()` calls the check once per tick before the write
> - (operational) — live stale daemon (PID predating both this fix and `08933ed`) killed + relaunched once to bootstrap

### [fix-phantom-mtime-reflag.md](plans/completed/fix-phantom-mtime-reflag.md)
`plans/completed/` · 2026-06-30
> Fixes a session that kept re-surfacing in the "your turn" tier (ccbar `◆` / ccdash) hours
> after it was acked, with no genuine hand-back. Root cause: Claude Code rewrites the
> transcript JSONL in place to update metadata (`ai-title`, `mode`, `file-history-snapshot`),
> bumping the file's `st_mtime` to "now" with no new turn. `ccstatus.py` keyed both `age` and
> the ack/dismiss auto-clear (`_marker_active`) off raw `st_mtime`, so each phantom rewrite
> faked a fresh age and raced past the ack marker, re-arming the tier. Switches the activity
> clock to the newest *timestamped* transcript turn (metadata records carry no `timestamp`),
> with `st_mtime` as a fail-open fallback.
>
> **Key changes:**
> - `+ ccstatus.py` — `_parse_ts(ts)` helper (ISO-8601 'Z' → epoch, comparable to marker mtime)
> - `~ ccstatus.py` — `_transcript_usage` also returns `last_turn_ts` (newest timestamped entry, same tail-walk)
> - `~ ccstatus.py` — `get_sessions()` uses `activity_mtime = last_turn_ts or jp_mtime` for `age` and both `_marker_active` calls
> - `~ CLAUDE.md` — age + responded-to/dismiss design notes updated (last timestamped turn, not raw `st_mtime`)

### [title-prefer-haiku-synopsis.md](plans/completed/title-prefer-haiku-synopsis.md)
`plans/completed/` · 2026-06-30
> Fixes opaque session titles like `refseg-workspace-c8` / `ccmonitor-d8`. These are Claude
> Code's own default placeholder `name` (`<cwd-basename>-<short-hash>`) from `claude agents
> --json`, not Haiku or `short_id`. Because that `name` is never empty, the old title chain
> (`name → synopsis → short_id`) made the `.synopsis` branch dead code, masking good Haiku
> names that already existed. Reorders the fallback to put the Haiku synopsis first, so a
> session shows its summarized name whenever one exists. Tradeoff: real Claude-Code titles are
> also overridden by the Haiku name.
>
> **Key changes:**
> - `~ ccstatus.py` — title fallback `name → synopsis → short_id` ⇒ `synopsis → name → short_id`
> - `~ ccstatus.py` — `Session.title` contract comment updated to `haiku name → Claude name → short_id`

### [ccstatus-provider-ccdash-tui.md](plans/completed/ccstatus-provider-ccdash-tui.md)
`plans/completed/` · 2026-06-28
> Establishes the provider/consumer architecture. `ccstatus.py` gathers + normalizes one
> `Session` record per live session from the supported `claude agents --json` API plus
> tmux/`/proc` and the roster, emitting `--json`, a pipe-friendly TSV, `--watch`, and `--serve`.
> Its first consumer is `ccdash.py`, a Textual TUI run in a `tmux display-popup` that
> color-codes state (blocked-first), previews the live pane, and on Enter jumps to the
> session's pane via `switch-client`. Retired the brittle `_classify_pane` pane-scraper in
> favor of the API, and switched age to transcript mtime.
>
> **Key changes:**
> - `+ ccstatus.py` — `Session` dataclass, `get_sessions()`, CLI (`--json/--watch/--serve/--state`)
> - `+ ccdash.py` — Textual TUI consumer (PEP-723 `uv run --script`)
> - `+ ccsynopsis.py` — `claude -p` Haiku summarizer → `state/<sid>.synopsis`
> - `~ claude_status.py` — reduced to an `os.execv` shim → `ccstatus --serve`
> - `- _classify_pane` — pane-scrape state heuristic, retired
> - `~ ~/.claude/settings.json` — `ccsynopsis` on the `Stop` hook; `~/.tmux.conf` — `prefix G` popup

### [ccbar-tmux-statusbar.md](plans/completed/ccbar-tmux-statusbar.md)
`plans/completed/` · 2026-06-29
> Adds a third consumer of the `ccstatus` contract: a tmux `status-right` segment that is
> quiet until a session needs you. `ccbar.py` is stdlib-only (fast spawn each
> `status-interval`) and reads the cached `run/status.json` snapshot rather than re-invoking
> the provider, printing `⛔ <title> +N more` only when a session is `blocked` (and not
> acknowledged/dismissed), with a faint `⚠` when the snapshot is stale. Replaces the
> `tmux-dotbar` plugin.
>
> **Key changes:**
> - `+ ccbar.py` — `render()`, `main()`; reads `run/status.json`, escapes `#`→`##`
> - `~ ccstatus.py` — `--serve` also writes the full-fidelity `run/status.json` (`_write_status_json`, `STATUS_JSON`)
> - `~ ~/.tmux.conf` — removed `tmux-dotbar`; `status-right` runs `ccbar.py`, `status-interval 2`

### [ccdash-your-turn-engaged-fix.md](plans/completed/ccdash-your-turn-engaged-fix.md)
`plans/completed/` · 2026-06-29
> Fixes "your turn" false positives on freshly opened sessions: a brand-new session is already
> `idle` before Claude responds, so it was wrongly flagged as needing a reply. Adds a
> provider-computed `engaged` boolean (transcript has ≥1 assistant turn) — computed for free
> from the tail read `_transcript_usage` already does — and requires it in both consumers'
> `_awaiting`, so only a genuine hand-back (which always ends in an assistant turn) counts.
>
> **Key changes:**
> - `~ ccstatus.py` — `_transcript_usage` returns `(model, ctx, engaged)`; `Session.engaged` field; populate in `get_sessions`
> - `~ ccdash.py` — `_awaiting` requires `s.engaged`
> - `~ ccbar.py` — `_awaiting` requires `s.get("engaged")` (snapshot-driven, fail-quiet on old snapshots)

### [faq-stale-serve-daemon-engaged.md](plans/completed/faq-stale-serve-daemon-engaged.md) `faq` `error`
`plans/completed/` · 2026-06-30
> Postmortem of a confusing operational symptom: ccdash/notch showed a session needing
> attention but ccbar's `◆` "your turn" segment never appeared. Root cause was a ~24h-old
> `ccstatus.py --serve` daemon serving pre-`engaged` code — its `status.json` omitted the
> `engaged` key, so ccbar's `_awaiting` (`s.get("engaged")` → `None` → falsy) permanently
> suppressed the tier. Not a code regression and not caused by running ccdash; the fix is to
> restart the daemon (killing the tmux *session* didn't signal the process). See topic 1.
>
> **Key changes:**
> - (no code) — operational fix: `pkill -f 'ccstatus.py --serve'` + relaunch so the snapshot carries the current schema
> - possible hardening (deferred): `ccbar` show `⚠` on a *schema-stale* snapshot (rows lack `engaged`), not just a time-stale one

### [ccmonitor-ignore-list.md](plans/completed/ccmonitor-ignore-list.md)
`plans/completed/` · 2026-06-29
> A persistent, file-based regex ignore list (`~/.claude/run/ccmonitor-ignore`, override via
> `$CCMONITOR_IGNORE`) honored by both ccdash and ccbar, to permanently hide clutter (stale
> background agents, headless summarizers). Each pattern is searched case-insensitively against
> a session's kind/title/cwd independently. The provider hosts the matcher but never applies it
> (`get_sessions()` stays complete); the consumers filter — ccdash shows an `N ignored` count,
> ccbar mirrors the matcher stdlib-only so ignored sessions never alert. Fail-open throughout.
>
> **Key changes:**
> - `~ ccstatus.py` — `IGNORE_FILE`, `load_ignore_patterns()`, `is_ignored()` (provider never filters)
> - `~ ccdash.py` — filter in `_apply`, `N ignored` count
> - `~ ccbar.py` — `_load_ignore`/`_ignored` mirror (stdlib-only), filter in `render`
> - `+ ~/.claude/run/ccmonitor-ignore` — the ignore-pattern file (the headless interface)

### [ccdash-jump-autoack-your-turn.md](plans/completed/ccdash-jump-autoack-your-turn.md)
`plans/completed/` · 2026-06-29
> Jumping (`enter`/`o`) into a soft "your turn" (purple `◆`) session now auto-acks it after a
> successful `switch-client` — visiting it counts as handling it, so it clears from the dashboard
> and the ccbar `◆` segment. Hard `blocked` (⛔) rows are deliberately excluded and keep alerting
> until truly resolved. Settles `acknowledged` as "attention handled" (the loose reading the
> manual `a` mute already implies); reuses `_awaiting`/`_touch`/`ACK_DIR`; verified live.
>
> **Key changes:**
> - `~ ccdash.py` — `action_jump` touches `ACK_DIR` when `_awaiting(s)`, after the switch

### [ccbar-your-turn-tier.md](plans/completed/ccbar-your-turn-tier.md)
`plans/completed/` · 2026-06-29
> Extends the ccbar status segment from one tier to two: a loud `⛔ <title> +N more` (bold
> yellow) for `blocked` plus a separate soft `◆ <title> +M more` (magenta) for "your turn" (idle,
> handed back), mirroring ccdash's `_awaiting` stdlib-only. No provider change (the snapshot
> already carries the needed fields). Hardened so a malformed snapshot can't brick `status-right`:
> non-dict rows skipped, titles coerced + length-capped (`MAX_OUT`, trailing `RESET`), and
> `main()` swallows any exception → empty.
>
> **Key changes:**
> - `~ ccbar.py` — `_awaiting`, `TURN`/`TURN_GLYPH`, `_segment`, two-tier `render`, `MAX_OUT`, `main()` catch-all

### [ccdash-respond-summary-reader.md](plans/completed/ccdash-respond-summary-reader.md)
`plans/completed/` · 2026-06-29
> Three follow-ups to the shipped stack: respond to a session from the TUI (headless-first
> file-based outbox delivered via tmux `send-keys`), anchor the Haiku summary window at the
> user's last message, and a full-screen scrollable reader. Adds the `ccsend.py` mechanism
> and `ccdash` compose/drive/reader surfaces; the provider gains the shared transcript-window
> helper both the summarizer and reader use. See topics 2–4 for the responding and synopsis
> facets.
>
> **Key changes:**
> - `+ ccsend.py` — outbox contract (`enqueue`/`drain`) + the single `deliver()` `send-keys` seam + CLI
> - `~ ccstatus.py` — `turns_since_last_user()`, `pending_interaction()`, public `transcript_path()`
> - `~ ccsynopsis.py` — summary window anchored at the last user message
> - `~ ccdash.py` — `c`/`v`/`p` bindings, `action_compose`/`action_drive`, `ReaderScreen`/`VimScroll`, outbox drain on tick

---

## 2. AI synopsis & summarization (ccsynopsis)

The Stop-hook Haiku summarizer that evolves a sticky per-session name and a richer hidden
"understanding" as a moving average.

### [synopsis-no-anchor-on-low-confidence.md](plans/active/synopsis-no-anchor-on-low-confidence.md)
`plans/active/` · 2026-06-30
> The EMA's "keep the title identical unless the focus has genuinely shifted" stickiness
> (intended to stop flicker) had no way to recognize a *bad* first impression: when the
> earliest "recent activity" window is sparse/boilerplate, Haiku can honestly write a vague,
> placeholder-sounding title/understanding, which then gets fed back as the prior title
> forever, never self-correcting even once the session has real content. Fix is prompt-only:
> Haiku answers the literal sentinel `UNKNOWN` for both fields when there's no prior title and
> no concrete signal yet, instead of guessing; `_parse()` normalizes that to `None` so the
> existing `if not title: return` guard in `_worker()` already skips the write unchanged. See
> topic 1 for the dashboard-visible symptom.
>
> **Key changes:**
> - `~ ccsynopsis.py` — `SENTINEL = "UNKNOWN"`; `SYS`/`PROMPT_TEMPLATE` instruct the cold-start
>   escape hatch (scoped to "no prior title yet")
> - `~ ccsynopsis.py` `_parse()` — case/punctuation-tolerant exact-token sentinel → `None`
>   normalization, independently per field
> - `+ CLAUDE.md` — new design-decision bullet after "Synopsis is a moving average"

### [title-prefer-haiku-synopsis.md](plans/completed/title-prefer-haiku-synopsis.md)
`plans/completed/` · 2026-06-30
> Promotes the Haiku `.synopsis` to be the primary source of a session's display title. The
> old order let Claude Code's never-empty placeholder `name` (`<cwd-basename>-<short-hash>`)
> win first, so the summarizer's good names were never shown. Reordering the title fallback to
> `synopsis → name → short_id` makes the Stop-hook summary the visible name whenever it exists.
> See topic 1 for the full title-derivation context.
>
> **Key changes:**
> - `~ ccstatus.py` — title fallback reordered so `.synopsis` (Haiku) wins over the agents `name`

### [ccstatus-provider-ccdash-tui.md](plans/completed/ccstatus-provider-ccdash-tui.md)
`plans/completed/` · 2026-06-28
> Introduces the AI synopsis: the `Stop` hook detaches `claude -p` (Haiku) to summarize a
> session asynchronously, writing `state/<sid>.synopsis` keyed by transcript mtime. As-built,
> context pollution was fixed by running from a neutral cwd with
> `--exclude-dynamic-system-prompt-sections` and restructuring the prompt so the model
> summarizes rather than continues the dialogue; verified the summarizer never appears in
> `claude agents --json`.
>
> **Key changes:**
> - `+ ccsynopsis.py` — `--worker <sid> <transcript>`, async Haiku summarizer
> - `~ ~/.claude/settings.json` — `ccsynopsis` added to the existing `Stop` hook
> - `~ ccstatus.py` — `synopsis` field on `Session`, served from the cache file

### [ccdash-respond-summary-reader.md](plans/completed/ccdash-respond-summary-reader.md)
`plans/completed/` · 2026-06-29
> Moves the summarizer's reading window from a fixed last-6-turns tail to everything since
> the user's last genuine message, so the running understanding reads as "what Claude has
> done since I last spoke." A shared `turns_since_last_user()` helper lives in the provider
> and is reused by both the summarizer and the new reader; EMA blending is unchanged.
>
> **Key changes:**
> - `~ ccsynopsis.py` — `_recent_text` → `turns_since_last_user(transcript_path)`
> - `+ turns_since_last_user()` — `ccstatus.py` (shared transcript-window helper)
> - `+ pending_interaction()` — `ccstatus.py` (parses a pending AskUserQuestion/ExitPlanMode)

---

## 3. Headless input & responding (ccsend)

The file-based outbox contract and the single tmux `send-keys` delivery seam that any program
(the TUI, a CLI, an external script) can drive.

### [ccdash-respond-summary-reader.md](plans/completed/ccdash-respond-summary-reader.md)
`plans/completed/` · 2026-06-29
> Responding is headless-first: the representation of an input is a JSON file under
> `run/outbox/<sid>/` (`{"type":"text"|"keys"}`) that any program can write, and delivery is a
> single tmux `send-keys` seam (lifted from the Mac app's `ToolApprovalHandler`). `ccdash`
> exposes two producers — a compose box (`c`, text+Enter; covers messages and single-choice
> menus) and a key-passthrough drive mode (`v`, for multi-select / arbitrary menus) — and
> drains the outbox each tick. Fail-open throughout; live-verified that digit+Enter accepts a
> real `ExitPlanMode` menu.
>
> **Key changes:**
> - `+ ccsend.py` — `enqueue`/`deliver`/`drain`, `KEY_OK` whitelist, `ccsend <sid> "msg"`/`--keys`/`--drain` CLI
> - `~ ccdash.py` — `action_compose`, `DriveScreen(ModalScreen)` key passthrough, `_TMUX_KEY` map, outbox drain on refresh
> - `~ ccstatus.py` — `transcript_path()` public seam for the reader

---

## 4. SSH bridge & remote control

The reverse-SSH-tunnel transport that lets the Mac notch app monitor and control remote Linux
Claude Code sessions, and the message/permission content that rides it — plus the *opposite
axis*, one Linux box's Python consumers mirroring another host's sessions over SSH.

### [remote-sync-hysteresis-ghosts.md](plans/active/remote-sync-hysteresis-ghosts.md)
`plans/active/` · 2026-07-03 · touches `ccremote.py` / `ccstatus.py` / `ccdash.py` / `ccbar.py` / `tests/`
> The robustness follow-up to [[remote-sync-health]] after its loudness proved *too* loud:
> single slow ssh execs on a healthy psc (a successful exec measured at 12.8s during a
> busy-login-node episode) blew the 8s timeout, were misclassified `unreachable`, and went
> red instantly — while closed psc sessions haunted the dashboard orange because a bare
> `/proc/<pid>` existence check can't survive pid recycling across bridges2's shared-$HOME/
> per-node-/proc login nodes. Moves the verdict writer-side as sidecar-v2 *data* (pure
> `make_health` hysteresis ladder: degraded → down only after 3 fails/25s/auth×2), collapses
> the consumers' triplicated policy onto one `read_verdict` with self-scaling thresholds,
> identity-checks `_alive` (uid + claude/node comm; false-dead beats false-blocked), adds
> ccdash's optimistic remote-ack overlay, and introduces `tests/`.
>
> **Key changes:**
> - `~ ccremote.py` — primitives + sidecar v2 (`state`, `bad_since`, `remote_mtimes` ring →
>   `remote_write_cadence_s`), `timeout`/`write-failed` classes, threaded `sync_once`
> - `~ ccstatus.py` — `read_verdict`/`_sidecar_verdict`, verdict-gated `load_remote_sessions`
>   + `load_remote_health` `{state,error,age_s,since}`, `_proc_identity_ok` in `_alive`
> - `~ ccbar.py` / `~ ccdash.py` — stdlib verdict mirror / dim-degraded + red-down lines,
>   `_ack_overlay`; `+ tests/` — 75 tests incl. the two-copy lockstep table

### [remote-sync-health.md](plans/completed/remote-sync-health.md)
`plans/completed/` · 2026-07-01 · touches `ccremote.py` / `ccstatus.py` / `ccdash.py` / `ccbar.py` / `ccremote-up.sh`
> Hardens the [[ccdash-remote-session-monitor]] sync path after a real miss: a psc session's
> AskUserQuestion never alerted locally because the sync chain wasn't running — and the design
> silently dropped stale mirrors, so "sync broken" rendered exactly like "all quiet". Adds
> end-to-end **content** freshness (the ssh fetch prepends a `#cc# <now> <mtime>` sentinel, both
> stamps on the *remote's* clock, closing the "phantom-fresh" hole where a dead remote provider's
> frozen file keeps the mirror mtime fresh) and per-host `.health` sidecars with a classified
> error taxonomy (`auth` — dead password-auth ControlMaster, rerun ccremote-up.sh /
> `unreachable` / `no-file` / `garbled`). Both consumers surface broken sync loudly and
> warn-forever: a red `⚠<host>` ccbar segment and a bold-red `⚠ <host> — <why>` ccdash row for
> any host in `ccmonitor-remotes` that isn't healthy (commenting a host out is the mute). Also
> fixes two ccbar coupling bugs (a missing local snapshot no longer hides remote rows; the
> local-stale `⚠` no longer swallows remote sections), makes remote-record reconstruction
> tolerant of schema skew, and aligns ccremote's ControlPersist with ccremote-up.sh (12h).
> Considered and rejected: a message broker/queue — the snapshot is level-triggered state and
> SSH already-reliable transport; the missing pieces were freshness metadata and loudness.
>
> **Key changes:**
> - `~ ccremote.py` — `_fetch` sentinel header (skew-free `remote_status_age_s`), `.health`
>   sidecar per tick (`{synced_at, ok, error, consecutive_failures, remote_status_age_s,
>   sessions}`), rc/stderr error classification + `_master_gone` (`ssh -O check`) tie-break,
>   `sync_host`/`sync_once` return health dicts, `_serve` logs health *transitions*,
>   ControlPersist from `$CCREMOTE_CONTROL_PERSIST` (default 12h)
> - `~ ccstatus.py` — `load_remote_sessions` drops content-stale mirrors
>   (`REMOTE_CONTENT_STALE_S=30`) + tolerant rebuild via `_SESSION_DEFAULTS` (only
>   `session_id`-less records dropped); `+ load_remote_health()` (per-configured-host state:
>   `ok|missing|stale-mirror|stale-content|auth|unreachable|no-file|garbled`)
> - `~ ccbar.py` — `_load()` decoupled local/remote reads; local-stale is a prepended dim `⚠`
>   segment (not an early return); `+ _unhealthy_hosts()` → red `⚠host1,host2 +N` segment;
>   `_content_stale` frozen-mirror drop
> - `~ ccdash.py` — `_Section` gains `style`; `_apply` renders bold-red `⚠ <host> — <msg> (age)`
>   rows (`_HEALTH_MSG`) under `── remote ──`, shown even with zero live remote rows
> - `~ ccremote-up.sh` / `~ CLAUDE.md` — dead-master → `auth` surfacing documented; module rows
>   + "Remote monitoring" design bullet updated (two clocks, fail-open-is-not-silent)

### [ccdash-remote-session-monitor.md](plans/completed/ccdash-remote-session-monitor.md)
`plans/completed/` · 2026-07-01 · touches `ccremote.py` / `ccstatus.py` / `ccdash.py` / `ccbar.py`
> Lets this machine's `ccdash`/`ccbar` show Claude sessions running on *other* hosts. Because
> `claude agents --json` is strictly machine-local, the fix is to *mirror the remote's
> already-normalized snapshot over* rather than query it: a new `ccremote.py` daemon SSH-copies
> each configured host's `~/.claude/run/status.json` (the portable `Session[]` contract, **not**
> `~/.claude.json`, which is mere config) to `run/remote/<host>.json`, and the consumers fold
> those in via a new `ccstatus.load_remote_sessions()`. Reuses the Mac app's "pull the status
> file over SSH" idiom ([[ssh-bridge-plan]] / `remote-ssh-stages.md`) but Python-side, keeping
> the provider local + consumer-side merge discipline of the ignore list. Remote rows are grouped
> in their **own section** (a `── remote ──` heading in ccdash, a `│`-divided section in ccbar),
> labeled `host:<name>`; jump/respond/drive stay gated (view-only, since the remote `pane_id` isn't
> a local pane), but **ack pushes to the host over SSH** (`remote_ack` → the remote's ccstatus
> recomputes `acknowledged`). A mirror older than ~15s is dropped so a downed host never shows
> phantom-live rows. The **opposite axis** to the rest of this topic (Mac→remote); here it's this
> box→remote. Also under topic 1 (it extends the ccstatus/ccdash/ccbar stack).
>
> **Key changes:**
> - `+ ccremote.py` — SSH syncer: `load_hosts`/`_fetch`/`sync_host`/`sync_once`, `--serve` daemon (self-healing) + one-shot CLI, ControlPersist, per-host remote-path + env overrides, stdlib-only fail-open; `remote_ack(host, sid, on)` (ssh touch/rm the remote ack marker, `sid` shell-guarded)
> - `+ ccremote-up.sh` — launcher: bring up the SSH master once (password/2FA hosts), then `exec ccremote --serve`
> - `~ ccstatus.py` — `Session.host` field; `load_remote_sessions()` (stale-drop + tolerant `Session(**rec)` rebuild); `REMOTE_DIR`/`REMOTE_STALE_S`; env-overridable `--serve` output paths
> - `~ ccdash.py` — merge remotes in `load()`; `host:<name>` title + magenta remote-target in `_cells`; `_Section("remote")` heading in `_apply`; `action_ack` pushes over SSH for remote rows; gate jump/compose/drive on `s.host`; peek + `ReaderScreen` skip remote pane capture
> - `~ ccbar.py` — `_load_remote()` reads/ tags `run/remote/*.json` (stale-drop); `render()` splits local vs remote into `│`-divided sections (`_two_tier`)
> - `~ CLAUDE.md` — `ccremote.py` module-index row + remote notes + "mirror the remote's status.json" design bullet
> - `+ ~/.claude/run/ccmonitor-remotes` — hosts file (the headless interface); dir `~/.claude/run/remote/`

### [ssh-bridge-plan.md](plans/completed/ssh-bridge-plan.md)
`plans/completed/` · 2026-04-17
> The full SSH-bridge implementation plan. A remote hook (`ccbridge-hook.py`) connects to the
> Mac over a reverse SSH tunnel (TCP, `AF_INET`) using claude-island's existing JSON protocol,
> discovering the bridge via `~/.claude/run/bridge_port` and auto-denying on failure. The Mac
> gains a TCP listener alongside its Unix socket, an SSH host picker, a tunnel-lifecycle actor,
> an idempotent remote hook installer, and a remote tmux controller — so remote sessions
> appear in the notch and permissions/messages route back over the socket.
>
> **Key changes:**
> - `+ hooks/ccbridge-hook.py` — remote hook, `send_event()` single transport swap point
> - `~ HookSocketServer.swift` — `startTCPServer(port:)`, `acceptTCPConnection()` alongside Unix socket
> - `+ SSHTunnelManager.swift`, `+ RemoteHookInstaller.swift`, `+ RemoteTmuxController.swift`
> - `+ RemoteHostPickerRow.swift`; `~ Settings.swift` (`remoteSSHHost`, `remoteBridgeEnabled`)
> - `~ NotchMenuView.swift`, `~ ClaudeSessionMonitor.swift`, `~ AppDelegate.swift` — wire tunnel + remote routing

### [remote-messages-and-permissions.md](plans/active/remote-messages-and-permissions.md)
`plans/active/` · 2026-04-17
> Forwards conversation content for remote sessions (which the Mac can't read from JSONL)
> through the existing TCP bridge: user text via `UserPromptSubmit`, assistant text read from
> the JSONL tail at `Stop` time. Also adds an expandable permission-detail view so the user
> sees the full tool input before allow/deny. Stage 0 (the parallel-approval-override bugfix)
> is complete; remaining stages are planned. See topics 5 and 7 for the UI and approval
> facets.
>
> **Key changes:**
> - `~ hooks/ccbridge-hook.py` — forward `prompt`; `_read_last_assistant_message()` at `Stop`
> - `~ HookEvent` (Swift) — add `messageRole` field; `~ SessionStore.processHookEvent` — `appendMessageFromHook()` (remote-guarded)
> - `+ PermissionDetailView.swift`; `~ SessionPhase.swift` — `PermissionContext.fullInput`
> - `~ ClaudeInstancesView.swift` — expand/collapse permission detail in `InstanceRow`

---

## 5. claude-island notch UI (Swift)

The macOS notch app's display surfaces — session rows, the usage battery, permission
indicators, and notch sizing/layout.

### [remote-messages-and-permissions.md](plans/active/remote-messages-and-permissions.md)
`plans/active/` · 2026-04-17
> Adds remote-session chat content and a structured permission-detail view to the notch. The
> `InstanceRow` gains an expand/collapse detail section that renders the full tool input
> (Bash command block, Edit diff, file paths) instead of a 1-line truncation, auto-expanding
> while waiting for approval. (Abstracted under topic 4.)
>
> **Key changes:**
> - `+ PermissionDetailView.swift` — structured, untruncated tool-input display
> - `~ ClaudeInstancesView.swift` — `InstanceRow` expand/collapse + detail integration
> - `~ SessionPhase.swift` — `PermissionContext.fullInput` (priority-sorted, no truncation)

### [usage-battery-bar.md](plans/completed/usage-battery-bar.md)
`plans/completed/` · 2026-04-17
> First render of the account-global usage battery in the notch chrome. Adds the
> `UsageBarView` — a horizontal battery whose fill is proportional to remaining 5-hour quota,
> colored green/yellow/red, with a live reset countdown — hidden when usage is nil or stale.
> (Pipeline + data source abstracted under topic 6.)
>
> **Key changes:**
> - `+ UsageBarView.swift` — battery bar + `TimelineView` reset countdown
> - `+ Models/UsageInfo.swift`; `~ SessionStore.swift` — global `currentUsage`, `SessionEvent.usage`
> - `~ ClaudeSessionMonitor.swift` — `@Published usageInfo`

### [notch-battery-permission-swap.md](plans/completed/notch-battery-permission-swap.md)
`plans/completed/` · 2026-04-17
> A small layout fix: the amber permission `?` and the battery both fought for the notch's
> left cluster. Resolves it by having the `?` replace the battery while a permission is
> pending (notch width stays constant), gating the battery render and its width contribution
> behind a single `shouldShowUsageBattery` predicate.
>
> **Key changes:**
> - `~ NotchView.swift` — `shouldShowUsageBattery` gate, `usageExtraWidth` returns 28/0, gated `UsageBatteryView` render

### [fix-parallel-tool-approval-override.md](plans/completed/fix-parallel-tool-approval-override.md)
`plans/completed/` · 2026-04-17
> A state-machine bugfix (also under topic 7): an unrelated parallel `PostToolUse` was
> overwriting the `waitingForApproval` phase and making the approval UI vanish. Guards phase
> transitions so only the pending tool, a new PermissionRequest, or session-level events can
> move off `waitingForApproval`.
>
> **Key changes:**
> - `~ SessionStore.swift` — `processHookEvent()` guard around the `waitingForApproval` transition

---

## 6. Usage / rate-limit battery (statusline pipeline)

The account-global Claude Max 5-hour usage indicator: the statusline data source, the bridge
plumbing that carries it, and the Mac-local and remote feeds.

### [usage-battery-bar.md](plans/completed/usage-battery-bar.md)
`plans/completed/` · 2026-04-17
> Phase 1: source rate-limit data from Claude Code's statusline subsystem (the only read path
> exposing `rate_limits.five_hour`), writing `~/.claude/run/usage.json` atomically and pushing
> a TCP `usage` event over the bridge to the notch battery. Extracts the TCP send path into a
> shared `bridge_send.py` so the statusline script and the hook share one connect/write path.
> Fail-open: the statusline never exits non-zero.
>
> **Key changes:**
> - `+ hooks/ccmonitor-statusline.py` — reads stdin, writes `usage.json`, TCP-sends
> - `+ hooks/bridge_send.py` — shared TCP connect/write, extracted from `ccbridge-hook.py`
> - `~ setup.sh` — merge a `statusLine` stanza (idempotent, don't clobber existing)
> - Mac: `+ UsageInfo`, `~ SessionStore`/`HookSocketServer`, `+ UsageBarView.swift`

### [usage-battery-mac-local.md](plans/completed/usage-battery-mac-local.md)
`plans/completed/` · 2026-04-17
> Phase 2: feed the same battery from Mac-local Claude Code sessions, not just remote hosts.
> A statusline wrapper fire-and-forgets to `ccmonitor-statusline.py` and delegates to the
> user's preserved original prompt; the Mac writes its own `bridge_port` (loopback) and flags
> 127.0.0.1 peers as local. Also splits the tunnel launcher to first `pkill` orphaned remote
> tunnel sessions, fixing an exit-255 reconnect loop.
>
> **Key changes:**
> - `~ HookInstaller.swift` — install statusline + `bridge_send.py`, write wrapper, preserve `statusline-original.sh`
> - `~ HookSocketServer.swift` — write/remove `run/bridge_port`; `isRemote` from peer `sockaddr_in`
> - `~ SSHTunnelManager.swift` — `launchTunnel()` pre-pkill of orphaned remote tunnels

### [notch-battery-permission-swap.md](plans/completed/notch-battery-permission-swap.md)
`plans/completed/` · 2026-04-17
> A follow-up layout fix on the battery: when a permission request is pending, the actionable
> `?` replaces the ambient battery in the notch's left cluster so neither clips and the notch
> width stays constant. (Abstracted under topic 5.)
>
> **Key changes:**
> - `~ NotchView.swift` — `shouldShowUsageBattery` gate combining usage-present, not-stale, and no-pending-permission

---

## 7. Permission approval flow & bugfixes

How tool-permission requests are surfaced, detailed, and approved/denied — across the bridge
and the notch state machine.

### [fix-parallel-tool-approval-override.md](plans/completed/fix-parallel-tool-approval-override.md)
`plans/completed/` · 2026-04-17
> Fixes a race where a `PermissionRequest` for one tool was instantly cleared by an unrelated
> parallel tool's `PostToolUse`, which `determinePhase()` resolved to `.processing` and
> `canTransition()` allowed — so the approval UI vanished before the user could act. The fix
> guards the `waitingForApproval` phase: only the pending tool's events, a new
> PermissionRequest, or session-level events (`Stop`/`SessionEnd`/`UserPromptSubmit`) may
> transition off it.
>
> **Key changes:**
> - `~ SessionStore.swift` — `processHookEvent()` guard before the phase transition

### [remote-messages-and-permissions.md](plans/active/remote-messages-and-permissions.md)
`plans/active/` · 2026-04-17
> Beyond forwarding remote messages, this plan deepens the approval surface with an
> expandable, untruncated permission-detail view (full Bash command, Edit diff, file paths) so
> security-sensitive allow/deny decisions are informed. Stage 0 — the parallel-approval-override
> guard — is complete and folded in. (Abstracted under topic 4.)
>
> **Key changes:**
> - `~ SessionPhase.swift` — `PermissionContext.fullInput` (priority-sorted, no truncation)
> - `+ PermissionDetailView.swift`; `~ ClaudeInstancesView.swift` — auto-expand detail while waiting for approval

### [ssh-bridge-plan.md](plans/completed/ssh-bridge-plan.md)
`plans/completed/` · 2026-04-17
> Establishes the remote approval path: a remote `PermissionRequest` blocks on a `recv()` over
> the TCP socket, the Mac stores the fd in `pendingPermissions` exactly like a local Unix fd,
> and `respondToPermission()` writes the allow/deny decision back over the same socket —
> auto-denying on timeout. (Abstracted under topic 4.)
>
> **Key changes:**
> - `+ hooks/ccbridge-hook.py` — blocking `recv()` for PermissionRequest, auto-deny fallback
> - `~ HookSocketServer.swift` — transport-agnostic `handleClient(fd)` serves both socket types
> - `~ ClaudeSessionMonitor.swift` — route remote approvals back over the TCP fd
