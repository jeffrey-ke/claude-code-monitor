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
uv run --with pytest -m pytest tests/   # unit tests (ccremote primitives, read_verdict, _alive)
```

The stack is a **provider** (`ccstatus.py`, the single normalized source of truth,
sourced from the supported `claude agents --json` API) feeding **consumers** (the
`ccdash.py` TUI and the `ccbar.py` tmux status-line segment today; any tool that reads
`ccstatus --json` / the `~/.claude/run/status.json` snapshot tomorrow).

## Module index

| Module | Role | Key exports |
|---|---|---|
| `ccstatus.py` | **Provider** — normalized `Session` per live session from `claude agents --json` + tmux/`/proc` (pane id) + roster + synopsis/understanding cache; title falls back name→haiku→code; derives `acknowledged`/`dismissed` from `run/{ack,dismissed}/<sid>` touch-files; sets `engaged` (transcript has ≥1 assistant turn) so consumers can tell a genuine "your turn" hand-back from a never-answered fresh idle session; sets `turn_complete` (the transcript's newest conversational entry is a turn-end marker — pure `_turn_complete_step`, computed in the same `_transcript_usage` tail read); sets `focused` (an attached tmux client is viewing this pane right now — pane active ∧ window active ∧ session attached, parsed by the pure `_parse_panes` from the same `list-panes` call that maps pane ids) and hosts the shared ◆ policy pair `handed_back(s)` (turn-over+engaged+unmarked, where turn-over = `idle` OR `busy`/`shell` with `turn_complete` — so a hand-back whose lingering background shells/agents pin the CLI status still alerts; the yazi bug) / `awaiting(s)` (= `handed_back ∧ ¬focused` — what consumers ◆-alert on; ccdash imports both, ccbar mirrors `awaiting` stdlib-only with a lockstep test table) plus `touch_marker` (the write side of the ack/dismiss markers); transcript helpers `transcript_path` / `turns_since_last_user` / `pending_interaction`; consumer-side ignore-list helpers `load_ignore_patterns` / `is_ignored` and the remote-session loaders `load_remote_sessions` (reads `run/remote/<host>.json` mirrors written by `ccremote.py`, gated on the shared `read_verdict` rule — rows render while the host's verdict is `ok`/`degraded`, the bare mirror-mtime>15s rule survives only as the no-sidecar backstop; rebuilds records *tolerantly* — missing schema fields get typed defaults via `_SESSION_DEFAULTS`, only a `session_id`-less record is dropped — and tags each `Session.host`) + `load_remote_health` (per-configured-host `{state, error, age_s, since}`: state = read_verdict's `ok`/`degraded`/`down`/`missing`/`stale-mirror`/`stale-content`, error = ccremote's fetch class `auth`/`timeout`/`unreachable`/`no-file`/`garbled`/`write-failed`; covers exactly the ccmonitor-remotes hosts so commenting one out is the mute) built on the pure `read_verdict(health, sidecar_age_s)` (the ONE consumer trust rule: stale-mirror at `max(15, 3·interval+ssh_timeout)`, stale-content at `max(30, 5·remote_write_cadence)` — thresholds scale to the sidecar's self-description; ccbar mirrors it stdlib-only, one test table runs against both) — all consumer-side, the provider itself never filters or reaches off-box; `_alive` is identity-checked (`_proc_identity_ok`: same uid + claude/node comm or claude in cmdline), not bare `/proc` existence, so a recycled pid on a multi-login-node host (bridges2: shared `$HOME`, per-node `/proc`) can't freeze a closed session at blocked/busy forever; CLI `--json`/`--watch`/`--serve`/`--state`; `--serve` also auto-acks a hand-back that lands while its pane is focused (`_auto_ack_focused` — "seen = handled": the ordinary sticky ack marker, applied in-memory before the write so ccbar never sees a one-tick unacked focused hand-back; daemon-only policy, never in the `--json` read path), flocks `run/serve.lock` so a second daemon exits loudly instead of silently racing the snapshot writes (`_serve_lock` — two racing daemons ENOENT-spammed ccserve.log for 10 days), logs per-session `(state, turn_complete, awaiting)` *transitions* to stderr, never per-tick (`_log_transitions` — the forensic trail for "why didn't the ◆ fire") and writes both the claude-island TSV (`run/status`, override `$CCSTATUS_STATUS_FILE`) and a full-fidelity `Session[]` snapshot (`run/status.json`, override `$CCSTATUS_STATUS_JSON` — both tiny atomically-overwritten snapshots, not append logs, so relocatable on a small-`$HOME` host) | `Session`, `get_sessions()`, `load_remote_sessions()` |
| `ccremote.py` | **Remote syncer** — mirror other machines' sessions onto this box for the local consumers. Reads a hosts file `run/ccmonitor-remotes` (override `$CCMONITOR_REMOTES`, one `host`/`user@host` per line, `#` comments — same idiom as the ignore list; an optional 2nd whitespace field overrides the remote snapshot path for a host with a small/quota'd `$HOME`, default `$CCMONITOR_REMOTE_STATUS` or `~/.claude/run/status.json`), and for each host runs `ssh <host> cat <remote-status-path>` (the remote's own `--serve` snapshot, already the portable `Session[]` contract) and drops it verbatim at `run/remote/<host>.json` (atomic tmp+replace, SSH ControlPersist for cheap reuse — persist window `$CCREMOTE_CONTROL_PERSIST`, default 12h, matching ccremote-up.sh). The same ssh exec prepends a `#cc# <now> <mtime>` sentinel header — both stamps from the *remote's* clock, so the content age is skew-free — and every tick writes a `run/remote/<host>.health` **v2 sidecar** (non-`.json`, invisible to the mirror globs) carrying a verdict computed once, writer-side, by the pure `make_health` hysteresis ladder: `state: ok|degraded|down` (failure → `degraded`, quiet-ish; `down` only at 3 consecutive fails / a ≥25s streak / 2 fails for `auth`), plus `bad_since`, `interval_s`+`ssh_timeout_s` (consumers scale their stale threshold to these), and a `remote_mtimes` ring yielding `remote_write_cadence_s` (observed refresh granularity → adaptive frozen-content threshold). `error` classified `auth` (control master gone — rerun ccremote-up.sh; probed via `ssh -O check`) / `timeout` (a stalled exec on a healthy host — NOT unreachable; SSH_TIMEOUT=20 because a healthy psc exec was measured at 12.8s) / `unreachable` / `no-file` / `garbled` / `write-failed`. Built from pure, unit-tested primitives (`build_fetch_cmd`/`parse_sentinel`/`classify_ssh_failure`/`parse_snapshot`/`atomic_write_json`/`make_health`); `sync_once` runs hosts in parallel so one slow host can't stretch the tick. `--serve[=SECS]` daemon (self-heals on source change like ccstatus; logs per-host `(state, error)` *transitions* to stderr, not every tick) + a bare one-shot for debugging; stdlib-only, fail-open per host (an unreachable host leaves its previous mirror to age out — the sidecar, not mirror deletion, carries the bad news). `$CCMONITOR_REMOTE_DIR` overrides the mirror dir (isolated test/worktree runs; NB ControlPath ≤108 bytes). The consumers fold the mirrors in via `ccstatus.load_remote_sessions()`, trusting the sidecar via `read_verdict`. Also exposes `remote_ack`/`remote_dismiss(host, sid, on)` — `ssh <host> touch/rm ~/.claude/run/{ack,dismissed}/<sid>` (shared `_remote_mark` + pure `build_mark_cmd`; reuses the master; `sid` shell-guarded) so ccdash can ack/dismiss a remote row at its source | `load_hosts`, `sync_host`, `sync_once`, `remote_ack`, `remote_dismiss` |
| `ccremote-up.sh` | **Remote-sync launcher** — for a remote that needs a password/2FA. Ensures `run/remote/` exists, brings up the shared SSH master at ccremote's exact ControlPath **once** interactively (long `ControlPersist`, so it outlives poll gaps; reused by every `BatchMode` fetch), then `exec`s `ccremote.py --serve`. Hosts from args or `ccmonitor-remotes`; `$CCREMOTE_CONTROL_PERSIST` / `$CCREMOTE_ARGS` override lifetime / launch mode. Idempotent — skips a host whose master (`ssh -O check`) is already up. A master that later dies surfaces as `error:"auth"` in the health sidecar → red `⚠<host>` in ccbar / "auth needed — rerun ccremote-up.sh" in ccdash; rerunning this script is the fix | one-command startup |
| `ccsend.py` | **Input mechanism** — deliver an input into a session via tmux `send-keys`; headless-first **outbox** contract `run/outbox/<sid>/<uniq>.json` (`{"type":"text"\|"keys",…}`) any program can write; `enqueue`/`deliver`/`drain` + a `ccsend <sid> "msg"` / `--keys` / `--drain` CLI; fail-open | `enqueue`, `deliver`, `drain` |
| `ccdash.py` | **Consumer #1** — Textual TUI (PEP-723 `uv run --script`); blocked-first table; `c`=respond (compose → text+Enter), `v`=drive (keypress→pane passthrough for multi-select/any menu), `p`=full-screen scrollable reader; `a`=responded-to (mutes orange), `d`=hide row, `D`=show-hidden; `enter`=jump via `tmux switch-client -t <pane_id>` (auto-acks a your-turn row — on `handed_back`, not `awaiting`, so the focused row under the popup acks too); "your turn" count/tier/peek-line use the imported `ccstatus.awaiting`, so the focused session (the pane you're in) never counts as needing you; merges remote sessions from `load_remote_sessions()` under a `── remote ──` section heading (an inert `_Section` row; title shown as `host:<name>`, TMUX column shows the remote's own target in magenta; view-only — jump/respond/drive gated since the remote `pane_id` isn't a local pane, but `a`=ack and `d`=dismiss **push to the host over SSH** (threaded via `_push_remote_mark` → `ccremote.remote_ack`/`remote_dismiss`, off the event loop so a slow ssh never freezes the TUI) so the remote's own ccstatus recomputes `acknowledged`/`dismissed`, with an **optimistic overlay** — the row de-oranges/hides instantly, dissolves when the mirror agrees / at a 20s TTL, and reverts if the push fails); shows a bold-red inert `⚠ <host> — <why> (age)` line under the remote heading for every configured host whose sync is confirmed broken, and a dim `⚠ <host> — sync degraded: <why>` note for a mere blip (rows still rendered) — `load_remote_health()` verdict+error → `_health_line`; the section renders even with zero live remote rows, so vanished rows are never mistaken for all-quiet; honors the shared `ccmonitor-ignore` regex list (filters rows, `N ignored` count); drains the outbox each tick; runs in `display-popup` | `CCDash` |
| `ccbar.py` | **Consumer #2** — tmux `status-right` segment (plain `python3`, stdlib-only so it spawns fast each `status-interval`); reads the `run/status.json` snapshot **plus any `run/remote/<host>.json` mirrors** (gated on its stdlib `_read_verdict` mirror of ccstatus's rule — rows render for `ok`/`degraded` verdicts, mirror-mtime only as the no-sidecar backstop; remote sessions render as their own section after a dim `│` divider, each labeled `host:<title>`) and prints a *quiet-until-needed* alert across **two tiers**: `⛔ <title> +N more` (bold yellow) for `blocked`, and a separate `◆ <title> +M more` (magenta) for "your turn" (idle, handed back, not acked/dismissed, and not `focused` — the pane you're watching live never soft-alerts; mirrors `ccstatus.awaiting`); empty when nothing needs you. Local and remote are loaded *independently*: a missing/stale local snapshot shows the faint `⚠` and drops only local rows — never the remote sections. A red `⚠<host>[,<host>] +N` segment (`_unhealthy_hosts()`, warn-forever) names every configured host whose verdict is *confirmed* red — `missing`/`stale-mirror`/`stale-content`/`down`; a `degraded` blip stays out of the bar (hysteresis: one slow ssh tick must not flash red) — so broken monitoring is loud rather than silently row-less; honors the shared `ccmonitor-ignore` regex list (mirrored stdlib-only) so ignored sessions never alert. Output is length-capped + fully fail-open so a corrupt snapshot can't brick the bar. Replaces the `tmux-dotbar` plugin | `render()`, `main()` |
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

