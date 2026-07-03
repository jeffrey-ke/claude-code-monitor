---
description:
alwaysApply: true
---

# ccmonitor

Remote monitor & control for Claude Code sessions via SSH bridge.

## Quick start

The Mac app (claude-island) handles setup automatically:
1. Open claude-island → notch menu → SSH Bridge → select a host
2. App establishes reverse SSH tunnel, deploys hooks, writes `bridge_port`
3. Remote Claude Code sessions appear in the notch within seconds

For the standalone server-side monitor (no Mac app):
```bash
bash setup.sh              # install hooks, merge settings.json
python ccstatus.py         # one-shot TSV of all sessions (pipe-friendly)
python ccstatus.py --json  # the Session[] contract (consumed by ccdash + others)
uv run --script ccdash.py  # live TUI dashboard (also bound to tmux `prefix G`)
python claude_status.py    # back-compat: == ccstatus.py --serve → ~/.claude/run/status
```

The stack is a **provider** (`ccstatus.py`, the single normalized source of truth,
sourced from the supported `claude agents --json` API) feeding **consumers** (the
`ccdash.py` TUI and the `ccbar.py` tmux status-line segment today; any tool that reads
`ccstatus --json` / the `~/.claude/run/status.json` snapshot tomorrow).

## Module index

| Module | Role | Key exports |
|---|---|---|
| `ccstatus.py` | **Provider** — normalized `Session` per live session from `claude agents --json` + tmux/`/proc` (pane id) + roster + synopsis/understanding cache; title falls back name→haiku→code; derives `acknowledged`/`dismissed` from `run/{ack,dismissed}/<sid>` touch-files; sets `engaged` (transcript has ≥1 assistant turn) so consumers can tell a genuine "your turn" hand-back from a never-answered fresh idle session; transcript helpers `transcript_path` / `turns_since_last_user` / `pending_interaction`; consumer-side ignore-list helpers `load_ignore_patterns` / `is_ignored` and the remote-session loaders `load_remote_sessions` (reads `run/remote/<host>.json` mirrors written by `ccremote.py`, drops mirrors that are mtime-stale *or* whose `.health` sidecar says the content itself is frozen (`remote_status_age_s > REMOTE_CONTENT_STALE_S = 30`), rebuilds records *tolerantly* — missing schema fields get typed defaults via `_SESSION_DEFAULTS`, only a `session_id`-less record is dropped — and tags each `Session.host`) + `load_remote_health` (per-configured-host sync health for the consumers: `ok`/`missing`/`stale-mirror`/`stale-content`/`auth`/`unreachable`/`no-file`/`garbled`; covers exactly the ccmonitor-remotes hosts so commenting one out is the mute) — all consumer-side, the provider itself never filters or reaches off-box; CLI `--json`/`--watch`/`--serve`/`--state`; `--serve` writes both the claude-island TSV (`run/status`, override `$CCSTATUS_STATUS_FILE`) and a full-fidelity `Session[]` snapshot (`run/status.json`, override `$CCSTATUS_STATUS_JSON` — both tiny atomically-overwritten snapshots, not append logs, so relocatable on a small-`$HOME` host) | `Session`, `get_sessions()`, `load_remote_sessions()` |
| `ccremote.py` | **Remote syncer** — mirror other machines' sessions onto this box for the local consumers. Reads a hosts file `run/ccmonitor-remotes` (override `$CCMONITOR_REMOTES`, one `host`/`user@host` per line, `#` comments — same idiom as the ignore list; an optional 2nd whitespace field overrides the remote snapshot path for a host with a small/quota'd `$HOME`, default `$CCMONITOR_REMOTE_STATUS` or `~/.claude/run/status.json`), and for each host runs `ssh <host> cat <remote-status-path>` (the remote's own `--serve` snapshot, already the portable `Session[]` contract) and drops it verbatim at `run/remote/<host>.json` (atomic tmp+replace, SSH ControlPersist for cheap reuse — persist window `$CCREMOTE_CONTROL_PERSIST`, default 12h, matching ccremote-up.sh). The same ssh exec prepends a `#cc# <now> <mtime>` sentinel header — both stamps from the *remote's* clock, so the content age is skew-free — and every tick writes a `run/remote/<host>.health` sidecar (non-`.json`, invisible to the mirror globs): `{synced_at, ok, error, consecutive_failures, remote_status_age_s, sessions}`, `error` classified `auth` (control master gone — rerun ccremote-up.sh; probed via `ssh -O check`) / `unreachable` / `no-file` / `garbled`. `--serve[=SECS]` daemon (self-heals on source change like ccstatus; logs per-host health *transitions* to stderr, not every tick) + a bare one-shot for debugging; stdlib-only, fail-open per host (an unreachable host leaves its previous mirror to age out — the sidecar, not mirror deletion, carries the bad news). The consumers fold the mirrors in via `ccstatus.load_remote_sessions()`; the mirror's mtime is the syncer-liveness clock, the sidecar's `remote_status_age_s` the content-freshness clock. Also exposes `remote_ack(host, sid, on)` — `ssh <host> touch/rm ~/.claude/run/ack/<sid>` (reuses the master; `sid` shell-guarded) so ccdash can ack a remote row at its source | `load_hosts`, `sync_host`, `sync_once`, `remote_ack` |
| `ccremote-up.sh` | **Remote-sync launcher** — for a remote that needs a password/2FA. Ensures `run/remote/` exists, brings up the shared SSH master at ccremote's exact ControlPath **once** interactively (long `ControlPersist`, so it outlives poll gaps; reused by every `BatchMode` fetch), then `exec`s `ccremote.py --serve`. Hosts from args or `ccmonitor-remotes`; `$CCREMOTE_CONTROL_PERSIST` / `$CCREMOTE_ARGS` override lifetime / launch mode. Idempotent — skips a host whose master (`ssh -O check`) is already up. A master that later dies surfaces as `error:"auth"` in the health sidecar → red `⚠<host>` in ccbar / "auth needed — rerun ccremote-up.sh" in ccdash; rerunning this script is the fix | one-command startup |
| `ccsend.py` | **Input mechanism** — deliver an input into a session via tmux `send-keys`; headless-first **outbox** contract `run/outbox/<sid>/<uniq>.json` (`{"type":"text"\|"keys",…}`) any program can write; `enqueue`/`deliver`/`drain` + a `ccsend <sid> "msg"` / `--keys` / `--drain` CLI; fail-open | `enqueue`, `deliver`, `drain` |
| `ccdash.py` | **Consumer #1** — Textual TUI (PEP-723 `uv run --script`); blocked-first table; `c`=respond (compose → text+Enter), `v`=drive (keypress→pane passthrough for multi-select/any menu), `p`=full-screen scrollable reader; `a`=responded-to (mutes orange), `d`=hide row, `D`=show-hidden; `enter`=jump via `tmux switch-client -t <pane_id>` (auto-acks a your-turn row); merges remote sessions from `load_remote_sessions()` under a `── remote ──` section heading (an inert `_Section` row; title shown as `host:<name>`, TMUX column shows the remote's own target in magenta; view-only — jump/respond/drive gated since the remote `pane_id` isn't a local pane, but `a`=ack **pushes to the host over SSH** (threaded via `_push_remote_ack` → `ccremote.remote_ack`, off the event loop so a slow ssh never freezes the TUI) so the remote's own ccstatus recomputes `acknowledged`); shows a bold-red inert `⚠ <host> — <why> (age)` line under the remote heading for every configured host whose sync is broken (`load_remote_health()` states → `_HEALTH_MSG`; the section renders even with zero live remote rows) so vanished rows are never mistaken for all-quiet; honors the shared `ccmonitor-ignore` regex list (filters rows, `N ignored` count); drains the outbox each tick; runs in `display-popup` | `CCDash` |
| `ccbar.py` | **Consumer #2** — tmux `status-right` segment (plain `python3`, stdlib-only so it spawns fast each `status-interval`); reads the `run/status.json` snapshot **plus any `run/remote/<host>.json` mirrors** (dropping mirrors that are mtime-stale or whose `.health` sidecar shows frozen content; remote sessions render as their own section after a dim `│` divider, each labeled `host:<title>`) and prints a *quiet-until-needed* alert across **two tiers**: `⛔ <title> +N more` (bold yellow) for `blocked`, and a separate `◆ <title> +M more` (magenta) for "your turn" (idle, handed back, not acked/dismissed — mirrors ccdash's `_awaiting`); empty when nothing needs you. Local and remote are loaded *independently*: a missing/stale local snapshot shows the faint `⚠` and drops only local rows — never the remote sections. A red `⚠<host>[,<host>] +N` segment (`_unhealthy_hosts()`, warn-forever) names every configured host whose sync is broken (syncer dead, auth needed, host unreachable, or remote provider frozen) so broken monitoring is loud rather than silently row-less; honors the shared `ccmonitor-ignore` regex list (mirrored stdlib-only) so ignored sessions never alert. Output is length-capped + fully fail-open so a corrupt snapshot can't brick the bar. Replaces the `tmux-dotbar` plugin | `render()`, `main()` |
| `ccsynopsis.py` | **Evolving name** — Stop-hook async EMA summarizer; feeds the prior understanding + title back into `claude -p` Haiku (neutral cwd) so the name drifts slowly. Reads the window **since the user's last message** (`turns_since_last_user`) = "what Claude did since I last spoke". Writes `{sid}.understanding` (hidden moving-average state) + `{sid}.synopsis` (sticky name read by ccstatus) | `--worker <sid> <transcript>` |
| `claude_status.py` | Back-compat shim → `ccstatus.py --serve` (writes `~/.claude/run/status` for claude-island) | `os.execv` |
| `hooks/ccmonitor-hook.sh` | Server hook — maps lifecycle events to working/idle/blocked state files (legacy; ccstatus no longer reads these) | stdin JSON → `~/.claude/run/state/{sid}` |
| `hooks/ccbridge-hook.py` | Bridge hook — sends events to Mac via TCP, handles permission responses | `send_event()`, hookSpecificOutput JSON |
| `setup.sh` | Server setup — installs ccmonitor hook, merges settings.json (idempotent) | one-time install |
| `diagnose.py` | Server diagnostics — dumps pane captures, classifier results | one-shot verification |
| `claude-island/` | macOS notch app (Swift, git submodule) — displays sessions, approves permissions, sends messages | See `claude-island/CLAUDE.md` |

## Data flow

See `.docs_claude/architecture.md` for the full architecture diagram and flows.

## Key design decisions

- **Provider / consumer split**: `ccstatus.py` gathers + normalizes (no display
  opinions); consumers (`ccdash.py`, `ccbar.py`, pipes) own all display/sort/act policy.
  The TUI is the first usability test of the `Session` contract.
- **Remote monitoring = mirror the remote's `status.json`, not query it**: `claude agents
  --json` (and thus `get_sessions()`) is strictly machine-local (resolves pids against local
  `/proc`, correlates with local tmux), so the way to see another box's sessions is to *copy its
  already-normalized snapshot over*. `ccremote.py` SSH-mirrors each configured host's
  `run/status.json` to `run/remote/<host>.json`; consumers fold those in via
  `load_remote_sessions()` (tagging `Session.host`). This reuses the claude-island Mac app's
  "pull the status file over SSH" idiom (`claude-island/remote-ssh-stages.md`) but Python-side,
  and keeps the provider/consumer discipline of the ignore list: the *provider stays local and
  never reaches off-box*; a dedicated syncer produces files; consumers merge them. The mirrored
  file is already the portable `Session[]` contract, so there's **zero schema translation** —
  and pointedly **not** `~/.claude.json` (that's global config/telemetry, not live session
  state). Remote rows are **view-only**: their `pane_id` is the *remote's* tmux id (meaningless,
  possibly colliding, locally), so every local-tmux path — jump, compose, drive, pane capture —
  guards on `s.host`. The one exception is **ack**, which is *not* a tmux drive: `a` on a remote
  row **pushes to the source** — `ccremote.remote_ack` does `ssh <host> touch/rm
  ~/.claude/run/ack/<sid>` (over the existing master), so the remote's own ccstatus recomputes
  `acknowledged` and it flows back through the next mirror (single source of truth; the remote's
  own notch/dashboard agree; transcript-based auto-clear is accurate). Both consumers group remote
  sessions into their own section (a `── remote ──` heading in ccdash, a `│`-divided section in
  ccbar). Liveness is **two clocks**: the mirror's mtime (syncer liveness — older than ~15s ⇒ the
  syncer/host is down, rows dropped) and the sidecar's `remote_status_age_s` (content freshness,
  measured on the *remote's own clock* in the fetch itself — beyond 30s ⇒ the remote's provider
  froze while `ssh cat` kept refreshing the mirror, the "phantom-fresh" hole; rows dropped too).
  Fail-open per host (an unreachable host leaves its previous mirror to age out) — but **fail-open
  is not silent**: broken sync must be *loud*, because dropped rows are indistinguishable from
  "all quiet" (the failure that ate a real 'psc asked a question' alert). ccremote classifies
  every failure into the `.health` sidecar, ccbar shows a red `⚠<host>` segment and ccdash a
  bold-red `⚠ <host> — <why>` line for any host listed in `ccmonitor-remotes` that isn't healthy —
  warn-forever, no decay; the hosts file is the intent declaration and commenting a host out is
  the mute. (Considered and rejected: a message broker/queue — the snapshot model is
  state-not-events, SSH is already reliable transport; the missing pieces were end-to-end
  freshness and loudness, not delivery guarantees.)
- **Ignore list is consumer-side, file-based, shared**: `~/.claude/run/ccmonitor-ignore`
  (override via `$CCMONITOR_IGNORE`) is one regex per line (`#` comments); each pattern is
  searched case-insensitively against a session's `kind`, `title`, and `cwd` independently
  (so `^Smoke test` anchors the title, `background` hides that kind, a repo name hides a tree).
  The **provider never applies it** — `get_sessions()` always emits the full `Session[]` so the
  notch / `--serve` stay complete; only the display consumers drop matches. `ccdash` filters in
  `_apply` (shows an `N ignored` count) via `load_ignore_patterns`/`is_ignored`; `ccbar` mirrors
  the same matcher stdlib-only (no provider import) so an ignored session never alerts in the bar
  either. Edits take effect live (re-read each tick). Fail-open: a bad pattern or missing file
  ignores nothing. The file *is* the headless interface — any editor/script writes it.
- **`status.json` is the full-fidelity feed for status-bar consumers**: the existing
  `run/status` TSV is *lossy* (`SERVE_STATE` collapses `busy`+`shell`→`working` for the
  notch), so `--serve` also writes the raw `Session[]` as `run/status.json`. Cheap
  consumers (the tmux bar) read that cached snapshot instead of re-invoking the provider —
  a fresh `ccstatus --json` per status tick would hammer `claude agents --json`.
- **The tmux bar is quiet until a session needs you**: `ccbar.py` shows nothing unless a
  session needs you, then surfaces **two tiers** — a loud `⛔` (bold yellow) for `blocked`
  and a softer `◆` (magenta) for "your turn" (idle + **engaged** + handed back, not
  acked/dismissed; `engaged` = Claude has produced ≥1 assistant turn, so a freshly opened
  never-answered idle session isn't a false "needs you") —
  each as its own segment naming the session ccdash floats to the top of that tier (ascending
  age) with `+N more`. The tier predicates are duplicated from ccdash (`_awaiting`) rather
  than imported, keeping ccbar stdlib-only. A stale snapshot (dead daemon) shows a faint `⚠`,
  never a false "all clear". `#` in titles is doubled (`##`) because tmux re-parses `#()`
  output as a format string. Defensive by design (it feeds tmux's format parser): non-dict
  rows skipped, titles coerced + length-capped, total output bounded by `MAX_OUT` with a
  trailing `RESET`, and `main()` swallows any exception → empty, so a bug can never brick
  `status-right`.
- **State from `claude agents --json`**, not pane scraping — the supported API gives
  state + Claude-generated title; the old `_classify_pane` regex heuristic is retired.
- **Age (and the ack/dismiss auto-clear) from the last *timestamped* transcript turn**, not
  the file's `st_mtime` and not `sessions/<pid>.json statusUpdatedAt`. `statusUpdatedAt` runs
  *stale* on older CLIs; raw `st_mtime` runs *ahead* because Claude Code rewrites the JSONL in
  place for metadata (`ai-title`, `mode`, `file-history-snapshot`, …) with no new turn — that
  phantom bump both faked a fresh age and raced past an ack marker, re-raising a settled "your
  turn". The metadata records carry no `timestamp`; the newest timestamped entry
  (`_transcript_usage` → `last_turn_ts`, with `st_mtime` as fail-open fallback) ignores them
  while still advancing on a real user/assistant/system turn.
- **Jump from a popup**: a `display-popup -E` is a pure overlay, so `tmux switch-client
  -t <pane_id>` retargets the underlying client; exiting closes the popup.
- **Synopsis is a moving average, not a snapshot**: each Stop step feeds the prior
  `{sid}.understanding` + title back to Haiku and asks it to evolve them *slowly*, so
  the name is sticky while the theme holds and lags through momentary tangents. The
  understanding is the richer hidden state; the title is its sticky projection.
- **Title falls back to the Haiku name**: `title = agents name → {sid}.synopsis (Haiku)
  → short_id`, so sessions Claude Code hasn't auto-named (no approved plan yet) show a
  readable title instead of an opaque code. The `synopsis` *column* then carries the
  richer `{sid}.understanding` text so the two columns stay distinct.
- **Responded-to / dismiss are touch-files that auto-clear on activity**: `ccdash` writes
  `~/.claude/run/{ack,dismissed}/<sid>`; `ccstatus` derives `acknowledged`/`dismissed` by
  comparing each marker's mtime against the last *timestamped* transcript turn (not raw
  `st_mtime` — see the age note above), so a stale mark clears the moment the session takes a
  real new turn, but an in-place metadata rewrite no longer falsely re-arms it. `acknowledged`
  also maps `blocked→idle` in
  `--serve` (quiets the notch); `dismissed` is ccdash-only (hidden row; `D` reveals).
- **Synopsis is async + neutral-cwd**: the Stop hook detaches `claude -p` (Haiku) so it
  never blocks; the neutral cwd + `--exclude-dynamic-system-prompt-sections` keep the
  summarized project's CLAUDE.md out of the summarizer's context.
- **Summary window = since the user's last message**: `turns_since_last_user` (in `ccstatus`)
  anchors the summarizer/reader window at the last genuine human turn (type=`user`, not
  meta/sidechain, prose not a `tool_result`/wrapper), so the understanding reads as "what
  Claude has done since I last spoke" — the slice you scan before replying. Fail-open: if no
  user turn is in the tail, it falls back to the last-N turns.
- **Responding is a file-based outbox, send-keys is the only delivery**: `ccsend` is the
  mechanism — the representation of an input is a JSON file under `run/outbox/<sid>/`
  (`{"type":"text"|"keys"}`) that *any* program can write (the ccdash TUI is one writer, the
  `ccsend` CLI another). Delivery is a single tmux `send-keys` seam (the only reliable way to
  drive an attached session — same as the Mac app's `ToolApprovalHandler`). `text` = literal
  text + Enter (a message, or a single-choice digit "1" to accept a plan / answer a question /
  approve a permission); `keys` = named tmux keys for arbitrary menus. ccdash exposes a compose
  box (`c`) and a key-passthrough **drive mode** (`v`, for multi-select & any in-terminal menu);
  it drains the outbox each refresh tick. Fail-open (ssh-bridge-bugs.md #4): never deliver
  something unparsed, never crash on a tmux error, never blind-retry a partial send.
- **Reader vs glance peek**: the inline peek is a quick at-a-glance grab; `p` opens a
  full-screen **scrollable** reader (running understanding + the pending question/plan with its
  real options + the conversation since your last message, untruncated + the live pane tail)
  for serious reading.
- **Five states**: `blocked`, `busy`, `shell`, `idle`, `dead` (mapped back to
  working/blocked/idle by `--serve` for claude-island). `blocked` = "needs you": a
  background `state=blocked` **or** an interactive `status=waiting` — which covers a
  permission prompt, an AskUserQuestion menu, and a plan-approval menu alike (all three
  are reported identically as `waitingFor="permission prompt"`, verified live). The reason
  rides along in `Session.waiting_for` and shows as a yellow "⏳" line in ccdash's peek.
  The consumer-derived **"your turn"** tier sits just below `blocked`: an `idle` session that
  is also `engaged` (Claude produced ≥1 assistant turn) and not acked/dismissed — the `engaged`
  gate excludes a brand-new idle session that was never answered, which `idle` alone can't.
- **Atomic file writes**: tmp + `os.replace` everywhere; everything fail-open.
- **Transport swap**: `send_event()` in ccbridge-hook.py is the single TCP/Unix swap point
- **Port discovery**: hook reads `~/.claude/run/bridge_port`; missing file = no bridge = exit 0
- **No hook uninstall**: hooks are harmless when bridge is down (can't connect → exit 0)
- **Stale tunnel cleanup**: `SSHTunnelManager` kills orphaned `ssh -N` processes on startup

## Where to look next

Documentation, plans, style guidance, and investigation notes live in `.docs_claude/`.

- `.docs_claude/plans/active/` — plans currently in progress
- `.docs_claude/plans/completed/` — finished plans (includes SSH bridge plan)
- `.docs_claude/style-and-beliefs/` — code style and design principles
- `.docs_claude/architecture.md` — system architecture and data flow diagrams
- `.docs_claude/progress.md` — stage history and what's been built
- `ssh-bridge-bugs.md` — bugs found during SSH bridge development
- `claude-island/CLAUDE.md` — Swift app build commands and architecture

## Plans & workflow

Plans are first-class artifacts in `.docs_claude/plans/`.

- **Small change** (one file, obvious fix): no plan needed.
- **Medium change** (new feature, wire up a subsystem): lightweight plan in `plans/active/`.
- **Complex change** (new architecture, pipeline redesign): full execution plan with goal, approach, staged checklist, and decision log in `plans/active/`.

Move completed plans to `plans/completed/`.

A topic-organized + chronological index of every plan lives at
`.docs_claude/PLANS_TOC.md` (per-plan abstract + "Key changes" list).

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

**Before planning any new implementation:**
1. Read `plans/active/` — don't duplicate in-progress work.
2. Read `plans/completed/` — learn from past decisions and avoid re-solving solved problems.
3. Read relevant docs in `.docs_claude/` — context that shaped the current design.

## Core beliefs

Before planning any implementation, read `/reusable-parts` and apply its guidelines to the design.
