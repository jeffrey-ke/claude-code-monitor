#!/usr/bin/env bash
set -euo pipefail

HOOK_SRC="$(cd "$(dirname "$0")" && pwd)/hooks/ccmonitor-hook.sh"
HOOK_DST="$HOME/.claude/hooks/ccmonitor-hook.sh"
SETTINGS="$HOME/.claude/settings.json"
STATE_DIR="$HOME/.claude/run/state"

# 1. Directories
mkdir -p "$HOME/.claude/hooks" "$STATE_DIR"

# 2. Install hook script
cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"

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

echo "Installed hook:    $HOOK_DST"
echo "Updated settings:  $SETTINGS"
echo "State directory:   $STATE_DIR"
echo ""
echo "Restart Claude Code sessions for hooks to take effect."
