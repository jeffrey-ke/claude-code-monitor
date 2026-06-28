#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=0.60"]
# ///
"""
ccdash.py — a live TUI dashboard of all Claude Code sessions. First consumer of the
`ccstatus` provider.

Designed to run inside a tmux popup:

    bind-key G display-popup -E -w 90% -h 85% -e COLORTERM=truecolor \\
      "uv run --script $HOME/repo/ccmonitor/ccdash.py"

Keys (also in the footer):  enter/o jump · p peek · / filter · s sort · r refresh · ? help · q quit
Permission-blocked sessions sort to the top. `enter` switches the underlying tmux
client to the selected session's pane and closes the popup.
"""
import subprocess
import sys
from pathlib import Path

# Reuse the provider's public seam (stdlib-only; safe under uv's script venv).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ccstatus import get_sessions  # noqa: E402

from rich.console import Group  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import DataTable, Footer, Header, Input, Static  # noqa: E402

STATE_STYLE = {
    "blocked": "bold yellow",   # needs you — permission prompt
    "busy": "cyan",
    "shell": "blue",
    "idle": "dim",
    "dead": "grey50",
}
DOT = "●"
SORTS = ["state", "age", "title", "cwd"]   # 'state' = provider order (blocked-first)


def fmt_age(s):
    if s is None:
        return "-"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _capture_pane(pane_id, lines=24):
    try:
        return subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id, "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class CCDash(App):
    CSS = """
    DataTable { height: 1fr; }
    #peek { height: 16; border: round $panel; padding: 0 1; color: $text-muted; }
    #filter { dock: bottom; display: none; }
    #filter.-on { display: block; }
    """
    BINDINGS = [
        ("enter,o", "jump", "jump"),
        ("p", "toggle_peek", "peek"),
        ("slash", "filter", "filter"),
        ("s", "cycle_sort", "sort"),
        ("r", "refresh", "refresh"),
        ("j", "cursor_down", "down"),
        ("k", "cursor_up", "up"),
        ("question_mark", "help", "help"),
        ("q", "quit", "quit"),
    ]

    def __init__(self):
        super().__init__()
        self.rows = []          # list[Session] in display order; indexed by cursor_row
        self.filter = ""
        self.sort_mode = "state"
        self.peek_on = True

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self.table = DataTable(zebra_stripes=True, cursor_type="row")
        self.peek = Static("", id="peek")
        self.filter_input = Input(placeholder="filter title / synopsis / cwd …", id="filter")
        yield self.table
        yield self.peek
        yield self.filter_input
        yield Footer()

    def on_mount(self):
        self.title = "ccdash"
        self.table.add_columns(" ", "TITLE", "SYNOPSIS", "CWD", "AGE", "TMUX")
        self.peek.border_title = "preview"
        self.load()
        self.set_interval(1.5, self.load)

    # ── data refresh (off the event loop) ────────────────────────────────────

    @work(thread=True, exclusive=True, group="refresh")
    def load(self):
        sessions = get_sessions()
        self.call_from_thread(self._apply, sessions)

    def _ordered(self, sessions):
        if self.filter:
            f = self.filter.lower()
            sessions = [s for s in sessions
                        if f in f"{s.title} {s.synopsis} {s.cwd_short}".lower()]
        if self.sort_mode == "age":
            sessions = sorted(sessions, key=lambda s: s.age_s if s.age_s is not None else 1 << 30)
        elif self.sort_mode == "title":
            sessions = sorted(sessions, key=lambda s: s.title.lower())
        elif self.sort_mode == "cwd":
            sessions = sorted(sessions, key=lambda s: s.cwd_short.lower())
        # 'state' keeps the provider's blocked-first order
        return sessions

    def _apply(self, sessions):
        sessions = self._ordered(sessions)
        prev = None
        if self.rows and 0 <= self.table.cursor_row < len(self.rows):
            prev = self.rows[self.table.cursor_row].session_id

        self.rows = sessions
        self.table.clear()
        for s in sessions:
            self.table.add_row(*self._cells(s))

        if prev:
            for i, s in enumerate(sessions):
                if s.session_id == prev:
                    self.table.move_cursor(row=i)
                    break
        self.sub_title = f"{len(sessions)} session{'s' * (len(sessions) != 1)}  ·  sort:{self.sort_mode}"
        self._update_peek()

    def _cells(self, s):
        style = STATE_STYLE.get(s.state, "")
        age_style = "red" if (s.state == "busy" and (s.age_s or 0) > 600) else "dim"
        return (
            Text(DOT, style=style),
            Text(s.title[:36], style=style if s.state == "blocked" else ""),
            Text(s.synopsis[:64], style="italic dim"),
            Text(s.cwd_short[-30:], style="dim"),
            Text(fmt_age(s.age_s), style=age_style),
            Text(s.tmux_target or ("bg" if s.kind == "background" else "-"), style="dim"),
        )

    # ── peek panel ───────────────────────────────────────────────────────────

    def _selected(self):
        i = self.table.cursor_row
        return self.rows[i] if self.rows and 0 <= i < len(self.rows) else None

    def on_data_table_row_highlighted(self, _event):
        self._update_peek()

    def _peek_render(self, s, body):
        """Understanding header + divider + live pane tail."""
        head = Text(s.understanding or s.synopsis or "(building understanding…)",
                    style="italic cyan")
        return Group(head, Rule(style="dim"), Text(body))

    def _update_peek(self):
        if not self.peek_on:
            return
        s = self._selected()
        if not s:
            self.peek.update("")
            return
        self.peek.border_title = f"preview · {s.title}"
        if s.pane_id:
            self._peek_capture(s)
        else:
            self.peek.update(self._peek_render(s, "(background session — no tmux pane)"))

    @work(thread=True, exclusive=True, group="peek")
    def _peek_capture(self, s):
        body = _capture_pane(s.pane_id, lines=12) or "(no pane content)"
        self.call_from_thread(self.peek.update, self._peek_render(s, body))

    # ── actions ──────────────────────────────────────────────────────────────

    def action_jump(self):
        s = self._selected()
        if not s:
            return
        if not s.pane_id:
            self.notify(f"{s.title}: background session — no tmux pane", severity="warning")
            return
        # The popup is a pure overlay, so switch-client retargets the underlying
        # client (session+window+pane via the pane id); exiting closes the popup.
        try:
            subprocess.run(["tmux", "switch-client", "-t", s.pane_id], timeout=3)
        except (OSError, subprocess.SubprocessError):
            self.notify("tmux switch-client failed", severity="error")
            return
        self.exit()

    def action_toggle_peek(self):
        self.peek_on = not self.peek_on
        self.peek.display = self.peek_on
        if self.peek_on:
            self._update_peek()

    def action_cycle_sort(self):
        self.sort_mode = SORTS[(SORTS.index(self.sort_mode) + 1) % len(SORTS)]
        self.load()

    def action_refresh(self):
        self.load()

    def action_cursor_down(self):
        self.table.action_cursor_down()

    def action_cursor_up(self):
        self.table.action_cursor_up()

    def action_filter(self):
        self.filter_input.add_class("-on")
        self.filter_input.focus()

    def action_help(self):
        self.notify("enter/o jump · p peek · / filter · s sort · r refresh · q quit",
                    title="ccdash keys", timeout=6)

    def on_input_changed(self, event: Input.Changed):
        self.filter = event.value
        self.load()

    def on_input_submitted(self, _event: Input.Submitted):
        self.filter_input.remove_class("-on")
        self.table.focus()


def _selftest():
    """Headless mount + one refresh, no real tty (for CI / smoke checks)."""
    import asyncio

    async def run():
        app = CCDash()
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert app.table.columns, "columns not initialized"
    asyncio.run(run())
    print("ccdash selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        CCDash().run()
