# SSH Bridge for Claude-Island: Implementation Plan

## Context

Claude-island is a macOS notch app that monitors local Claude Code sessions via a Unix
socket. We want it to also monitor and control **remote** Claude Code sessions running
on a Linux server. The remote hook connects to the Mac over a reverse SSH tunnel (TCP),
using the same JSON protocol claude-island already speaks. The Mac app needs a UI to
pick an SSH config host and manage the tunnel lifecycle.

Key decisions from our design conversation:
- TCP over reverse SSH tunnel (`ssh -R 19876:localhost:19876 remote`)
- Remote hook is nearly identical to `claude-island-state.py` but uses `AF_INET`
- Hook discovers bridge via `~/.claude/run/bridge_port` file convention
- Auto-deny on connection failure/timeout (safe fallback)
- Install hooks idempotently, never uninstall (harmless when bridge is down)
- Remote tmux send-keys over SSH for message sending
- Permission approval flows through the TCP socket response (same as local)

---

## Step 1: Remote hook script (`ccbridge-hook.py`)

**New file**: `hooks/ccbridge-hook.py`

Adapt `claude-island-state.py` (the existing local hook) with these changes:
- Read port from `~/.claude/run/bridge_port`. If file missing → `exit 0`
- Connect via `socket.AF_INET` to `localhost:<port>` instead of `AF_UNIX`
- Same JSON payload format, same blocking `recv()` for PermissionRequest
- On connect failure → `exit 0` (fall through to normal Claude UI)
- On recv timeout for permissions → auto-deny
- Also write state files to `~/.claude/run/state/{sid}` for `claude_status.py` compatibility

Transport abstraction: `send_event()` is the single function that handles the
socket connection. Swapping transport means changing only this function.

### Manual test 1: Hook in isolation
```bash
# On remote machine:
# 1. No bridge_port file — hook should exit silently
echo '{"hook_event_name":"PreToolUse","session_id":"test","cwd":"/tmp","tool_name":"Bash"}' | python3 hooks/ccbridge-hook.py
echo $?  # should be 0

# 2. Create a fake listener and bridge_port file
echo 19876 > ~/.claude/run/bridge_port
python3 -c "
import socket, json
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 19876))
s.listen(1)
print('listening...')
conn, addr = s.accept()
data = conn.recv(4096)
print('received:', json.loads(data))
conn.close()
s.close()
"
# In another terminal, send a test event:
echo '{"hook_event_name":"PreToolUse","session_id":"test","cwd":"/tmp","tool_name":"Bash"}' | python3 hooks/ccbridge-hook.py
# The listener should print the JSON payload

# 3. Test permission request flow
# Start listener that sends back an "allow" response:
python3 -c "
import socket, json
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 19876))
s.listen(1)
conn, addr = s.accept()
data = json.loads(conn.recv(4096))
print('got permission request:', data)
conn.sendall(json.dumps({'decision': 'allow'}).encode())
conn.close()
s.close()
"
# Send a PermissionRequest:
echo '{"hook_event_name":"PermissionRequest","session_id":"test","cwd":"/tmp","tool_name":"Bash","tool_input":{"command":"ls"},"status":"waiting_for_approval"}' | python3 hooks/ccbridge-hook.py
# Should print hookSpecificOutput JSON with decision allow

# Clean up
rm ~/.claude/run/bridge_port
```

**Feedback checkpoint**: Does the hook behave correctly in all three cases?

---

## Step 2: TCP listener in `HookSocketServer.swift`

**Modify**: `ClaudeIsland/Services/Hooks/HookSocketServer.swift`

Add a TCP server socket alongside the existing Unix socket:
- New method `startTCPServer(port:)` — creates `AF_INET` socket, binds to
  `127.0.0.1:<port>`, listens, creates a second `DispatchSource.makeReadSource`
- New `acceptTCPConnection()` — calls `accept()` on the TCP socket, delegates
  to the existing `handleClient(_:)` method (identical JSON handling)
- Store TCP socket fd (`private var tcpSocket: Int32 = -1`) and its
  DispatchSource (`private var tcpAcceptSource: DispatchSourceRead?`)
- `start()` calls both `startServer()` (Unix) and `startTCPServer(port: 19876)`
- `stop()` cleans up both sockets
- Port constant: `static let tcpPort: UInt16 = 19876`

The key insight: `handleClient(fd)` is transport-agnostic. It reads JSON from an
fd, parses it, dispatches events, and optionally holds the fd open for permission
responses. TCP and Unix socket fds are interchangeable at this level.

### Manual test 2: TCP listener
```bash
# Build and run claude-island on Mac (Cmd+R in Xcode)
# Check Console.app or Xcode console for: "Listening on TCP port 19876"

# From Mac terminal, test with netcat:
echo '{"session_id":"test-tcp","event":"UserPromptSubmit","status":"processing","cwd":"/tmp","pid":12345}' | nc localhost 19876

# Should see the event logged in Xcode console
# Should see a session appear briefly in the notch UI (or at least in logs)
```

**Feedback checkpoint**: Does the TCP listener accept connections and process events
the same way as the Unix socket?

