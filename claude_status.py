#!/usr/bin/env python3
"""Back-compat shim — the monitor was split into a provider + consumers.

`claude_status.py` used to poll hook-written state files and write
`~/.claude/run/status` for the claude-island Mac UI. That job now lives in the
provider, `ccstatus.py --serve`, which sources live state from the supported
`claude agents --json` API instead of scraping panes. This shim preserves the old
entrypoint (and the 2s cadence) so existing launchers / the ccmonitor tmux pane
keep working unchanged.

    python claude_status.py        # == python ccstatus.py --serve 2.0
"""
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    ccstatus = Path(__file__).resolve().parent / "ccstatus.py"
    os.execv(sys.executable, [sys.executable, str(ccstatus), "--serve", "2.0"])
