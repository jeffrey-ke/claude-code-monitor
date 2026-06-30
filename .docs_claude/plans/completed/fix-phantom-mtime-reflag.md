# Fix: phantom transcript rewrites re-flag acked "your turn" sessions

## Context

A session ("Persistent splits mechanism validation") kept surfacing in the "your turn"
tier (`◆` in ccbar, awaiting tier in ccdash) even though the user had jumped into it
several times and had not sent it a message in ~16 hours.

Root cause (verified live on transcript `dca61951-…jsonl`):

- The newest **timestamped** entry in that transcript is `00:44:02Z` (~16h ago — the user's
  last real exchange). But the file's `st_mtime` **and** `ctime` are both ~37 min ago.
- `ctime` moving too means Claude Code **rewrote the JSONL in place**. The tail shows the
  mutable metadata records it rewrites: `ai-title`, `mode`, `permission-mode`,
  `last-prompt`, `file-history-snapshot`. Regenerating the AI title / updating a snapshot
  rewrites the whole file → `st_mtime` jumps to "now" with **no new turn appended**.
- `ccstatus.py` keys two things off raw `st_mtime` (`jp_mtime`):
  1. **`age`** (`ccstatus.py:494-499`) — so a 16h-idle session displays as "37m".
  2. **The ack/dismiss auto-clear** `_marker_active` (`ccstatus.py:414-422`):
     `ack_mark >= transcript_mtime`. A phantom rewrite pushes `transcript_mtime` past the
     ack the user wrote earlier → `acknowledged` flips to `false` → `idle + engaged + not
     acknowledged` → re-flagged as "your turn", with no genuine hand-back.

So it is not the user's messages re-triggering the tier — it is Claude Code's background
metadata rewrites bumping the file mtime past the ack marker.

Key observation that makes the fix clean: the phantom-rewrite records **carry no
`timestamp` field**, while every real turn (`user` / `assistant` / `system` hook) does.
Using "newest timestamped entry" as the activity clock naturally ignores exactly the
rewrites that cause the false re-flag, while still clearing the ack on a real new turn.

## Approach

Stop using raw file `st_mtime` as the activity clock. Derive a **last-real-activity epoch**
from the newest `timestamp` field inside the transcript tail, and use it for both `age` and
the marker comparison. Fall back to `st_mtime` when no timestamped entry is found (fail-open;
preserves today's behavior for odd/empty transcripts).

### Changes — all in `ccstatus.py`

1. **Surface the last-turn timestamp from the tail walk.** `_transcript_usage`
   (`ccstatus.py:221-252`) already reads the last 256 KB and iterates lines in reverse.
   Extend it to also capture the first (i.e. newest, file-append order) line that has a
   parseable `timestamp`, convert it to epoch seconds, and return it alongside
   `(model, ctx, engaged)`. Parse with
   `datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()` (the markers' mtimes are
   absolute epoch seconds, so a UTC-derived epoch is directly comparable). Note the scan must
   not stop at the first `assistant` line — keep scanning for the newest timestamp regardless
   of type, since the last entry may be a `system` hook turn after the assistant message.

2. **Compute `activity_mtime = last_turn_ts or jp_mtime`** in `get_sessions()`
   (around `ccstatus.py:490-521`), where `jp_mtime` is today's `jp.stat().st_mtime`.

3. **Use `activity_mtime` for:**
   - `age = int(now - activity_mtime)` (replaces the `jp_mtime`-based age at ~`:495`).
   - both `_marker_active(ACK_DIR, sid, activity_mtime)` and
     `_marker_active(DISMISS_DIR, sid, activity_mtime)` (`ccstatus.py:520-521`).

No changes needed in `ccdash.py` / `ccbar.py`: `_awaiting` and the tiers are unchanged; they
just receive a correctly-derived `acknowledged` and `age_s`.

### Why this is correct, not just a mask

- A **real** new turn (user reply, Claude's next assistant message, a Stop-hook `system`
  entry) carries a newer `timestamp` → `activity_mtime` advances → ack still auto-clears.
  The desirable "auto-clear on genuine activity" behavior is preserved.
- A **metadata-only** rewrite (no timestamped turn added) no longer advances
  `activity_mtime`, so a session the user has acked stays acked until something real happens.
- `engaged` is already derived from the same tail and is unaffected.

This refines the CLAUDE.md design note "Age from transcript mtime, not `statusUpdatedAt`":
`st_mtime` avoids the *stale* failure of `statusUpdatedAt`, but has the opposite *runs-ahead*
failure from in-place rewrites. The last timestamped turn is robust against both.

## Verification

1. `python ccstatus.py --json` and confirm the splits session
   (`dca61951-…`) now reports `acknowledged: true` **and** an `age_s` of ~16h (matching the
   last real turn), not ~37m.
2. Touch the transcript without adding a turn to simulate a metadata rewrite:
   `touch ~/.claude/projects/-home-jeffk-repo-refseg-workspace/dca61951-….jsonl`
   then re-run `--json`: `acknowledged` must **stay** the same and `age_s` must **not** reset
   (proves age/ack no longer follow raw mtime).
3. Append a real assistant/user JSON line with a fresh `timestamp` to a scratch copy and
   confirm `age_s` drops and a prior ack clears (proves genuine activity still registers).
4. Re-run the cross-session sanity scan (mtime-vs-last-turn): actively-running sessions
   should still show ~0 gap; only idle-but-rewritten sessions change.
5. Launch `uv run --script ccdash.py`: the splits row should no longer sit in the orange/your
   -turn tier after being acked, and its age should read realistically.
