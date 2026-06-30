# ccbar — surface the "your turn" tier alongside blocked (COMPLETED)

**Status: COMPLETED 2026-06-29** (branch `ccdash`, commit `40623ae`).

## Context

`ccbar.py` (Consumer #2, the tmux `status-right` segment) was *quiet-until-needed* but only
for `blocked` sessions — it showed `⛔ <title> +N more` and nothing else. The stack already
models a softer, separate attention tier, **"your turn"**: an idle session Claude has handed
back to you (`_awaiting(s)` in ccdash). A session waiting on your reply but not hard-blocked
never surfaced in the bar. Goal: add it as a second, distinct segment.

## What was built

- **Two tiers in `render()`**: a loud `⛔ <title> +N more` (bold yellow) for `blocked`, and a
  separate `◆ <title> +M more` (magenta) for "your turn", joined by 3 spaces; empty when
  nothing needs you. Each segment names the session ccdash floats to the top of that tier
  (ascending age).
- **`_awaiting` mirrored stdlib-only** from ccdash (`state=="idle" and not acked/dismissed`) —
  ccbar must not import the provider, so the predicate is deliberately duplicated (same as the
  blocked filter).
- **Defensive hardening** (it feeds tmux's format parser): non-dict rows skipped, titles
  coerced via `str(...)`, total output clamped to `MAX_OUT=200` with a trailing `RESET` (no
  dangling `#[...]`), and `main()` wrapped in a catch-all → empty output so a bug can never
  brick `status-right`.

## Design decisions

- **Blocked stays first/loud, your-turn second/soft** — mirrors ccdash's tier order. tmux has
  no `purple`, so `magenta` is the closest named color to ccdash's `AWAIT_STYLE`.
- **No provider change** — `status.json` already carries `state`/`acknowledged`/`dismissed`.
- **Belt-and-suspenders on top of the existing fail-open `_load`** — the user explicitly didn't
  want a malformed snapshot to brick the bar.

## Verification (all passed)

- `render()` matrix: empty / blocked-only / your-turn-only / blocked+2-awaiting (glyphs, counts,
  colors) / acked+dismissed excluded / stale `⚠` / `#`→`##` / non-dict rows / non-str title /
  oversized-title clamp ends in `RESET` / `main()` swallows an injected exception.

## Files

`ccbar.py` (`_awaiting`, `TURN`/`TURN_GLYPH`, `_segment`, rewritten `render`/`main`, `MAX_OUT`),
`CLAUDE.md` (ccbar row + "quiet until needed" bullet).