---

## Step 3: SSH host picker UI + settings persistence

**Modify**: `ClaudeIsland/Core/Settings.swift`
- Add `remoteSSHHost: String` (stored in UserDefaults, default empty string)
- Add `remoteBridgeEnabled: Bool` (stored in UserDefaults, default false)

**New file**: `ClaudeIsland/UI/Components/RemoteHostPickerRow.swift`
- SwiftUI view that parses `~/.ssh/config` to list available hosts
- Parse logic: read file, extract `Host` directives (skip wildcards like `Host *`)
- Dropdown/picker showing host names, selected host stored in `AppSettings`
- Toggle for enabling/disabling the bridge
- Show connection status (connected/disconnected/reconnecting)

**Modify**: `ClaudeIsland/UI/Views/NotchMenuView.swift`
- Add `RemoteHostPickerRow` in the settings section (after Sound picker, before
  system settings divider)

### SSH config parsing
```swift
// Read ~/.ssh/config, extract Host lines
// "Host tesu" → "tesu"
// Skip "Host *", skip patterns with wildcards
func parseSSHConfigHosts() -> [String] {
    let configPath = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".ssh/config")
    guard let content = try? String(contentsOf: configPath) else { return [] }
    return content.components(separatedBy: .newlines)
        .compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("Host ") else { return nil }
            let host = String(trimmed.dropFirst(5)).trimmingCharacters(in: .whitespaces)
            guard !host.contains("*") && !host.contains("?") else { return nil }
            return host
        }
}
```

### Manual test 3: Host picker
```bash
# Build and run (Cmd+R)
# Open the notch menu (click notch → settings/menu)
# Should see a "Remote Host" picker with hosts from ~/.ssh/config
# Select a host → quit and relaunch → should persist the selection
# Toggle bridge on/off → should persist
```

**Feedback checkpoint**: Does the picker show your SSH hosts? Does the selection
persist across app restarts?

---

## Step 4: SSH tunnel manager

**New file**: `ClaudeIsland/Services/Remote/SSHTunnelManager.swift`

Actor that manages the reverse SSH tunnel lifecycle:
- `connect(host:)` — launches `ssh -N -R 19876:localhost:19876 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes <host>` via `Process`
- `disconnect()` — terminates the SSH process
- Monitors process termination — on unexpected exit, reconnects after 5s delay
- Publishes connection state: `.disconnected`, `.connecting`, `.connected`, `.reconnecting`
- On connect success: writes `bridge_port` file on remote via separate SSH command:
  `ssh <host> "mkdir -p ~/.claude/run && echo 19876 > ~/.claude/run/bridge_port"`
- On disconnect: optionally removes the bridge_port file (best effort, non-blocking)

State detection: `ssh -N` produces no stdout. We detect "connected" when the process
has been alive for >2 seconds without exiting (the SSH handshake and tunnel setup
complete in <1s if the host is reachable). Alternatively, probe with
`ssh <host> 'ss -tln | grep 19876'` after launch.

### Manual test 4: Tunnel
```bash
# Build and run with a valid SSH host selected and bridge enabled
# Check Xcode console for tunnel connection logs
# On remote, verify tunnel is up:
ssh <your-host> 'ss -tln | grep 19876'
# Should show LISTEN on 127.0.0.1:19876

# Kill the SSH process manually:
pkill -f "ssh -N -R 19876"
# App should detect and reconnect within ~5-10 seconds
# Check Xcode console for reconnection logs

# Quit the app cleanly
# Verify tunnel is gone:
ssh <your-host> 'ss -tln | grep 19876'
# Should show nothing
```

**Feedback checkpoint**: Does the tunnel establish? Does it reconnect on failure?
Does it clean up on quit?

---

## Step 5: Remote hook installer

**New file**: `ClaudeIsland/Services/Remote/RemoteHookInstaller.swift`

Installs the bridge hook on the remote machine over SSH. Called once after tunnel
connects. Idempotent — safe to run repeatedly.

