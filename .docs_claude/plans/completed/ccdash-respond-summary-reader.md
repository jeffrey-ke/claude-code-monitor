# Plan: ccdash — respond to sessions (headless), summary anchored at last user message, scrollable reader

**Status: COMPLETED 2026-06-29** (branch `ccdash`, commit `9af3d7a`). As-built notes at the bottom.

## Context

Three follow-ups to the shipped `ccstatus`/`ccdash` stack (branch `ccdash`), all driven by
living in the dashboard:

1. **Respond to a session from the TUI** — type a message, accept a plan, answer a question
   or a permission prompt, without leaving the dashboard. **Headless-first**: the underlying
   representation of an input is a **file on disk** that *any* program can write — the TUI is
   one writer, a `ccsend` CLI wrapper is another. The proven, only-reliable delivery seam is
   **tmux `send-keys`** (the Mac app's `ToolApprovalHandler.sendKeys` already does exactly
   this: `send-keys -t <pane> -l <text>` then a separate `send-keys -t <pane> Enter`; its
   approve/deny shortcuts are literally `"1"`/`"2"`/`"n"` + Enter — in production today).
2. **Summary anchored at the last user message** — the Haiku running understanding should
   read the conversation from the **last message *I* sent** forward, i.e. "what has Claude
   done since I last spoke," instead of a fixed last-6-turns tail.
3. **Scrollable reader** — today the peek is a truncated grey `tmux capture-pane` grab, fine
   for a glance but not for reading the full last message / plan / question. Make a
   **full-screen scrollable reader** for serious reading.

**Decisions locked with the user:** summary anchor = *since my last message*; reader =
*full-screen scrollable modal*; respond = *simplest dumb mechanism* = tmux `send-keys`,
exposed two ways — a **compose box** (text + Enter; covers messages + single-choice menus)
and a **drive mode** key-passthrough (your real keypresses are mapped to tmux key names, so
multi-select / arbitrary menus work with zero escape-sequence juggling). All inputs ride one
file-based contract. Mechanism stays in a provider (`ccsend`); policy (keys, modes, render)
stays in `ccdash` — matches the repo's mechanism/policy + provider/consumer split.

Constraint carried from `ssh-bridge-bugs.md` #4: **fail-open** — never deliver something you
didn't parse, never crash on a tmux error, never blind-retry a partial send.

---

## New component — `ccsend.py` (mechanism, headless-first)

The on-disk input contract **and** the single `send-keys` point. New file.

```python
# ── the contract: a per-session outbox queue any program can write ────────────
OUTBOX = Path.home() / ".claude" / "run" / "outbox"          # outbox/<sid>/<uniq>.json
# payload schema (one JSON object per file):
#   {"type": "text", "text": "...",            "ts": <epoch>}   # send literal text, then Enter
#   {"type": "keys", "keys": ["Down","Space"], "ts": <epoch>}   # send named tmux keys in order

def enqueue(sid, payload):                # writer side — atomic, fail-open
    d = OUTBOX / sid; d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")          # unique ⇒ concurrent writers never clobber
    os.write(fd, json.dumps(payload).encode()); os.close(fd)
    os.replace(tmp, d / f"{Path(tmp).stem}.json")

# ── delivery: the ONE tmux send-keys seam (lifted from ToolApprovalHandler) ───
KEY_OK = {"Enter","Up","Down","Left","Right","Space","Tab","Escape","BSpace"}  # whitelist; ignore others

def deliver(session, payload):            # returns (ok: bool, reason: str)
    if not session.pane_id:               # background / no pane → caller notifies, no send
        return False, "no tmux pane"
    t = payload.get("type")
    if t == "text":
        _send(session.pane_id, "-l", payload["text"])         # literal text — no special-char interpretation
        _send(session.pane_id, "Enter")                       # Enter as a SEPARATE send-keys (proven pattern)
    elif t == "keys":
        for k in payload["keys"]:
            if k in KEY_OK: _send(session.pane_id, k)         # named key
            else:           _send(session.pane_id, "-l", k)   # treat anything else as one literal char
    else:
        return False, f"unknown payload type {t!r}"
    return True, "sent"

def _send(pane, *args):                   # tmux send-keys -t <pane> <args…>; fail-open
    subprocess.run(["tmux","send-keys","-t",pane,*args], timeout=3, ...)

def drain(sessions=None):                 # deliver pending outbox files; unlink on success, keep on failure
    sessions = sessions or get_sessions(); by_id = {s.session_id: s for s in sessions}
    for sid_dir in sorted(OUTBOX.glob("*")):
        s = by_id.get(sid_dir.name)
        for f in sorted(sid_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            ok = s and deliver(s, json.loads(f.read_text()))[0]
            if ok: f.unlink()             # leave un-deliverable files visible (no session / tmux down)
```

