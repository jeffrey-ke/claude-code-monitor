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

Keys (also in the footer):  enter/o jump · c respond · v drive · p read · a responded-to ·
d hide · D show-hidden · / filter · s sort · r refresh · ? help · q quit
Permission-blocked sessions sort to the top. `enter` switches the underlying tmux client
to the selected session's pane and closes the popup. `c` opens a compose box that types a
message / menu answer into the session (then Enter); `v` opens drive mode (your keypresses
go straight to the pane — for multi-select and any in-terminal menu); `p` opens a full-screen
scrollable reader. `a` mutes a stale orange marker (auto-returns on new activity); `d` hides
a row. Responding is a file-based outbox (see ccsend.py) so any program can do it too.
"""
import subprocess
import sys
import time
from pathlib import Path

# Reuse the provider/sender public seams (stdlib-only; safe under uv's script venv).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ccstatus import (  # noqa: E402
    get_sessions, STATE_ORDER, transcript_path, turns_since_last_user, pending_interaction,
)
from ccsend import enqueue, deliver, drain  # noqa: E402

from rich.console import Group  # noqa: E402
from rich.rule import Rule  # noqa: E402  (rich Rule — only for the inline glance peek, no bg)
from rich.text import Text  # noqa: E402
from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import VerticalScroll  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import (  # noqa: E402
    DataTable, Footer, Header, Input, Markdown, Rule as HRule, Static,
)

STATE_STYLE = {
    "blocked": "bold yellow",   # needs you — permission prompt
    "busy": "cyan",
    "shell": "blue",
    "idle": "dim",
    "dead": "grey50",
}
DOT = "●"
SORTS = ["state", "age", "title", "cwd"]   # 'state' = provider order (blocked-first)

# Drive mode: map a Textual key name → the tmux send-keys token. Printable characters
# (letters, digits, punctuation) aren't here — they ride event.character and go via `-l`.
_TMUX_KEY = {
    "enter": "Enter", "space": "Space", "tab": "Tab", "shift+tab": "BTab", "backspace": "BSpace",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End",
    "delete": "DC", "insert": "IC",
}

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


# ── reader content (composed as native widgets in a scroll container) ─────────

def _question_renderable(questions):
    """A pending AskUserQuestion as plain Text lines (a short, Rule-free Group is safe in a
    single Static; the fragile case is a long mixed Rule/Markdown Group)."""
    rows = []
    for q in questions:
        rows.append(Text(q.get("question", ""), style="bold"))
        if q.get("multiSelect"):
            rows.append(Text("  (select one or more — use v / drive: ↓ Space … Enter)",
                             style="dim italic"))
        for i, o in enumerate(q.get("options", []), 1):
            rows.append(Text(f"  {i}. {o.get('label', '')}", style="cyan"))
            if o.get("description"):
                rows.append(Text(f"     {o['description']}", style="dim"))
    return Group(*rows)


class VimScroll(VerticalScroll):
    """VerticalScroll + vim/less keys (inherits ↑/↓/Home/End/PageUp/PageDown from the base)."""
    BINDINGS = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
        Binding("g", "scroll_home", show=False),
        Binding("G", "scroll_end", show=False),
        Binding("ctrl+d", "half_down", show=False),
        Binding("ctrl+u", "half_up", show=False),
    ]

    def action_half_down(self):
        self.scroll_relative(y=self.scrollable_content_region.height // 2, animate=False)

    def action_half_up(self):
        self.scroll_relative(y=-(self.scrollable_content_region.height // 2), animate=False)


class ReaderScreen(ModalScreen):
    """Full-screen scrollable reader for one session — serious reading, not a glance.

    Composed as native child widgets (Static paragraphs + textual Rule dividers + textual
    Markdown for a pending plan) inside a VerticalScroll — NOT one Static holding a giant rich
    Group, which routes through a fragile RichVisual/background seam in Textual 8.x and crashes.
    A ModalScreen already blocks the parent App's key bindings, so c/v/a don't leak while reading.
    """
    CSS = """
    ReaderScreen { align: center middle; }
    #reader-box { width: 90%; height: 90%; border: round $panel; background: $surface; padding: 0 1; }
    #reader-box > Static, #reader-box > Markdown { height: auto; }
    #reader-box > Markdown { margin: 0; background: $surface; }
    """
    BINDINGS = [Binding("escape,q", "dismiss", "close", show=False)]

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.pane_tail = None        # Static holding the live pane capture (None for bg sessions)

    def compose(self) -> ComposeResult:
        with VimScroll(id="reader-box"):
            yield from self._doc()

    def _doc(self):
        """Build the reading document once, as a stream of native widgets."""
        s = self.session
        yield Static(Text(s.understanding or s.synopsis or "(building understanding…)",
                          style="bold cyan"))
        if s.state == "blocked" and not s.acknowledged:
            yield Static(Text(f"⏳ {s.waiting_for or 'needs you'}", style="bold yellow"))
        jp = transcript_path(s.cwd, s.session_id)
        pend = pending_interaction(jp)
        if pend and pend["kind"] == "plan":
            yield HRule(line_style="heavy")
            yield Static(Text("pending plan — accept with c then “1”, or v to drive",
                              style="bold yellow"))
            yield Markdown(pend.get("plan") or "(empty plan)")
        elif pend and pend["kind"] == "question":
            yield HRule(line_style="heavy")
            yield Static(Text("pending question — answer with c (a digit) or v to drive",
                              style="bold yellow"))
            yield Static(_question_renderable(pend["questions"]))
        yield HRule(line_style="dashed")
        yield Static(Text("conversation since your last message", style="dim"))
        turns = turns_since_last_user(jp, turn_max=20000)    # untruncated — this is for reading
        if turns:
            for t in turns:
                who, color = ("you", "green") if t["role"] == "user" else ("claude", "white")
                yield Static(Group(Text(f"▌ {who}", style=f"bold {color}"),
                                   Text(t["text"], style=color)))
        else:
            yield Static(Text("(no transcript turns found)", style="dim"))
        if s.pane_id:
            yield HRule(line_style="dashed")
            yield Static(Text("live pane", style="dim"))
            self.pane_tail = Static(Text(_capture_pane(s.pane_id, lines=200) or "(no pane content)"))
            yield self.pane_tail

    def on_mount(self):
        box = self.query_one("#reader-box", VimScroll)
        box.border_title = f"read · {self.session.title}    j/k ↑↓ · g/G top/bottom · q close"
        box.focus()
        if self.pane_tail is not None:           # keep just the pane tail live (no full re-mount)
            self.set_interval(2.0, self._refresh_pane)

    def _refresh_pane(self):
        if not self.is_mounted or self.pane_tail is None:
            return
        self.pane_tail.update(
            Text(_capture_pane(self.session.pane_id, lines=200) or "(no pane content)"))


class DriveScreen(ModalScreen):
    """Remote keyboard for one pane: every keypress is forwarded via send-keys; Esc exits.
    The dumbest possible way to drive any in-terminal menu (incl. multi-select) — you press
    the exact keys you'd press in the session, and ccdash maps them to tmux key names."""
    CSS = """
    DriveScreen { align: center middle; }
    #drive-box { width: 90%; height: 90%; border: thick $accent; background: $surface; }
    #drive-pane { padding: 0 1; }
    """

    def __init__(self, session):
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        self.pane = Static(id="drive-pane")
        self.box = VerticalScroll(self.pane, id="drive-box")
        self.box.can_focus = False               # so the screen (not the scroll) gets every key
        yield self.box

    def on_mount(self):
        self.box.border_title = f"DRIVE · {self.session.title} · your keys → the pane · Esc exits"
        self.set_focus(None)        # no focusable child ⇒ the screen receives every key (incl. arrows)
        self._tail()
        self.set_interval(0.4, self._tail)

    def _tail(self):
        if not self.is_mounted:          # skip late ticks during teardown (agent-confirmed gotcha)
            return
        body = _capture_pane(self.session.pane_id, lines=60) or "(no pane content)"
        self.pane.update(Text(body))
        self.box.scroll_end(animate=False)

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss()
        else:
            tok = _TMUX_KEY.get(event.key)
            if tok:
                deliver(self.session, {"type": "keys", "keys": [tok]})
            elif event.character and event.character.isprintable():
                deliver(self.session, {"type": "keys", "keys": [event.character]})
            self.call_after_refresh(self._tail)
        event.stop()
        event.prevent_default()


