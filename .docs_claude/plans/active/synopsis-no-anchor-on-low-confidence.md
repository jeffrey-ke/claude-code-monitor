# Plan: Don't anchor the synopsis EMA on a low-confidence first impression

## Goal

A ccdash row was observed showing title `(awaiting plan details)` and synopsis
`(unable to determine — no prior context or plan files found)` for a `~/repo/refseg-workspace`
session that, by the time it was inspected, actually had a full transcript with real content.
Stop this kind of low-signal Haiku output from ever being written as if it were a real
title/understanding, without touching the intentional EMA stickiness once a real title exists.

## Root Cause Analysis

Neither placeholder string is hardcoded anywhere in the codebase (confirmed by grep). They are
literal free-text output from the `ccsynopsis.py` Haiku worker, written into
`~/.claude/run/state/<sid>.synopsis` / `.understanding` and surfaced by `ccstatus.py`'s
`title`/`_synopsis()` fields.

`ccsynopsis.py`'s `_worker()` only guards against a completely *empty* title
(`if not title: return`). When the "recent activity" window fed to Haiku is sparse/low-signal
(e.g. very early in a session, before real task content accumulates — `turns_since_last_user`'s
fail-open fallback can hand back boilerplate), Haiku honestly but uselessly writes something
like the placeholder-sounding text above. That's non-empty, so it passes the guard and gets
written. The synopsis prompt is deliberately a sticky moving average ("keep the TITLE identical
to the prior title... prefer no change" — `ccsynopsis.py:46-54`), which is the right behavior
once a real title exists, but it also means this *first, low-confidence* impression gets fed
back as the prior title on every subsequent Stop hook and never self-corrects.

This is compounded by [[title-prefer-haiku-synopsis]] (completed earlier this session): titles
now *always* prefer the Haiku `.synopsis` over Claude Code's own name unconditionally, so a junk
Haiku first-impression is no longer masked by Claude Code's own placeholder name the way it
would have been before that change — it now surfaces directly in the dashboard.

## Approach

Have Haiku self-report "no real signal yet" via an explicit sentinel (`UNKNOWN`) instead of
guessing a vague placeholder, and treat that sentinel as equivalent to "absent" in `_parse()`.
This requires zero changes to `_worker()` — its existing guards (`if not title: return`,
`if understanding: ...`) already do the right thing once `_parse()` hands back `None`.

### 1. New constant (`ccsynopsis.py`, near `UNDERSTANDING_MAX`/`TITLE_MAX`)

```python
SENTINEL = "UNKNOWN"  # Haiku's explicit "no real signal yet" answer; never written to state
```

### 2. Prompt changes (`ccsynopsis.py` — `SYS` and `PROMPT_TEMPLATE`)

Append to `SYS` (after the existing stickiness sentence): instruct that *if there is no prior
title yet* and the latest activity gives no concrete task/code/plan signal, answer `UNKNOWN`
for both fields instead of guessing — but once a prior title exists, always keep evolving it,
never answer `UNKNOWN` at that point. This scopes the escape hatch tightly to cold starts so it
can never fire once a session has an established title.

Restate the same rule in `PROMPT_TEMPLATE`'s TASK block (matches the existing duplication
between `SYS` and TASK) and document `SENTINEL` as a legal value in the response format spec.
`PROMPT_TEMPLATE` only needs f-string interpolation for `SENTINEL`; the `{prev}`/`{prev_title}`/
`{recent}` fields stay exactly as today's `.format()` call (`ccsynopsis.py:112`) expects.

### 3. Guard logic (`ccsynopsis.py` — `_parse()` only)

After the existing TITLE/UNDERSTANDING extraction and fallback-first-line logic, normalize a
sentinel hit to `None` for each field independently:

```python
if title and title.upper().rstrip(".!") == SENTINEL:
    title = None
if understanding and understanding.upper().rstrip(".!") == SENTINEL:
    understanding = None
```

`.upper()` tolerates case drift; `.rstrip(".!")` tolerates trivial trailing punctuation — an
exact-token comparison, not fragile phrase-matching. Checking independently means a hedged
`UNDERSTANDING: UNKNOWN` alongside a confident `TITLE:` doesn't discard the good title (and
vice versa); `title`'s `None` still acts as `_worker`'s existing master gate for the whole step
(`if not title: return`, `ccsynopsis.py:163-164`), `understanding`'s `None` independently
skips just that file's write (`if understanding:`, line 166) — no change to that gating logic.

No changes needed to `_evolve()`, `_worker()`, or `ccstatus.py` (its `_synopsis()` fallback
chain at `ccstatus.py:427-439` and `turns_since_last_user` fail-open fallback at
`ccstatus.py:303-335` already do the right thing once `ccsynopsis.py` simply declines to write
junk).

### 4. CLAUDE.md doc update

Add one bullet to "Key design decisions", directly after the existing "Synopsis is a moving
average, not a snapshot" bullet (`CLAUDE.md:96-99`), matching its terse style:

```markdown
- **Low-confidence first impressions are never anchored**: with no prior title yet, if the
  latest activity gives Haiku nothing concrete to go on, the prompt has it answer the literal
  sentinel `UNKNOWN` for both fields instead of guessing a vague placeholder; `_parse` maps
  that back to `None`, so the existing `if not title: return` skips the write — the moving
  average above only ever anchors on genuine signal, not a "no context yet" first impression.
```

## Critical files

- `ccsynopsis.py` — `SENTINEL` constant, `SYS`, `PROMPT_TEMPLATE`, `_parse()`
- `CLAUDE.md` — one new bullet (~line 96-99)
- `ccstatus.py` — read-only reference only, no changes

## Verification

- `python3 ccsynopsis.py --worker <sid> <transcript>` against a constructed sparse-activity
  transcript (no prior `.synopsis`/`.understanding`, minimal/boilerplate recent turns):
  confirm `_parse()` returns `(None, None)` and that `~/.claude/run/state/<sid>.synopsis` /
  `.understanding` are NOT created.
- Same CLI against a transcript with sparse recent activity but a pre-existing real
  `.synopsis` file: confirm the title is left unchanged (sticky), not reset to `UNKNOWN` then
  skipped — SYS instructs Haiku to keep evolving once a prior title exists.
- Against a transcript with genuine concrete first-task content (no prior title, but real
  signal): confirm a real title/understanding is still produced — the sentinel must not fire
  just because it's a new session.

## Key changes

- `~` `ccsynopsis.py`: `SYS` + `PROMPT_TEMPLATE` instruct Haiku to answer `UNKNOWN` for both
  fields on a no-signal cold start instead of guessing a vague placeholder
- `~` `ccsynopsis.py` `_parse()`: normalize a sentinel hit to `None` per field (case/punctuation
  tolerant exact-token match), so existing `_worker()` guards skip the write unchanged
- `+` `CLAUDE.md`: new "Low-confidence first impressions are never anchored" design-decision
  bullet
