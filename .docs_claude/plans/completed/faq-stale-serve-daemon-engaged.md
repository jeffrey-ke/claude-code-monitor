# FAQ / postmortem — stale `--serve` daemon silently kills the ccbar `◆` tier (RESOLVED)

**Status: RESOLVED 2026-06-30** (branch `ccdash`). Diagnosed + fixed live; no code change.
**Tags:** `faq` `error` — a recurring operational gotcha, not a code regression.

## Symptom

The notch monitor / ccdash showed a session needing attention, but the tmux statusline
(`ccbar`) stayed empty — no `◆` "your turn" segment ever appeared. It *looked* like a
regression or a state-machine dead-end (e.g. "does running ccdash auto-ack everything into
oblivion?").

## Root cause — a long-lived daemon serving pre-feature code

`ccbar.py` reads **only** the cached `~/.claude/run/status.json` snapshot, written by a
long-running `ccstatus.py --serve` daemon. That daemon process had been alive for ~24h —
started **Jun 29 10:39** when HEAD was `499cc86`, which had **no `engaged` field at all**.
The `engaged` gate for the "your turn" tier landed ~11h later in `664861a` (Jun 29 21:31).

So the daemon kept serving its in-memory pre-`engaged` `Session` dataclass: `asdict()`
omitted the key entirely, and every row in `status.json` lacked `engaged`. ccbar's
`_awaiting` requires it:

```python
return (s.get("state") == "idle" and s.get("engaged")   # missing key -> None -> falsy
        and not s.get("acknowledged") and not s.get("dismissed"))
```

Missing key → `s.get("engaged")` is `None` → falsy → the `◆` tier can **never** fire. This
is exactly the fail-quiet back-compat path the engaged-fix plan documented ("old snapshots
lacking the key → falsy → no false ◆ *until the next `--serve` write*") — but the unstated
assumption was that the daemon would be **restarted** to pick up new code. A daemon that
outlives a schema change turns "fail-quiet" into a permanent silent dead-end.

The live provider was always correct: `ccstatus.py --json` and `get_sessions()` both
computed `engaged` per session. Only the stale cached snapshot was wrong.

## Not the dashboard

ccdash's auto-ack-on-jump worked as designed (acked rows auto-clear on the session's next
write via mtime compare). Running the dashboard does **not** dead-end anything — the culprit
was purely the stale serve process, independent of ccdash.

## Fix

Restart the `--serve` daemon so it serializes the current schema:

```bash
pkill -f 'ccstatus.py --serve'     # killing the tmux *session* does NOT signal the daemon —
                                   # kill the process directly (it had outlived its pane)
nohup python3 ccstatus.py --serve 2.0 > ~/.claude/run/ccserve.log 2>&1 & disown
```

Verified after restart: `"engaged" in row0` → `True`, per-row `engaged` True/False, and a
synthetic your-turn row renders `#[fg=magenta]◆ demo#[default]`.

**Gotcha worth remembering:** killing the tmux session the daemon was launched from did
**not** kill the daemon (PID survived its pane). Confirm with `pgrep -af 'ccstatus.py
--serve'` and check the process start time (`ps -o lstart`) against the last schema commit.

## Possible hardening (not done)

Have `ccbar` treat a *schema-stale* snapshot like a *time-stale* one: if rows are present but
lack the `engaged` key, show the faint `⚠` instead of a silent "all clear", so a
forgotten-restart daemon announces itself rather than going quietly dark. Tracked as a
follow-up idea; deferred.

## Files

None (operational fix). Diagnosis touched `ccstatus.py` / `ccbar.py` only as reading.
