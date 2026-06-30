# Title prefers the Haiku synopsis over Claude Code's name (DONE)

**Status: DONE 2026-06-30** (branch `ccdash`). One-line change in `ccstatus.py`.

## Context — the odd `<repo>-<hash>` session names

Sessions in the notch / ccdash were showing opaque names like `refseg-workspace-c8`,
`refseg-workspace-60`, `ccmonitor-d8`. These are **not** from Haiku and **not** ccmonitor's
`short_id` fallback — they come straight from `claude agents --json` as the `name` field.
That value is **Claude Code's own default placeholder** for a session it hasn't auto-titled
yet: `<cwd-basename>-<short-hash>` (the 2-char suffix is an internal CC hash, unrelated to
the visible session id — `ccmonitor-d8` ↔ sid `4449928e…`). Once a session does something
nameable (approved plan, etc.) Claude Code swaps in a descriptive title.

The old title chain was:

```python
title = rec.get("name") or _read_state(sid, ".synopsis") or sid[:8]
```

Because Claude Code's `name` is **never empty** (always at least the placeholder), the
`or .synopsis` branch was effectively dead — the Haiku synopsis could never win. Yet Haiku
had already produced good names that were being masked, e.g. `refseg-workspace-60` →
"Persistent splits mechanism validation", `refseg-workspace-c8` → "M2F dataset wrapper
architecture for filtering".

## Decision — always prefer the Haiku synopsis

Considered (and rejected) a placeholder-pattern detector (`_is_default_name`) that would
only defer to the synopsis when `name` matched `<basename>-<hex>`. The user chose the
simpler rule: **always use the Haiku name when one exists**, regardless of whether the
agents `name` is a real title or a placeholder.

## Change

Single edit in `ccstatus.py` — reorder the title fallback so the synopsis comes first:

```python
title=_read_state(sid, ".synopsis") or rec.get("name") or sid[:8],
```

Plus the `Session.title` contract comment (`ccstatus.py:62`): `Claude name → haiku name →
short_id` became `haiku name → Claude name → short_id`.

New order: **haiku synopsis → Claude Code name → short_id**. Sessions with no synopsis yet
still fall back to the agents name, then `sid[:8]`. The `synopsis` *column* is untouched
(still shows the richer `understanding`), so the two columns stay distinct.

**Tradeoff (accepted):** sessions that *did* earn a real Claude Code title now also show the
Haiku name instead — e.g. `mask2former-gligen-segmenter-implementation` renders as "Training
M2F glichen-segmenter". The user opted for Haiku unconditionally.

## Verification

`python ccstatus.py` after the edit confirmed the placeholder titles were replaced by their
Haiku synopses; sessions with no synopsis still showed the agents name / short_id. The
`--serve` daemon was restarted so `status.json` (consumed by ccbar/notch) serves the new
title order.

## Files

- `ccstatus.py` — title fallback reorder (line ~509) + `Session.title` doc comment (line 62).

## Key changes

- `~` `ccstatus.py` title fallback: `name → synopsis → short_id` ⇒ `synopsis → name → short_id`
- `~` `ccstatus.py` `Session.title` contract comment updated to match
