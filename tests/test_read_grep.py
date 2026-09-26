"""opencode read/grep part parity: grep tool + result param on tool events."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from buddy_core.tools import grep, tool_impl
from buddy_core.agent import _fire_on_tool


class TestGrep(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "sample.py")
        with open(self.f, "w") as fh:
            fh.write("def add(a, b):\n    return a + b\n\n# TODO later\n")

    def test_finds_matches_with_lines(self):
        out = grep("def add", self.d)
        self.assertIn("1 match", out)
        self.assertIn("sample.py:1:", out)

    def test_count_header_drives_ui(self):
        out = grep("return|TODO", self.d)
        self.assertRegex(out, r"^2 matches? for ")
        self.assertIn("sample.py:2:", out)
        self.assertIn("sample.py:4:", out)

    def test_no_match_and_bad_regex(self):
        self.assertIn("no matches", grep("zzz_nope", self.d))
        self.assertIn("bad regex", grep("([bad", self.d))

    def test_include_filter_and_missing_path(self):
        self.assertIn("no matches", grep("def add", self.d, include="*.rs"))
        self.assertIn("path not found", grep("x", os.path.join(self.d, "nope")))

    def test_via_tool_impl(self):
        out = tool_impl("grep", {"pattern": "def add", "path": self.d})
        self.assertIn("1 match", out)


class TestFireResult(unittest.TestCase):
    def test_eight_arg_callback_gets_result(self):
        seen = []

        def cb(name, status, secs, args, diff, call_id, tail, result):
            seen.append((name, status, result))

        _fire_on_tool(cb, "read_file", "ok", 0.1, {"path": "x"},
                      diff=None, call_id="c1", tail=None, result="file body")
        self.assertEqual(seen, [("read_file", "ok", "file body")])

    def test_seven_arg_callback_still_works(self):
        seen = []

        def cb(name, status, secs, args, diff, call_id, tail):
            seen.append((name, status))

        _fire_on_tool(cb, "grep", "ok", 0.1, {"pattern": "p"},
                      call_id="c2", result="2 matches")
        self.assertEqual(seen, [("grep", "ok")])

    def test_never_raises(self):
        _fire_on_tool(None, "grep", "ok", 0.1)  # must not raise


if __name__ == "__main__":
    unittest.main()
