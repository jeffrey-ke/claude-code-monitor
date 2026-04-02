# ccmonitor — Progress as of 2026-04-02

## Current state: hooks backend is live

The hooks backend (`get_sessions_hooks`) is the active backend. It replaced the tmux
capture-pane scraping approach which had chronic pane-width truncation issues.

### What's deployed

- **Hook script**: `~/.claude/hooks/ccmonitor-hook.sh` — receives JSON from Claude Code
  lifecycle events, writes one state file per session to `~/.claude/run/state/{session_id}`
- **Settings**: `~/.claude/settings.json` has hooks registered for `PreToolUse`,
  `PostToolUse`, `Stop`, `Notification`, `SessionStart`
- **Setup script**: `setup.sh` — idempotent, works on a fresh machine. Creates dirs,
  installs hook, merges hooks into settings.json via `jq` without clobbering existing config
- **Monitor**: `claude_status.py` with `get_sessions_hooks()` as active backend.
  Reads state files, looks up session names from `~/.claude/sessions/*.json`,
  resolves tmux targets via `_find_pane_pid` ancestor walk, writes `~/.claude/run/status`

### What works

- **State detection**: `working`, `blocked`, `idle` all detected correctly via hook events
- **Tmux target resolution**: sessions show as `eval:2.0`, `ipl:1.0`, etc. — jumpable
  with `tmux switch-client -t <target>`
- **Stale session cleanup**: dead PIDs pruned automatically (state file deleted if
  session_id maps to a dead process)
- **Atomic writes**: both hook state files and status output use tmp + mv/os.replace

### Key discovery during implementation

`$PPID` in the hook script is NOT Claude's PID — it's an ephemeral intermediary process
spawned by Claude Code to run the hook. The real Claude PID comes from
`~/.claude/sessions/{pid}.json` (the filename IS the PID). The monitor resolves this by
building a `{session_id: {pid, name}}` index from session files, then matching against
`session_id` in the hook state files.

### Tmux scraping backend (preserved)

`get_sessions_tmux()` still exists in `claude_status.py`. To revert:
change `get_sessions = get_sessions_hooks` → `get_sessions = get_sessions_tmux`.

The scraping backend has known issues:
- Narrow panes truncate `[ctx: XX%]`, causing false `working` classification
- `working` detection relies on `↑ N tokens` pattern in pane text, which is fragile
- Sessions running inside nvim always show `[ctx:` in nvim's statusline, making
  idle/working ambiguous

### Reboot survival (as of 2026-04-02)

| Component | Survives reboot? | Manual action? |
|---|---|---|
| `~/.claude/hooks/ccmonitor-hook.sh` | Yes (file on disk) | No |
| `~/.claude/settings.json` (hooks config) | Yes (file on disk) | No |
| Claude Code sessions | No | User starts them in tmux |
| Hooks firing | Auto once sessions start | No |
| **`claude_status.py`** | **No — not daemonized** | **Must start manually** |
| Stale state files from old sessions | Linger on disk | No — monitor prunes dead PIDs |
| `~/.claude/run/status` | Stale from before reboot | Overwritten once monitor starts |

**Only manual step after reboot: start `claude_status.py`.** Failure is silent — status
file shows stale data with no alert. Fix: systemd user service or tmux auto-launch.

### Failure modes

| What dies | Symptom | Detection | Impact |
|---|---|---|---|
| Hook script fails | State files stop updating | Timestamps go stale | Monitor shows frozen state. **Silent.** |
| `claude_status.py` dies | Status file stops updating | File mtime stale | Local consumers see stale data. **Silent.** |
| tmux server dies | All sessions gone | `list-panes` errors | Monitor writes "(no sessions)". **Visible.** |
| Claude session ends | No more hook events | State file + dead PID | Monitor prunes it. **Handled.** |
| SSH connection drops | Local consumer sees nothing | SSH error | **Visible.** |
| `settings.json` malformed | Hooks don't register | No state files created | Monitor shows nothing. **Silent.** |

**Key risk**: silent staleness. Both "hook dies" and "monitor dies" produce stale-but-
plausible output. Mitigation: local consumer should check status file mtime and warn
if older than e.g. 10 seconds.

### Remaining work

1. Make `claude_status.py` persistent (systemd user service — auto-start, auto-restart)
2. Build local Mac consumer (tmux status line, `watch` + SSH, or Swift notification app)
3. Local consumer should check status file mtime to detect silent staleness
4. Sessions already running when hooks are installed don't appear until they fire an event
   — could add a hybrid fallback or accept the trickle-in behavior
