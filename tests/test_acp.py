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


class TestACPBrain(unittest.TestCase):
    """brain='acp': the decision-engine conversation rides buddy's own ACP
    client — no API key, the coding agent's sign-in is the credential."""

    def setUp(self):
        self.mock = ["python3", os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "mock_acp_brain.py")]
        os.environ.pop("ACP_BRAIN_MOCK", None)

    def tearDown(self):
        os.environ.pop("ACP_BRAIN_MOCK", None)

    def test_acp_brain_final(self):
        from buddy_core.brain import _completion
        cfg = {"brain": "acp", "cwd": "/tmp", "acp_brain": "mock",
               "acp_agents": {"mock": {"command": self.mock[0],
                                       "args": [self.mock[1]]}}}
        resp = _completion(cfg, [{"role": "user", "content": "hi"}], None)
        self.assertEqual(
            resp["choices"][0]["message"]["content"], "acp brain online")

    def test_acp_brain_toolcall(self):
        from buddy_core.brain import _completion
        cfg = {"brain": "acp", "cwd": "/tmp", "acp_brain": "mock",
               "acp_agents": {"mock": {"command": self.mock[0],
                                       "args": [self.mock[1]]}}}
        os.environ["ACP_BRAIN_MOCK"] = "toolcall"
        tools = [{"type": "function", "function": {
            "name": "get_time", "description": "clock", "parameters": {}}}]
        resp = _completion(cfg, [{"role": "user", "content": "hi"}], tools)
        msg = resp["choices"][0]["message"]
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get_time")
        self.assertEqual(
            msg["tool_calls"][0]["function"]["arguments"], '{"tz": "UTC"}')

    def test_acp_brain_no_agents(self):
        from buddy_core.brain import _completion
        resp = _completion({"brain": "acp", "cwd": "/tmp"},
                           [{"role": "user", "content": "hi"}], None)
        self.assertIn("no agents configured",
                      resp["choices"][0]["message"]["content"])

    def test_acp_brain_unknown_agent(self):
        from buddy_core.brain import _completion
        cfg = {"brain": "acp", "cwd": "/tmp",
               "acp_agents": {"claude": {"command": "claude-agent-acp"}},
               "acp_brain": "nope"}
        resp = _completion(cfg, [{"role": "user", "content": "hi"}], None)
        self.assertIn("no agent named 'nope'",
                      resp["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
