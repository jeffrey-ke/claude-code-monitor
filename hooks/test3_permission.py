#!/usr/bin/env python3
"""Test 3: Permission round-trip — listener sends 'allow', verify hook outputs approval JSON."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
HOOK = SCRIPT_DIR / "ccbridge-hook.py"
PORT = 19876
PORT_FILE = Path.home() / ".claude" / "run" / "bridge_port"

# Ensure state dir exists
(Path.home() / ".claude" / "run" / "state").mkdir(parents=True, exist_ok=True)

# Write bridge_port file
PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
PORT_FILE.write_text(str(PORT))

# Start listener
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", PORT))
srv.listen(1)
srv.settimeout(10)
print(f"Listener on port {PORT}, sending PermissionRequest...")

# Send PermissionRequest via hook
event = json.dumps({
    "hook_event_name": "PermissionRequest",
    "session_id": "test",
    "cwd": "/tmp",
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
    "status": "waiting_for_approval",
})
proc = subprocess.Popen(
    ["python3", str(HOOK)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
proc.stdin.write(event.encode())
proc.stdin.close()

# Accept connection, read request, send allow response
try:
    conn, addr = srv.accept()
    data = conn.recv(4096)
    received = json.loads(data)
    print(f"Received permission request: {json.dumps(received, indent=2)}")

    # Send allow decision
    conn.sendall(json.dumps({"decision": "allow"}).encode())
    conn.close()
    print("Sent 'allow' response")

except socket.timeout:
    print("FAIL: no connection received within 10 seconds")
    proc.kill()
    srv.close()
    PORT_FILE.unlink(missing_ok=True)
    sys.exit(1)

# Read hook's stdout — should be hookSpecificOutput JSON
proc.wait(timeout=10)
stdout = proc.stdout.read().decode().strip()
srv.close()

print(f"\nHook stdout: {stdout}")

if stdout:
    output = json.loads(stdout)
    decision = (output
                .get("hookSpecificOutput", {})
                .get("decision", {})
                .get("behavior"))
    if decision == "allow":
        print("\nPASS: permission approved correctly")
    else:
        print(f"\nFAIL: expected behavior 'allow', got '{decision}'")
else:
    print("\nFAIL: hook produced no output")

# Cleanup
PORT_FILE.unlink(missing_ok=True)
state_file = Path.home() / ".claude" / "run" / "state" / "test"
state_file.unlink(missing_ok=True)
