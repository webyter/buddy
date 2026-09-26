#!/usr/bin/env python3
"""Smoke tests for buddy's modular architecture.

Run:  python3 -m unittest discover -s tests -v
Stdlib only.
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestSplit(unittest.TestCase):
    def test_all_fragments_parse(self):
        """Every .py in buddy_core/ must be valid Python."""
        for py in sorted((ROOT / "buddy_core").glob("*.py")):
            ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        ast.parse((ROOT / "buddy.py").read_text(encoding="utf-8"))

    def test_fragments_match_update_files_and_core_files(self):
        """All files listed in UPDATE_FILES must actually exist on disk."""
        from buddy_core.config import UPDATE_FILES
        for f in UPDATE_FILES:
            self.assertTrue((ROOT / f).is_file(), f"missing UPDATE file: {f}")

    def test_loader_exposes_key_globals(self):
        """buddy.py must expose its key entry-points as module attributes."""
        import buddy
        for name in ("main", "chat", "doctor", "setup", "serve", "list_jobs"):
            self.assertTrue(hasattr(buddy, name), f"buddy.{name} missing")

    def test_unknown_command_prints_usage(self):
        """An unknown CLI command must exit non-zero and show usage text."""
        p = subprocess.run(
            [sys.executable, "buddy.py", "no-such-cmd"],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertNotEqual(p.returncode, 0)
        combined = p.stdout + p.stderr
        self.assertIn("usage:", combined.lower())


if __name__ == "__main__":
    unittest.main()
