"""Headless self-evolution run: buddy reviews itself and makes one real
improvement. confirm=None → unattended; source edits land on the caged
buddy/autonomous branch (we are on it), not main."""
import sys
import traceback

sys.path.insert(0, ".")

from buddy_core.config import load_config
from buddy_core.skills import evolve_pass, _cage_autonomous_edits
from buddy_core.mcp import MCPManager

cfg = load_config()
mcp = MCPManager()
try:
    note = evolve_pass(cfg, mcp, confirm=None)
    note = _cage_autonomous_edits(note)
    print("=== EVOLVE RESULT ===")
    print(note)
except Exception:
    print("=== EVOLVE FAILED ===")
    traceback.print_exc()
    sys.exit(1)
