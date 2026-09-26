"""ACP client tests against the mock ACP agent (buddy-testing/mock_acp_agent.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buddy_core.acp import acp_tool, run_acp_agent, ACPError  # noqa: E402

MOCK = ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_acp.py")]


class TestACP(unittest.TestCase):
    def test_roundtrip(self):
        out = run_acp_agent(MOCK[0], [MOCK[1]], "hello doc", cwd="/tmp")
        self.assertIn("hello doc", out)

    def test_unknown_agent(self):
        out = acp_tool({"acp_agents": {"a": {"command": "true"}}}, "nope", "hi")
        self.assertIn("unknown ACP agent", out)

    def test_list_empty(self):
        self.assertIn("no ACP agents", acp_tool({}, "list", ""))

    def test_broken_agent(self):
        with self.assertRaises(ACPError):
            run_acp_agent("false", [], "hi", cwd="/tmp", timeout=10)


if __name__ == "__main__":
    unittest.main()
