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

Keys (also in the footer):  enter/o jump · a responded-to · d hide · D show-hidden ·
p peek · / filter · s sort · r refresh · ? help · q quit
Permission-blocked sessions sort to the top. `enter` switches the underlying tmux
client to the selected session's pane and closes the popup. `a` mutes a stale orange
marker (auto-returns on new activity); `d` hides a row you don't care about.
"""
import subprocess
import sys
from pathlib import Path

# Reuse the provider's public seam (stdlib-only; safe under uv's script venv).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ccstatus import get_sessions, STATE_ORDER  # noqa: E402

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

# Interactive markers — touch to set, unlink to clear; ccstatus derives the booleans
# (auto-clear when the session writes something new). See ccstatus._marker_active.
ACK_DIR = Path.home() / ".claude" / "run" / "ack"
DISMISS_DIR = Path.home() / ".claude" / "run" / "dismissed"


def _touch(dir_, sid):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / sid).touch()        # mtime = now ⇒ marker active until the transcript advances


def _clear(dir_, sid):
    try:
        (dir_ / sid).unlink()
    except OSError:
        pass


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
        ("a", "ack", "ack"),                  # toggle responded-to (mutes the orange marker)
        ("d", "dismiss", "dismiss"),          # toggle hide row
        ("D", "toggle_dismissed", "hidden"),  # reveal / re-hide dismissed rows
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
        self.show_dismissed = False

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
        if not self.show_dismissed:
            sessions = [s for s in sessions if not s.dismissed]
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
        else:  # 'state': blocked-first, but acked/dismissed sink below the live rows
            sessions = sorted(sessions, key=lambda s: (
                s.dismissed, s.acknowledged,
                STATE_ORDER.get(s.state, 5),
                s.age_s if s.age_s is not None else 1 << 30))
        return sessions

    def _apply(self, sessions):
        acked = sum(1 for s in sessions if s.acknowledged)
        hidden = sum(1 for s in sessions if s.dismissed)
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
        bits = [f"{len(sessions)} session{'s' * (len(sessions) != 1)}"]
        if acked:
            bits.append(f"{acked} acked")
        if hidden:
            bits.append(f"{hidden} hidden" + (" (shown)" if self.show_dismissed else ""))
        bits.append(f"sort:{self.sort_mode}")
        self.sub_title = "  ·  ".join(bits)
        self._update_peek()

    def _cells(self, s):
        if s.dismissed:
            dot, style, title_style = "·", "strike grey50", "strike grey50"
        elif s.acknowledged:
            dot, style, title_style = "✓", "dim green", ""   # responded-to: calm, not orange
        else:
            dot, style = DOT, STATE_STYLE.get(s.state, "")
            title_style = style if s.state == "blocked" else ""
        age_style = "red" if (s.state == "busy" and (s.age_s or 0) > 600) else "dim"
        return (
            Text(dot, style=style),
            Text(s.title[:36], style=title_style),
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
        """Optional 'needs you' line + understanding header + divider + live pane tail."""
        parts = []
        if s.state == "blocked" and not s.acknowledged:
            parts.append(Text(f"⏳ {s.waiting_for or 'needs you'}", style="bold yellow"))
        parts.append(Text(s.understanding or s.synopsis or "(building understanding…)",
                          style="italic cyan"))
        parts.append(Rule(style="dim"))
        parts.append(Text(body))
        return Group(*parts)

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

    def action_ack(self):
        """Mark the selected session responded-to (mutes its orange marker). Toggles."""
        s = self._selected()
        if not s:
            return
        (_clear if s.acknowledged else _touch)(ACK_DIR, s.session_id)
        self.notify(f"{s.title}: {'un-acked' if s.acknowledged else 'responded-to'}")
        self.load()

    def action_dismiss(self):
        """Hide the selected session from the dashboard. Toggles."""
        s = self._selected()
        if not s:
            return
        (_clear if s.dismissed else _touch)(DISMISS_DIR, s.session_id)
        self.notify(f"{s.title}: {'restored' if s.dismissed else 'dismissed'}")
        self.load()

    def action_toggle_dismissed(self):
        """Reveal (struck-through) or re-hide dismissed rows."""
        self.show_dismissed = not self.show_dismissed
        self.load()

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
        self.notify("enter jump · a responded-to · d hide · D show-hidden · p peek · "
                    "/ filter · s sort · r refresh · q quit",
                    title="ccdash keys", timeout=8)

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
