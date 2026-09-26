"""todo_write: opencode todowrite parity."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buddy_core.tools import todo_write  # noqa: E402


class TestTodoWrite(unittest.TestCase):
    def test_valid_list(self):
        out = todo_write([{"content": "a", "status": "completed"},
                          {"content": "b", "status": "in_progress"},
                          {"content": "c", "status": "pending"}])
        self.assertEqual(out, "1/3 todos")

    def test_rejects_empty(self):
        self.assertIn("non-empty", todo_write([]))
        self.assertIn("non-empty", todo_write("nope"))

    def test_rejects_bad_status(self):
        out = todo_write([{"content": "a", "status": "done"}])
        self.assertIn("unknown todo status", out)

    def test_rejects_blank_content(self):
        out = todo_write([{"content": "  ", "status": "pending"}])
        self.assertIn("content", out)


if __name__ == "__main__":
    unittest.main()
