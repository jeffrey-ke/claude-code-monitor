# Plan: Remote Session Messages & Permission Details in Notch

## Goal

Three issues with the notch UI:
1. **Remote session messages aren't shown** — the notch shows status (working/idle/blocked) but no conversation content for remote sessions
2. **Permission request details aren't shown** — the notch shows tool name + truncated 1-line input, but not enough detail to make an informed allow/deny decision
3. **BUG: Parallel tool events override waitingForApproval** — when a PermissionRequest fires while other tools run in parallel, the approval UI vanishes instantly because an unrelated PostToolUse transitions the phase from `waitingForApproval` to `processing` (see `ssh-bridge-bugs.md` #6)

## Root Cause Analysis

### Why remote messages are missing

Local sessions get chat history via `ConversationParser` reading `~/.claude/projects/{projectDir}/{sessionId}.jsonl` on the Mac. For remote sessions, that JSONL file lives on the remote Linux machine — not accessible locally.

The bridge hook (`ccbridge-hook.py`) sends status events over TCP but omits message content:
- `UserPromptSubmit`: has `prompt` field in stdin JSON but hook doesn't forward it
- `Stop`: fires when Claude finishes but stdin JSON doesn't include assistant response text
- `PreToolUse`/`PostToolUse`: sends tool name + input (these DO work — tool calls appear)

So the notch shows tool activity for remote sessions but zero user/assistant text messages.

### Why permission details are insufficient

`PermissionContext` has `toolName`, `toolInput` (full dict), and `formattedInput` (truncated to 100 chars/value). The `InstanceRow` shows this on a single line:

```
Bash  command: git status && git diff...
```

For a security-sensitive decision (allow Bash to run `rm -rf`?), the user needs to see the full command, the full file path for Edit/Write, etc. — not a 1-line truncation.

## Approach

### Part 1: Remote Session Messages

**Strategy**: Forward message content through the existing TCP bridge hook events. No new transport needed.

#### 1a. User messages via `UserPromptSubmit`

The hook receives `data["prompt"]` from Claude Code. Forward it:

**`ccbridge-hook.py`** — in the `UserPromptSubmit` branch:
```python
if event == "UserPromptSubmit":
    state["status"] = "processing"
    state["message"] = data.get("prompt")  # NEW: forward user's message text
```

**`HookEvent` (Swift)** — already has a `message: String?` field. No struct change needed.

**`SessionStore.processHookEvent`** — when event is `UserPromptSubmit` and `message` is present:
- Create a `ChatHistoryItem` of type `.user(message)` and append to `session.chatItems`
- Update `session.conversationInfo.lastMessage`, `.lastMessageRole = "user"`, `.firstUserMessage`, `.lastUserMessageDate`

#### 1b. Assistant messages via `Stop` event

The `Stop` hook stdin JSON does NOT include assistant response text. Two options:

**Option A (recommended): Read last assistant message from JSONL in the hook**

At `Stop` time, the hook reads the tail of the JSONL conversation file to extract the last assistant message. The JSONL path follows the convention `~/.claude/projects/{projectDir}/{sessionId}.jsonl` where `projectDir` = `cwd` with `/` and `.` replaced by `-`.

```python
# In ccbridge-hook.py, Stop branch:
def _read_last_assistant_message(session_id, cwd):
    """Read the last assistant message from the JSONL conversation file."""
    project_dir = cwd.replace("/", "-").replace(".", "-")
    jsonl_path = Path.home() / ".claude" / "projects" / project_dir / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return None, None
    # Read last ~8KB (enough for the last few messages)
    with open(jsonl_path, "rb") as f:
        f.seek(0, 2)  # end
        size = f.tell()
        f.seek(max(0, size - 8192))
        tail = f.read().decode("utf-8", errors="replace")
    # Parse lines in reverse, find last assistant message
    for line in reversed(tail.strip().split("\n")):
        try:
            entry = json.loads(line)
            if entry.get("type") == "assistant":
                # Extract text from content blocks
                for block in entry.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        return block["text"], entry.get("message", {}).get("model")
        except json.JSONDecodeError:
            continue
    return None, None
```

Then in the `Stop` branch:
```python
elif event == "Stop":
    state["status"] = "waiting_for_input"
    text, model = _read_last_assistant_message(session_id, cwd)
    if text:
        state["message"] = text
        state["message_role"] = "assistant"
```

**Option B (fallback): SSH from Mac to read remote JSONL**

More complex, higher latency, requires SSH connection management. Only consider if Option A proves insufficient (e.g., JSONL not yet flushed at `Stop` time).

#### 1c. Swift-side: Process message content from hook events

**`HookEvent`** — add one new field:
```swift
let messageRole: String?  // "user" or "assistant"
```

CodingKeys: `message_role`

**`SessionStore.processHookEvent`** — after phase transition, add message processing:
```swift
// After existing phase/tool processing:
if let message = event.message, !message.isEmpty {
    let role = event.messageRole ?? (event.event == "UserPromptSubmit" ? "user" : nil)
    if let role = role {
        appendMessageFromHook(session: &session, role: role, message: message)
    }
}
```

New helper:
```swift
private func appendMessageFromHook(session: inout SessionState, role: String, message: String) {
    let itemType: ChatHistoryItemType = role == "user" ? .user(message) : .assistant(message)
    let item = ChatHistoryItem(
        id: "hook-\(role)-\(Date().timeIntervalSince1970)",
        type: itemType,
        timestamp: Date()
    )
    session.chatItems.append(item)
    
    // Update conversation info
    session.conversationInfo.lastMessage = String(message.prefix(200))
    session.conversationInfo.lastMessageRole = role
    if role == "user" {
        session.conversationInfo.lastUserMessageDate = Date()
        if session.conversationInfo.firstUserMessage == nil {
            session.conversationInfo.firstUserMessage = String(message.prefix(200))
        }
    }
}
```

**Guard for local sessions**: Only create chat items from hook messages for `isRemote` sessions. Local sessions get their messages from JSONL parsing, so we'd get duplicates otherwise.

#### 1d. Summary extraction for remote sessions

Remote sessions won't get `summary` from JSONL parsing. Use the first user message as the display title (already the fallback in `displayTitle`). This is good enough — the summary is a nice-to-have parsed from the JSONL `summary` field.

### Part 2: Permission Request Detail View

**Strategy**: Add an expandable detail section below the `InstanceRow` when waiting for approval. Show the full tool input in a structured, readable format.

#### 2a. Permission detail view component

New view: `PermissionDetailView.swift`

Shows when a session is `waitingForApproval` and the user taps the row (or the row auto-expands):

```
┌──────────────────────────────────────────┐
│ ✻ Fix bug in auth module                │  ← session title
│   Bash  git diff HEAD~1                 │  ← tool + preview (existing)
│ ┌────────────────────────────────────┐   │
│ │ command:                           │   │  ← EXPANDED detail
│ │ git diff HEAD~1 --stat &&          │   │
│ │ git diff HEAD~1 -- src/auth.py     │   │
│ │                                    │   │
│ │ description:                       │   │
│ │ Show recent auth changes           │   │
│ └────────────────────────────────────┘   │
│              [Chat] [Deny] [Allow]       │
└──────────────────────────────────────────┘
```

Key design:
- Auto-expand for the first/only waiting-for-approval session
- Dark code-block background (`.white.opacity(0.05)`) with monospace font
- Show each tool input field on its own labeled line
- Full content — no truncation (scroll if needed, but most inputs fit)
- Special formatting per tool type:
  - **Bash**: show `command` prominently, `description` secondary
  - **Edit**: show `file_path`, `old_string` → `new_string` as a diff
  - **Write**: show `file_path`, first ~20 lines of `content`
  - **Read**: show `file_path`
  - **Grep/Glob**: show `pattern`, `path`
  - **WebFetch**: show `url`
  - **MCP tools**: show all fields

#### 2b. Integrate into InstanceRow

In `ClaudeInstancesView.swift`, modify `InstanceRow`:
- Add `@State private var isExpanded: Bool` (default `true` when `isWaitingForApproval`)
- When expanded and waiting for approval, show `PermissionDetailView` below the title line
- Single tap toggles expansion; approval buttons stay visible regardless

#### 2c. PermissionContext improvements

`PermissionContext.formattedInput` currently truncates to 100 chars. Add a new property:

```swift
/// Full structured input for detail view (no truncation)
var fullInput: [(label: String, value: String)] {
    guard let input = toolInput else { return [] }
    return input.map { key, value in
        let valueStr: String
        switch value.value {
        case let str as String: valueStr = str
        case let num as Int: valueStr = String(num)
        case let num as Double: valueStr = String(num)
        case let bool as Bool: valueStr = bool ? "true" : "false"
        default: valueStr = String(describing: value.value)
        }
        return (label: key, value: valueStr)
    }.sorted { a, b in
        // Put most important fields first per tool type
        fieldPriority(a.label) < fieldPriority(b.label)
    }
}

private func fieldPriority(_ field: String) -> Int {
    switch field {
    case "command", "file_path", "path", "url", "pattern", "query": return 0
    case "description", "old_string", "new_string", "content": return 1
    default: return 2
    }
}
```

## Staged Checklist

### Stage 0: Fix parallel tool approval override (COMPLETE ✓)
- [x] Diagnosed bug: parallel PostToolUse events override `waitingForApproval` phase
- [x] Fixed `SessionStore.processHookEvent`: when `waitingForApproval`, only allow phase change if event matches pending toolUseId, is a new PermissionRequest, or is session-level (Stop/End/UserPromptSubmit)
- [x] Documented in `ssh-bridge-bugs.md` #6
- [x] Saved as completed plan: `plans/completed/fix-parallel-tool-approval-override.md`

### Stage 1: User messages for remote sessions
- [ ] `ccbridge-hook.py`: Forward `data["prompt"]` as `state["message"]` in `UserPromptSubmit`
- [ ] `HookEvent` (Swift): Add `messageRole: String?` field with CodingKey `message_role`
- [ ] `SessionStore.processHookEvent`: For remote sessions, create `ChatHistoryItem` from hook message
- [ ] Update `conversationInfo` from hook messages (lastMessage, lastMessageRole, etc.)
- [ ] Test: send message to remote session, verify it appears in notch chat view

### Stage 2: Assistant messages for remote sessions
- [ ] `ccbridge-hook.py`: Add `_read_last_assistant_message()` helper
- [ ] `ccbridge-hook.py`: In `Stop` branch, read + forward last assistant message
- [ ] `SessionStore`: Process assistant messages same as user messages (guarded by `isRemote`)
- [ ] Test: remote Claude responds, verify assistant text appears in notch chat view
- [ ] Handle edge cases: empty JSONL, partial writes, very long assistant messages (truncate at ~4KB in hook)

### Stage 3: Permission detail view
- [ ] Add `PermissionContext.fullInput` computed property (no truncation, sorted by priority)
- [ ] Create `PermissionDetailView.swift` — structured display of full tool input
- [ ] Add tool-specific formatting (Bash command block, Edit diff, file paths, etc.)
- [ ] Integrate into `InstanceRow` with expand/collapse state
- [ ] Auto-expand when waiting for approval, collapse after decision
- [ ] Test: trigger permission request, verify full details visible in notch

### Stage 4: Polish
- [ ] Remote session badge (small "SSH" or antenna icon next to session title)
- [ ] Handle message deduplication if bridge reconnects (use hook event timestamp as ID component)
- [ ] Truncate very long assistant messages in the chat list (show "..." with expand)
- [ ] Keyboard shortcut for approve/deny when permission detail is showing

## Decision Log

| Decision | Choice | Rationale |
|----------|--------|-----------|
| User messages | Forward via hook `message` field | Zero new infrastructure, reuses existing HookEvent.message |
| Assistant messages | Read JSONL tail in hook at Stop time | Only the hook has local filesystem access; avoids SSH from Mac |
| Permission details | Expandable inline detail, not separate view | Keeps context — user sees session + details + buttons together |
| Local session guard | Only create chatItems from hooks for `isRemote` | Local sessions use JSONL parsing; mixing would cause duplicates |
| JSONL read size | 8KB tail | Last assistant message is rarely >4KB; 8KB gives margin |

## Files Modified

| File | Change | Status |
|------|--------|--------|
| `claude-island/.../SessionStore.swift` | Guard phase transitions during `waitingForApproval` | **Done** |
| `ssh-bridge-bugs.md` | Documented bug #6 | **Done** |
| `hooks/ccbridge-hook.py` | Forward user message, read+forward assistant message | Planned |
| `claude-island/.../HookSocketServer.swift` | Add `messageRole` to `HookEvent` | Planned |
| `claude-island/.../SessionStore.swift` | Process hook messages into chatItems for remote sessions | Planned |
| `claude-island/.../SessionPhase.swift` | Add `fullInput` to `PermissionContext` | Planned |
| `claude-island/.../ClaudeInstancesView.swift` | Expand/collapse + permission detail integration | Planned |
| `claude-island/.../PermissionDetailView.swift` | **NEW** — structured permission detail display | Planned |