CLI: `ccsend <sid|prefix> "message"` (enqueue text + drain) · `ccsend <sid> --keys Down Space Enter`
(enqueue keys + drain) · `ccsend --drain` (deliver everything pending). `sid` resolves by
prefix through `get_sessions()` (reuse the provider; matches the `--state` selector style).

Why a queue **dir** of unique files (not one `<sid>.json`): lets the TUI, a CLI, and an
external script all drop inputs without clobbering, and a stuck/un-deliverable input stays
visible instead of being silently overwritten.

## `ccstatus.py` — shared transcript-window helper (provider owns transcript parsing)

Both the summarizer and the reader need the same window — "turns since my last message" — so it
lives once in the provider next to `_transcript_path` / `_first_user_prompt`, reused by both.

```python
# NEW — reuses _looks_like_wrapper(); predicate for a genuine human turn established by exploration:
#   type=="user"  AND  not isSidechain  AND  text is real (not a tool_result block)  AND  not a <…>/Caveat: wrapper
def turns_since_last_user(jsonl_path, tail_bytes=512*1024, max_turns=40, turn_max=320):
    """[{'role','text'}] from the LAST genuine user message to the end. Fail-open:
    if no user turn is found in the tail, return the last `max_turns` (old behaviour)."""
    # read tail → parse user/assistant text turns (same content-shape handling as _first_user_prompt)
    # find index of last turn where role=='user' and is a genuine human message → slice [idx:]
```

(Optional polish, same file) `pending_interaction(jsonl_path)` → parse the last assistant
`tool_use` (`AskUserQuestion` / `ExitPlanMode`) into `{kind, prompt|question, options,
multiSelect}` so the reader can render the actual options instead of raw pane text. The
question/options live in the assistant turn's `tool_use.input`; the plan text in
`ExitPlanMode`'s `input.plan` (established by exploration).

## `ccsynopsis.py` — anchor the summarizer window at the last user message

```python
# BEFORE  _recent_text(): fixed last-6-turns tail
turns = [...]                                   # parsed user/assistant text turns from a 128KB tail
return "\n".join(turns[-N_TURNS:])              # last 6, regardless of where I last spoke
```
```python
# AFTER   reuse the provider helper; window = everything since my last message
from ccstatus import turns_since_last_user      # same-dir import (ccdash already does this)
turns = turns_since_last_user(transcript_path)  # last user msg → end ("what Claude did since I spoke")
if not turns: return None
return "\n".join(f"{t['role']}: {t['text']}" for t in turns)   # same "role: text" shape the model already gets
```

EMA blending is unchanged — the prior understanding is still fed back each Stop; only the
*reading window* moves. Net: the running understanding now describes the work done since the
user last spoke, which is exactly what you read before responding.

## `ccdash.py` — respond producers (compose + drive) and the scrollable reader

```python
from ccsend import enqueue, deliver, drain      # reuse the provider seam (stdlib-only)

# BINDINGS — add three; c & v are the respond surface, p re-points to the reader
("c", "compose", "respond"),                    # type a message / single-choice answer → text + Enter
("v", "drive", "drive"),                         # toggle key-passthrough (multi-select & any menu)
("p", "reader", "read"),                          # open the full-screen scrollable reader
```

**Compose** (reuse the existing `Input` widget pattern used for `/` filter):
```python
def action_compose(self):                        # open a bottom input bound to the selected session
def on_input_submitted(...):                      # if it's the compose input and a session is selected:
    enqueue(s.session_id, {"type":"text","text":value,"ts":time.time()})   # TUI WRITES THE FILE …
    drain([... current sessions ...])                                       # … then delivers it
    _touch(ACK_DIR, s.session_id)                # auto-mark responded-to (mutes orange; auto-clears on reply)
    self.notify(f"sent → {s.title}")
```

