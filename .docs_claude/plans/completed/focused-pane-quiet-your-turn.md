# Quiet ◆ for the focused pane — "seen = handled"

Status: **implemented** on branch `ccdash`. Approved plan copy:
`~/.claude/plans/can-we-plan-to-jaunty-key.md`.

## Why

The consumers alert on two tiers: ⛔ blocked (needs intervention) and ◆ "your turn" (Claude
finished and handed back). The ◆ tier is valuable for background sessions but pure noise when
the user is *already viewing that pane live* — the alert announces a response they're watching
finish. The fix generalizes [[ccdash-jump-autoack-your-turn]]'s principle ("visiting counts
as handling") from an explicit jump to passive focus, reusing the existing ack-marker
machinery — no new state mechanism. ⛔ is never suppressed (user-confirmed: intervention must
stay loud even when focused).

## Design

- **Fact (provider)**: `Session.focused: bool = False` — an attached tmux client is viewing
  this pane right now. Three flags added to the existing `_tmux_panes` query:
  `#{pane_active}` ∧ `#{window_active}` ∧ `#{session_attached} > 0` (verified on tmux 3.4:
  exactly the viewed pane reads `1 1 1`; a detached session's active pane reads `1 1 0`).
  The numeric flags sit *before* the free-text target in `_PANE_FMT`, parsed with
  `split("\t", 5)`, so a tab in a session name can't shift fields. Pure `_parse_panes`
  primitive; `_tmux_panes` is the thin subprocess shell.
- **Policy (shared predicates + daemon)**, split in `ccstatus`'s consumer-side section:
  - `handed_back(s)` = idle ∧ engaged ∧ ¬acked ∧ ¬dismissed — the raw hand-back; what
    jump-ack and the auto-ack act on. Requiring `idle` is what structurally exempts ⛔.
  - `awaiting(s)` = `handed_back(s) ∧ ¬focused` — what consumers ◆-alert on.
  - `_auto_ack_focused` in the `--serve` tick (the always-on actor): a hand-back that lands
    focused gets the ordinary `run/ack/<sid>` marker — **sticky**, so switching away later
    doesn't re-raise ◆ — and the in-memory row is marked *before* the snapshots are written,
    so ccbar never sees a one-tick unacked focused hand-back. Auto-clears on the next real
    turn via the untouched `_marker_active`. Skips `s.host` rows (a mirrored row's marker
    lives on its host).
- **Fail-open everywhere**: no tmux / list-panes error / old snapshot / old-schema remote
  mirror ⇒ `focused` False ⇒ alert exactly as today. A missing key can cause an extra
  alert, never a missed one.
- Remote rows carry the *remote's* own `focused`; a remote on updated code auto-acks at the
  source and it flows back through the mirror (single source of truth, same as remote-ack).
  No `ccremote.py` change (it copies snapshot bytes verbatim).

## Decision log

- **Auto-ack lives in `_serve`, not `get_sessions`**: `--json`/TSV are read APIs and must
  not mutate ack state as a side effect of a read (any polling script would silently ack
  everything focused). ccdash's live path is covered by the `awaiting()` display gate; the
  marker still lands within one 2s tick for stickiness. Daemon-less ccdash therefore gets
  live-display suppression only — accepted, fails open toward alerting.
- **Jump-ack switched `_awaiting` → `handed_back`**: the popup overlays the focused pane,
  so the focused session's row *is* selectable; with the old predicate (now containing
  `¬focused`) jumping to it would skip the ack.
- **Sticky over display-only** (user-chosen): a hand-back seen live is handled; switching
  away doesn't re-raise. The re-arm path is a genuine *new* turn clearing the marker.
- **⛔ guard is load-bearing**: ccbar's blocked filter drops acknowledged rows, so a wrong
  auto-ack on a blocked row would silently kill the loud tier. `handed_back`'s `idle`
  requirement prevents it; an explicit regression test pins it.
- ccdash's idle-row cell styling (purple ◆ glyph) left alone — a focused hand-back may
  render purple for ≤2s until the ack lands; cosmetic.
- tmux can't see OS-level terminal focus: a focused pane in an unfocused terminal window
  still counts as "watching". Documented limitation.

## What changed

- `ccstatus.py`: `_PANE_FMT` + pure `_parse_panes` (focus flags) + thin `_tmux_panes`;
  `Session.focused` (defaulted → tolerant remote rebuild free, no `_SESSION_DEFAULTS`
  entry); `get_sessions` wires the third tuple element; consumer-side `handed_back` /
  `awaiting`; `touch_marker` (write side of the marker pair, moved from ccdash's `_touch`);
  `_auto_ack_focused` called in `_serve` between `get_sessions()` and the writes.
- `ccdash.py`: imports `handed_back`/`awaiting`/`touch_marker`; local `_awaiting`/`_touch`
  deleted; `_tier`, "yours" count, peek "↩ your turn" line use `awaiting`; jump-ack uses
  `handed_back`; `_clear` stays local.
- `ccbar.py`: the stdlib mirror `_awaiting` gains `and not s.get("focused")`; blocked
  filter untouched.
- `tests/test_your_turn.py` (30 tests): `_parse_panes` table (flag combos, garbage count,
  short line, tab-in-session-name maxsplit regression); lockstep awaiting table run against
  BOTH `ccstatus.awaiting` (via the tolerant `Session(**{**_SESSION_DEFAULTS, **rec})`
  constructor, doubling as the old-schema regression) and `ccbar._awaiting`;
  handed_back/awaiting divergence on a focused hand-back; `_auto_ack_focused` (marker +
  in-memory ack, unfocused skipped, **blocked+focused never acked**, remote skipped,
  idempotent via backdated-mtime check).

## Verified live

`tmux list-panes -a` with the new format showed exactly one `1 1 1` pane (the attached
client's view, `613:0.0`); `ccstatus --json` marked exactly that session `focused: true`.
The running `--serve` daemon self-healed onto the new code; the next snapshot carried
`focused: true, acknowledged: true` for that idle+engaged session with a fresh
`run/ack/<sid>` marker, and `ccbar.py` rendered no ◆ for it (only the pre-existing,
unrelated red `⚠psc` remote-sync warning). Full suite: 107 passed.

## Key changes

- `+` `ccstatus._PANE_FMT` / `_parse_panes`; `~` `_tmux_panes` → thin shell
- `+` `ccstatus.Session.focused` (defaulted fact); `~` `get_sessions` wiring
- `+` `ccstatus.handed_back` / `awaiting` (shared ◆ policy); `+` `touch_marker`
- `+` `ccstatus._auto_ack_focused`; `~` `_serve` calls it before the snapshot writes
- `~` `ccdash.py` — imports the shared predicates/marker; `- _awaiting`, `- _touch`;
  jump-ack on `handed_back`
- `~` `ccbar._awaiting` — `+ not s.get("focused")` clause (mirror, lockstep-tested)
- `+` `tests/test_your_turn.py` — 30 tests incl. the two-copy lockstep table + ⛔ guard
