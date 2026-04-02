# ccmonitor

Lightweight monitor for active Claude Code sessions running in tmux on a remote Linux server. Polls session state and writes a plain-text status table to `~/.claude/run/status` for consumption over SSH.

## Files

| File | Purpose |
|---|---|
| `claude_status.py` | Main monitor loop — polls every 2s, writes `~/.claude/run/status` |
| `diagnose.py` | Diagnostic tool — dumps raw pane captures and shows classifier results |
| `plan.md` | Architecture decisions and design constraints |
| `current_progress.md` | Debug progress and known open issues |

## Architecture

```
tmux panes (each running Claude Code)
    │
    │  tmux capture-pane → classify status bar text
    │  /proc/{pid}/status → walk process tree
    │  ~/.claude/sessions/{pid}.json → session metadata
    │
claude_status.py  (polls every POLL_INTERVAL seconds)
    │
    └─→ ~/.claude/run/status  (atomic write, plain text table)
```

**Backend swap**: one line change. `get_sessions = get_sessions_tmux` → `get_sessions = get_sessions_hooks` when the hooks backend is implemented. Everything downstream is identical.

## Key design decisions

**Three states only**: `working`, `blocked`, `idle`. No `compacting`, `new`, etc.

**Plain file output**: atomic write via tmp + `os.replace`. SSH already gives remote file access — no socket needed.

**Session PID mismatch**: `~/.claude/sessions/{pid}.json` uses Claude's PID, not the shell/pane PID. Must walk `/proc/{pid}/status` PPid field upward to find the ancestor pane PID. One session (PID 29144) runs inside nvim — chain is `claude → nvim → nvim → bash (pane)`. Walk depth of 10 is safe.

**Session file key**: `sessionId` (camelCase). Code checks both `sessionId` and `session_id` for safety.

**Stale session files**: Claude Code does not clean up `~/.claude/sessions/` on exit. Filter by checking `Path(f"/proc/{pid}").exists()` before use.

## Classification (`_classify_pane`)

```python
if "esc to interrupt" in lower:   → "working"
if "esc to cancel"    in lower:   → "blocked"
if [ctx: XX%] or token count:     → "idle"
```

**Known open issue**: `working` state is never detected in practice. Hypothesis: in plan mode, the status bar shows the plan mode indicator rather than `esc to interrupt` during tool execution. See `current_progress.md` for diagnostic steps.

## Running

```bash
python claude_status.py          # persistent loop, prints status each poll
python diagnose.py               # one-shot diagnostic dump
watch -n2 cat ~/.claude/run/status
```

## Consuming the output

```bash
ssh remote cat ~/.claude/run/status
watch -n2 "ssh remote cat ~/.claude/run/status"
```
