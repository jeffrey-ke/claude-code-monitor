# ccdash — peek panel no longer truncates the bottom (most recent) lines (COMPLETED)

**Status: COMPLETED 2026-06-30** (branch `ccdash`, uncommitted). Verified live.

## Context

The inline preview panel (`#peek`, toggled with `P`) rendered a session's understanding
line + a `Rule` divider + the last 12 lines of the live tmux pane, inside a `Static`
widget with a fixed CSS `height: 16`. `Static` doesn't scroll — when the rendered
content overflowed the box, the bottom got clipped. Since the header (an optional "⏳
needs you" line + the understanding/synopsis text, which can wrap to multiple rows)
has variable height, the 12-line pane-tail body that followed it routinely didn't all
fit in the remaining rows. That meant the newest, most relevant lines of the live pane
— the entire point of a live preview — were the ones getting cut off. User report:
"the dashboard peek truncates the bottom most lines: I want to see them."

## What was built

`self.peek` is now a non-focusable `VerticalScroll` wrapping an inner `Static`
(`self.peek_body`), scrolled to the end on every content update — the exact pattern
`DriveScreen` already used for its own live pane tail (`self.box = VerticalScroll(self.pane,
...)`, `self.box.can_focus = False`, update then `scroll_end(animate=False)`).

```python
self.peek_body = Static("")
self.peek = VerticalScroll(self.peek_body, id="peek", classes="" if self.peek_on else "-off")
self.peek.can_focus = False
...
def _set_peek(self, renderable):
    self.peek_body.update(renderable)
    self.peek.scroll_end(animate=False)
```

`_update_peek()` and the threaded `_peek_capture()` both route through `_set_peek`
instead of calling `.update()` directly on the (now-container) widget. No CSS change
needed — `#peek { height: 16; ... }` applies to the `VerticalScroll` the same way it did
to the `Static` (see `#drive-box` for precedent). `_capture_pane(..., lines=12)` is
unchanged — the fix is about making those already-correct tail lines visible, not
capturing more of them.

## Design decisions

- **Auto-scroll-to-bottom, not interactive scrolling.** Peek is documented as "a quick
  at-a-glance grab" (the full-screen `p` reader is for serious/scrollable reading), so
  the fix keeps that distinction: the panel always pins to the live tail by default. If
  content still overflows, clipping now happens at the top (older header content), never
  at the bottom (the live tail the user is asking to see).
- **`can_focus = False`** so the table keeps `j`/`k` row navigation; the peek container
  never steals focus, matching `DriveScreen`'s `self.box.can_focus = False`.

## Verification (live)

Launched `uv run --script ccdash.py` in a detached tmux session, toggled `P`, and
selected sessions with long understanding text and busy/verbose panes. Confirmed the
previously-clipped bottom lines (the actual live pane tail, e.g. a running TodoWrite
list) are now visible, `j`/`k` still move the table cursor, and `P` on/off (`-off` /
`display: none`) still works. `DriveScreen` (`v` drive mode) untouched, sanity-checked
unaffected.

## Files

`ccdash.py` (`compose()`, new `_set_peek` helper, `_update_peek`, `_peek_capture`).