**Drive mode** — dumb remote keyboard for the selected pane; nothing to juggle:
```python
self.driving = False                             # in __init__
def action_drive(self):                          # toggle; show "DRIVE → <title>" in sub_title
def on_key(self, event):                         # when self.driving and a session is selected:
    tok = _TMUX_KEY.get(event.key)               # enter→Enter, space→Space, up/down/left/right→Up/…, tab→Tab, backspace→BSpace
    if event.key == "escape": self.driving=False; return            # esc leaves drive mode
    deliver(s, {"type":"keys","keys":[tok or event.character]})     # one keypress → one send-keys (live, no file churn)
    event.stop()                                 # consume so it doesn't hit dashboard bindings
```
(Compose = the durable file path the user asked for; drive = live `deliver()` for latency.)
On each refresh tick also call `drain(sessions)` so inputs dropped by *other* programs get
delivered while ccdash is open. Background / no-pane session → `deliver` returns False → notify.

**Scrollable reader** — full-height modal, the "serious reading" surface:
```python
class ReaderScreen(ModalScreen):                 # opened by action_reader(self._selected())
    # a VerticalScroll containing, top→bottom:
    #   • running understanding (cyan)                                   ← s.understanding
    #   • ⏳ needs-you line if blocked (yellow)                          ← s.waiting_for
    #   • Rule
    #   • conversation since my last message, rendered turns             ← turns_since_last_user(jp)
    #       (render a pending AskUserQuestion/plan via pending_interaction → labelled options)
    #   • Rule
    #   • live pane tail (capture-pane)                                  ← _capture_pane(s.pane_id, lines=200)
    # BINDINGS: j/k, ctrl-d/ctrl-u, g/G, pageup/pagedown (Textual scroll defaults) ; q/escape closes
```
The compact inline peek stays as the at-a-glance row preview; `p` now opens the reader for
depth (the old `p` peek-on/off toggle is dropped — the glance peek is cheap, keep it always on).

## Files touched

| file | change |
|---|---|
| `ccsend.py` | **NEW** — outbox file contract (`enqueue`/`drain`) + the single `deliver()` `send-keys` seam + `ccsend` CLI |
| `ccstatus.py` | `turns_since_last_user()` (shared window) + optional `pending_interaction()`; no `Session` schema change |
| `ccsynopsis.py` | `_recent_text` → window anchored at last user message via `turns_since_last_user` |
| `ccdash.py` | import `ccsend`; `c`/`v`/`p` bindings + `action_compose`/`action_drive`/`action_reader`; `ReaderScreen`; `drain()` on refresh tick; auto-ack on respond; `_TMUX_KEY` map |
| `CLAUDE.md` | module-index row for `ccsend`; design bullets: respond = outbox file + `send-keys` (compose/drive); summary anchored at last user message; scrollable reader |

No new dependencies (tmux + stdlib + Textual already in use). New state dir
`~/.claude/run/outbox/<sid>/` (auto-created).

## Verification (end-to-end, live — same approach used last time)

1. **Send mechanism:** spawn a controlled session that calls `AskUserQuestion` (and one in
   `--permission-mode plan`). From `ccsend` CLI: `ccsend <sid> "1"` accepts the plan / picks
   option 1; `ccsend <sid> "hello"` posts a message to an idle session. Confirm via the pane
   capture that the keystrokes landed. **This live test also confirms digit+Enter accepts a
   plan menu** (the one unproven assumption; the Mac app proves it for permission prompts).
2. **Multi-select:** on a `multiSelect` `AskUserQuestion`, enter drive mode and toggle two
   options with ↓/Space then Enter — confirm the recorded `toolUseResult.answers` has both.
3. **Headless / any-writer:** drop an outbox file by hand (`echo '{"type":"text","text":"hi"}'
   > ~/.claude/run/outbox/<sid>/x.json`) and confirm ccdash's tick `drain()` delivers + removes
   it. `ccsend --drain` delivers from a cold start (no TUI open).
4. **Summary anchor:** send a message into a session; on the next Stop, confirm
   `<sid>.understanding` reflects only post-message activity (not the older tail).
5. **Reader:** `p` opens the full-screen reader; understanding + conversation-since-last-msg +
   pane tail render; j/k/PgUp/PgDn/g/G scroll; a pending question/plan shows its options;
   q/esc closes.
6. **Fail-open:** kill a pane mid-send / point at a background session → notify, no crash, the
   outbox file stays for retry. `uv run --script ccdash.py --selftest` still prints OK.

## Known limitation (operational, not code)

