#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="$HOME/.claude/run/state"
INPUT=$(cat)

event=$(echo "$INPUT" | jq -r '.hook_event_name')
session_id=$(echo "$INPUT" | jq -r '.session_id')
cwd=$(echo "$INPUT" | jq -r '.cwd // ""')

case "$event" in
    PreToolUse|PostToolUse|SubagentStart)  state="working" ;;
    Stop|SubagentStop|SessionStart)        state="idle"    ;;
    Notification)                          state="blocked" ;;
    *)                                     exit 0          ;;
esac

tmp="$STATE_DIR/${session_id}.tmp"
out="$STATE_DIR/${session_id}"

jq -nc \
    --arg state "$state" \
    --arg pid "$PPID" \
    --arg session_id "$session_id" \
    --arg cwd "$cwd" \
    --arg ts "$(date +%s)" \
    '{state:$state, pid:$pid, session_id:$session_id, cwd:$cwd, ts:$ts}' \
    > "$tmp"
mv "$tmp" "$out"

exit 0
