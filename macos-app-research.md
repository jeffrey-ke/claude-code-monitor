# macOS Consumer App — Research Notes (2026-04-02)

## Our architecture vs. notchi / claude-island

Both notchi and claude-island run **locally** — they receive events via Unix domain
sockets from Claude Code hooks on the same machine. Our setup is different: Claude Code
runs on a **remote** Linux server. The macOS app's job is:

```
Remote: hooks → state files → claude_status.py → ~/.claude/run/status (plain text)
                                                        │
                                                   SSH fetch
                                                        │
Local Mac: fetch → local copy → parse → consumers (notifications, widgets, menu bar)
```

This means the macOS app is a **polling consumer**, not a socket listener. The interface
is the status file — same format regardless of whether it's read locally or over SSH.

## Reference apps

### notchi (sk-ruban/notchi)
- macOS 15+, SwiftUI + AppKit
- Lives in the MacBook notch area as a transparent NSPanel
- Animated pixel-art sprites per session (tamagotchi style)
- Unix socket listener at `/tmp/notchi-events.sock`
- Emotion analysis via Claude Haiku API (classifies user prompts as happy/sad/neutral)
- Sound notifications with terminal focus detection (suppresses when terminal is active)
- Session state: idle, working, sleeping, compacting, waiting
- Hook events: UserPromptSubmit, PreToolUse, PostToolUse, PermissionRequest, Stop, etc.

### claude-island (farouqaldori/claude-island)
- macOS 15.6+, SwiftUI + AppKit + Combine
- Also lives in the notch, but expands to show full chat history + tool results
- Permission management directly from the notch (approve/deny tool execution)
- Actor-based SessionStore (single source of truth, thread-safe)
- Incremental JSONL conversation parsing
- Python hook script at `~/.claude/hooks/claude-island-state.py`
- Unix socket at `/tmp/claude-island.sock`
- 9+ specialized tool result renderers (diffs, bash output, grep, etc.)
- Smart session sorting: approval > processing > waiting > idle

### Key patterns both apps share
- **NSPanel** positioned at notch, level `.mainMenu + 3`, non-activating
- **Hook installer** that auto-deploys scripts and merges settings.json
- **JSON over Unix socket** for event delivery
- **Singleton state stores** with reactive SwiftUI binding
- **LSUIElement = true** to hide from Dock

## What we need (much simpler)

We don't need sockets, conversation parsing, or emotion analysis. Our app:

1. **Fetches**: `ssh remote cat ~/.claude/run/status` on a timer (every 2-5 seconds)
2. **Parses**: the fixed-width text table into structured data
3. **Exposes**: parsed sessions to consumers

Consumers are independent modules that read the parsed data:

| Consumer | Trigger | Framework |
|---|---|---|
| Menu bar icon + dropdown | Always visible | SwiftUI MenuBarExtra |
| macOS notification | State transition → blocked | UNUserNotificationCenter |
| Desktop widget | Periodic refresh | WidgetKit |
| Notch visualization | Optional, future | NSPanel (like notchi) |

## Minimal Swift building blocks

### Menu bar app (entry point)
```swift
@main
struct CCMonitorApp: App {
    var body: some Scene {
        MenuBarExtra("CCMonitor", systemImage: "circle.fill") {
            StatusView()
        }
    }
}
```

### SSH fetch
```swift
func fetchStatus() -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
    process.arguments = ["remote", "cat", "~/.claude/run/status"]
    let pipe = Pipe()
    process.standardOutput = pipe
    try? process.run()
    process.waitUntilExit()
    return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
}
```

### Parse status file
```swift
struct SessionStatus {
    let target: String   // eval:2.0
    let state: String    // working, blocked, idle
    let name: String
    let cwd: String
}

func parseStatus(_ text: String) -> [SessionStatus] {
    text.split(separator: "\n").dropFirst().compactMap { line in
        let s = String(line)
        guard s.count >= 46 else { return nil }
        // Fixed-width columns: TARGET(12) STATE(8) NAME(25) CWD(rest)
        let target = s.prefix(12).trimmingCharacters(in: .whitespaces)
        let state = s.dropFirst(13).prefix(8).trimmingCharacters(in: .whitespaces)
        let name = s.dropFirst(22).prefix(25).trimmingCharacters(in: .whitespaces)
        let cwd = s.dropFirst(48).trimmingCharacters(in: .whitespaces)
        return SessionStatus(target: target, state: state, name: name, cwd: cwd)
    }
}
```

### Notification on blocked
```swift
func notifyIfBlocked(previous: [SessionStatus], current: [SessionStatus]) {
    for session in current where session.state == "blocked" {
        let wasBlocked = previous.first { $0.target == session.target }?.state == "blocked"
        if !wasBlocked {
            let content = UNMutableNotificationContent()
            content.title = "Claude blocked"
            content.body = "\(session.name.isEmpty ? session.target : session.name) needs permission"
            content.sound = .default
            let request = UNNotificationRequest(identifier: session.target, content: content, trigger: nil)
            UNUserNotificationCenter.current().add(request)
        }
    }
}
```

## App structure (proposed)

```
CCMonitor/
├── CCMonitorApp.swift         # @main, MenuBarExtra scene
├── StatusFetcher.swift        # Timer + SSH fetch + parse
├── StatusParser.swift         # parseStatus() — shared with any consumer
├── Models/
│   └── SessionStatus.swift    # Parsed session struct
├── Views/
│   └── StatusView.swift       # Menu bar dropdown showing sessions
├── Consumers/
│   ├── NotificationConsumer.swift  # Fires macOS notification on blocked
│   └── WidgetBridge.swift          # Writes to shared container for WidgetKit
└── Widget/                    # WidgetKit extension (separate target)
    └── CCMonitorWidget.swift
```

Key principle: `StatusFetcher` owns the polling loop and parsed data. Consumers
subscribe to it. Adding a new consumer means adding one file, not changing the fetcher.