Driving an in-terminal menu by keystroke depends on Claude Code's menu key handling. The
proven-safe path (digit+Enter for single choice; ↓/Space/Enter passthrough for multi-select)
is verified live in step 1–2; if a future menu resists blind keystrokes, `enter` (jump into
the session) remains the always-correct fallback.

---

## As-built notes (deviations discovered during implementation)

- **Drive mode is a dedicated `DriveScreen(ModalScreen)`, not a `self.driving` flag on the main
  screen.** Research into Textual 8.2.7 key dispatch confirmed (a) a `ModalScreen` already
  truncates the App's binding chain, so the parent dashboard's `c`/`v`/`a` bindings can't fire
  while a modal is up, and (b) a screen-level `on_key` doing `event.stop()` + `event.prevent_default()`
  with **no focusable children** (so focus rests on the screen) captures *every* key including
  arrows — exactly what passthrough needs. The screen shows a live `capture-pane` tail and
  forwards each keypress via `deliver(keys=[tok])`; Esc dismisses. Cleaner than a flag + app-level
  on_key, and avoids fighting the binding system.
- **The reader composes native child widgets — NOT one `Static` holding a rich `Group`.** A
  single `Static` fed a long rich `Group` containing `rich.rule.Rule` / `rich.markdown.Markdown`
  **and** styled with a CSS `background` crashes Textual 8.2.7's Visual system
  (`'NoneType' object has no attribute 'render_strips'` at `visual.py:227`, via the fragile
  `RichVisual`/background compositing seam). The fix (confirmed by reading the v8.2.7 source):
  build the document as a stream of native widgets inside the `VerticalScroll` — `Static`
  paragraphs + `textual.widgets.Rule` dividers + a `textual.widgets.Markdown` widget for a
  pending plan — with the themed background/border on the **container**. Short Rule-free `Text`
  `Group`s inside a `Static` are fine (per-turn role+text). The inline glance peek keeps its rich
  `Rule` because it has no background set.
- **Reader scrolling is a `VimScroll(VerticalScroll)` subclass with extra `BINDINGS`** (j/k →
  scroll_down/up, g/G → scroll_home/end, ctrl+d/ctrl+u → half-page via `scroll_relative` on
  `scrollable_content_region.height`); it inherits ↑/↓/Home/End/PageUp/PageDown from the base
  and is `.focus()`ed on mount. Esc/q close via the built-in `action_dismiss`. The pane tail is
  the only thing refreshed on the interval (the rest is built once) so scroll position doesn't jump.
- **`send-keys` hardening (from the Mac app's pattern + research):** literal text/chars are sent
  as `send-keys -l -- <s>` (the `--` stops a leading `-`/glyph being read as a flag); named keys
  are sent **without** `-l`. `KEY_OK` was expanded to the full tmux named-key set
  (`BTab`/`DC`/`IC`/`F1..F12`), and `_TMUX_KEY` maps Textual key names → tmux tokens
  (`shift+tab`→`BTab`, `delete`→`DC`, `insert`→`IC`, etc.). Ctrl/Alt combos are not forwarded
  in drive mode (event.character is None → skipped), which keeps `ctrl+c` etc. from killing a
  session by accident.
- **`drain` robustness:** corrupt outbox files are dropped (fail-open); an orphan file whose
  session is gone is reaped after `STALE_S = 3600`s (else recent undelivered files stay visible
  for retry); empty queue dirs are `rmdir`'d.
- **`transcript_path()` public seam** added to `ccstatus` so the reader doesn't import an
  underscore-private (`_transcript_path`).
- **Live verification done:** `ccsend <prefix> "1"` accepted a **real** `ExitPlanMode` plan menu
  through the full CLI path (resolve → enqueue → drain → deliver), engaging auto mode — the one
  unproven assumption (digit drives a real Claude menu) confirmed. Reader (turns / pending plan
  Markdown / multi-select question / background no-pane), drive, and compose were pilot-tested
  under Textual's `run_test`; `--selftest` passes; rendered live in a real tmux pane with the new
  footer keys. Authoritative Textual 8.2.7 patterns were sourced via two web-research subagents
  (key dispatch / modal scroll, and the Visual rendering seam) rather than trial-and-error.
- **Committed** as `9af3d7a` on branch `ccdash` (5 files, +596/−58); the pre-existing
  `claude-island` submodule change was left uncommitted, as before.
