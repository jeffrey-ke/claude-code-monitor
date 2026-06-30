# ccdash — jumping into a "your turn" session auto-acks it (COMPLETED)

**Status: COMPLETED 2026-06-29** (branch `ccdash`, commit `7963b86`). Verified live.

## Context

In ccdash, `enter`/`o` (jump) ran `tmux switch-client` and exited the popup but did **not**
ack the session (`action_jump`). For a soft "your turn" (purple `◆`) row, that meant it kept
showing purple in the dashboard *and* the ccbar `◆` segment even after you'd gone there to deal
with it. The user wanted visiting a your-turn session to count as handling it.

## What was built

`action_jump` now touches `ACK_DIR` after a successful `switch-client`, **only** when the row
is the soft tier (`_awaiting(s)`):

```python
if _awaiting(s):                      # visiting a "your turn" session = handling it;
    _touch(ACK_DIR, s.session_id)     # blocked (⛔) stays alerting until truly resolved
```

Reuses the existing `_awaiting` predicate, `_touch`, and `ACK_DIR`. No provider/contract change.

## Design decisions

- **`acknowledged` = "attention handled", not strict "responded-to"** (settled with the user).
  The manual `a` mute already establishes this loose reading, so a deliberate jump is a valid
  handling signal. Accepted tradeoff: glance-but-don't-reply leaves it muted until the session's
  next write re-clears it (mtime compare).
- **Blocked (⛔) is excluded** — a permission/plan/question is a real request that should keep
  alerting (dashboard, ccbar, notch via `--serve`'s `blocked→idle` ack map) until truly handled.
- **Headless interface**: ack's interface is the filesystem contract `run/ack/<sid>`; written
  via the in-process `_touch` (same seam `a`/`c` use). Not routed through `ccsend` (that's the
  input/keystroke mechanism). Noted asymmetry: ack has no `ccack` CLI the way inputs have
  `ccsend` — a possible follow-up, out of scope here.
- **Fail-open**: placed after the successful switch and behind the existing early returns, so a
  failed switch or a background/no-pane session writes no ack and never crashes.

## Verification (live)

Idle un-acked session went purple; `enter` switched in and the row cleared from the dashboard
and the ccbar `◆` segment, with `run/ack/<sid>` present. Blocked rows were not acked. Compiles;
`--selftest` passes.

## Files

`ccdash.py` (`action_jump` + module-header doc line).
