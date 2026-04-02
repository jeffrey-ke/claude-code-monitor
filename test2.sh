# For each live claude PID, show its full ancestry
for f in ~/.claude/sessions/*.json; do
    pid=$(basename $f .json)
    [ -d "/proc/$pid" ] || continue
    echo "=== Claude PID $pid ==="
    # Walk up 6 levels
    current=$pid
    for i in $(seq 6); do
        comm=$(cat /proc/$current/comm 2>/dev/null)
        ppid=$(awk '/^PPid:/{print $2}' /proc/$current/status 2>/dev/null)
        echo "  $current ($comm) → parent $ppid"
        [ "$ppid" = "0" ] || [ "$ppid" = "1" ] && break
        current=$ppid
    done
    echo
done
