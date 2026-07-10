# Missed ◆ hand-back alert (yazi session) — root cause + fix

## Context

The `yazi` session (`fe5c3b64`, `claude:0.0`, pane `%1301`) finished its turn at
**10:21:16** — transcript ends with the final assistant text, then
`system/stop_hook_summary` + `system/turn_duration` — and sat at the `❯` prompt waiting
for the user. No ◆ "your turn" alert ever appeared in ccbar/ccdash.

### Root cause (confirmed by live forensics, not a hypothesis)

The pane's status line read **"2 shells still running · ← 1 agent"**. When the turn
ended, Claude Code (CLI 2.1.206) set the session status to `shell` — not `idle` —
because background shells/agents were still alive
(`~/.claude/sessions/3069779.json`: `"status":"shell"`, `statusUpdatedAt` = exactly the
stop-hook timestamp). `claude agents --json` reports this as `"status":"busy"`,
`_normalize_state` (ccstatus.py:454) passes `busy`/`shell` through verbatim, and
`handed_back` (ccstatus.py:541) hard-requires `state == "idle"`. So the hand-back was
structurally invisible: **a session that hands back while background shells/agents are
still running never produces a ◆**, no matter how long it waits.

### Exonerated

- **The focused-pane feature is NOT involved.** The row had `focused: false`,
  `acknowledged: false`, and no ack/dismiss marker exists for `fe5c3b64`.
  `_auto_ack_focused` only acts on `handed_back` rows, which this never became.
- Not the ignore list (no pattern matches), not a stale snapshot (status.json was 2s
  fresh), not `engaged` (true).

### Secondary findings (fold into the fix)

1. **The serve log exists but is useless for this.** `~/.claude/run/ccserve.log` is the
   stdout/stderr of an *old* `--serve` daemon (pid 475643, running since Jun 30, cwd
   `~/repo/ccmonitor`). It contains only `status.tmp -> status` ENOENT spam: **two
   daemons** (475643 and 3041103, the one in tmux session `ccmonitor`) race on the same
   tmp path; the loser's `os.replace` fails every tick. No state history is logged
   anywhere — that's why "look in the serve log" had no answer.
