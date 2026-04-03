# ccmonitor

Lightweight monitor for active Claude Code sessions running in tmux on a remote Linux server. Writes a plain-text status table to `~/.claude/run/status` for consumption over SSH.

**Progress log**: see `current_progress.md` (updated 2026-04-02) — includes reboot
survival chart and failure mode analysis

**macOS app research**: see `macos-app-research.md` (2026-04-02) — notchi/claude-island
analysis, Swift building blocks, proposed app structure

**macOS app plan**: see `macos-app-plan.md` (2026-04-02) — implementation plan with
milestones, file structure, SSH considerations, verification steps

## Long-term vision

Modify [claude-island](https://github.com/farouqaldori/claude-island) (cloned at
`claude-island/` in this repo) to work with remote Claude Code sessions via SSH.

**Stage 1 — Readonly**: claude-island consumes the status file fetched over SSH,
displaying remote session states (working/blocked/idle) in the notch UI. The fetch +
parse layer from ccmonitor replaces claude-island's local Unix socket listener.

**Stage 2 — Interactive**: Send commands to remote sessions from the Mac. Approve
permissions, send prompts, or interact with blocked sessions — routed over SSH to the
remote tmux panes. claude-island becomes a full remote control for Claude Code sessions.

## Files

| File | Purpose |
|---|---|
| `claude_status.py` | Main monitor — polls state files every 2s, writes `~/.claude/run/status` |
| `hooks/ccmonitor-hook.sh` | Hook script — receives Claude Code lifecycle events, writes per-session state |
| `setup.sh` | Idempotent setup — installs hook, merges settings.json, creates dirs |
| `diagnose.py` | Diagnostic tool — dumps raw pane captures and classifier results (tmux backend) |
| `plan.md` | Original architecture decisions and design constraints |
| `current_progress.md` | Current state, what works, what's left |

## Architecture (hooks backend, active)

```
Claude Code session
    ├─ PreToolUse/PostToolUse  → hook → writes "working"  to ~/.claude/run/state/{sid}
    ├─ Stop                    → hook → writes "idle"      to ~/.claude/run/state/{sid}
    ├─ Notification            → hook → writes "blocked"   to ~/.claude/run/state/{sid}
    ├─ SessionStart            → hook → writes "idle"      to ~/.claude/run/state/{sid}
    │
claude_status.py  (polls every 2s)
    ├─ reads ~/.claude/run/state/*         → state per session
    ├─ reads ~/.claude/sessions/*.json     → session name, Claude PID
    ├─ tmux list-panes + _find_pane_pid()  → tmux target (eval:2.0, etc.)
    │
    └─→ ~/.claude/run/status  (atomic write, plain text table)
```

## Setup

```bash
bash setup.sh    # installs hook, updates settings.json, creates state dir
```

Requires `jq`. Restart Claude Code sessions after running for hooks to take effect.

## Running

```bash
python claude_status.py          # persistent loop
cat ~/.claude/run/status         # one-shot read
ssh remote cat ~/.claude/run/status   # from local Mac
```

## Key design decisions

- **Three states only**: `working`, `blocked`, `idle`
- **Plain file output**: atomic write via tmp + `os.replace`
- **Backend swap**: `get_sessions = get_sessions_hooks` (one line to revert to tmux scraping)
- **PID resolution**: session files use Claude's PID as filename; ancestor walk finds tmux pane
- **Stale cleanup**: dead PIDs pruned automatically on each poll
