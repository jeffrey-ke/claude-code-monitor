---
description:
alwaysApply: true
---

# ccmonitor

Remote monitor & control for Claude Code sessions via SSH bridge.

## Quick start

The Mac app (claude-island) handles setup automatically:
1. Open claude-island → notch menu → SSH Bridge → select a host
2. App establishes reverse SSH tunnel, deploys hooks, writes `bridge_port`
3. Remote Claude Code sessions appear in the notch within seconds

For the standalone server-side monitor (no Mac app):
```bash
bash setup.sh              # install hooks, merge settings.json
python claude_status.py    # persistent poll loop → ~/.claude/run/status
```

## Module index

| Module | Role | Key exports |
|---|---|---|
| `claude_status.py` | Server monitor — polls state files every 2s, resolves tmux targets, writes status table | `get_sessions()`, `write_status()` |
| `hooks/ccmonitor-hook.sh` | Server hook — maps lifecycle events to working/idle/blocked state files | stdin JSON → `~/.claude/run/state/{sid}` |
| `hooks/ccbridge-hook.py` | Bridge hook — sends events to Mac via TCP, handles permission responses | `send_event()`, hookSpecificOutput JSON |
| `setup.sh` | Server setup — installs ccmonitor hook, merges settings.json (idempotent) | one-time install |
| `diagnose.py` | Server diagnostics — dumps pane captures, classifier results | one-shot verification |
| `claude-island/` | macOS notch app (Swift, git submodule) — displays sessions, approves permissions, sends messages | See `claude-island/CLAUDE.md` |

## Data flow

See `.docs_claude/architecture.md` for the full architecture diagram and flows.

## Key design decisions

- **Three states only**: `working`, `blocked`, `idle`
- **Atomic file writes**: tmp + `os.replace` everywhere
- **Backend swap**: `get_sessions = get_sessions_hooks` (one line to revert to tmux scraping)
- **Transport swap**: `send_event()` in ccbridge-hook.py is the single TCP/Unix swap point
- **Port discovery**: hook reads `~/.claude/run/bridge_port`; missing file = no bridge = exit 0
- **No hook uninstall**: hooks are harmless when bridge is down (can't connect → exit 0)
- **Stale tunnel cleanup**: `SSHTunnelManager` kills orphaned `ssh -N` processes on startup

## Where to look next

Documentation, plans, style guidance, and investigation notes live in `.docs_claude/`.

- `.docs_claude/plans/active/` — plans currently in progress
- `.docs_claude/plans/completed/` — finished plans (includes SSH bridge plan)
- `.docs_claude/style-and-beliefs/` — code style and design principles
- `.docs_claude/architecture.md` — system architecture and data flow diagrams
- `.docs_claude/progress.md` — stage history and what's been built
- `ssh-bridge-bugs.md` — bugs found during SSH bridge development
- `claude-island/CLAUDE.md` — Swift app build commands and architecture

## Plans & workflow

Plans are first-class artifacts in `.docs_claude/plans/`.

- **Small change** (one file, obvious fix): no plan needed.
- **Medium change** (new feature, wire up a subsystem): lightweight plan in `plans/active/`.
- **Complex change** (new architecture, pipeline redesign): full execution plan with goal, approach, staged checklist, and decision log in `plans/active/`.

Move completed plans to `plans/completed/`.

**Before planning any new implementation:**
1. Read `plans/active/` — don't duplicate in-progress work.
2. Read `plans/completed/` — learn from past decisions and avoid re-solving solved problems.
3. Read relevant docs in `.docs_claude/` — context that shaped the current design.

## Core beliefs

Before planning any implementation, read `/reusable-parts` and apply its guidelines to the design.
