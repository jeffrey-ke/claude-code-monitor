# SSH Bridge: Bugs Found During Development

## 1. ScrollView with zero-height frame hides SSH hosts

**Symptom**: The SSH host picker showed only "None" — all 7 hosts from `~/.ssh/config` were parsed correctly (confirmed via console log) but invisible in the UI.

**Cause**: The `hosts` array was initialized as empty (`[]`) and populated in `onAppear`. The `ScrollView` had `.frame(maxHeight: CGFloat(min(hosts.count, 6)) * 32)`, which evaluated to `maxHeight: 0` on first render because `hosts` was still empty when the expanded view appeared.

**Fix**: Initialize `hosts` eagerly with `parseSSHConfigHosts()` at declaration time instead of in `onAppear`. Also replaced `ScrollView` with a plain `ForEach` since 7 hosts doesn't need scrolling.

**Lesson**: In SwiftUI, `@State` properties used in frame calculations must have correct initial values — `onAppear` fires too late for layout.

## 2. `String(contentsOf: URL)` silently fails for `~/.ssh/config`

**Symptom**: `parseSSHConfigHosts()` returned empty, with the `guard let content = try?` silently swallowing the error.

**Fix**: Changed from `String(contentsOf: URL, encoding:)` to `String(contentsOfFile: path, encoding:)` using the `.path` property of the URL. Added debug logging to surface failures.

**Lesson**: When reading user files from a GUI app, prefer `contentsOfFile:` with a path string. Always add logging behind `try?` during development.

## 3. Stale SSH tunnel processes survive app quit

**Symptom**: After stopping the app in Xcode (stop button) and relaunching, the new tunnel failed with exit code 255 because the old `ssh -N -R 19876:localhost:19876` process was still alive, holding the remote port.

**Cause**: Xcode's stop button sends SIGKILL, which doesn't trigger `applicationWillTerminate` or Process termination handlers. The SSH child process gets orphaned.

**Fix**: Two-part fix:
1. Added `SSHTunnelManager.shared.disconnect()` in `applicationWillTerminate` for clean quits
2. Added `killStaleTunnels()` that runs `pkill -f "ssh -N -R 19876"` before launching a new tunnel

**Lesson**: Always assume child processes can outlive the parent. Kill stale processes by pattern on startup.

## 4. Bridge hook auto-deny conflicts with local permission approval

**Symptom**: When a permission request appeared in both the notch UI and the remote terminal, approving it locally in the terminal caused the bridge hook to emit repeated "Bridge connection lost — auto-denied for safety" errors.

**Cause**: Both hooks run in parallel. When the user approved locally, the `PostToolUse` event fired and the Mac side closed the bridge hook's TCP socket. The hook's `recv()` returned empty (connection closed), hit the "bridge_port exists but no response" path, and auto-denied — conflicting with the already-approved permission.

**Fix**: Removed the auto-deny fallback entirely. If `send_event()` returns `None` for any reason (can't connect, connection closed, timeout), the hook exits cleanly with no output, letting Claude Code's normal UI handle it.

**Lesson**: When multiple hooks can respond to the same event, the fallback behavior must be "do nothing" (exit 0 with no output), not "deny". Only emit a decision when you actually got one from the user.

## 5. New Swift files not appearing in Xcode project

**Symptom**: Files created on the Linux server (via Claude Code) weren't available to add to the Xcode project after `git pull`.

**Context**: This is expected behavior — Xcode projects track files explicitly in `project.pbxproj`. Files created outside Xcode must be added manually (Right-click group → Add Files → check target).

**For `.py` resource files**: Must be added to the **Copy Bundle Resources** build phase, not **Compile Sources**. The file appears in the project navigator but Xcode may default to the wrong build phase for non-Swift files.

## 6. Parallel tool events override waitingForApproval phase

**Symptom**: When a PermissionRequest fires (e.g., WebSearch) while another tool is running in parallel (e.g., WebFetch), the approval UI flashes in the notch for a fraction of a second and disappears. The user never gets a chance to approve/deny.

**Cause**: The state machine allows `waitingForApproval → processing` (legitimate path for "tool was approved"). But `PostToolUse` from an *unrelated* parallel tool also triggers this transition. Sequence:
1. `PermissionRequest(WebSearch)` → phase = `.waitingForApproval(WebSearch)` ✓
2. `PostToolUse(WebFetch)` → `determinePhase()` returns `.processing` → `canTransition(.waitingForApproval → .processing)` = `true` → phase overwritten to `.processing` ✗

The state machine can't distinguish "the pending tool was approved" from "some other tool completed."

**Impact**: Any session running parallel tools (Agents, concurrent tool calls) will lose permission request visibility if another tool event arrives before the user acts.

**Fix needed**: In `SessionStore.processHookEvent`, when the session is in `.waitingForApproval`, only allow transition to `.processing` if the event's `toolUseId` matches the pending permission's `toolUseId`. Non-matching tool events should be processed (tool tracking, chat items) but should NOT change the phase.

## 7. Zombie `waitingForApproval` chatItems resurrect the phase

**Symptom**: After approving a real permission request in the notch, the notch silently stays in "waiting for approval" for the next ~tens of seconds or until the next legitimate `PermissionRequest`/`Stop` arrives. Unrelated tool events in the window are visibly logged as `Preserving waitingForApproval: ignoring ... for unrelated tool`. The user sees "has a question for me" with nothing actually pending.

**Cause**: A consequence of the #6 fix. When a permission is approved, `processPermissionApproved` asks `findNextPendingTool` (`SessionStore.swift`) whether another tool is waiting. That function scans `session.chatItems` for any `.toolCall` whose `status == .waitingForApproval`. If a prior remote Claude Code session died mid-prompt (terminal killed, network blip, hook crash before `PostToolUse`), its chatItem never transitions away from `.waitingForApproval` — it's a zombie. `findNextPendingTool` returns it, and phase flips to `.waitingForApproval(zombieId)`. No event will ever match `zombieId`, so the #6 guard preserves the phase against every real event until a session-level event (`Stop` / `SessionStart` / `UserPromptSubmit`) arrives.

Parallel leak: `HookSocketServer.pendingPermissions[zombieId]` is also stranded with an open socket for the same reason (visible as a persistent entry in `cancelPendingPermission: ... currentKeys=[<zombieId>]` logs).

**Fix applied**:
1. `findNextPendingTool` now cross-checks `HookSocketServer.isPermissionLive(toolUseId:)` — a zombie chatItem with no live socket is skipped.
2. Turn-boundary sweep: on `Stop` / `SessionStart` / `UserPromptSubmit`, any `.waitingForApproval` chatItem without a live socket is re-marked `.interrupted` (`sweepZombieApprovals`). This also cleans up the chat UI so zombie tools don't render as "pending" forever.

The authoritative signal is the live socket in `HookSocketServer.pendingPermissions`, not the chatItem status, because the socket is the only thing the remote hook can actually be blocking on.
