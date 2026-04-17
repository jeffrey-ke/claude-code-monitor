# Fix: Parallel Tool Events Override waitingForApproval Phase

## Problem

When a `PermissionRequest` fires (e.g., WebSearch) while another tool runs in parallel (e.g., WebFetch), the approval UI in the notch vanishes instantly. The user never gets a chance to approve/deny.

**Root cause**: The state machine allows `waitingForApproval → processing` as the legitimate "tool was approved" path. But `PostToolUse` from an *unrelated* parallel tool also triggers this transition because `determinePhase()` returns `.processing` and `canTransition()` says yes.

Sequence:
1. `PermissionRequest(WebSearch)` → phase = `.waitingForApproval(WebSearch)` ✓
2. `PostToolUse(WebFetch)` → `.processing` → `canTransition` = `true` → phase overwritten ✗

## Fix

**File**: `claude-island/ClaudeIsland/Services/State/SessionStore.swift` — `processHookEvent()`

Added a guard before the phase transition: when session is `waitingForApproval`, only allow phase changes if:
- Event is about the **pending tool** (matching `toolUseId`)
- Event is a **new PermissionRequest** (another tool also needs approval)
- Event is **session-level** (`Stop`, `SessionEnd`, `UserPromptSubmit`)

Unrelated parallel tool events still get processed for tool tracking and chat items, but no longer overwrite the approval phase.

## Status

**Complete** — fix applied 2026-04-14. Needs rebuild on Mac.