2. **Reproduction recipe** (verified mechanism): in any claude session, ask Claude to
   start a long-running background shell (e.g. "run `sleep 600` in the background, then
   tell me a word and stop"). When the turn ends, `claude agents --json` keeps reporting
   `busy` while the prompt is ready → no ◆, no your-turn row in ccdash. Kill the sleep →
   status flips idle → alert appears. (A lingering background *agent* reproduces it the
   same way.)

## Terminology (user's choice: type 1 / type 2)

Canonical terms are **type 1 (T1)** and **type 2 (T2)**, defined once in a CLAUDE.md
glossary table so the mapping is never ambiguous:

| Term | Glyph | Meaning | Code predicate |
|---|---|---|---|
| **type 1 (T1)** — intervention | ⛔ (bold yellow) | Claude is stopped **mid-turn** and cannot proceed without you: permission prompt, AskUserQuestion menu, plan approval | `state == "blocked"` |
| **type 2 (T2)** — response-complete | ◆ (magenta) | Claude **completed its turn** and handed control back; you owe it a reply | `handed_back` / `awaiting` |

Docs and comments say "type 1 alert" / "type 2 alert" (glyph in parentheses on first
use). Code identifiers stay as they are (`blocked`, `handed_back`, `awaiting`).
Bug shorthand: "the yazi bug" = a type 2 alert lost to background-work status pinning.

## Fix design

Policy-level fix, not state-level: keep `Session.state` exactly as the CLI reports it
(the notch's `SERVE_STATE` busy→working mapping, sort order, and the state column stay
truthful — background work *is* running). What changes is the hand-back predicate: the
signal "the turn is over" comes from the transcript, which we already read every tick.

### 1. Derive `turn_complete` from the transcript tail (`ccstatus.py`)

New pure helper (unit-testable on synthetic JSONL) + wire into the existing tail read in
`_transcript_usage` (ccstatus.py:312) so there is **no extra I/O** — same 256 KiB tail,
same reversed walk. Rule, walking newest→oldest over parsed entries:

- skip: entries with no `timestamp` (mutable metadata: `custom-title`, `agent-name`,
  `file-history-snapshot`…), `isSidechain` entries (background-agent chatter),
  `system/local_command` (a `/rename`—`/model`-style command after hand-back must not
  mask it — the yazi tail literally ends with two of these plus a wrapper user entry),
  and non-genuine user entries (reuse `_is_genuine_user`/`_entry_text`,
  ccstatus.py:357–374 — catches tool_results and `<system-reminder>` wrappers).
- **True** ⇔ first non-skipped entry is `system` with subtype `turn_duration` or
  `stop_hook_summary` (the turn-end markers, present in CLI ≥2.1.x).
- **False** on anything else: a genuine user turn (prompt submitted, Claude about to
  run), an `assistant` entry (mid-generation), or any unrecognized type (fail toward
  "no alert", i.e. today's behavior — an old CLI without turn-end markers regresses to
  exactly the current behavior, never a false ◆).

Add `turn_complete: bool = False` to `Session`. Old snapshots / old-schema remote
mirrors get the typed default via `_SESSION_DEFAULTS` → falsy → unchanged behavior
(same fail-open idiom as `focused`).

Known accepted imperfections (document in the docstring):
- A parent session auto-resuming after a background agent completes may show
  `turn_complete=True` for the few seconds before its first assistant entry lands →
  a brief false ◆ that self-corrects.
- We cannot distinguish "2 shells" from "1 agent" (no per-kind counts anywhere in
  `sessions/<pid>.json` or `agents --json`) — both alert once the turn is over, which is
  what the yazi case wanted (Claude had asked the user a question).

### 2. Widen the hand-back predicate (both copies, lockstep)

`ccstatus.handed_back` (ccstatus.py:541):

```python
return ((s.state == "idle" or (s.state in ("busy", "shell") and s.turn_complete))
        and s.engaged and not s.acknowledged and not s.dismissed)
```

`blocked` remains structurally exempt (⛔ is never auto-acked / never softened — the
existing regression tests still hold). `awaiting = handed_back ∧ ¬focused` is untouched.
`_auto_ack_focused` and ccdash's jump-ack act on `handed_back`, so they extend to these
rows automatically — correct: visiting a turn-complete-but-shells-running session counts
as handling it.

`ccbar._awaiting` (ccbar.py:178) mirrors stdlib-only:

```python
state = s.get("state")
return ((state == "idle" or (state in ("busy", "shell") and s.get("turn_complete")))
        and s.get("engaged") and not s.get("acknowledged")
        and not s.get("dismissed") and not s.get("focused"))
```

### 3. Tests (`tests/test_your_turn.py` + new tail fixtures)

- Extend the lockstep table (runs against both `ccstatus.awaiting` and
  `ccbar._awaiting`): `busy+turn_complete → True`, `shell+turn_complete → True`,
  `busy alone → False`, `blocked+turn_complete → False` (⛔ exemption),
  `idle` rows unchanged, missing-key snapshot → False.
- New pure tests for the tail parser: (a) the exact yazi tail shape (assistant text →
  stop_hook_summary → turn_duration → custom-title/agent-name → 2× local_command →
  wrapper user entry) ⇒ True; (b) trailing genuine user turn ⇒ False; (c) trailing
  assistant/tool_use ⇒ False; (d) trailing sidechain entries skipped; (e) no turn-end
  markers at all (old CLI) ⇒ False.
- Auto-ack: focused busy+turn_complete hand-back gets the marker; blocked never does.

### 4. Diagnosability — make the next missed alert debuggable

- **Transition logging in `--serve`** (the answer to "can you look in the log"): keep a
  `{sid: (state, awaiting)}` dict across ticks; on change print one stderr line, e.g.
  `10:21:16 fe5c3b64 yazi: busy→busy turn_complete=True awaiting=True`. Transitions
  only, never per-tick — same idiom as ccremote's per-host transition log.
- **Single-writer guard**: at `--serve` startup take an exclusive `flock` on
  `run/serve.lock`; if held, print the owner and exit. Ends the silent two-daemon
  rename race permanently.
- **Runtime cleanup** (at implementation time, with the user's approval): kill the stale
  Jun-30 daemon pid 475643; keep 3041103 (the `ccmonitor` tmux pane one). The ENOENT
  spam in `ccserve.log` stops.

### 5. Docs (`CLAUDE.md`)

- Module-index row for ccstatus + "Key design decisions": new bullet — *hand-back is
  turn-completion, not `state == "idle"`*: background shells/agents pin the CLI status at
  `shell`/`busy` after the turn ends (the yazi bug), so ◆ derives from the transcript's
  turn-end markers; state stays as-reported for the notch/sort.
- Terminology glossary: T1 = intervention (⛔) / T2 = hand-back (◆), per the table above.

## Files to modify

- `ccstatus.py` — `_transcript_usage` (+ pure `_turn_complete` helper), `Session` field,
  `get_sessions` wiring, `handed_back`, `--serve` transition log + flock guard
- `ccbar.py` — `_awaiting` mirror
- `tests/test_your_turn.py` — lockstep table rows, tail-parser fixtures, auto-ack cases
- `CLAUDE.md` — design-decision bullet + glossary

## Verification

1. `uv run --with pytest -m pytest tests/` — new lockstep + parser tests pass.
2. **Live repro, before/after**: in a scratch claude session ask for a background
   `sleep 600` + a short answer. Before: `python ccstatus.py --json` shows the session
   `busy`, no ◆ in ccbar's output, no your-turn row in ccdash. After: same state `busy`
   but `turn_complete: true`, ◆ appears (`python3 ccbar.py` prints the segment), ccdash
   shows it in the your-turn tier; `enter`-jump acks it; typing a new prompt in the
   session clears `turn_complete` (genuine user turn) and the alert.
3. The still-live yazi session itself is the acceptance test: with the fix deployed it
   must show `turn_complete: true` and alert until visited.
4. `--serve` restart: transition lines appear on state changes; starting a second
   `--serve` exits immediately with the lock message.

## Outcome (2026-07-10, completed)

Implemented and verified live, end to end:

- The still-running yazi session read `turn_complete: true` on the real transcript
  (including the trailing `/rename` noise) the moment the daemon self-heal-restarted.
- Scratch repro (`claude --model haiku`, "run `sleep 600` in the background, say done"):
  snapshot row `state: busy, turn_complete: true`, ccbar rendered
  `◆ Background task execution te` — previously empty. A follow-up prompt flipped
  `turn_complete=False awaiting=False` (alert cleared), and the next hand-back re-raised
  it — the full lifecycle captured by the new transition log:
  `14:11:24 a501d947 ccrepro-43: busy→busy turn_complete=True awaiting=True`.
- The flock guard resolved the pre-existing two-daemon race *by itself*: both daemons
  re-exec'd on source change, the tmux-pane one (3041103) took `run/serve.lock`, and the
  stale Jun-30 daemon (475643) exited loudly into its own log. One writer remains.
- 134 tests pass (24 new: 6 lockstep table rows, 10 step-vote table, 4 transcript
  fixtures incl. the verbatim yazi tail, 1 auto-ack, plus pane/parser cases).
