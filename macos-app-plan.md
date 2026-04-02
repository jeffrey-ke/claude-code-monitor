# Plan: macOS CCMonitor Consumer App

## Context

The remote server writes `~/.claude/run/status` — a fixed-width plain text table with
session target, state, name, and cwd. The macOS app fetches this file over SSH, parses
it, and feeds consumers (menu bar, notifications, future widgets). The interface between
fetch and consumers is the parsed data — consumers never touch SSH or the raw file.

This is an MVP: menu bar app with a dropdown showing session states and macOS
notifications when a session transitions to `blocked`.

---

## Milestone 1: Xcode project + menu bar skeleton

Create a Swift Package / Xcode project at `ccmonitor/macos/CCMonitor/`.

**Files:**

### `CCMonitorApp.swift`
```
@main, App protocol
MenuBarExtra scene — icon changes color based on aggregate state:
  - red circle:  any session blocked
  - green circle: all sessions idle
  - yellow circle: any session working (none blocked)
Content: StatusView()
```

### `Models/SessionStatus.swift`
```
struct SessionStatus: Identifiable, Equatable
  id = target (e.g. "eval:2.0")
  target: String
  state: String     // "working", "blocked", "idle"
  name: String
  cwd: String
```

---

## Milestone 2: Fetch + Parse

### `StatusParser.swift`
Pure function, no I/O. Parses the fixed-width table from `write_status()`:

```
Header:  TARGET       STATE    NAME                      CWD
Columns: {:<12}       {:<8}    {:<25}                    rest
Offsets: 0-11         13-20    22-46                     48+
```

Special case: line is `(no sessions detected)` → return empty array.

### `StatusFetcher.swift`
`@Observable` class, owns the polling loop.

```
- ssh host configurable (stored in UserDefaults, editable from menu)
- Timer fires every 3 seconds
- Runs: /usr/bin/ssh <host> cat ~/.claude/run/status
- Async via Task { } — Process runs on background thread, publishes on @MainActor
- Publishes: sessions: [SessionStatus], lastFetch: Date?, fetchError: String?
- Tracks previousSessions for transition detection
- SSH timeout: 5 seconds (kill process if it hangs)
```

---

## Milestone 3: Menu bar view

### `Views/StatusView.swift`
SwiftUI view shown in MenuBarExtra dropdown:

```
- Section: list of sessions, each row shows:
    colored circle (red/yellow/green) + name (or target if unnamed) + state
    subtitle: cwd (shortened)
- Section: status line ("Last updated: 3s ago" or "SSH error: ...")
- Section: Settings
    - Text field: SSH host (e.g. "tesu")
    - Polling interval stepper
- Divider + Quit button
```

---

## Milestone 4: Notification consumer

### `Consumers/NotificationConsumer.swift`
Subscribes to StatusFetcher. On each update:

```
- Compare current vs previous sessions
- For each session that transitioned TO "blocked" (wasn't blocked before):
    fire UNNotificationRequest with:
      title: "Claude blocked"
      body: "{name or target} needs permission"
      identifier: target (deduplicates)
- Request notification permission on first launch
```

---

## File structure

```
macos/CCMonitor/
├── CCMonitor.xcodeproj/
├── CCMonitor/
│   ├── CCMonitorApp.swift
│   ├── StatusFetcher.swift
│   ├── StatusParser.swift
│   ├── Models/
│   │   └── SessionStatus.swift
│   ├── Views/
│   │   └── StatusView.swift
│   └── Consumers/
│       └── NotificationConsumer.swift
└── Info.plist                 # LSUIElement = true
```

---

## Implementation order

1. Create Xcode project (SwiftUI App, macOS 14+)
2. `SessionStatus` model
3. `StatusParser` — pure function, testable standalone
4. `StatusFetcher` — SSH + timer + @Observable
5. `CCMonitorApp` — MenuBarExtra wired to StatusFetcher
6. `StatusView` — dropdown UI
7. `NotificationConsumer` — blocked transition alerts
8. Test end-to-end: run app, verify it shows remote sessions, trigger a blocked
   state, verify notification fires

---

## SSH considerations

- Assumes `~/.ssh/config` has the host alias (e.g. `Host tesu`) with key auth
- No password prompts — if SSH hangs, the 5s timeout kills the process
- If SSH fails, `fetchError` is set and shown in the menu bar dropdown
- Poll interval of 3s means ~3s latency for state changes. Acceptable for "ambient
  awareness" — notifications for `blocked` don't need sub-second delivery

---

## What this does NOT include (future)

- WidgetKit widget (separate target, needs shared App Group container)
- Notch visualization (NSPanel like notchi/claude-island)
- Multiple remote hosts
- Sound on notification (system default only for now)

---

## Verification

1. Build and run from Xcode
2. Confirm menu bar icon appears with correct color
3. Confirm dropdown lists sessions matching `cat ~/.claude/run/status` on remote
4. Trigger a permission prompt on remote → verify notification fires on Mac
5. Kill `claude_status.py` on remote → verify error shown in dropdown
6. Change SSH host in settings → verify it reconnects
