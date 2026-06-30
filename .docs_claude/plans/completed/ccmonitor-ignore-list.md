# ccmonitor — shared regex ignore list for the dashboard & tmux bar (COMPLETED)

**Status: COMPLETED 2026-06-29** (branch `ccdash`).

## Context

The dashboard had only a transient `/` filter and per-session `d` dismiss — no *persistent*
way to permanently hide sessions. Background agents (`kind: background`) stuck blocked for days,
and the headless `ccsynopsis` Haiku summarizers, cluttered both ccdash and the ccbar `⛔`/`◆`
segments. The user wanted a persistent **ignore pattern**, honored in both the dashboard and the
tmux bar.

## What was built

- **File-based contract**: `~/.claude/run/ccmonitor-ignore` (override via `$CCMONITOR_IGNORE`),
  one regex per line, `#` comments, blank lines skipped. Edits take effect live (re-read each
  tick). The file *is* the headless interface — any editor/script writes it; no CLI needed.
- **Matching**: each pattern is case-insensitively `re.search`ed against the session's `kind`,
  `title`, and `cwd` **independently**, so `^Smoke test` anchors the title, `background` hides
  that kind, and a repo name hides a whole tree.
- **Provider hosts the matcher but never applies it**: `ccstatus.load_ignore_patterns()` /
  `is_ignored()` (next to the other consumer-facing helpers). `get_sessions()` still emits the
  full `Session[]` so `--serve` / the notch stay complete.
- **Consumers filter**: `ccdash` drops matches in `_apply` and shows an `N ignored` count;
  `ccbar` mirrors the matcher stdlib-only (no provider import) and drops matches in `render()`
  so an ignored session never alerts in the bar.

## Design decisions

- **Consumer-side, not provider-side** (user chose "dashboard + tmux bar", not "everywhere in
  provider") — keeps the provider opinion-free and the notch unaffected; the ignore is display
  policy, which the provider/consumer split assigns to consumers.
- **Regex over a blanket `kind==background` toggle** (user chose flexibility) — pattern control
  hides exactly what you name without losing background agents you care about.
- **Per-field match, not one concatenated string** — so `^title` anchors don't break against a
  prepended `kind`.
- **Fail-open**: a bad pattern is skipped (not the whole list); a missing/unreadable file
  ignores nothing.

## Verification (all passed)

`is_ignored` unit (title/kind/cwd matches + non-match); provider still emits all sessions while
consumers drop the ignored ones (live: 7 emitted → 5 shown); ccbar render drops ignored rows;
`$CCMONITOR_IGNORE` override honored by both ccdash and ccbar; bad-regex line skipped fail-open.
All three modules compile; `ccdash --selftest` passes.

## Files

`ccstatus.py` (`IGNORE_FILE`, `load_ignore_patterns`, `is_ignored`; `import re`), `ccdash.py`
(import + filter in `_apply` + `N ignored` count), `ccbar.py` (`IGNORE_FILE`, `_load_ignore`,
`_ignored`, filter in `render`; `import re`), `CLAUDE.md` (module rows + ignore-list bullet),
`~/.claude/run/ccmonitor-ignore` (template).
