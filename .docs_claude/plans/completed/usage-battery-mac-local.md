# Usage Battery Bar — Mac-local sources (Phase 2)

Extends the Phase 1 usage battery (see `usage-battery-bar.md`) so Mac-local
Claude Code sessions also feed the account-global bar, not just remote hosts
over SSH.

## Problem

Phase 1 shipped data from remote hosts only. When the user was working in
Claude Code on the Mac itself, the bar never refreshed.

## Pipeline addition

Same pipeline as Phase 1 but with a loopback source:

```
Mac-local Claude Code
  → invokes statusLine wrapper (~/.claude/hooks/ccmonitor-statusline-wrapper.sh)
    ├─ fire-and-forget → ccmonitor-statusline.py (reads stdin, writes usage.json, TCP-sends)
    └─ delegates stdin → ~/.claude/statusline-original.sh (preserved user prompt)
  → TCP-send hits localhost:19876 (loopback via ~/.claude/run/bridge_port)
  → HookSocketServer receives, dispatches Usage event
  → SessionStore → NotchView battery updates
```

## Changes

1. **`HookInstaller.swift`** — on app launch, copies
   `ccmonitor-statusline.py` + `bridge_send.py` from bundle to
   `~/.claude/hooks/`, writes `ccmonitor-statusline-wrapper.sh`, merges
   `.statusLine` into `~/.claude/settings.json`. If a prior
   `.statusLine.command` existed, saves it verbatim to
   `~/.claude/statusline-original.sh` so the wrapper can delegate to it.
   Idempotent; `statusline-original.sh` written only once so user edits
   survive re-runs.

2. **`HookSocketServer.swift`** — writes `~/.claude/run/bridge_port` with
   `Self.tcpPort` (19876) after TCP listen succeeds; removes on `stop()`.
   Mirrors the remote-side write in `SSHTunnelManager.writeBridgePort(host:)`.

3. **`HookSocketServer.acceptTCPConnection()`** — captures the peer
   `sockaddr_in` and flags `isRemote: false` when peer is 127.0.0.1, so logs
   and state distinguish Mac-local from real remote hosts.

4. **`SSHTunnelManager.launchTunnel()`** — splits into an async shim that
   first runs `pkill -u "$USER" -f "^sshd: $USER$"` on the remote (via ssh)
   to clear orphaned tunnel sessions, then launches the reverse tunnel. The
   regex targets only tunnel sessions (`sshd: user` with no tty suffix) —
   shell sessions (`sshd: user@pts/N`) and exec sessions (`sshd: user@notty`)
   are untouched. Fixes a long-standing bug where exit-255 reconnect loops
   couldn't recover if the Mac-side ssh client died uncleanly and left the
   remote sshd holding the forwarded port. (See `ssh-bridge-bugs.md` #3 for
   the original partial fix, which only cleaned up the Mac-side ssh client.)

## Out of scope (deferred)

- Stale-guard tuning (still 10 min).
- Persisting usage across Mac app restarts (loading `usage.json` on startup).
- Uninstaller to restore original `.statusLine`.

## Verification

- `jq '.statusLine.command' ~/.claude/settings.json` → wrapper path
- `cat ~/.claude/statusline-original.sh` → prior prompt verbatim
- `cat ~/.claude/run/bridge_port` → 19876
- Mac-local Claude Code prompt → `~/.claude/run/usage.json` populates
- Notch battery updates within seconds
- Logs distinguish `Received (remote)` vs `Received (local)` correctly
- After ungraceful quit + relaunch, tunnel reconnects without manual
  intervention on remote