Steps:
1. Copy `ccbridge-hook.py` to remote: pipe the script content via
   `ssh <host> "cat > ~/.claude/hooks/ccbridge-hook.py && chmod +x ~/.claude/hooks/ccbridge-hook.py"`
   (The script is bundled in the app's Resources, same as `claude-island-state.py`)
2. Read remote settings: `ssh <host> "cat ~/.claude/settings.json"`
3. Parse JSON, check each event type for a hook containing `ccbridge-hook.py`
4. If missing, add it (same structure as `HookInstaller.swift:71-88`)
5. If changed, write back: pipe new JSON via
   `ssh <host> "cat > ~/.claude/settings.json"`

Bundle the hook script: add `ccbridge-hook.py` to the Xcode project as a resource
(same as `claude-island-state.py` is bundled).

### Manual test 5: Hook installation
```bash
# With tunnel connected, check remote:
ssh <your-host> 'cat ~/.claude/hooks/ccbridge-hook.py' # should exist
ssh <your-host> 'jq .hooks ~/.claude/settings.json'    # should contain ccbridge entries

# Run app again — should NOT duplicate hooks
ssh <your-host> 'jq ".hooks.PreToolUse | length" ~/.claude/settings.json'
# Count should not increase on repeated launches

# Start a NEW Claude Code session on remote
# Should see it appear in the notch UI on Mac!
```

**Feedback checkpoint**: Are hooks installed? Are they idempotent? Do remote
sessions start appearing in the UI?

---

## Step 6: Remote tmux commands

**New file**: `ClaudeIsland/Services/Remote/RemoteTmuxController.swift`

Actor that sends commands to remote tmux sessions over SSH:
- `sendMessage(host:target:text:)` — runs
  `ssh <host> tmux send-keys -t <target> -l <text>` then
  `ssh <host> tmux send-keys -t <target> Enter`
- `sendEscape(host:target:)` — runs
  `ssh <host> tmux send-keys -t <target> Escape`

Uses `ProcessExecutor.shared.run()` (already exists) for command execution.

**Modify**: `ClaudeIsland/Services/Session/ClaudeSessionMonitor.swift`
- When routing a permission approval for a remote session, it goes through the TCP
  socket response (handled by `HookSocketServer.respondToPermission()` — already works
  because the TCP fd is stored in `pendingPermissions` just like a Unix fd)
- When sending a text message to a remote session, use `RemoteTmuxController`
  instead of local `TmuxController`
- Need a way to distinguish remote vs local sessions — add `isRemote` flag to
  `SessionState` or check if session came through TCP

### Manual test 6: Remote commands
```bash
# With remote sessions visible in the notch:
# 1. Trigger a permission request on remote (e.g. run a Bash command)
#    → Should appear in notch with approve/deny buttons
#    → Click approve → should go through, command executes on remote
#    → Check remote tmux pane — the tool should have been approved

# 2. Try deny — should deny with message

# 3. Send a message to a remote session via the notch UI
#    → Should appear in the remote tmux pane
```

**Feedback checkpoint**: Can you approve/deny remote permissions from the Mac?
Can you send messages?

---

## Step 7: Wire everything together in AppDelegate

**Modify**: `ClaudeIsland/App/AppDelegate.swift`

Startup sequence becomes:
1. (existing) `HookInstaller.installIfNeeded()` — local hooks
2. (existing) Window setup, screen observer, updater
3. (new) Start `HookSocketServer` with TCP listener enabled
4. (new) If `AppSettings.remoteBridgeEnabled` and host is set:
   a. `SSHTunnelManager.shared.connect(host:)`
   b. On connected: `RemoteHookInstaller.install(host:)`
5. (existing) Remove Stage 1 temporary SSH test code

React to settings changes:
- Host changed or bridge toggled → disconnect old tunnel, connect new one
- Bridge disabled → disconnect tunnel

**Modify**: `ClaudeIsland/Services/Session/ClaudeSessionMonitor.swift`
- Tag sessions from TCP connections as remote (could add a flag in `HookEvent`
  based on which accept loop accepted the connection, or infer from missing
  local PID)

### Manual test 7: End-to-end
```bash
# Fresh app launch with remote host configured:
# 1. App starts → tunnel connects → hooks install → bridge_port written
# 2. Start Claude Code session on remote
# 3. Session appears in notch within 1-2 seconds
# 4. Make Claude do something that needs approval
# 5. Approve from Mac notch
# 6. Command executes on remote
# 7. Session shows "working" while processing, "idle" when done
# 8. Change SSH host in settings → old tunnel drops, new one connects
# 9. Disable bridge → tunnel drops, remote sessions disappear
# 10. Quit app → tunnel drops, hooks remain but are harmless
```

**Feedback checkpoint**: Full flow working end-to-end?

---

## Files summary

| File | Action | Purpose |
|---|---|---|
| `hooks/ccbridge-hook.py` | **new** | Remote hook script (bundled as resource) |
| `Services/Hooks/HookSocketServer.swift` | **modify** | Add TCP listener alongside Unix socket |
| `Core/Settings.swift` | **modify** | Add `remoteSSHHost`, `remoteBridgeEnabled` |
| `UI/Components/RemoteHostPickerRow.swift` | **new** | SSH config host picker UI |
| `UI/Views/NotchMenuView.swift` | **modify** | Add remote host picker to menu |
| `Services/Remote/SSHTunnelManager.swift` | **new** | SSH tunnel lifecycle |
| `Services/Remote/RemoteHookInstaller.swift` | **new** | Idempotent remote hook install |
| `Services/Remote/RemoteTmuxController.swift` | **new** | Remote tmux send-keys over SSH |
| `Services/Session/ClaudeSessionMonitor.swift` | **modify** | Route remote sessions correctly |
| `App/AppDelegate.swift` | **modify** | Wire tunnel + installer into startup |

New Swift files must be added to the Xcode project manually after `git pull`
(Right-click group -> Add Files -> check ClaudeIsland target).

## Build

After each step that adds/modifies Swift files:
```bash
git pull  # on Mac
# Open ClaudeIsland.xcodeproj
# Add any new .swift files to project
# Cmd+R to build and run
```
