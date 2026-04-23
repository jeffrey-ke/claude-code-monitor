# Remote chat content in the notch (Stages 1+2)

Shipped the hook-driven path that delivers user + assistant messages from remote
SSH-bridged Claude Code sessions into the Mac notch's chat view. Local sessions
are untouched; their chat still comes from `ConversationParser` reading JSONL
on disk.

Stage 3 of the original plan (permission request detail view) was **not built**
in this round — deferred. Active plan remains at
`.docs_claude/plans/active/remote-messages-and-permissions.md` with Stages 1+2
marked done.

## What ships

- **Remote user messages**: `UserPromptSubmit` hook forwards `data["prompt"]`
  as `state["message"]` + `message_role=user`. Appears as a normal user bubble
  in the notch.
- **Remote assistant messages**: `Stop` hook reads the JSONL tail on the remote
  host to extract the final assistant text for the current turn. Forwarded as
  `state["message"]` + `message_role=assistant`.
- **JSONL-flush race handling**: the Stop hook blocks up to a 2s ceiling while
  polling for a *newly-written* assistant entry (UUID changes vs the pre-poll
  snapshot). Exploits Claude Code's synchronous hook execution to impose the
  "entry observed before forwarding" ordering — symmetric with how
  `PermissionRequest` imposes "user decision before continuation."
- **Placeholder on timeout**: if the 2s ceiling expires with no new entry,
  forwards `"(assistant message unavailable — see remote terminal for latest
  reply; JSONL flush timeout)"` with `message_stale=True`. Renders italic +
  dimmed in the notch so the user can tell placeholder apart from a real reply.
- **Dedup**: chat-item id `hook-{event}-{sessionId}-{role}-{timestampMs}`
  prevents double-appends if events arrive more than once.
- **Remote-session gating**: Mac-local JSONL file reads, file-watchers, and
  agent-file parsing are all gated on `!isRemote` so remote sessions never
  trigger wasted I/O or "Failed to open file" warnings for files that only
  exist on the remote host.

## Design decisions worth remembering

