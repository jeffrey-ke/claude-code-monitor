# Architecture

## Overview

ccmonitor is a two-machine system: a Linux server running Claude Code sessions in tmux,
and a Mac running claude-island (notch overlay app) to monitor and control them.

## Data flow

```
┌─ LINUX SERVER ──────────────────────────────────────────────┐
│                                                              │
│  Claude Code session (in tmux pane)                         │
│    └─ hook fires on lifecycle events                        │
│        ├─ ccmonitor-hook.sh → state file (working/idle/blocked)
│        └─ ccbridge-hook.py → state file + TCP to Mac        │
│                                                              │
│  claude_status.py (poll loop, 2s)                           │
│    ├─ reads state files + session metadata + tmux targets   │
│    └─ writes ~/.claude/run/status (plain-text table)        │
│                                                              │
└──────────────── SSH reverse tunnel (port 19876) ────────────┘
                            ↕
┌─ MAC ───────────────────────────────────────────────────────┐
│                                                              │
│  claude-island (macOS notch app)                            │
│    ├─ HookSocketServer: Unix socket (local) + TCP (remote)  │
│    ├─ SSHTunnelManager: ssh -N -R (auto-reconnect)          │
│    ├─ SessionStore (actor): single mutation point            │
│    ├─ ClaudeSessionMonitor: @MainActor → SwiftUI            │
│    └─ Notch UI: session list, approve/deny, send messages   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## State flow

1. Claude Code fires a hook event (PreToolUse, Stop, PermissionRequest, etc.)
2. Hook script maps event to state and writes `~/.claude/run/state/{session_id}`
3. Bridge hook also sends JSON over TCP to Mac (via SSH tunnel on port 19876)
4. Mac's `HookSocketServer` receives it, decodes, dispatches to `SessionStore`
5. `SessionStore.process(_:)` updates session state (actor-isolated)
6. Combine publisher notifies `ClaudeSessionMonitor` on main thread
7. SwiftUI redraws notch UI

## Permission approval flow

1. `PermissionRequest` event arrives at bridge hook
2. Hook connects to Mac, sends JSON, blocks on `recv()` (up to 300s)
3. Mac shows approve/deny in notch UI
4. User clicks → `HookSocketServer.respondToPermission()` writes JSON response
5. Hook receives response, outputs `hookSpecificOutput` JSON to Claude Code
6. If user approves locally instead, hook's socket gets closed → exits cleanly

## Key abstractions

- **Backend swap**: `get_sessions = get_sessions_hooks` (one line in claude_status.py)
- **Transport swap**: `send_event()` in ccbridge-hook.py (AF_INET → AF_UNIX)
- **Single mutation point**: all SessionStore state changes via `process(_:)`
- **Port discovery**: hook reads `~/.claude/run/bridge_port` to find tunnel port