class CCDash(App):
    CSS = """
    DataTable { height: 1fr; }
    #peek { height: 16; border: round $panel; padding: 0 1; color: $text-muted; }
    #filter, #compose { dock: bottom; display: none; }
    #filter.-on, #compose.-on { display: block; }
    #compose { border: round $accent; }
    """
    BINDINGS = [
        ("enter,o", "jump", "jump"),
        ("c", "compose", "respond"),          # type a message / single-choice answer → text + Enter
        ("v", "drive", "drive"),              # remote keyboard for the pane (multi-select & any menu)
        ("p", "reader", "read"),              # full-screen scrollable reader
        ("a", "ack", "ack"),                  # toggle responded-to (mutes the orange marker)
        ("d", "dismiss", "dismiss"),          # toggle hide row
        ("D", "toggle_dismissed", "hidden"),  # reveal / re-hide dismissed rows
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
        self._compose_sid = None   # session a just-opened compose box is bound to

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self.table = DataTable(zebra_stripes=True, cursor_type="row")
        self.peek = Static("", id="peek")
        self.filter_input = Input(placeholder="filter title / synopsis / cwd …", id="filter")
        self.compose_input = Input(placeholder="message / answer …", id="compose")
        yield self.table
        yield self.peek
        yield self.filter_input
        yield self.compose_input
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
        drain(sessions)         # deliver any outbox inputs dropped by other programs while we're up
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

    def action_compose(self):
        """Open a compose box bound to the selected session (Enter sends text + Enter)."""
        s = self._selected()
        if not s:
            return
        if not s.pane_id:
            self.notify(f"{s.title}: background session — can't type into it", severity="warning")
            return
        self._compose_sid = s.session_id        # lock the target even if the cursor moves on refresh
        self.compose_input.value = ""
        self.compose_input.placeholder = f"→ {s.title[:34]}  (Enter sends · Esc cancels)"
        self.compose_input.add_class("-on")
        self.compose_input.focus()

    def action_drive(self):
        """Push remote-keyboard mode for the selected pane (multi-select & any menu)."""
        s = self._selected()
        if not s:
            return
        if not s.pane_id:
            self.notify(f"{s.title}: background session — can't drive it", severity="warning")
            return
        self.push_screen(DriveScreen(s))

    def action_reader(self):
        """Open the full-screen scrollable reader for the selected session."""
        s = self._selected()
        if s:
            self.push_screen(ReaderScreen(s))

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
        self.notify("enter jump · c respond · v drive · p read · a responded-to · d hide · "
                    "D show-hidden · / filter · s sort · r refresh · q quit",
                    title="ccdash keys", timeout=8)

    def _send_compose(self, text):
        s = next((x for x in self.rows if x.session_id == self._compose_sid), None)
        if not s:
            self.notify("compose target is gone", severity="warning")
            return
        payload = {"type": "text", "text": text, "ts": time.time()}
        enqueue(s.session_id, payload)          # the TUI writes the outbox file (headless contract) …
        if drain([s]):                           # … then delivers + removes it
            _touch(ACK_DIR, s.session_id)        # responded-to: mute orange (auto-clears on reply)
            self.notify(f"sent → {s.title}")
        else:
            self.notify(f"{s.title}: not delivered (no pane?)", severity="warning")
        self.load()

    def on_input_changed(self, event: Input.Changed):
        if event.input is self.filter_input:     # ignore keystrokes in the compose box
            self.filter = event.value
            self.load()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input is self.compose_input:
            self.compose_input.remove_class("-on")
            self.table.focus()
            self._send_compose(event.value)
        else:
            self.filter_input.remove_class("-on")
            self.table.focus()

    def on_key(self, event):
        """Esc closes an open compose/filter box without acting."""
        if event.key != "escape":
            return
        for inp in (self.compose_input, self.filter_input):
            if inp.has_class("-on"):
                inp.remove_class("-on")
                self.table.focus()
                event.stop()
                return


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
