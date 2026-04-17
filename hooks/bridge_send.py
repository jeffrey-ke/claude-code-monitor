"""
bridge_send.py — Shared TCP transport to Claude Island.

One connect/write path used by ccbridge-hook.py (per-event state) and
ccmonitor-statusline.py (rate-limit snapshots). Protocol is raw JSON bytes
followed by close; for events marked with status=="waiting_for_approval", the
sender blocks reading one response frame before closing.

Silent-failure contract: any socket/OS/JSON error returns None. The caller
decides what "no response" means (deny by default in the permission path,
no-op for fire-and-forget events).
"""
import json
import socket
from pathlib import Path

BRIDGE_PORT_FILE = Path.home() / ".claude" / "run" / "bridge_port"
TIMEOUT_SECONDS = 300


def _read_bridge_port():
    try:
        return int(BRIDGE_PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def send_event(state):
    """Send event to Claude Island via TCP. Returns response dict or None.

    Swap AF_INET for AF_UNIX here to change transport for every caller.
    """
    port = _read_bridge_port()
    if port is None:
        return None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_SECONDS)
        sock.connect(("127.0.0.1", port))
        sock.sendall(json.dumps(state).encode())

        if state.get("status") == "waiting_for_approval":
            response = sock.recv(4096)
            sock.close()
            if response:
                return json.loads(response.decode())
        else:
            sock.close()

        return None
    except (socket.error, OSError, json.JSONDecodeError):
        return None
