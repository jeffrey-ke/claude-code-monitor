#!/usr/bin/env bash
# ccremote-up.sh — bring up the shared SSH master(s) ccremote needs, then run the syncer.
#
# ccremote.py fetches each remote's status.json with `ssh -o BatchMode=yes` (it NEVER prompts),
# reusing a shared SSH master connection at ControlPath ~/.claude/run/remote/.ssh-%r@%h:%p. For a
# host that needs a password / 2FA, that master can't be created non-interactively — so this
# wrapper opens it ONCE interactively (you authenticate here), with a long ControlPersist so it
# outlives poll gaps, then hands off to `ccremote.py --serve`. The 3s poll cadence keeps the
# master warm, so you only authenticate again after a reboot or a long idle gap.
#
# If the master dies anyway (reboot, network drop), it can't be reopened non-interactively —
# but it no longer fails silently: ccremote writes `error:"auth"` into the host's
# run/remote/<host>.health sidecar, ccbar shows a red ⚠<host>, and ccdash shows
# "<host> — auth needed — rerun ccremote-up.sh". Rerunning this script is the fix (it's
# idempotent). Manual probe: `ssh -O check -o "ControlPath=<CTL>" <host>`.
#
# The "control socket" is LOCAL (a unix socket on THIS box representing the live SSH connection);
# nothing is created on the remote.
#
# Usage:
#   ccremote-up.sh                 # hosts from ~/.claude/run/ccmonitor-remotes
#   ccremote-up.sh psc other-host  # explicit hosts
#   CCREMOTE_CONTROL_PERSIST=24h ccremote-up.sh    # override the master lifetime (default 12h)
#   CCREMOTE_ARGS="--serve=5" ccremote-up.sh       # override how ccremote is launched
set -euo pipefail

RUN="${HOME}/.claude/run"
REMOTE_DIR="${RUN}/remote"
CTL="${REMOTE_DIR}/.ssh-%r@%h:%p"           # MUST match ccremote.py's _CTL (its ControlPath)
PERSIST="${CCREMOTE_CONTROL_PERSIST:-12h}"  # long enough to outlast gaps; polls keep it warm
REMOTES="${CCMONITOR_REMOTES:-${RUN}/ccmonitor-remotes}"
CCREMOTE="$(cd "$(dirname "$0")" && pwd)/ccremote.py"

# 1. Ensure the control-socket directory exists (ssh won't create it).
mkdir -p "${REMOTE_DIR}"

# 2. Collect hosts: explicit args, else the first field of each ccmonitor-remotes line.
hosts=("$@")
if [ "${#hosts[@]}" -eq 0 ]; then
    if [ -r "${REMOTES}" ]; then
        while read -r first _rest; do
            case "${first}" in ''|\#*) continue ;; esac   # skip blank / comment lines
            hosts+=("${first}")
        done < "${REMOTES}"
    fi
fi
if [ "${#hosts[@]}" -eq 0 ]; then
    echo "ccremote-up: no hosts (pass as args, or add them to ${REMOTES})" >&2
    exit 1
fi

# 3. For each host: reuse a live master, else open one interactively (authenticate once).
for host in "${hosts[@]}"; do
    if ssh -O check -o "ControlPath=${CTL}" "${host}" 2>/dev/null; then
        echo "ccremote-up: master already up for ${host}"
    else
        echo "ccremote-up: opening SSH master for ${host} (authenticate once)…"
        ssh -fN -M -o "ControlPath=${CTL}" -o ControlPersist="${PERSIST}" "${host}"
        ssh -O check -o "ControlPath=${CTL}" "${host}"   # confirm it came up
    fi
done

# 4. Hand off to the syncer (replaces this process so Ctrl-C / signals reach it directly).
#    `${VAR---serve}` (no colon) defaults only when UNSET, so `CCREMOTE_ARGS=""` runs a one-shot.
echo "ccremote-up: starting ccremote (${CCREMOTE_ARGS---serve})…"
# shellcheck disable=SC2086
exec python3 "${CCREMOTE}" ${CCREMOTE_ARGS---serve}
