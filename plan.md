# ccmonitor — Conversation Summary

## What we're building

A lightweight monitoring service that runs persistently on a remote Linux server and tracks the state of active Claude Code sessions running in tmux. The output is a plain text file that a local macOS machine can read over SSH — either by polling it directly, displaying it in a tmux status line, or consuming it from a Swift notification app.

The end goal is ambient awareness: a user SSHed into a remote machine with several Claude Code sessions running can glance at their local machine and know which sessions are working, which are blocked waiting for permission, and which are idle.

---

## What matters — design constraints from the conversation

**One function to swap backends.** The system starts with a tmux-scraping backend (no setup required on the remote). Eventually it will switch to a Claude Code hooks backend (real-time, event-driven). The constraint is that swapping backends means changing exactly one line: `get_sessions = get_sessions_tmux` → `get_sessions = get_sessions_hooks`. Everything downstream — the state logic, the file writer — is identical regardless of backend.

**Three states only.** The notification use cases are "this session is blocked" and "this session is finished." That means only three states are needed: `working`, `blocked`, `idle`. Additional states like `compacting` or `new` are implementation details of Claude Code that don't need to surface to the user.

**One output file, plain text, atomically written.** The output is a fixed-width table written to `~/.claude/run/status`, modeled on `ps aux` output. It's readable by `cat`, `awk`, `watch`, any text consumer, and `ssh remote cat`. Atomic writes (tmp file + `os.replace`) mean readers never see partial state. This is the right abstraction for SSH access — no network socket needed because SSH already gives you file access.

**Don't over-abstract prematurely.** An earlier version had `EventSource` and `StateWriter` ABCs, a `SessionStore` class, three output formats (status table, per-session key=value files, JSONL event log), and a daemon wiring layer. That's appropriate for a general library but not for this specific application. The right size is ~100 lines: one backend function, a classifier, an atomic writer, a main loop.

**Unix domain socket vs network socket.** A Unix domain socket is a file on the filesystem — IPC between processes on the same machine only, not reachable remotely. A TCP socket has an IP address and port and can be reached from other machines. For this use case (SSH access pattern), a plain file is the right output — SSH gives you remote file access without needing a network socket at all.

---

## Architecture

```
Remote machine
─────────────────────────────────────────────────────────
tmux panes (each running a Claude Code session)
    │
    │  tmux capture-pane → classify status bar text
    │  /proc/{pid}/status → walk process tree
    │  ~/.claude/sessions/{pid}.json → session metadata
    │
claude_status.py (polls every 2s)
    │
    └─→ ~/.claude/run/status  (atomic write)

Local machine
─────────────────────────────────────────────────────────
ssh remote cat ~/.claude/run/status
watch -n2 "ssh remote cat ~/.claude/run/status"
tmux status-right (awk the file)
Swift app (FileManager + Timer, or sshfs mount)
```

**Future: hooks backend**

```
Claude Code lifecycle events
    │
    └─→ hook script (reads stdin, writes to Unix socket)
            │
            └─→ daemon (reads socket, updates state)
                    │
                    └─→ ~/.claude/run/status  (same output, same consumers)
```

---

## What we learned from testing

**The session file key is `sessionId`, not `session_id`.** Claude Code writes `~/.claude/sessions/{pid}.json` with the key `sessionId`. The code needs to check both (`data.get("sessionId") or data.get("session_id")`) for safety.

**The tmux pane PID is the shell, not Claude.** `tmux list-panes` reports the PID of the shell process (bash/zsh) running in the pane. Claude Code runs as a child of that shell. So you cannot look up the pane PID directly in `~/.claude/sessions/` — you have to go the other direction: take Claude's PID from the session file and walk up `/proc/{pid}/status` (PPid field) until you find an ancestor that matches a known pane PID.

**One session was launched from inside nvim.** PID 29144 had the chain `claude → nvim → nvim → bash (pane)`. The ancestor walk needs enough depth (10 levels is safe) and must stop at `tmux: server` rather than continuing to systemd.

**The walk had an off-by-one bug.** The original `_find_ancestor_pane` checked `if parent in pane_pids` after fetching the parent — meaning it checked the parent before moving to it, so it never checked the current node. The fix is to check `if pid in pane_pids` at the top of the loop before fetching the parent.

**Classification regex needs to match the actual status bar format.** The initial regex matched `12k/200k` token counts. The actual format in these sessions is `[ctx: 22%]`. The fix adds a second pattern: `re.search(r'\[ctx:\s*\d+%\]', lower)`.

**Session files can be stale.** Claude Code does not clean up `~/.claude/sessions/` when a session ends. The session files for PIDs that are no longer alive need to be filtered out by checking `Path(f"/proc/{pid}").exists()` before using them.

**One pane was too narrow.** `datagen:0.1` showed `[ctxChecki` — the terminal was too narrow and the status bar was truncated before the `%]`, so the regex couldn't match. This is a display artifact on the remote, not fixable by the monitor.

**All 7 sessions are alive.** PIDs 29144, 4095857, 492770, 503632, 56119, 793198, 97449 all have live processes. Four have named sessions (`zed-neural-depth-init`, `examination of datagen`, `ipl refactor`, `isaac-sim-claude-role`), three are unnamed.

---

## Current status

The corrected `claude_status.py` should resolve all 7 sessions correctly. The next steps are:

1. Run the corrected script and verify all 7 sessions appear in console output
2. Confirm `~/.claude/run/status` looks correct
3. Verify `ssh remote cat ~/.claude/run/status` works from local Mac
4. Make the script persistent on the remote (tmux window or systemd user service)
5. Build local consumer (tmux status line and/or Swift notification app)
6. Eventually swap to hooks backend by replacing `get_sessions_tmux` with `get_sessions_hooks`

---

## Reference: two repos that inspired this

**`gavraz/recon`** — Rust TUI dashboard for managing many Claude Code sessions in tmux. Uses the same tmux-scraping approach: `tmux capture-pane` + status bar text patterns + `~/.claude/sessions/{pid}.json`. Also shows a "tamagotchi" pixel-art view of sessions.

**`sk-ruban/notchi`** — macOS notch companion app (Swift). Uses Claude Code hooks instead of scraping: registers shell scripts via `~/.claude/settings.json`, hook scripts forward JSON payloads to a Unix domain socket, app runs a state machine and animates sprites per session. This is what the hooks backend will look like.
