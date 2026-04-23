# Fix: Zombie waitingForApproval ChatItems Resurrect the Phase

## Problem

After approving a real permission request in the notch, the notch silently stays in "waiting for approval" for ~tens of seconds (until the next legitimate `PermissionRequest` / `Stop` arrives). Unrelated tool events in the window log as `Preserving waitingForApproval: ignoring ... for unrelated tool`. The user sees "has a question for me" when nothing is actually pending.

**Root cause**: A direct consequence of the bug #6 fix (`fix-parallel-tool-approval-override`). When a permission is approved, `processPermissionApproved` asks `findNextPendingTool` whether another tool is waiting. That function scanned `session.chatItems` for any `.toolCall` whose `status == .waitingForApproval`. If a prior remote Claude Code session died mid-prompt (terminal killed, network blip, hook crash before `PostToolUse`), its chatItem never transitioned away from `.waitingForApproval` — it's a zombie. `findNextPendingTool` returned it, phase flipped to `.waitingForApproval(zombieId)`, and no future event could ever match `zombieId` — so the #6 guard preserved the phase against every real event until a session-level event arrived.

Log evidence (from user's session `eab90516`):
```
7:13:10  permissionApproved(tool: toolu_01X57K)
7:13:10  Switched to next pending tool: toolu_01D1pv     ← zombie (never appeared as PreToolUse/PermissionRequest/PostToolUse in log)
7:13:13  Preserving waitingForApproval: ignoring PostToolUse for unrelated tool (event.toolUseId=toolu_01X57K...)
         ctx.toolUseId=toolu_01D1pv...                    ← stuck
...     (~20s of stuck phase until next real PermissionRequest)
```

Parallel leak: `HookSocketServer.pendingPermissions[<zombieId>]` also stranded with an open socket (visible as persistent `cancelPendingPermission: ... currentKeys=[toolu_017YjU67kc]` logs).

## Fix

The authoritative signal for "this permission is actually live" is a socket entry in `HookSocketServer.pendingPermissions`, not `ToolStatus.waitingForApproval` on a chatItem. The chatItem status is a rendering artifact; the socket is what the remote hook is actually blocking on.

**File**: `claude-island/ClaudeIsland/Services/Hooks/HookSocketServer.swift`
- Added `isPermissionLive(toolUseId:) -> Bool` — thread-safe locked read of `pendingPermissions`.

**File**: `claude-island/ClaudeIsland/Services/State/SessionStore.swift`
- `findNextPendingTool` now skips candidates where `HookSocketServer.isPermissionLive(toolUseId:) == false`, with a debug log (`findNextPendingTool: skipping zombie <id> (no live permission socket)`).
- New `sweepZombieApprovals(in:)` helper: re-marks stale `.waitingForApproval` chatItems as `.interrupted` when there's no matching live socket.
- Sweep fires on turn boundaries: `Stop`, `SessionStart`, `UserPromptSubmit`. This also cleans up the chat UI so zombie tools don't render as "pending" forever.

**File**: `ssh-bridge-bugs.md` — documented as bug #7.

## Verification

- `xcodebuild -scheme ClaudeIsland -configuration Debug build` → **BUILD SUCCEEDED**, no warnings.
- Expected runtime behavior on next repro:
  - After approving, log shows `findNextPendingTool: skipping zombie <id> (no live permission socket)` instead of `Switched to next pending tool`.
  - On next `Stop` / new prompt, zombies in chat history flip to `.interrupted`.

## Not addressed (follow-up candidates)

- The `pendingPermissions` map itself can still accumulate stranded sockets if a remote session dies. Not causing the UI symptom, but if it becomes an issue, add a periodic GC keyed on session inactivity or max age.

## Status

**Complete** — fix applied 2026-04-22. Built cleanly. Needs in-flight verification on the user's next stuck-session repro.