- **Two alert types (canonical terminology)**: **type 1 (T1)** = ⛔ intervention (bold
  yellow) — Claude is stopped *mid-turn* and cannot proceed without you: permission
  prompt, AskUserQuestion menu, plan approval; code predicate `state == "blocked"`.
  **type 2 (T2)** = ◆ response-complete (magenta) — Claude *completed its turn* and
  handed control back; you owe it a reply; code predicates `handed_back`/`awaiting`.
  Docs and comments say "type 1 alert" / "type 2 alert"; code identifiers keep their
  descriptive names. "The yazi bug" = a type 2 alert lost to background-work status
  pinning (see the turn-completion bullet below).
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
  guards on `s.host`. The exceptions are the **markers** — ack and dismiss — which are *not* tmux
  drives: `a`/`d` on a remote row **push to the source** — `ccremote.remote_ack`/`remote_dismiss`
  do `ssh <host> touch/rm ~/.claude/run/{ack,dismissed}/<sid>` (over the existing master), so the
  remote's own ccstatus recomputes `acknowledged`/`dismissed` and it flows back through the next
  mirror (single source of truth; the remote's own notch/dashboard agree; transcript-based
  auto-clear is accurate — a *local* marker would be a silent no-op, since remote rows are
  rebuilt from the mirror every tick). Both consumers group remote
  sessions into their own section (a `── remote ──` heading in ccdash, a `│`-divided section in
  ccbar). **The verdict is data, computed once at the writer**: ccremote's pure `make_health`
  ladder writes `state: ok|degraded|down` into the v2 sidecar with hysteresis — a failed tick is
  `degraded` (quiet-ish: rows stay rendered, ccdash shows a dim note, ccbar shows nothing);
  `down` (red, warn-forever) needs 3 consecutive fails / a ≥25s streak / 2 fails for `auth`.
  One slow ssh exec (measured 12.8s on a *healthy* psc during a busy episode — hence
  SSH_TIMEOUT=20 and the distinct `timeout` class) can no longer flash a false red ⚠. Consumers
  keep only the two read-side judgments a writer can't make about itself, in the shared pure
  `read_verdict`: sidecar too old ⇒ `stale-mirror` (threshold `max(15, 3·interval+ssh_timeout)`
  from the sidecar's own self-description) and remote file frozen on the *remote's* clock ⇒
  `stale-content` (threshold `max(30, 5·remote_write_cadence)` from the sidecar's observed
  write-cadence ring — the "phantom-fresh" hole where the remote provider died but `ssh cat`
  kept refreshing the mirror). Fail-open per host (an unreachable host leaves its previous
  mirror to age out) — but **fail-open is not silent**: broken sync must be *loud*, because
  dropped rows are indistinguishable from "all quiet" (the failure that ate a real 'psc asked
  a question' alert). ccbar shows a red `⚠<host>` segment and ccdash a bold-red
  `⚠ <host> — <why>` line for any host listed in `ccmonitor-remotes` whose verdict is red —
  warn-forever, no decay; the hosts file is the intent declaration and commenting a host out is
  the mute. (Considered and rejected: a message broker/queue — the snapshot model is
  state-not-events, SSH is already reliable transport; the missing pieces were end-to-end
  freshness and loudness, not delivery guarantees.)
- **`_alive` is an identity check, not a `/proc` existence check**: on a multi-login-node host
  (bridges2: round-robin login nodes, shared `$HOME` on Lustre, per-node `/proc`) the agents
  list is machine-shared while pids are node-local, so a closed session's recycled pid kept it
  frozen at `blocked`/`busy` forever — a permanent phantom "needs you" (the orange-ghost bug).
  `_proc_identity_ok` requires same-uid AND a claude-ish command (comm `claude`/`node` or
  `claude` in cmdline). False-dead beats false-blocked: a genuinely cross-node session shows
  grey `dead`, never a phantom alert. `pid None` stays vouched (blocked background sessions
  have no live process by design).
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
  and a softer `◆` (magenta) for "your turn" (turn over + **engaged** + handed back, not
  acked/dismissed, not **focused**; turn over = `idle` OR `busy`/`shell` with
  **turn_complete** — see the turn-completion bullet; `engaged` = Claude has produced ≥1
  assistant turn, so a freshly opened never-answered idle session isn't a false "needs
  you"; `focused` = the pane you're watching live, which never soft-alerts — see "Seen =
  handled" below) —
  each as its own segment naming the session ccdash floats to the top of that tier (ascending
  age) with `+N more`. The ◆ predicate mirrors `ccstatus.awaiting` rather than importing it,
  keeping ccbar stdlib-only (a lockstep table in `tests/test_your_turn.py` runs against both
  copies). A stale snapshot (dead daemon) shows a faint `⚠`,
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
- **Seen = handled — the focused pane never soft-alerts**: a "Claude finished" ◆ for the pane
  you're watching live is noise, so the tier splits into a fact and a policy. The fact is
  `Session.focused` — an attached tmux client is viewing this pane *right now* (`pane_active`
  ∧ `window_active` ∧ `session_attached > 0`, three flags on the same `list-panes` call that
  maps pane ids; pure `_parse_panes`). The policy is `awaiting = handed_back ∧ ¬focused`
  (`handed_back` = idle+engaged+unmarked) in both consumers, **plus stickiness**: the `--serve`
  daemon — the always-on actor, same precedent as its acked-`blocked→idle` notch mapping —
  writes the *ordinary* ack marker for a hand-back that lands focused (`_auto_ack_focused`,
  in-memory before the snapshot write, so ccbar never flashes a one-tick ◆), generalizing
  jump-auto-ack's "visiting counts as handling" to passive focus. Switching away later doesn't
  re-raise ◆; the marker auto-clears on the session's next real turn as always. ⛔ blocked is
  exempt **by construction** (`handed_back` requires `idle` — load-bearing: ccbar drops acked
  blocked rows, so a wrong auto-ack would silently hide a permission prompt; regression-tested)
  and deliberately keeps alerting even when focused. Fail-open throughout: no tmux / an old
  snapshot / an old-schema mirror ⇒ `focused=False` ⇒ alert as before — a missing key can cost
  an extra alert, never a missed one. Auto-ack skips mirrored rows (their marker lives on their
  host; a remote on current code auto-acks at the source and it flows back through the mirror).
  Known limit: tmux can't see OS-level window focus — a focused pane in a backgrounded
  terminal still counts as "watching".
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
  The consumer-derived **"your turn"** tier (type 2, ◆) sits just below `blocked`: a session
  whose turn is over (`idle`, or `busy`/`shell` with `turn_complete`) that is also `engaged`
  (Claude produced ≥1 assistant turn), not acked/dismissed, and not `focused`
  (you're not already watching it — see "Seen = handled") — the `engaged` gate excludes a
  brand-new idle session that was never answered, which `idle` alone can't.
- **Hand-back = turn completion, not `state == "idle"` (the yazi bug)**: when a turn ends
  while background shells/agents are still alive, the CLI pins the session status at
  `shell`/`busy` (observed live: `sessions/<pid>.json` flipped to `shell` at the exact
  stop-hook timestamp; `claude agents --json` reported `busy`) — so gating ◆ on `idle`
  made that hand-back structurally invisible: Claude asked a question, sat at the `❯`
  prompt for hours, and no alert ever fired. The truth is in the transcript: a completed
  turn ends with `system/turn_duration` + `stop_hook_summary` markers, so the provider
  derives `turn_complete` (pure `_turn_complete_step`, same `_transcript_usage` tail read,
  no extra I/O) = the newest *conversational* entry is a turn-end marker — skipping
  untimestamped metadata, sidechain chatter, `system/local_command`, and wrapper/tool_result
  user entries (a post-hand-back `/rename` must not mask it), while a genuine user prompt
  or an assistant entry reads "turn live". `handed_back` then accepts `idle` OR
  `busy`/`shell`+`turn_complete`; `state` itself stays as-reported (the notch keeps showing
  "working" — background work *is* running; sort order unchanged). Fail-open toward "no
  alert": an old CLI without turn-end markers behaves exactly as before. ⛔ blocked is
  still structurally exempt from ◆/auto-ack. Accepted imperfection: a parent auto-resuming
  after a background task can read `turn_complete=True` for the few seconds before its
  first assistant entry lands — a brief false ◆ that self-corrects.
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
