# Stage 3 — Permission Detail View in the Notch

## Context

When a remote (or local) Claude Code session hits a tool approval, the notch
currently shows a one-line truncated preview next to `[Chat] [Deny] [Allow]`:

```
Bash  command: git status && git diff HEAD~1 --stat && git dif...
```

100-character truncation per value in `PermissionContext.formattedInput`
(`SessionPhase.swift:19-39`). For security-sensitive decisions — "allow Bash
to run this?", "allow Edit to change this file?" — you can't see the full
command, the full diff, or the target file path, so you can't actually judge
allow vs. deny from the notch alone.

Stage 3 of `.docs_claude/plans/active/remote-messages-and-permissions.md`
adds an expanded, tool-aware detail panel that slots under the existing
`InstanceRow` tool preview line whenever the session is
`waitingForApproval`. The amber `?` indicator in the notch header
(from the completed `notch-battery-permission-swap`) stays as-is — it's the
compact cue; this is the expanded body.

Stages 1+2 shipped: remote user/assistant messages now flow through the hook.
Stage 3 is the last UI piece for the "remote messages & permissions" plan.

## Mechanisms we must not disturb

Three recently-shipped fixes touch the approval path. The plan below
respects all of them:

- **`fix-parallel-tool-approval-override`** (SessionStore phase-transition
  guard). Stage 3 is UI-only — no changes to `processHookEvent` or phase
  transitions, so the guard stays intact.
- **`fix-zombie-waitingforapproval`** (zombie chatItems + `isPermissionLive`
  socket cross-check). By the time `session.phase == .waitingForApproval`,
  there's a live socket — so the detail view will never be shown for a
  zombie. When a sweep moves the phase off `.waitingForApproval`, the row's
  `onChange(isWaitingForApproval)` auto-collapses the detail. No new socket
  lookups needed from the UI layer.
- **`notch-battery-permission-swap`** (amber `?` replaces the battery).
  Stage 3 does not touch `NotchView`, `shouldShowUsageBattery`, or
  `PermissionIndicatorIcon`. The `?` is the compact header cue; the new
  panel is the expanded body inside `InstanceRow`. They're complementary.

## Reuse first (what already exists)

