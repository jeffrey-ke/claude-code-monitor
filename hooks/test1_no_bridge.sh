#!/usr/bin/env bash
# Test 1: No bridge_port file — hook should exit silently with code 0
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

rm -f ~/.claude/run/bridge_port

echo '{"hook_event_name":"PreToolUse","session_id":"test","cwd":"/tmp","tool_name":"Bash"}' \
  | python3 "$SCRIPT_DIR/ccbridge-hook.py"
rc=$?

if [ $rc -eq 0 ]; then
    echo "PASS: hook exited with code 0 (no bridge_port file)"
else
    echo "FAIL: hook exited with code $rc (expected 0)"
fi
