# your turn — suppress false positives on never-answered idle sessions (COMPLETED)

**Status: COMPLETED 2026-06-29** (branch `ccdash`).

## Context

The "your turn" tier (ccdash purple `◆`, ccbar `◆` segment) is `_awaiting(s)` =
`idle and not acked and not dismissed`. That over-fired: a **freshly started session you just
opened is already `idle`** (Claude hasn't been asked anything yet), so it was flagged as needing
a reply though nothing was ever handed back. The user saw false positives "when I start and make
a new session." `idle` alone can't tell "Claude finished a turn and handed back" from "this
session never produced a response."

## What was built

A provider-computed boolean **`engaged`** on the `Session` contract — the transcript has ≥1
assistant turn (Claude has actually responded) — required by both consumers' `_awaiting`.

- **`ccstatus.py`**: `_transcript_usage()` now returns `(model, ctx, engaged)` — it already
  scans the tail in reverse for the last assistant message, so `engaged=True` once any assistant
  turn is seen (**no extra I/O**). New `Session.engaged` field (defaulted for snapshot
  back-compat), populated in `get_sessions()`; a session with no transcript file → `engaged=False`.
- **`ccdash.py` / `ccbar.py`**: both `_awaiting` predicates now require `engaged`
  (`s.engaged` / `s.get("engaged")`).

## Design decisions

- **"Any assistant turn in the tail" suffices** (not the stricter "assistant *after* my last
  message"): a genuine hand-back's final turn *is* an assistant message, so it's always in the
  tail; a never-answered session has none. The rare transient (you just sent a message,
  momentarily idle before Claude starts) self-clears the instant state flips to `busy`, and
  replying via `c`/jump already auto-acks — so the stricter form isn't worth a second parse.
- **The fact lives in the contract, not just ccdash** — `ccbar` reads only `status.json` and
  computes its own `_awaiting`, so it needs `engaged` in the snapshot. Old snapshots lacking the
  key → `.get("engaged")` falsy → no false `◆` until the next `--serve` write (fail-quiet).

## Verification (all passed)

Live: `--json` rows carry `engaged`; the idle session showed `engaged=True` (genuine your-turn),
a never-answered one `engaged=False`. ccbar unit: `idle + engaged=False` → no `◆`; `engaged=True`
→ `◆`; missing key → empty. `blocked` unaffected (alerts regardless of `engaged`). All three
modules compile; `ccdash --selftest` passes.

## Files

`ccstatus.py` (`_transcript_usage` 3rd return, `Session.engaged`, populate in `get_sessions`),
`ccdash.py` (`_awaiting`), `ccbar.py` (`_awaiting`), `CLAUDE.md` (provider row + your-turn /
states notes).
