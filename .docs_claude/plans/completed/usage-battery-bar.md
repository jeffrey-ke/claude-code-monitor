# Usage Battery Bar in Notch

Display Claude Max 5-hour usage % and reset countdown as a battery bar in the
claude-island notch.

## Goal

A global (account-wide, not per-host) horizontal battery bar rendered in the
notch, showing `rate_limits.five_hour.used_percentage` and a live countdown to
`rate_limits.five_hour.resets_at`.

## Data source

Claude Code's **statusline** subsystem is the only read path that exposes parsed
rate-limit data. When Claude Code invokes the configured `statusLine.command`, it
passes JSON on stdin that, for Claude.ai Pro/Max subscribers, includes:

```json
{
  "rate_limits": {
    "five_hour":  { "used_percentage": 73, "resets_at": 1744812000 },
    "seven_day":  { "used_percentage": 41, "resets_at": 1745280000 }
  }
}
```

Caveat: the field is populated only *after* the first API response in a session,
so cold sessions briefly show nothing. User is on Max — data path confirmed.

Alternatives ruled out:
- Hook payloads — no rate-limit fields (open issue upstream)
- On-disk cache — no such file in `~/.claude/`
- `/status` — interactive only, no `--json`
- OTEL — only emits cost/tokens, not utilization
- Env vars — none

## Approach

Pipeline: `statusline → file → TCP event → Mac → notch UI`.

```
Claude Code process
  → statusline invocation (stdin JSON)
  → ccmonitor-statusline.py
      ├─ writes ~/.claude/run/usage.json (atomic, global)
      ├─ if ~/.claude/run/bridge_port exists: TCP "usage" event to Mac
      └─ stdout: user's existing statusline text (or empty)
  → ccbridge TCP server (already exists)
  → SessionStore.process(usageEvent)
  → ClaudeSessionMonitor.usageInfo (@MainActor @Published)
  → UsageBarView in notch chrome
```

The file write exists so that (a) the no-bridge standalone monitor case still
has the data, (b) ccbridge-hook.py can re-push on next hook fire if a TCP send
dropped.

## Stages

### Stage 1 — Statusline script (server)

**New file**: `hooks/ccmonitor-statusline.py`

Reads stdin JSON, extracts `rate_limits.five_hour.{used_percentage, resets_at}`,
writes `~/.claude/run/usage.json` atomically, sends TCP `usage` event if bridge
is up, echoes whatever statusline text we decide (initially empty string).

Shape of the usage file:

```json
{
  "five_hour_used_pct": 73,
  "five_hour_resets_at": 1744812000,
  "updated_at": 1744808400
}
```

**Refactor**: extract TCP send logic from `hooks/ccbridge-hook.py` into
`hooks/bridge_send.py` so the statusline script and the hook share one
connect/write path. `reusable-parts` principle — one part, two callers.

Exit code always 0. Never break the user's statusline.

### Stage 2 — `setup.sh` wiring

Merge a `statusLine` stanza into `~/.claude/settings.json`. Detect existing
`statusLine.command` — if present, log a warning and skip (don't clobber). User
can opt in manually by calling our script from their own statusline.

Idempotent — re-running setup.sh must be a no-op on a configured host.

### Stage 3 — Mac: event ingestion

In `claude-island/ClaudeIsland/Services/` (likely `HookSocketServer.swift` and
`SessionStore.swift`):

- Add `SessionEvent.usage(UsageInfo)` case
- New model: `Models/UsageInfo.swift` — `fiveHourUsedPct: Int`, `fiveHourResetsAt: Date`, `receivedAt: Date`
- `SessionStore` holds a global `currentUsage: UsageInfo?` (not per-session)
- Publish to `ClaudeSessionMonitor.usageInfo` on main actor

### Stage 4 — Notch UI

**New file**: `claude-island/ClaudeIsland/Views/UsageBarView.swift`

- Horizontal battery bar, fill proportional to `100 - usedPct`
- Color: green ≥ 50% remaining, yellow 20–50%, red < 20%
- Label: `"27% left · resets 2h 14m"` — countdown driven by `TimelineView(.periodic(from: .now, by: 30))` or a lightweight `Timer.publish`
- Placement: compact form — a small pill in the notch's chrome area (TBD on first render; see `ClaudeIslandView` layout)
- Hidden when `usageInfo == nil` or data is older than 10 minutes (stale guard)

### Stage 5 — Verification

On a Max-subscribed remote host:
1. Run `setup.sh`, confirm `settings.json` has our `statusLine`
2. Open a Claude Code session, send one prompt
3. `cat ~/.claude/run/usage.json` — verify fields present
4. Mac notch shows the bar with correct % and countdown
5. Wait 30s — countdown decrements
6. Re-run `setup.sh` — no duplicate entries, no errors
7. Remove `bridge_port` — statusline still writes file, doesn't error
8. Restore bridge — next prompt repopulates the Mac

## Decision log

- **Statusline over API proxy**: proxy would intercept HTTPS response headers for
  `anthropic-ratelimit-*`, giving us data even on API-key plans, but it means
  running a local MITM on every remote host. Statusline is free and official.
- **Global, not per-host**: subscription quota is account-global. Showing
  per-host bars would misrepresent the shared account state.
- **5h only**: user confirmed. 7d rarely triggers for most work. Can add later
  as a secondary thin bar without schema changes (already writing both to disk
  is trivial — we'll record both in the file even though only 5h renders).
- **Fail open**: statusline never exits non-zero; a broken monitor must not
  break the user's Claude Code session.
- **Shared TCP module**: `bridge_send.py` is extracted before the second caller
  is added. Copy-paste is how drift starts.

## Open for later

- Weekly (7d) bar — trivial once plumbing is in place
- Toast/banner when 5h hits 90% — prevent surprise block mid-task
- Historical usage graph — requires keeping a ring buffer of snapshots, out of
  scope for this plan
