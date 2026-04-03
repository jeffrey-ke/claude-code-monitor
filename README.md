# ccmonitor

Remote monitoring and control for Claude Code sessions running in tmux on a Linux server, displayed on a Mac via a notch overlay UI.

## What this is

Two components working together:

1. **Server side** (this repo root) — A lightweight hooks-based monitor that tracks Claude Code session states and writes them to a plain-text status file consumable over SSH.

2. **Mac side** (`claude-island/`) — A fork of [claude-island](https://github.com/farouqaldori/claude-island) being modified to display remote session states in the MacBook notch, and eventually send commands back to remote sessions over SSH.

## Architecture

```
Remote Linux server                          Local Mac
─────────────────                            ─────────
Claude Code sessions                         Claude Island (notch UI)
  ├─ hook events fire                          ├─ ssh fetch every 3s
  ├─ ccmonitor-hook.sh writes state files      ├─ parse status table
  ├─ claude_status.py polls + resolves tmux    ├─ display in notch overlay
  └─→ ~/.claude/run/status (plain text)        └─ approve/deny/send prompts (planned)
```

Session states: `working` (running tools), `blocked` (waiting for permission), `idle` (waiting for input).

## Server setup

```bash
bash setup.sh          # installs hook, updates settings.json, creates dirs
python claude_status.py  # start the monitor (writes ~/.claude/run/status)
```

Requires `jq`. Restart Claude Code sessions after setup for hooks to take effect.

## Status file format

```
TARGET       STATE    NAME                      CWD
eval:2.0     working  my-project                ~/repo/code
ipl:1.0      blocked  another-session           ~/other/dir
main:0.3     idle                               ~/work
```

Readable from anywhere: `ssh remote cat ~/.claude/run/status`

## Roadmap

| Stage | Description | Status |
|---|---|---|
| Server monitor | Hooks + status file on Linux | Done |
| Stage 1 | SSH fetch + parse from Mac app | Code written, awaiting test |
| Stage 2 | Remote sessions in notch UI | Planned |
| Stage 3 | Visual differentiation + settings | Planned |
| Stage 4 | Remote permission approval | Planned |
| Stage 5 | Richer remote detail (pane captures) | Planned |
| Stage 6 | Send prompts to remote sessions | Planned |

See `claude-island/remote-ssh-stages.md` for stage details.

## Files

| File | Purpose |
|---|---|
| `claude_status.py` | Main monitor — polls state files, writes status table |
| `hooks/ccmonitor-hook.sh` | Hook script — maps Claude events to working/blocked/idle |
| `setup.sh` | Idempotent setup — installs hook, merges settings, creates dirs |
| `claude-island/` | Fork of claude-island being modified for remote SSH monitoring |
| `current_progress.md` | Server-side progress and failure mode analysis |
| `macos-app-plan.md` | Original macOS consumer app design |