| Decision | Choice | Why |
|---|---|---|
| Transport for messages | Existing TCP hook bridge | Zero new infrastructure |
| Assistant source | JSONL tail on the remote, read inside the hook | Claude Code's Stop stdin JSON doesn't include the reply text |
| Freshness detection | **UUID-change** vs snapshot at hook start | Wall-clock thresholds always fail for rapid back-to-back turns (see bug #4 below) |
| Ceiling behavior | 2s hard ceiling → placeholder, never stale content | Silent incorrectness (stale text shown as current reply) is worse than brief honest silence |
| Local-session guard | Gate chatItem append on `event.isRemote` | ConversationParser still owns local chat items; mixing would double-append |
| `isRemote` signal | Payload-tagged by the hook (`is_remote: true`) | Peer-IP inference fails because SSH tunnels make remote connections look like loopback (see bug #1) |
| Project-dir naming | Replace `/`, `.`, **and `_`** with `-` | Claude Code replaces `_` too; we missed it (see bug #3) |

## Bugs discovered during verification

1. **`isRemote` via peer IP was always false for remote sessions.** The Mac
   inferred remoteness from `peerAddr.sin_addr.s_addr != inet_addr("127.0.0.1")`
   in `HookSocketServer.acceptTCPConnection`. SSH reverse tunnels forward
   remote TCP connections so they arrive on the Mac as `127.0.0.1` — same as
   the Mac's own statusline. Every remote event was flagged local, so none of
   the gates fired and no chat content ever appeared.
   **Fix**: the remote hook now adds `"is_remote": true` to every payload.
   `HookEvent` decodes it directly; the peer-IP inference is gone.

2. **The Mac app bundled a stale copy of `ccbridge-hook.py`.** The installer
   deploys from `claude-island/ClaudeIsland/Resources/ccbridge-hook.py`, which
   had drifted from the tracked source at `hooks/ccbridge-hook.py` (6456 B vs
   10428 B). Remote hosts kept getting pre-Stage-1+2 code even after a rebuild.
   **Fix**: new `PBXShellScriptBuildPhase` ("Sync hooks from repo") copies
   `hooks/{ccbridge-hook,bridge_send,ccmonitor-statusline}.py` into `Resources/`
   at build time. Declared input/output paths let Xcode skip the copy when
   nothing's changed.

3. **Project-dir naming convention missed `_`.** Claude Code's JSONL path is
   `~/.claude/projects/{cwd with / . _ → -}/…`. Our code only replaced `/` and
   `.`, so for a `cwd` like `/home/jeffk/repo/visual_servoing/datagen2_isaacsim`
   we read from a non-existent directory. Hit this on the first remote session
   whose path had underscores.
   **Fix**: added `.replace("_", "-")` in
   `hooks/ccbridge-hook.py:_project_dir`, and in 6 Swift sites across
   `ConversationParser.swift`, `JSONLInterruptWatcher.swift`, and
   `AgentFileWatcher.swift` (latent bug for local users with underscored paths).

4. **30-second "fresh" window was too wide for back-to-back turns.** The Stop
   poll originally accepted any assistant entry whose `timestamp` was within
   `hook_start_ts - 30s`. When the user asked two questions in quick
   succession, the previous turn's entry (written seconds ago) fell inside
   the window and was returned as "fresh." Notch showed the wrong answer
   attached to the new turn.
   **Fix**: switched detector from wall-clock threshold to
   **UUID-change**. Snapshot the last-assistant entry's UUID at hook start,
   poll until a different UUID shows up. No window, no race with rapid turns.

5. **"API Error: Output blocked by content filtering policy" on test
   prompts.** Red herring — Anthropic's server-side safety classifier
   triggered on "recite a poem"-type prompts. Confirmed: our hook never
   writes to stdout on `UserPromptSubmit`/`Stop`, so it cannot inject content
   into the prompt. Noted here only because it initially looked like the
   hook was breaking Claude Code.

## Files changed

Python (hooks):
- `hooks/ccbridge-hook.py` — new message forwarding on UserPromptSubmit/Stop,
  `_project_dir`, `_read_last_assistant_entry` (returns text+uuid),
  `_poll_fresh_assistant_message` (2s blocking UUID-change poll),
  `is_remote: true` payload tag.

Swift (`claude-island/ClaudeIsland/…`):
- `Services/Hooks/HookSocketServer.swift` — `HookEvent` gains `messageRole`,
  `messageStale`; `isRemote` moved from post-decode assignment to
  payload-decoded field; removed peer-IP inference in `acceptTCPConnection`.
- `Services/State/SessionStore.swift` — new `appendRemoteChatMessage` helper;
  gates on `!isRemote` for `scheduleFileSync`, `loadHistoryFromFile`,
  `populateSubagentToolsFromAgentFiles`; new public `isRemote(sessionId:)`
  lookup for cross-module gating.
- `Services/Session/ClaudeSessionMonitor.swift` — gate `InterruptWatcherManager.startWatching` on `!event.isRemote`.
- `Services/Session/ConversationParser.swift` — `_` → `-` added to all 4
  project-dir derivations.
- `Services/Session/JSONLInterruptWatcher.swift` — `.` and `_` replacements
  added (was missing both).
- `Services/Session/AgentFileWatcher.swift` — `.` and `_` replacements added.
- `Services/Chat/ChatHistoryManager.swift` — `ChatHistoryItem` gains
  `isStale: Bool = false`; `syncFromFile` gated on remote-session lookup.
- `UI/Views/ChatView.swift` — `AssistantMessageView` accepts `isStale`;
  stale bubbles render italic + dimmed, skipping Markdown.

Build plumbing:
- `claude-island/ClaudeIsland.xcodeproj/project.pbxproj` — added
  `PBXShellScriptBuildPhase` "Sync hooks from repo" as the first build phase;
  runs `cp hooks/*.py → Resources/`.
- `claude-island/ClaudeIsland/Resources/{ccbridge-hook,bridge_send,ccmonitor-statusline}.py`
  — initial manual sync (subsequent builds handle this automatically).

## What's deferred

- **Stage 3**: permission detail view — expandable `PermissionDetailView`
  inside `InstanceRow` showing full tool input (full Bash command, full
  Edit old→new, etc.). Will be started as a fresh plan when picked up.
- **Stage 4 polish**: expand/collapse for very long assistant replies in
  the chat list; keyboard shortcut for approve/deny while permission detail
  is visible.
- Minor: the "Hooks + statusLine already installed" log message is
  misleading (it refers to settings.json, not the hook file bytes — the
  files *are* redeployed every reconnect). Trivial wording fix available.
