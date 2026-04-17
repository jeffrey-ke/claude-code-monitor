# Progress & Stage History

## Stage 1 — Readonly SSH fetch (completed 2026-04-03)

claude-island fetches `~/.claude/run/status` over SSH, parses fixed-width table,
displays remote sessions in console log. StatusParser + RemoteSessionStatus model.

See: `claude-island/stage1-progress.md`

## Stage 2 — SSH Bridge (completed 2026-04-14)

Full bidirectional bridge: remote hook sends events to Mac via TCP over reverse SSH
tunnel. Mac can approve/deny permissions and send messages to remote sessions.

Components built:
- `ccbridge-hook.py` — remote hook (TCP transport, state file compat)
- TCP listener in `HookSocketServer` (alongside Unix socket)
- SSH host picker in notch menu (reads `~/.ssh/config`)
- `SSHTunnelManager` — reverse tunnel lifecycle, auto-reconnect, stale cleanup
- `RemoteHookInstaller` — idempotent hook deployment over SSH
- `RemoteTmuxController` — tmux send-keys over SSH
- `isRemote` flag on HookEvent/SessionState for routing

See: `.docs_claude/plans/completed/ssh-bridge-plan.md`, `ssh-bridge-bugs.md`

## Not yet built

- Chat message display for remote sessions (requires JSONL fetch or hook extension)
- Visual differentiation (SSH badge on remote sessions)
- Multiple simultaneous remote hosts
