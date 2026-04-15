#!/usr/bin/env python3
"""Test 2: Event delivery — start a listener, send an event, verify it arrives."""
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
print(f"Listener on port {PORT}, sending test event...")

# Send event via hook in a subprocess
event = json.dumps({
    "hook_event_name": "PreToolUse",
    "session_id": "test",
    "cwd": "/tmp",
    "tool_name": "Bash",
})
proc = subprocess.Popen(
    ["python3", str(HOOK)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
proc.stdin.write(event.encode())
proc.stdin.close()

# Accept connection and read
try:
    conn, addr = srv.accept()
    data = conn.recv(4096)
    conn.close()
    received = json.loads(data)
    print(f"Received: {json.dumps(received, indent=2)}")

    if received.get("event") == "PreToolUse" and received.get("session_id") == "test":
        print("\nPASS: event delivered correctly")
    else:
        print("\nFAIL: unexpected payload")
except socket.timeout:
    print("\nFAIL: no connection received within 10 seconds")

proc.wait()
srv.close()

# Check state file was written
state_file = Path.home() / ".claude" / "run" / "state" / "test"
if state_file.exists():
    state = json.loads(state_file.read_text())
    print(f"State file: {state}")
    if state.get("state") == "working":
        print("PASS: state file written correctly")
    else:
        print(f"FAIL: expected state 'working', got '{state.get('state')}'")
    state_file.unlink()
else:
    print("FAIL: state file not written")

# Cleanup
PORT_FILE.unlink(missing_ok=True)
