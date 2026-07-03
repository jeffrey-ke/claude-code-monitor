import sys
from pathlib import Path

# The modules under test live at the repo root (no package layout — they're standalone
# scripts by design), so put it on the path for every test module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
