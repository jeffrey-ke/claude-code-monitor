#!/usr/bin/env bash
set -euo pipefail

HOOKS_SRC_DIR="$(cd "$(dirname "$0")" && pwd)/hooks"
HOOK_SRC="$HOOKS_SRC_DIR/ccmonitor-hook.sh"
HOOK_DST="$HOME/.claude/hooks/ccmonitor-hook.sh"
STATUSLINE_SRC="$HOOKS_SRC_DIR/ccmonitor-statusline.py"
STATUSLINE_DST="$HOME/.claude/hooks/ccmonitor-statusline.py"
BRIDGE_SEND_SRC="$HOOKS_SRC_DIR/bridge_send.py"
BRIDGE_SEND_DST="$HOME/.claude/hooks/bridge_send.py"
SETTINGS="$HOME/.claude/settings.json"
STATE_DIR="$HOME/.claude/run/state"

# 1. Directories
mkdir -p "$HOME/.claude/hooks" "$STATE_DIR"

# 2. Install hook scripts
cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"
cp "$STATUSLINE_SRC" "$STATUSLINE_DST"
chmod +x "$STATUSLINE_DST"
cp "$BRIDGE_SEND_SRC" "$BRIDGE_SEND_DST"

# 3. Merge hooks into settings.json
HOOK_CMD="bash $HOOK_DST"
HOOKS_JSON=$(cat <<'ENDJSON'
{
  "PreToolUse":        [{"matcher": "", "hooks": [{"type": "command", "command": "PLACEHOLDER"}]}],
  "PostToolUse":       [{"matcher": "", "hooks": [{"type": "command", "command": "PLACEHOLDER"}]}],
  "Stop":              [{"matcher": "", "hooks": [{"type": "command", "command": "PLACEHOLDER"}]}],
  "PermissionRequest": [{"matcher": "", "hooks": [{"type": "command", "command": "PLACEHOLDER"}]}],
  "UserPromptSubmit":  [{"matcher": "", "hooks": [{"type": "command", "command": "PLACEHOLDER"}]}],
  "SessionStart":      [{"matcher": "", "hooks": [{"type": "command", "command": "PLACEHOLDER"}]}]
}
ENDJSON
)
HOOKS_JSON=$(echo "$HOOKS_JSON" | jq --arg cmd "$HOOK_CMD" '
    walk(if type == "string" and . == "PLACEHOLDER" then $cmd else . end)
')

if [ -f "$SETTINGS" ]; then
    jq --argjson hooks "$HOOKS_JSON" '.hooks = $hooks' "$SETTINGS" > "${SETTINGS}.tmp"
    mv "${SETTINGS}.tmp" "$SETTINGS"
else
    echo "{\"hooks\": $HOOKS_JSON}" | jq . > "$SETTINGS"
fi

# 4. Merge statusLine (with clobber-guard for user-configured statuslines)
STATUSLINE_JSON=$(jq -n --arg cmd "$STATUSLINE_DST" '{type: "command", command: $cmd}')
EXISTING_STATUSLINE=$(jq -r '.statusLine.command // ""' "$SETTINGS" 2>/dev/null || echo "")
if [ -z "$EXISTING_STATUSLINE" ] || [ "$EXISTING_STATUSLINE" = "$STATUSLINE_DST" ]; then
    jq --argjson sl "$STATUSLINE_JSON" '.statusLine = $sl' "$SETTINGS" > "${SETTINGS}.tmp"
    mv "${SETTINGS}.tmp" "$SETTINGS"
    STATUSLINE_STATUS="Installed statusLine: $STATUSLINE_DST"
else
    STATUSLINE_STATUS="WARNING: existing statusLine is '$EXISTING_STATUSLINE' — skipped (invoke $STATUSLINE_DST from your wrapper to enable usage bar)"
fi

echo "Installed hook:    $HOOK_DST"
echo "Updated settings:  $SETTINGS"
echo "State directory:   $STATE_DIR"
echo "$STATUSLINE_STATUS"
echo ""
echo "Restart Claude Code sessions for hooks to take effect."