| Need | Existing thing | Where |
|---|---|---|
| Active permission on a session | `session.activePermission: PermissionContext?` | `SessionState.swift:113` |
| Tool name for header | `MCPToolFormatter.formatToolName(_)` | `Utilities/MCPToolFormatter.swift` |
| Edit old→new diff | `EditInputDiffView(input: [String: String])` | `ToolResultViews.swift:65` |
| Diff lines (red/green) | `SimpleDiffView` | `ToolResultViews.swift:871` |
| Code preview w/ line numbers + overflow | `FileCodeView` | `ToolResultViews.swift:634` |
| Icon button (for chevron) | `IconButton` | `ClaudeInstancesView.swift:392` |
| Approve/deny wiring | `onApprove` / `onReject` closures already passed to `InstanceRow`; resolve to `ClaudeSessionMonitor.{approve,deny}Permission` | `ClaudeSessionMonitor.swift:101-130` |
| AnyCodable → String unwrap pattern | `ToolEventProcessor.extractToolInput` (private; we'll replicate the same switch) | `ToolEventProcessor.swift:210-224` |

No new socket/hook code, no new state store mutations, no new approve/deny
path.

## Approach

### 1. `PermissionContext` — add structured accessors

**File**: `claude-island/ClaudeIsland/Models/SessionPhase.swift`

Keep `formattedInput` (still used by the 1-line tool preview). Add:

```swift
/// AnyCodable-unwrapped copy of toolInput as strings. Mirrors the
/// unwrap logic in ToolEventProcessor.extractToolInput; extended to
/// handle Double and arrays so the detail view doesn't show "...".
var toolInputStrings: [String: String] {
    guard let input = toolInput else { return [:] }
    var out: [String: String] = [:]
    for (key, value) in input {
        out[key] = Self.stringify(value.value)
    }
    return out
}

/// Untruncated (label, value) pairs sorted so decision-relevant fields
/// (command, file_path, url, pattern, query) come first.
var fullInput: [(label: String, value: String)] {
    toolInputStrings
        .map { (label: $0.key, value: $0.value) }
        .sorted { Self.fieldPriority($0.label) < Self.fieldPriority($1.label) }
}

private static func fieldPriority(_ field: String) -> Int {
    switch field {
    case "command", "file_path", "path", "url", "pattern", "query": return 0
    case "description", "old_string", "new_string", "content": return 1
    default: return 2
    }
}

private static func stringify(_ v: Any) -> String {
    switch v {
    case let s as String: return s
    case let n as Int: return String(n)
    case let n as Double: return String(n)
    case let b as Bool: return b ? "true" : "false"
    case let arr as [Any]:
        return "[" + arr.map(stringify).joined(separator: ", ") + "]"
    default: return String(describing: v)
    }
}
```

(`toolInputStrings` is internally useful because `EditInputDiffView` takes
`[String: String]` directly — no conversion at the call site.)

### 2. `PermissionDetailView.swift` — new file

**File**: `claude-island/ClaudeIsland/UI/Views/PermissionDetailView.swift` (NEW)

Dispatches on raw `toolName`, not the `MCPToolFormatter` alias:

| Tool name | Renderer |
|---|---|
| `Bash` | Monospace block: `command` prominent, `description` dimmed below. Full command, no truncation, `.textSelection(.enabled)`. |
| `Edit`, `MultiEdit` | `EditInputDiffView(input: ctx.toolInputStrings)` — already wraps `SimpleDiffView` and derives `filename` from `file_path`. |
| `Write` | Filename header + `FileCodeView(filename:, content: input["content"] ?? "", startLine: 1, totalLines: ..., maxLines: 15)`. |
| `Read` | `file_path` prominent; optional `offset`/`limit` line-range badge. |
| `Grep`, `Glob` | `pattern` (monospace) + `path` dimmed if present. |
| `WebFetch`, `WebSearch` | `url` / `query` prominent; `prompt` below if present. |
| everything else (incl. `mcp__*`) | Generic: iterate `ctx.fullInput`, one row per field — label (dim) + value (monospace). |

Styling matches `FileCodeView` + `CodeLineView`
(`ToolResultViews.swift:634-730`):

- Container: `.background(Color.white.opacity(0.05))` + rounded corners +
  modest padding. Do **not** add a horizontal `ScrollView` — the outer
  vertical `ScrollView` in `instancesList` (`ClaudeInstancesView.swift:69`)
  handles overflow, and we want text to wrap, not scroll horizontally.
- Text: `.font(.system(size: 11, design: .monospaced))`,
  `.foregroundColor(.white.opacity(0.8))`, `.lineLimit(nil)`,
  `.textSelection(.enabled)`.
- Labels: `.foregroundColor(.white.opacity(0.4))`, slightly smaller.

### 3. `InstanceRow` — expand state + slot the detail view

**File**: `claude-island/ClaudeIsland/UI/Views/ClaudeInstancesView.swift`

Wrap the current `HStack` body (lines 148-276) in a `VStack(alignment:
.leading, spacing: 4)`. First child is the unchanged `HStack`; second is:

```swift
if let permission = session.activePermission,
   !isInteractiveTool,
   isDetailExpanded {
    PermissionDetailView(context: permission)
        .padding(.leading, 32)  // align with text column (14 indicator + 10 spacing + 8 lead padding)
        .padding(.trailing, 14)
        .transition(.opacity.combined(with: .move(edge: .top)))
}
```

Use `session.activePermission` directly — no ad-hoc extraction via pattern
matching.

State additions (~line 129):

```swift
@State private var isDetailExpanded: Bool = true
```

```swift
.onChange(of: session.phase.isWaitingForApproval) { _, waiting in
    isDetailExpanded = waiting   // expand on entry, collapse on exit
}
.animation(.spring(response: 0.3, dampingFraction: 0.8), value: isDetailExpanded)
```

Collapse toggle: add a chevron `IconButton` inside `InlineApprovalButtons`
(`ClaudeInstancesView.swift:328-388`) — icon `"chevron.up"` when expanded,
`"chevron.down"` when collapsed — wired via a new `onToggleDetail: () ->
Void` closure passed alongside `onChat`/`onApprove`/`onReject`. The existing
staggered animation covers the new button with a small added delay.

### Deliberately out of scope

- **`ChatApprovalBar` in `ChatView.swift`** (`ChatView.swift:1094`) has the
  same truncation issue. The new `PermissionDetailView` is written to be
  reusable there, but wiring it into the chat view is a *separate* change —
  don't touch `ChatView` now. User's ask was the notch.
- Stage 4 polish (long-message expand/collapse, keyboard shortcuts).
- `pendingPermissions` GC (flagged as a follow-up in
  `fix-zombie-waitingforapproval`).

## Critical files

| File | Change |
|---|---|
| `claude-island/ClaudeIsland/Models/SessionPhase.swift` | Add `toolInputStrings`, `fullInput`, `fieldPriority`, `stringify` to `PermissionContext` |
| `claude-island/ClaudeIsland/UI/Views/PermissionDetailView.swift` | **NEW** — per-tool detail renderer |
| `claude-island/ClaudeIsland/UI/Views/ClaudeInstancesView.swift` | VStack-ify `InstanceRow`, add `isDetailExpanded`, chevron toggle, slot detail view |

## Verification

After user builds the app themselves:

1. **Bash**: in any live session, ask Claude to run a multi-line Bash
   command. Notch row should auto-expand, show the full command in a
   monospace block; description below if present.
2. **Edit**: ask Claude to edit a file. `SimpleDiffView` should render full
   red/green diff with no truncation.
3. **Write**: ask Claude to create a new file >15 lines. Filename header
   + first 15 lines + `... (N more lines)` overflow marker.
4. **MCP / unknown tool**: trigger any MCP tool; generic `fullInput` output
   renders labels + values.
5. **Collapse/expand**: click the chevron in `InlineApprovalButtons`;
   panel collapses; click again; expands. Approve/deny still work in either
   state.
6. **Auto-expand reset**: deny one permission; trigger a new one. The new
   one opens expanded even if the previous had been collapsed.
7. **Remote session**: same flow over the SSH bridge. Hook already forwards
   full `toolInput` on `PermissionRequest`; no hook change needed.
8. **Parallel tool during approval** (bug #6 regression check): while one
   row is waiting for approval, let another session fire an unrelated
   `PostToolUse`. The approval row must stay expanded — confirms the
   phase-guard fix still holds under the new UI.
9. **Zombie session** (bug #7 regression check): kill a remote terminal
   mid-approval, trigger a new session. Old row should not render the
   detail view forever — `onChange` collapses it when phase moves off
   `.waitingForApproval`.
10. **Notch header**: amber `?` should still swap with the battery exactly
    as before. Notch width unchanged.
