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
python ccstatus.py         # one-shot TSV of all sessions (pipe-friendly)
python ccstatus.py --json  # the Session[] contract (consumed by ccdash + others)
uv run --script ccdash.py  # live TUI dashboard (also bound to tmux `prefix G`)
python claude_status.py    # back-compat: == ccstatus.py --serve → ~/.claude/run/status
```

The stack is a **provider** (`ccstatus.py`, the single normalized source of truth,
sourced from the supported `claude agents --json` API) feeding **consumers** (the
`ccdash.py` TUI today; any tool that reads `ccstatus --json` tomorrow).

## Module index

| Module | Role | Key exports |
|---|---|---|
| `ccstatus.py` | **Provider** — normalized `Session` per live session from `claude agents --json` + tmux/`/proc` (pane id) + roster + synopsis cache; CLI `--json`/`--watch`/`--serve`/`--state` | `Session`, `get_sessions()` |
| `ccdash.py` | **Consumer #1** — Textual TUI (PEP-723 `uv run --script`); blocked-first table; peek panel = running understanding header + live pane tail; `enter`=jump via `tmux switch-client -t <pane_id>`; runs in `display-popup` | `CCDash` |
| `ccsynopsis.py` | **Evolving name** — Stop-hook async EMA summarizer; feeds the prior understanding + title back into `claude -p` Haiku (neutral cwd) so the name drifts slowly. Writes `{sid}.understanding` (hidden moving-average state) + `{sid}.synopsis` (sticky name read by ccstatus) | `--worker <sid> <transcript>` |
| `claude_status.py` | Back-compat shim → `ccstatus.py --serve` (writes `~/.claude/run/status` for claude-island) | `os.execv` |
| `hooks/ccmonitor-hook.sh` | Server hook — maps lifecycle events to working/idle/blocked state files (legacy; ccstatus no longer reads these) | stdin JSON → `~/.claude/run/state/{sid}` |
| `hooks/ccbridge-hook.py` | Bridge hook — sends events to Mac via TCP, handles permission responses | `send_event()`, hookSpecificOutput JSON |
| `setup.sh` | Server setup — installs ccmonitor hook, merges settings.json (idempotent) | one-time install |
| `diagnose.py` | Server diagnostics — dumps pane captures, classifier results | one-shot verification |
| `claude-island/` | macOS notch app (Swift, git submodule) — displays sessions, approves permissions, sends messages | See `claude-island/CLAUDE.md` |

## Data flow

See `.docs_claude/architecture.md` for the full architecture diagram and flows.

## Key design decisions

- **Provider / consumer split**: `ccstatus.py` gathers + normalizes (no display
  opinions); consumers (`ccdash.py`, pipes) own all display/sort/act policy. The TUI
  is the first usability test of the `Session` contract.
- **State from `claude agents --json`**, not pane scraping — the supported API gives
  state + Claude-generated title; the old `_classify_pane` regex heuristic is retired.
- **Age from transcript mtime**, not `sessions/<pid>.json statusUpdatedAt` (older CLI
  versions leave the latter hours stale).
- **Jump from a popup**: a `display-popup -E` is a pure overlay, so `tmux switch-client
  -t <pane_id>` retargets the underlying client; exiting closes the popup.
- **Synopsis is a moving average, not a snapshot**: each Stop step feeds the prior
  `{sid}.understanding` + title back to Haiku and asks it to evolve them *slowly*, so
  the name is sticky while the theme holds and lags through momentary tangents. The
  understanding is the richer hidden state; the title is its sticky projection.
- **Synopsis is async + neutral-cwd**: the Stop hook detaches `claude -p` (Haiku) so it
  never blocks; the neutral cwd + `--exclude-dynamic-system-prompt-sections` keep the
  summarized project's CLAUDE.md out of the summarizer's context.
- **Five states**: `blocked`, `busy`, `shell`, `idle`, `dead` (mapped back to
  working/blocked/idle by `--serve` for claude-island).
- **Atomic file writes**: tmp + `os.replace` everywhere; everything fail-open.
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
