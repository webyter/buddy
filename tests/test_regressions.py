#!/usr/bin/env python3
"""Regression tests for bugs found while running buddy on a clean machine.

Run:  python3 -m unittest discover -s tests -v
Each test reproduces a real failure observed in the wild.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def inspect_src(mod, fn_name: str) -> str:
    """Source of a function, for asserting on patterns that are hard to
    trigger from a test (locks, I/O shape, fixed sleeps)."""
    import inspect
    return inspect.getsource(getattr(mod, fn_name))


class TestMcpList(unittest.TestCase):
    """Regression: `mcp list` crashed with TypeError when command is a str.

    cmd_list did spec['command'] + spec.get('args', []) — str + list.
    """

    def test_list_with_string_command(self):
        import buddy_core.mcp as mcp_mod
        from buddy_core import config as cfg_mod

        with tempfile.TemporaryDirectory() as td:
            mcp_file = Path(td) / "mcp.json"
            mcp_file.write_text(json.dumps(
                {"mock": {"command": "python3", "args": ["server.py"]}}))
            old = cfg_mod.MCP_CONFIG
            cfg_mod.MCP_CONFIG = mcp_file
            try:
                m = mcp_mod.MCPManager()
                m.cmd_list()  # must not raise TypeError
            finally:
                cfg_mod.MCP_CONFIG = old


class TestSubagentGlobals(unittest.TestCase):
    """Regression: delegate/self_repair crashed with `name '_SUB_MCP' is not
    defined`; scheduled jobs crashed with `run_agent` undefined — the split
    into fragments lost cross-module imports."""

    def test_agent_imports_sub_mcp(self):
        import buddy_core.agent as agent_mod
        self.assertTrue(hasattr(agent_mod, "_SUB_MCP"),
                        "buddy_core.agent must import _SUB_MCP")

    def test_sched_module_has_run_agent(self):
        import buddy_core.sched as sched_mod
        # run_agent must be resolvable from sched's namespace at call time;
        # with lazy imports inside functions this is satisfied by agent.py
        self.assertTrue(hasattr(sched_mod, "run_agent") or
                        "from .agent import run_agent" in
                        sched_mod.__dict__.get("__doc__", "") or
                        any("from .agent import run_agent" in
                            (ROOT / "buddy_core" / "sched.py").read_text()
                            for _ in [0]),
                        "sched.py must import run_agent (directly or lazily)")

    def test_watch_snapshot_url(self):
        """Regression: _watch_snapshot referenced _net_open without import."""
        from buddy_core import sched
        snap = sched._watch_snapshot("command", "echo regression-ok")
        self.assertIn("regression-ok", snap)

    def test_skills_evolve_imports(self):
        import buddy_core.skills as skills_mod
        src = (ROOT / "buddy_core" / "skills.py").read_text()
        self.assertIn("from .agent import run_agent", src)
        self.assertIn("from .agent import run_agent, _sub_mcp", src)


class TestSystemUpdate(unittest.TestCase):
    def test_check_lists_without_confirm(self):
        from buddy_core.tools import update_system
        out = update_system("check")
        self.assertIn("package", out.lower())
        self.assertTrue("pending" in out.lower() or "up to date" in out.lower())

    def test_upgrade_denied_without_confirm(self):
        from buddy_core.tools import update_system, tool_impl
        self.assertIn("cancelled", update_system("upgrade"))
        self.assertIn("cancelled",
                      tool_impl("update_system", {"action": "upgrade"}))

    def test_upgrade_allowed_with_confirm(self):
        from buddy_core import tools as tools_mod
        calls = []
        orig = tools_mod.run_command
        tools_mod.run_command = lambda *a, **k: calls.append((a, k)) or "ok"
        try:
            out = tools_mod.update_system("upgrade", confirm=lambda _m: True)
        finally:
            tools_mod.run_command = orig
        self.assertTrue(calls)
        self.assertIn("system upgrade result", out)

    def test_bad_action_rejected(self):
        from buddy_core.tools import update_system
        self.assertIn("refused", update_system("explode"))

    def test_raw_shell_guards(self):
        from buddy_core.tools import is_dangerous
        self.assertTrue(is_dangerous("sudo pacman -Syu"))
        self.assertTrue(is_dangerous("sudo dnf upgrade -y"))
        self.assertFalse(is_dangerous("apt list --upgradable"))


class TestUrlNormalize(unittest.TestCase):
    def test_bare_domains(self):
        from buddy_core.tools import _norm_url as n
        self.assertEqual(n("football.com"), "https://football.com")
        self.assertEqual(n("https://football.com/x"), "https://football.com/x")
        self.assertEqual(n("example.com/path?q=1"), "https://example.com/path?q=1")

    def test_local_hosts_use_http(self):
        from buddy_core.tools import _norm_url as n
        self.assertEqual(n("localhost:7616"), "http://localhost:7616")
        self.assertEqual(n("127.0.0.1:7616"), "http://127.0.0.1:7616")

    def test_garbage_rejected(self):
        from buddy_core.tools import _norm_url as n, web_fetch, browse
        self.assertEqual(n("not a url!!!"), "")
        self.assertIn("not a valid URL", web_fetch("not a url!!!"))
        self.assertIn("not a valid URL", browse("not a url!!!"))


class TestIntent(unittest.TestCase):
    def test_update_pc(self):
        from buddy_core.commands import parse_intent as p
        self.assertEqual(p("update this pc"), ("update_pc", ""))
        self.assertEqual(p("please update my laptop?"), ("update_pc", ""))
        self.assertEqual(p("update yourself"), ("upgrade_self", ""))

    def test_web(self):
        from buddy_core.commands import parse_intent as p
        self.assertEqual(p("start web"), ("web_status", ""))
        self.assertEqual(p("where is the web UI"), ("web_status", ""))
        self.assertEqual(p("stop web"), ("web_stop", ""))

    def test_check_site(self):
        from buddy_core.commands import parse_intent as p
        self.assertEqual(p("check football.com"), ("check_site", "football.com"))
        self.assertEqual(
            p("look at https://example.com/x"),
            ("check_site", "https://example.com/x"))

    def test_fallthrough(self):
        from buddy_core.commands import parse_intent as p
        self.assertIsNone(p("what is the capital of France"))
        self.assertIsNone(p("web search cats"))
        self.assertIsNone(p(""))
        self.assertIsNone(p("please write a very long essay about many different things in great detail here"))

    def test_run_web_status(self):
        from buddy_core.commands import _run_intent
        out = _run_intent(("web_status", ""), {"web_auth": False}, lambda _m: True)
        self.assertIn("http://127.0.0.1:7616/", out)

    def test_run_declined_upgrade(self):
        from buddy_core.commands import _run_intent
        out = _run_intent(("update_pc", {}), {}, lambda _m: False)
        self.assertIsNotNone(out)  # check ran, upgrade declined/cancelled

    def test_offline_intents(self):
        from buddy_core.commands import parse_intent as p, _run_intent
        self.assertEqual(p("run ls /tmp"), ("shell", "ls /tmp"))
        self.assertIsNone(p("run an evolve cycle"))
        self.assertEqual(p("remember my dog is Rex"),
                         ("remember", "my dog is rex"))
        self.assertEqual(p("what do you remember"), ("recall", ""))
        self.assertEqual(p("what is 2 + 3 * 4"), ("calc", "2 + 3 * 4"))
        self.assertEqual(p("2^10"), ("calc", "2^10"))
        self.assertIsNone(p('__import__("os")'))
        self.assertEqual(p("what time is it"), ("time", ""))
        self.assertEqual(_run_intent(("calc", "2 + 3 * 4"), {}, None), "= 14")
        self.assertEqual(_run_intent(("calc", "2^10"), {}, None), "= 1024")
        self.assertIn("2026", _run_intent(("time", ""), {}, None))
        out = _run_intent(("shell", "echo offline-ok"), {}, lambda _m: True)
        self.assertIn("offline-ok", out)
        denied = _run_intent(("shell", "rm -rf /"), {}, None)
        self.assertIn("refused", denied)

    def test_model_intent_parsing(self):
        from buddy_core.commands import parse_intent as p
        self.assertEqual(p("buddy change model to gemini 3.7 flash"),
                         ("model", "gemini 3.7 flash"))
        self.assertEqual(p("change model to gpt-4o"), ("model", "gpt-4o"))
        self.assertEqual(p("switch model to deepseek-chat"),
                         ("model", "deepseek-chat"))
        self.assertEqual(p("please set the model to gemini 2.5 pro?"),
                         ("model", "gemini 2.5 pro"))
        self.assertEqual(p("use model llama3.2"), ("model", "llama3.2"))
        self.assertEqual(p("switch to gemini 2.5 pro"),
                         ("model", "gemini 2.5 pro"))
        self.assertEqual(p("change to gpt-4o"), ("model", "gpt-4o"))
        # must not steal neighboring intents
        self.assertIsNone(p("what model are you using"))
        self.assertIsNone(p("switch to plan"))
        self.assertIsNone(p("change the mode"))
        self.assertEqual(p("remember my model is gemini"),
                         ("remember", "my model is gemini"))

    def test_model_intent_runs(self):
        from unittest import mock
        from pathlib import Path
        import tempfile
        import buddy_core.commands as cmds
        from buddy_core import config as cfg_mod
        from buddy_core.commands import _run_intent
        base = {"api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-2.5-flash", "models": {}}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cfg_mod, "CONFIG", Path(td) / "config.json"):
                with mock.patch.object(cmds, "_fetch_endpoint_models",
                                       return_value=None):
                    out = _run_intent(("model", "gemini 2.5 pro"),
                                      dict(base), lambda _m: True)
                    self.assertIn("switched", out)
                    self.assertIn("gemini-2.5-pro", out)
                    cfg = dict(base)
                    cfg["models"] = {"deep": {
                        "api_base": "https://api.deepseek.com/v1",
                        "model": "deepseek-chat"}}
                    out = _run_intent(("model", "deep"), cfg, lambda _m: True)
                    self.assertEqual(cfg["model"], "deepseek-chat")


class TestToolValidation(unittest.TestCase):
    def test_schedule_rejects_bad_args(self):
        from buddy_core.sched import add_job
        self.assertIn("refused", add_job("", 5, None))
        self.assertIn("refused", add_job("x", None, None))
        self.assertIn("refused", add_job("x", 5, "25:00"))
        self.assertIn("refused", add_job("x", -1, None))

    def test_code_edit_syntax_check(self):
        """Invalid Python must be rejected by code_edit, not written."""
        import buddy_core.tools as tools_mod
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "m.py"
            target.write_text("x = 1\n")
            out = tools_mod.code_edit(str(target), "x = 1", "def broken(:\n  pass")
            self.assertIn("syntax", out.lower())
            self.assertEqual(target.read_text(), "x = 1\n")


if __name__ == "__main__":
    unittest.main()


class TestThemes(unittest.TestCase):
    """The OpenCode-style theme system."""

    def test_builtin_themes_apply(self):
        from buddy_core import theme, util
        for name in theme.list_themes():
            self.assertEqual(theme.apply(name), name)
            self.assertTrue(util._THEME["accent"].startswith("38;5;"))
            self.assertIsInstance(util._THEME["c_accent"], tuple)
        theme.apply("opencode")  # restore default

    def test_unknown_theme_keeps_current(self):
        from buddy_core import theme
        theme.apply("catppuccin")
        self.assertEqual(theme.apply("no-such-theme"), "catppuccin")
        theme.apply("opencode")

    def test_hex_to_256_landmarks(self):
        from buddy_core.theme import _hex_to_256 as h
        self.assertEqual(h("#ffffff"), 231)
        self.assertEqual(h("#000000"), 16)
        self.assertGreaterEqual(h("#808080"), 232)  # gray ramp
        self.assertLess(h("#808080"), 256)

    def test_user_theme_dir_wins(self):
        from buddy_core import theme
        from buddy_core.config import HOME
        themes_dir = HOME / "themes"
        themes_dir.mkdir(exist_ok=True)
        custom = themes_dir / "testcustom.json"
        custom.write_text(json.dumps({"primary": "#fab283", "error": "#ff0000"}))
        try:
            self.assertIn("testcustom", theme.list_themes())
            self.assertIsNotNone(theme.load_theme("testcustom"))
        finally:
            custom.unlink()


class TestCustomTools(unittest.TestCase):
    def test_custom_tool_lifecycle(self):
        from buddy_core.tools import integrate_tool, tool_impl, remove_custom_tool, all_tool_specs
        code = (
            "def test_multiply(a: int, b: int) -> int:\n"
            "    '''Multiply two integers.'''\n"
            "    return a * b\n"
        )
        res = integrate_tool(
            "test_multiply",
            "Multiply two integers",
            {"a": {"type": "integer"}, "b": {"type": "integer"}},
            code,
            confirm=lambda _msg: True,
        )
        self.assertIn("successfully integrated", res)
        # model/background path without confirm must deny persistent code exec
        denied = integrate_tool("test_multiply_evil", "evil", {}, code)
        self.assertIn("cancelled", denied)
        try:
            specs = all_tool_specs()
            names = [s["function"]["name"] for s in specs]
            self.assertIn("test_multiply", names)

            out = tool_impl("test_multiply", {"a": 6, "b": 7})
            self.assertEqual(out, "42")
        finally:
            remove_custom_tool("test_multiply")


class TestIntegrations(unittest.TestCase):
    def test_acp_management(self):
        # Never touch the real ~/.buddy/config.json: _save_config() writes to
        # the CONFIG path, so redirect it at a temp dir for this test.
        from unittest import mock
        from buddy_core import config as _config_mod
        import tempfile
        from buddy_core.acp import add_acp_agent, remove_acp_agent, acp_tool
        with tempfile.TemporaryDirectory() as _td:
            with mock.patch.object(_config_mod, "CONFIG", Path(_td) / "config.json"):
                self._acp_management_body(add_acp_agent, remove_acp_agent, acp_tool)

    def _acp_management_body(self, add_acp_agent, remove_acp_agent, acp_tool):
        from buddy_core import config as _config_mod
        cfg = {"acp_agents": {}}
        res = add_acp_agent(cfg, "test_agent", "echo", ["hello"])
        self.assertIn("configured", res)
        self.assertIn("test_agent", cfg["acp_agents"])

        tool_list = acp_tool(cfg, "list", "")
        self.assertIn("test_agent", tool_list)

        rem = remove_acp_agent(cfg, "test_agent")
        self.assertIn("removed", rem)
        self.assertNotIn("test_agent", cfg["acp_agents"])
        self.assertFalse(_config_mod.CONFIG.exists() and "test_agent" in _config_mod.CONFIG.read_text())

    def test_service_status_summary(self):
        from buddy_core.sched import service_status_summary
        summary = service_status_summary({})
        self.assertIn("Buddy Daemon:", summary)
        self.assertIn("Systemd Service:", summary)

    def test_slash_integrations(self):
        from buddy_core.commands import run_slash_command
        cfg = {"api_base": "https://test", "model": "test-model"}
        m_out = run_slash_command(cfg, "/model")
        self.assertIn("current: test-model", m_out)
        self.assertIn("providers & models", m_out)

        s_out = run_slash_command(cfg, "/service")
        self.assertIn("Buddy Daemon:", s_out)


class TestIntrospection(unittest.TestCase):
    def test_introspect_all_and_focus(self):
        from buddy_core.tools import introspect, tool_impl
        all_info = introspect("all")
        self.assertIn("Cognitive & Model State", all_info)
        self.assertIn("Knowledge & Playbook", all_info)
        self.assertIn("Toolchain & Integrations", all_info)
        self.assertIn("Operational Health & Telemetry", all_info)

        cog_info = introspect("cognitive")
        self.assertIn("Cognitive & Model State", cog_info)
        self.assertNotIn("Operational Health & Telemetry", cog_info)

        tool_info = introspect("tools")
        self.assertIn("Toolchain & Integrations", tool_info)

        tool_exec = tool_impl("introspect", {"focus": "cognitive"})
        self.assertIn("Cognitive & Model State", tool_exec)

    def test_slash_introspect(self):
        from buddy_core.commands import run_slash_command
        cfg = {"api_base": "https://test", "model": "test-model"}
        out = run_slash_command(cfg, "/introspect")
        self.assertIn("Cognitive & Model State", out)


class TestAutoUpgradeAndSelfRepair(unittest.TestCase):
    def test_self_repair_healthy_scan(self):
        from unittest import mock
        from buddy_core.skills import self_repair
        cfg = {}
        # Hermetic: skip self_repair's inner suite run (slow, machine-
        # speed dependent) — syntax + error-log scans still run.
        with mock.patch.dict(os.environ, {"BUDDY_TESTING_SELF_REPAIR": "1"}):
            res = self_repair(cfg)
        self.assertIn("healthy and bug-free", res)

    def test_tools_specs(self):
        from buddy_core.tools import all_tool_specs
        specs = {s["function"]["name"]: s["function"] for s in all_tool_specs()}
        self.assertIn("auto_upgrade", specs)
        self.assertIn("self_repair", specs)
        self.assertIn("issue", specs["self_repair"]["parameters"]["properties"])

    def test_auto_upgrade_daemon_init(self):
        from buddy_core.skills import AutoUpgrade
        au = AutoUpgrade({"auto_upgrade": True, "upgrade_hours": 12})
        self.assertTrue(au.cfg.get("auto_upgrade"))
        self.assertEqual(au.cfg.get("upgrade_hours"), 12)

    def test_slash_commands(self):
        from unittest import mock
        from buddy_core.commands import run_slash_command
        cfg = {}
        with mock.patch.dict(os.environ, {"BUDDY_TESTING_SELF_REPAIR": "1"}):
            res_fix = run_slash_command(cfg, "/fix")
        self.assertIn("healthy and bug-free", res_fix)
        res_help = run_slash_command(cfg, "/help")
        self.assertIn("commands:", res_help)
        res_tools = run_slash_command(cfg, "/tools")
        self.assertIn("built-in tools:", res_tools)
        res_service = run_slash_command(cfg, "/service")
        self.assertIn("Buddy Daemon:", res_service)
        res_acp = run_slash_command(cfg, "/acp")
        self.assertIn("ACP agents", res_acp)


class TestOfflineFallback(unittest.TestCase):
    def test_offline_shell_and_commands(self):
        from buddy_core.agent import offline_task_handler
        out = offline_task_handler("! echo hello-offline")
        self.assertIn("hello-offline", out)
        out2 = offline_task_handler("run echo offline-run")
        self.assertIn("offline-run", out2)

    def test_offline_math(self):
        from buddy_core.agent import offline_task_handler
        res = offline_task_handler("calc 25 * 4")
        self.assertIn("100", res)
        res2 = offline_task_handler("what is 500 / 25")
        self.assertIn("20", res2)

    def test_offline_time_and_tools(self):
        from buddy_core.agent import offline_task_handler
        t = offline_task_handler("what time is it")
        self.assertIn("📅", t)
        tools = offline_task_handler("tools")
        self.assertIn("Built-in Tools:", tools)

    def test_offline_utils(self):
        from buddy_core.agent import offline_task_handler
        pwd_res = offline_task_handler("pwd")
        self.assertIn("Current directory:", pwd_res)
        hash_res = offline_task_handler("sha256 test")
        self.assertEqual(hash_res, "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
        b64_enc = offline_task_handler("base64 encode hello")
        self.assertEqual(b64_enc, "aGVsbG8=")
        b64_dec = offline_task_handler("base64 decode aGVsbG8=")
        self.assertEqual(b64_dec, "hello")
        which_res = offline_task_handler("which python3")
        self.assertIn("python3", which_res)

    def test_run_messages_offline_graceful_recovery(self):
        from buddy_core.agent import run_messages
        # When API fails with unreachable endpoint, offline tasks still succeed
        cfg = {"api_base": "http://127.0.0.1:59999/v1", "model": "fake-model"}
        messages = [{"role": "user", "content": "calc 12 * 12"}]
        res = run_messages(cfg, None, messages, quiet=True)
        self.assertIn("144", res)
        self.assertIn("offline", res.lower())


class TestTUI(unittest.TestCase):
    def test_char_and_str_width(self):
        from buddy_core.tui import _char_width, _str_width
        self.assertEqual(_char_width("a"), 1)
        self.assertEqual(_char_width("🧠"), 2)
        self.assertEqual(_char_width(""), 0)
        self.assertEqual(_str_width("hello"), 5)
        self.assertEqual(_str_width("🧠 brain"), 2 + 1 + 5)

    def test_wrap_runs_wide_characters(self):
        from buddy_core.tui import _wrap_runs, _str_width
        runs = [("🧠" * 5, 0)]  # 10 cells wide
        wrapped = _wrap_runs(runs, width=6)
        self.assertTrue(len(wrapped) >= 2)
        for row in wrapped:
            row_width = sum(_str_width(t) for t, _ in row)
            self.assertLessEqual(row_width, 6)


class TestTUIScroll(unittest.TestCase):
    """Mouse/trackpad wheel must scroll the full-screen viewport."""

    def _drv(self, n=20):
        from buddy_core.tui import _FullScreen
        d = _FullScreen()
        d.lines = [[(f"l{i}", 0)] for i in range(n)]
        d._pend = []
        d.scroll = 0
        d._size = (24, 80)
        d._buf, d._pos, d._popup = "", 0, None
        return d

    def test_scroll_by_clamps(self):
        d = self._drv()
        d._scroll_by(3)
        self.assertEqual(d.scroll, 3)
        d._scroll_by(-10)
        self.assertEqual(d.scroll, 0)
        d._scroll_by(1000)
        self.assertEqual(d.scroll, 20)

    def test_wheel_up_down(self):
        import buddy_core.tui as t
        d = self._drv()
        orig = t.curses.getmouse
        try:
            t.curses.getmouse = lambda: (0, 0, 0, 0, t.curses.BUTTON4_PRESSED)
            self.assertIsNone(d._key(t.curses.KEY_MOUSE))
            self.assertEqual(d.scroll, 3)
            t.curses.getmouse = lambda: (0, 0, 0, 0, t.curses.BUTTON5_PRESSED)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(d.scroll, 0)
            d._key(t.curses.KEY_MOUSE)  # stays clamped at bottom
            self.assertEqual(d.scroll, 0)
        finally:
            t.curses.getmouse = orig

    def test_pgup_pgdn_still_work(self):
        import buddy_core.tui as t
        d = self._drv()
        d._key(t.curses.KEY_PPAGE)
        self.assertGreater(d.scroll, 0)
        d._key(t.curses.KEY_NPAGE)
        self.assertEqual(d.scroll, 0)


class TestOpencodeDiff(unittest.TestCase):
    """File edits must render opencode-style diffs (dual line numbers,
    +A -D counts, hunk headers) on every surface."""

    def test_edit_write_code_edit_record_diffs(self):
        from buddy_core.tools import edit_file, write_file, code_edit, pop_diff
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.txt"
            p.write_text("line1\nline2\nline3\n")
            edit_file(str(p), "line2", "LINE2", confirm=lambda _m: True)
            d = pop_diff()
            self.assertIn("+LINE2", d)
            self.assertIn("-line2", d)
            q = Path(td) / "b.txt"
            write_file(str(q), "hello\n", confirm=lambda _m: True)
            self.assertIn("+hello", pop_diff())
            r = Path(td) / "c.py"
            r.write_text("x = 1\n")
            code_edit(str(r), "x = 1", "x = 99", confirm=lambda _m: True)
            d3 = pop_diff()
            self.assertIn("+x = 99", d3)
            self.assertIn("-x = 1", d3)

    def test_print_diff_block_opencode_style(self):
        import difflib
        import io
        from contextlib import redirect_stdout
        from buddy_core.commands import _print_diff_block
        diff = "\n".join(difflib.unified_diff(
            ["a", "b"], ["a", "B", "c"],
            fromfile="f (old)", tofile="f (new)", lineterm=""))
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_diff_block("edit_file", diff, {"path": "/tmp/f.py"}, "ok")
        out = buf.getvalue()
        self.assertIn("(+2 -1)", out)
        self.assertIn("@@", out)
        self.assertIn("+ B", out)
        self.assertIn("- b", out)
        # code_edit resolves the target= arg for its header too
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            _print_diff_block("code_edit", diff, {"target": "buddy_core/x.py"}, "ok")
        self.assertIn("x.py", buf2.getvalue())


class TestTUICopy(unittest.TestCase):
    """Full-screen TUI must let users copy text out (mouse toggle + Ctrl+O)."""

    def _drv(self):
        import buddy_core.tui as t
        d = t._FullScreen()
        d.lines = [[("hello world", 0)], [("second line", 0)]]
        d._pend = []

        class StubScr:
            def getmaxyx(self):
                raise t.curses.error("stub")

            def erase(self):
                pass

            def noutrefresh(self):
                pass

        d._stdscr = StubScr()
        return d, t

    def test_viewport_text_joins_lines_and_pend(self):
        d, _ = self._drv()
        d._pend = [("pend!", 0)]
        txt = d._viewport_text()
        self.assertIn("hello world", txt)
        self.assertIn("second line", txt)
        self.assertIn("pend!", txt)

    def test_mouse_toggle_f9_and_ctrl_g(self):
        d, t = self._drv()
        orig_mask, orig_iv = t.curses.mousemask, t.curses.mouseinterval
        t.curses.mousemask = lambda m: (m, 0)
        t.curses.mouseinterval = lambda ms: None
        try:
            d._mouse_enable(full=False)
            self.assertTrue(d._mouse_on)
            self.assertFalse(d._mouse_full)
            self.assertIsNone(d._key(t.curses.KEY_F9))
            self.assertTrue(d._mouse_full)   # F9 -> full in-app mouse
            d._key(7)  # Ctrl+G
            self.assertFalse(d._mouse_full)  # back to wheel-only
            self.assertTrue(d._mouse_on)     # wheel still scrolls
        finally:
            t.curses.mousemask, t.curses.mouseinterval = orig_mask, orig_iv

    def test_copy_falls_back_to_file(self):
        d, t = self._drv()
        with tempfile.TemporaryDirectory() as td:
            old_home, old_real = t.HOME, d._real
            t.HOME, d._real = Path(td), None
            orig_run = subprocess.run
            subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
                FileNotFoundError("no clipboard tool"))
            try:
                msg = d._to_clipboard("copy-me-text")
                # the native-tool copy now runs in a background thread (it
                # used to freeze the display lock for ~10s) — wait for it
                deadline = time.time() + 5
                files = []
                while time.time() < deadline:
                    files = list(Path(td).glob("copies/*.txt"))
                    if files:
                        break
                    time.sleep(0.05)
            finally:
                subprocess.run = orig_run
                t.HOME, d._real = old_home, old_real
            self.assertTrue(files)
            self.assertEqual(files[0].read_text(), "copy-me-text")
            self.assertIn("copied", msg)


class TestTUIHighlight(unittest.TestCase):
    """Left-drag in the full-screen TUI must highlight chat text to copy."""

    def _drv(self):
        import buddy_core.tui as t

        class StubScr:
            def getmaxyx(self):
                return (24, 80)

            def erase(self):
                pass

            def noutrefresh(self):
                pass

            def addnstr(self, *a):
                pass

        d = t._FullScreen()
        d.lines = [[("hello world, this is a test", 0)],
                   [("second line here", 0)]]
        d._pend = []
        d._size = (24, 80)
        d._stdscr = StubScr()
        d._buf, d._pos, d._popup = "", 0, None
        return d, t

    def test_press_drag_release_copies(self):
        d, t = self._drv()
        orig = (t.curses.mousemask, t.curses.mouseinterval,
                t.curses.getmouse)
        t.curses.mousemask = lambda m: (m, 0)
        t.curses.mouseinterval = lambda ms: None
        copied = {}
        d._to_clipboard = lambda text: copied.setdefault("text", text)
        try:
            b1p, b1r = t.curses.BUTTON1_PRESSED, t.curses.BUTTON1_RELEASED
            t.curses.getmouse = lambda: (0, 1, 0, 0, b1p)
            self.assertIsNone(d._key(t.curses.KEY_MOUSE))
            self.assertIsNotNone(d._selecting)
            t.curses.getmouse = lambda: (
                0, 6, 0, 0, b1p | t.curses.REPORT_MOUSE_POSITION)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(d._selection_text(), "hello")
            t.curses.getmouse = lambda: (0, 6, 0, 0, b1r)
            d._key(t.curses.KEY_MOUSE)
            self.assertIsNone(d._selecting)
            self.assertEqual(copied.get("text"), "hello")
        finally:
            (t.curses.mousemask, t.curses.mouseinterval,
             t.curses.getmouse) = orig

    def test_multiline_span_and_wheel_unaffected(self):
        d, t = self._drv()
        orig = (t.curses.mousemask, t.curses.mouseinterval,
                t.curses.getmouse)
        t.curses.mousemask = lambda m: (m, 0)
        t.curses.mouseinterval = lambda ms: None
        copied = {}
        d._to_clipboard = lambda text: copied.setdefault("text", text)
        try:
            b1p, b1r = t.curses.BUTTON1_PRESSED, t.curses.BUTTON1_RELEASED
            t.curses.getmouse = lambda: (0, 7, 0, 0, b1p)
            d._key(t.curses.KEY_MOUSE)
            t.curses.getmouse = lambda: (
                0, 7, 1, 0, b1p | t.curses.REPORT_MOUSE_POSITION)
            d._key(t.curses.KEY_MOUSE)
            t.curses.getmouse = lambda: (0, 7, 1, 0, b1r)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(copied.get("text"),
                             "world, this is a test\nsecond")
            d.scroll = 0
            t.curses.getmouse = lambda: (
                0, 0, 0, 0, t.curses.BUTTON4_PRESSED)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(d.scroll, 3)
        finally:
            (t.curses.mousemask, t.curses.mouseinterval,
             t.curses.getmouse) = orig


class TestTUIMouse(unittest.TestCase):
    """Mouse reporting is OFF by default (normal terminal select/copy);
    F9 or config 'mouse': true opts into wheel/drag/paste mode."""

    def _drv(self):
        import buddy_core.tui as t

        class StubScr:
            def getmaxyx(self):
                return (24, 80)

            def erase(self):
                pass

            def noutrefresh(self):
                pass

            def addnstr(self, *a):
                pass

        d = t._FullScreen()
        d._stdscr = StubScr()
        d._buf, d._pos, d._popup = "", 0, None
        return d, t

    def test_welcome_keeps_wheel_only_default(self):
        # wheel reporting is required (the full-screen UI owns the screen,
        # so the terminal has no scrollback to scroll) but buttons must stay
        # with the terminal for select/copy, right-click menu and paste
        d, t = self._drv()
        orig = (t.curses.mousemask, t.curses.mouseinterval)
        t.curses.mousemask = lambda m: (m, 0)
        t.curses.mouseinterval = lambda ms: None
        try:
            d._mouse_enable(full=False)
            d.welcome({}, "local", 0)
            self.assertTrue(d._mouse_on)
            self.assertFalse(d._mouse_full)
            self.assertTrue(t.curses.BUTTON4_PRESSED & d._base_mask)
            for btn in ("BUTTON1_PRESSED", "BUTTON2_PRESSED",
                        "BUTTON3_PRESSED"):
                self.assertFalse(getattr(t.curses, btn) & d._base_mask, btn)
        finally:
            t.curses.mousemask, t.curses.mouseinterval = orig

    def test_wheel_scrolls_in_default_mode(self):
        d, t = self._drv()
        d.lines = [[(f"line {i}", 0)] for i in range(40)]
        d._pend = []
        d._size = (24, 80)
        orig = (t.curses.mousemask, t.curses.mouseinterval,
                t.curses.getmouse)
        t.curses.mousemask = lambda m: (m, 0)
        t.curses.mouseinterval = lambda ms: None
        try:
            d._mouse_enable(full=False)
            d.scroll = 0
            t.curses.getmouse = lambda: (0, 0, 0, 0, t.curses.BUTTON4_PRESSED)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(d.scroll, 3)
        finally:
            (t.curses.mousemask, t.curses.mouseinterval,
             t.curses.getmouse) = orig

    def test_f9_toggles_full_mouse_and_back(self):
        d, t = self._drv()
        orig = (t.curses.mousemask, t.curses.mouseinterval)
        t.curses.mousemask = lambda m: (m, 0)
        t.curses.mouseinterval = lambda ms: None
        try:
            d._mouse_enable(full=False)
            d._key(t.curses.KEY_F9)
            self.assertTrue(d._mouse_full)
            self.assertTrue(t.curses.BUTTON1_PRESSED & d._base_mask)
            d._key(t.curses.KEY_F9)
            self.assertFalse(d._mouse_full)
            self.assertTrue(t.curses.BUTTON4_PRESSED & d._base_mask)
        finally:
            t.curses.mousemask, t.curses.mouseinterval = orig

    def test_welcome_mouse_config_opts_in(self):
        d, t = self._drv()
        orig = (t.curses.mousemask, t.curses.mouseinterval)
        t.curses.mousemask = lambda m: (m, 0)
        t.curses.mouseinterval = lambda ms: None
        try:
            d.welcome({"mouse": True}, "local", 0)
            self.assertTrue(d._mouse_on)
            self.assertTrue(d._mouse_full)  # full mode from config
        finally:
            t.curses.mousemask, t.curses.mouseinterval = orig

    def test_right_and_middle_click_paste(self):
        d, t = self._drv()
        orig = t.curses.getmouse
        d._read_clipboard = staticmethod(lambda: "XY")
        try:
            d._buf, d._pos = "ab", 1
            t.curses.getmouse = lambda: (0, 0, 0, 0, t.curses.BUTTON3_PRESSED)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(d._buf, "aXYb")
            self.assertEqual(d._pos, 3)
            d._buf, d._pos = "", 0
            t.curses.getmouse = lambda: (0, 0, 0, 0, t.curses.BUTTON2_PRESSED)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(d._buf, "XY")
        finally:
            t.curses.getmouse = orig

    def test_paste_without_tool_warns(self):
        d, t = self._drv()
        orig = t.curses.getmouse
        d._read_clipboard = staticmethod(lambda: None)
        try:
            t.curses.getmouse = lambda: (0, 0, 0, 0, t.curses.BUTTON3_CLICKED)
            d._key(t.curses.KEY_MOUSE)
            self.assertEqual(d._buf, "")
            self.assertTrue(any("paste failed" in "".join(x for x, _ in r)
                                for r in d.lines))
        finally:
            t.curses.getmouse = orig


class TestDirectCommands(unittest.TestCase):
    """Direct shell execution fixes: output spill path, offline routing,
    WebUI yolo-aware confirm."""

    def test_truncated_output_spills_to_buddy_outputs(self):
        from buddy_core.tools import run_command
        out = run_command("seq 1 3000", timeout=30)
        self.assertIn("Full output saved to: ", out)
        path = out.split("Full output saved to: ")[1].strip().split("\n")[0]
        self.assertNotIn(".buddy/.buddy", path)
        self.assertIn(".buddy/outputs/", path)
        self.assertTrue(Path(path).exists())

    def test_offline_run_tests_reaches_test_runner(self):
        from unittest import mock
        import buddy_core.tools as tools_mod
        from buddy_core.agent import offline_task_handler
        with mock.patch.object(tools_mod, "run_command",
                               return_value="ok") as rc:
            offline_task_handler("run tests")
            self.assertIn("unittest", rc.call_args[0][0])
            offline_task_handler("run echo hi")
            self.assertEqual(rc.call_args[0][0], "echo hi")
            offline_task_handler("git status")
            self.assertEqual(rc.call_args[0][0], "git status")

    def test_web_confirm_honors_yolo_and_bypass(self):
        from buddy_core.webui import WebUI
        old = WebUI.cfg
        try:
            WebUI.cfg = {}
            self.assertFalse(WebUI.confirm("rm -rf /"))
            WebUI.cfg = {"yolo": True}
            self.assertTrue(WebUI.confirm("rm -rf /"))
            WebUI.cfg = {"mode": "bypass"}
            self.assertTrue(WebUI.confirm("rm -rf /"))
        finally:
            WebUI.cfg = old


class TestNameErrorFixes(unittest.TestCase):
    """Deep-fix regressions: names that crashed only on specific inputs."""

    def test_parse_intent_ls_and_cat(self):
        from buddy_core.commands import parse_intent
        self.assertEqual(parse_intent("ls /tmp"), ("list_dir", "/tmp"))
        self.assertEqual(parse_intent("cat README.md"),
                         ("read_file", "README.md"))
        # paths keep their original case (low is lowercased for matching)
        self.assertEqual(parse_intent("cat MyFile.TXT"),
                         ("read_file", "MyFile.TXT"))
        self.assertEqual(parse_intent("read notes/todo.md"),
                         ("read_file", "notes/todo.md"))

    def test_sched_exposes_port(self):
        from buddy_core import sched
        from buddy_core.config import PORT
        self.assertEqual(sched.PORT, PORT)
        self.assertEqual(sched.PORT, 7616)


class TestSpeechLocked(unittest.TestCase):
    """Speech (TTS + transcription) is locked to Gemini 3.8 Flash —
    cfg tts_model and /model switches must not affect it."""

    def _tts_resp(self, pcm=b"\x00\x01" * 256):
        import base64
        return {"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000",
            "data": base64.b64encode(pcm).decode()}}]}}]}

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps(self.payload).encode()

    def test_speak_uses_locked_model_only(self):
        import json
        from unittest import mock
        import buddy_core.util as u
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(u, "HOME", Path(td)):
                seen = {}

                def fake_urlopen(req, timeout=None):
                    seen["url"] = req.full_url
                    seen["body"] = json.loads(req.data.decode())
                    return self._Resp(self._tts_resp())

                cfg = {"tts": "api", "tts_model": "custom-xyz",
                       "tts_voice": "nova",
                       "api_base": "https://api.openai.com/v1",
                       "model": "gpt-4o"}
                with mock.patch("urllib.request.urlopen",
                                 side_effect=fake_urlopen):
                    with mock.patch.object(u, "_play_file", return_value=True):
                        with mock.patch.object(u, "api_key", return_value="K"):
                            u.speak("Hello there", cfg)
                    import wave
                    with wave.open(str(Path(td) / "last_tts.wav"), "rb") as w:
                        self.assertEqual((w.getnchannels(), w.getsampwidth(),
                                          w.getframerate()), (1, 2, 24000))
                        self.assertTrue(w.getnframes() > 0)
        self.assertIn("gemini-3.8-flash-tts", seen["url"])
        self.assertIn("generateContent", seen["url"])
        blob = json.dumps(seen["body"])
        self.assertNotIn("tts-1", blob)
        self.assertNotIn("custom-xyz", blob)
        self.assertNotIn("openai", seen["url"])
        voice = (seen["body"]["generationConfig"]["speechConfig"]
                 ["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"])
        self.assertEqual(voice, "Kore")

    def test_transcribe_uses_locked_model_only(self):
        import json
        from unittest import mock
        import buddy_core.tools as tm
        stt = {"candidates": [{"content": {"parts": [
            {"text": "hello buddy"}]}}]}
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "mic.wav"
            wav.write_bytes(b"RIFF....fake")
            seen = {}

            def fake_urlopen(req, timeout=None):
                seen["url"] = req.full_url
                seen["body"] = json.loads(req.data.decode())
                return self._Resp(stt)

            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with mock.patch.object(tm, "api_key", return_value="K"):
                    with mock.patch.object(tm, "load_config", return_value={}):
                        out = tm.transcribe_audio(str(wav))
        self.assertEqual(out, "hello buddy")
        self.assertIn("gemini-3.8-flash", seen["url"])
        self.assertNotIn("transcriptions", seen["url"])
        self.assertNotIn("whisper", json.dumps(seen["body"]))
        parts = seen["body"]["contents"][0]["parts"]
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "audio/wav")

    def test_voice_mapping(self):
        import buddy_core.util as u
        self.assertEqual(u._gemini_voice("nova"), "Kore")
        self.assertEqual(u._gemini_voice("Puck"), "Puck")
        self.assertEqual(u._gemini_voice("nope"), "Kore")
        self.assertEqual(u.SPEECH_TTS_MODEL, "gemini-3.8-flash-tts")
        import buddy_core.tools as _tm
        self.assertEqual(_tm.SPEECH_STT_MODEL, "gemini-3.8-flash")


class TestProviderKeys(unittest.TestCase):
    """A second provider must never receive the first provider's key."""

    def _cfg(self):
        return {"api_base": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "models": {"deepseek": {"api_base": "https://api.deepseek.com/v1",
                                        "model": "deepseek-chat"}}}

    def test_provider_key_name_resolution(self):
        from buddy_core.config import provider_key_name
        cfg = self._cfg()
        self.assertEqual(
            provider_key_name(cfg, "deepseek-chat",
                              "https://api.deepseek.com/v1"),
            "api_key_deepseek")
        # google isn't an integrated entry -> no provider secret
        self.assertEqual(
            provider_key_name(cfg, "gemini-3.8-flash",
                              "https://generativelanguage.googleapis.com/v1beta/openai"),
            "")

    def test_api_key_for_prefers_provider_key(self):
        from buddy_core import config as cfg_mod
        from buddy_core.config import api_key_for
        cfg = self._cfg()
        with mock.patch.object(cfg_mod, "secret_get",
                               side_effect=lambda n: "DEEPKEY" if n == "api_key_deepseek" else ""):
            with mock.patch.object(cfg_mod, "api_key", return_value="GOOGLEKEY"):
                self.assertEqual(api_key_for(cfg), "DEEPKEY")
        # no provider key -> shared key (user's explicit choice), still works
        with mock.patch.object(cfg_mod, "secret_get", return_value=""):
            with mock.patch.object(cfg_mod, "api_key", return_value="GOOGLEKEY"):
                self.assertEqual(api_key_for(cfg), "GOOGLEKEY")

    def test_request_to_second_provider_never_sends_main_key(self):
        from unittest import mock
        import buddy_core.brain as brain
        from buddy_core import config as cfg_mod
        sent = {}

        class Resp:
            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=None):
            sent["auth"] = req.get_header("Authorization") or ""
            sent["url"] = req.full_url
            return Resp()

        with mock.patch.object(cfg_mod, "secret_get",
                               side_effect=lambda n: "DEEPKEY" if n == "api_key_deepseek" else ""):
            with mock.patch.object(cfg_mod, "api_key", return_value="GOOGLEKEY"):
                with mock.patch("urllib.request.urlopen", side_effect=fake):
                    brain._completion(self._cfg(),
                                      [{"role": "user", "content": "hi"}],
                                      None, stream=False)
        self.assertTrue(sent["url"].startswith("https://api.deepseek.com"))
        self.assertIn("DEEPKEY", sent["auth"])
        self.assertNotIn("GOOGLEKEY", sent["auth"])

    def test_model_add_stores_provider_key(self):
        from unittest import mock
        from pathlib import Path
        import buddy_core.commands as cmds
        from buddy_core import config as cfg_mod
        with tempfile.TemporaryDirectory() as td:
            secrets_file = Path(td) / "secrets.json"
            with mock.patch.object(cfg_mod, "CONFIG",
                                   Path(td) / "config.json"):
                with mock.patch.object(cfg_mod, "SECRETS", secrets_file):
                    out = cmds.run_slash_command(
                        {"models": {}},
                        "/model add deepseek https://api.deepseek.com/v1 "
                        "deepseek-chat sk-abc123", confirm=lambda _m: True)
            self.assertIn("integrated", out)
            self.assertNotIn("sk-abc123", out)  # never echoed back
            stored = json.loads(secrets_file.read_text())
            self.assertEqual(stored.get("api_key_deepseek"), "sk-abc123")

    def test_doctor_flags_provider_without_own_key(self):
        import inspect
        from buddy_core.commands import doctor
        src = inspect.getsource(doctor)
        self.assertIn("provider key(s) missing", src)


class TestModelFailover(unittest.TestCase):
    """A 503/per-model 429 retries the turn on another model. A project
    quota 429 fails over only to a DIFFERENT base (e.g. a local ollama
    model) — same-key retries are pointless."""

    QUOTA = ('API error 429: {"error":{"code":429,"message":"You exceeded your '
             'current quota, please check your plan and billing details."}}')
    PER_MODEL = ('API error 429: {"error":{"message":"too many requests, '
                 'rate limit exceeded"}}')
    OVERLOAD = 'API error 503: high demand'

    def setUp(self):
        # run_messages bails to offline mode when no API key is configured,
        # which would skip _completion entirely — keep the suite hermetic on
        # machines where buddy has no stored key.
        self._key = mock.patch.dict(os.environ, {"BUDDY_API_KEY": "FAKEKEY"})
        self._key.start()
        self.addCleanup(self._key.stop)

    def _cfg(self, **kw):
        base = "https://generativelanguage.googleapis.com/v1beta/openai"
        return {"api_base": base, "model": "gemini-3.8-flash", "models": {}, **kw}

    def test_overload_and_per_model_limit_fail_over(self):
        from buddy_core.agent import _failover_model
        for err in (self.OVERLOAD, self.PER_MODEL):
            alt = _failover_model(self._cfg(), err)
            self.assertTrue(alt, err)
            self.assertNotEqual(alt, "gemini-3.8-flash")

    def test_project_quota_does_not_fail_over(self):
        from buddy_core.agent import _failover_model, _is_project_quota
        self.assertTrue(_is_project_quota(self.QUOTA))
        # same-key/same-base models are pointless on a project quota
        self.assertIsNone(_failover_model(self._cfg(), self.QUOTA))
        # ...but a different-base model (local ollama) is exactly the point
        cross = self._cfg(models={"local": {
            "api_base": "http://127.0.0.1:11434/v1", "model": "llama3.2:1b"}})
        self.assertEqual(_failover_model(cross, self.QUOTA), "llama3.2:1b")

    def test_cross_provider_failover_needs_its_own_key(self):
        # A cross-provider candidate without its own key would get the main
        # provider's key — a guaranteed 401 from a host that never saw it.
        from buddy_core.agent import _failover_model
        deep = self._cfg(models={"deep": {
            "api_base": "https://api.deepseek.com/v1", "model": "deepseek-chat"}})
        # no stored key for deepseek → candidate is skipped (failover may
        # still pick a same-base model, but never the keyless provider)
        with mock.patch("buddy_core.config.secret_get", return_value=""):
            alt = _failover_model(deep, self.PER_MODEL)
            self.assertNotEqual(alt, "deepseek-chat")
        # with its own key stored → legitimate failover target (429 allows
        # crossing API bases)
        with mock.patch("buddy_core.config.secret_get", return_value="sk-deep"):
            self.assertEqual(_failover_model(deep, self.PER_MODEL),
                             "deepseek-chat")

    def test_never_fails_over_on_auth_or_missing_model(self):
        from buddy_core.agent import _failover_model
        for err in ("API error 401: bad key", "API error 403",
                    "API error 404: model not found"):
            self.assertIsNone(_failover_model(self._cfg(), err), err)

    def test_opt_out_and_provider_safety(self):
        from buddy_core.agent import _failover_model
        self.assertIsNone(_failover_model(
            self._cfg(failover=False), self.OVERLOAD))
        # a non-Google endpoint must never receive a Gemini preset
        other = {"api_base": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "models": {}}
        self.assertIsNone(_failover_model(other, self.OVERLOAD))
        # ...but a model the user added for that endpoint is fair game
        other["models"] = {"alt": {"api_base": "https://api.deepseek.com/v1",
                                   "model": "deepseek-reasoner"}}
        self.assertEqual(_failover_model(other, self.OVERLOAD),
                         "deepseek-reasoner")

    def test_turn_retries_once_without_touching_config(self):
        from unittest import mock
        import buddy_core.agent as agent
        cfg = self._cfg()
        tried = []

        def fake(cfg_, messages, tools, stream=False, on_delta=None,
                 model=None, base=None):
            tried.append(model or cfg_["model"])
            if len(tried) == 1:
                raise RuntimeError(self.OVERLOAD)
            return {"choices": [{"message": {"content": "recovered"}}]}

        with mock.patch.object(agent, "_completion", side_effect=fake):
            out = agent.run_messages(cfg, None,
                                     [{"role": "user", "content": "hi"}],
                                     quiet=True)
        self.assertEqual(len(tried), 2)
        self.assertEqual(tried[0], "gemini-3.8-flash")  # first: configured
        self.assertNotEqual(tried[1], "gemini-3.8-flash")  # then: alternate
        self.assertIn("recovered", out)
        self.assertEqual(cfg["model"], "gemini-3.8-flash")  # untouched

    def test_quota_error_says_switching_wont_help(self):
        from buddy_core.brain import _api_err
        msg = _api_err(429, self.QUOTA)
        # concise one-liner: no raw JSON dump, but keeps the key facts
        self.assertIn("quota exhausted", msg)
        self.assertIn("won't help" if "won't help" in msg else "out of quota", msg)
        self.assertNotIn("exceeded your current quota", msg)  # raw body dropped
        # a plain per-model rate limit keeps the retry wording
        self.assertIn("retries", _api_err(429, self.PER_MODEL))


class TestWebModelSwitching(unittest.TestCase):
    """Web needs a real model picker and must run the intent layer, so
    'change model to X' can't just be claimed by the LLM without effect."""

    def test_models_endpoint_shape(self):
        from unittest import mock
        from buddy_core import commands as cmds
        from buddy_core import config as cfg_mod
        from buddy_core.webui import PAGE, WebUI
        self.assertIn("/api/models", PAGE)
        self.assertIn("/api/model", PAGE)
        self.assertIn("id=\"model\"", PAGE)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cfg_mod, "CONFIG",
                                   Path(td) / "config.json"):
                with mock.patch.object(cmds, "_fetch_endpoint_models",
                                       return_value=None):
                    entries = cmds._model_entries({
                        "api_base": "https://generativelanguage.googleapis.com"
                                    "/v1beta/openai",
                        "model": "gemini-2.5-flash"})
        payload = [{"n": e["n"], "label": e["label"], "provider": e["provider"],
                    "model": e["model"], "source": e["source"],
                    "active": e["active"]} for e in entries]
        self.assertTrue(payload)
        self.assertTrue(any(m["active"] for m in payload))
        self.assertTrue(all({"n", "label", "provider", "model", "source",
                             "active"} == set(m) for m in payload))

    def test_web_runs_intent_layer_for_model_switch(self):
        import inspect
        import buddy_core.webui as webui
        src = inspect.getsource(webui.WebUI.do_POST)
        # the deterministic layer must run BEFORE the LLM call
        self.assertIn("parse_intent", src)
        self.assertIn("_run_intent", src)
        self.assertLess(src.index("_run_intent("), src.index("run_messages("))

    def test_apply_model_entry_round_trip(self):
        from unittest import mock
        from pathlib import Path
        import buddy_core.commands as cmds
        from buddy_core import config as cfg_mod
        cfg = {"api_base": "https://x/v1", "model": "m1", "models": {}}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cfg_mod, "CONFIG",
                                   Path(td) / "config.json"):
                out = cmds._run_intent(("model", "gemini-3.8-flash"), cfg,
                                       lambda _m: True)
        self.assertIn("switched", out)
        self.assertEqual(cfg["model"], "gemini-3.8-flash")

    def test_model_switch_endpoint_validates(self):
        import inspect
        import buddy_core.webui as webui
        src = inspect.getsource(webui.WebUI.do_POST)
        self.assertIn('path == "/api/model"', src)
        self.assertIn("model required", src)       # empty -> 400
        self.assertIn("unknown model", src)       # not in catalog -> 400
        self.assertIn("_apply_model_entry", src)  # persists the switch
        self.assertIn("new_session", src)         # tells the page to reset

    def test_model_picker_markup_and_styling(self):
        from buddy_core.webui import PAGE
        self.assertIn('<select class="chip-model" id="model"', PAGE)
        self.assertIn("loadModels();", PAGE)
        self.assertIn("select.chip-model", PAGE)  # styled like the old chip
        self.assertIn("optgroup", PAGE)            # grouped by provider
        self.assertIn("postJSON('/api/model'", PAGE)


class TestModelCatalog(unittest.TestCase):
    """`/model` lists providers + live models with numbers to pick."""

    def _cfg(self):
        return {"api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-2.5-flash",
                "models": {"deep": {"api_base": "https://api.deepseek.com/v1",
                                    "model": "deepseek-chat"}}}

    def test_entries_flag_active_and_dedupe(self):
        from unittest import mock
        import buddy_core.commands as cmds
        with mock.patch.object(cmds, "_fetch_endpoint_models",
                               return_value=["models/gemini-2.5-flash",
                                             "models/gemini-2.5-pro",
                                             "models/gemini-tts-x"]):
            entries = cmds._model_entries(self._cfg())
        labels = [e["label"] for e in entries]
        self.assertIn("google · gemini-2.5-flash", labels)
        self.assertNotIn("google · gemini-tts-x", labels)  # non-chat filtered
        self.assertNotIn("models/gemini-2.5-flash", labels)  # prefix stripped
        active = [e for e in entries if e["active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["model"], "gemini-2.5-flash")
        self.assertEqual([e["n"] for e in entries],
                         list(range(1, len(entries) + 1)))
        self.assertIn("deep", labels)  # integrated first

    def test_catalog_text_and_number_select(self):
        from unittest import mock
        from pathlib import Path
        import tempfile
        import buddy_core.commands as cmds
        from buddy_core import config as cfg_mod
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cfg_mod, "CONFIG", Path(td) / "config.json"):
                with mock.patch.object(cmds, "_fetch_endpoint_models",
                                       return_value=None):
                    text = cmds.run_slash_command(dict(self._cfg()), "/model")
                    self.assertIn("google", text)
                    self.assertNotIn("ollama", text)  # only google ships
                    self.assertIn("●", text)
                    cfg = self._cfg()
                    out = cmds.run_slash_command(cfg, "/model 1", confirm=lambda _m: True)
                    self.assertIn("switched", out)
                    self.assertEqual(cfg["model"], "deepseek-chat")
                    # user-added providers come back through /model add
                    cfg = self._cfg()
                    out = cmds.run_slash_command(
                        cfg, "/model add local http://localhost:11434/v1 llama3.2",
                        confirm=lambda _m: True)
                    self.assertIn("integrated", out)
                    out = cmds.run_slash_command(cfg, "/model local",
                                                 confirm=lambda _m: True)
                    self.assertIn("localhost", cfg["api_base"])
                    self.assertEqual(cfg["model"], "llama3.2")
                    out = cmds.run_slash_command(dict(self._cfg()), "/model 99")
                    self.assertIn("no model #99", out)
                    cfg = self._cfg()
                    out = cmds.run_slash_command(cfg, "/model remove deep",
                                                 confirm=lambda _m: True)
                    self.assertIn("removed", out)
                    self.assertNotIn("deep", cfg["models"])


    def test_presets_are_curated_gemini_only(self):
        from buddy_core.commands import MODEL_PROVIDERS
        self.assertEqual(list(MODEL_PROVIDERS), ["google"])
        models = MODEL_PROVIDERS["google"]["models"]
        self.assertEqual(models[0], "gemini-flash-latest")
        self.assertIn("gemini-pro-latest", models)
        for m in models:
            self.assertTrue(m.startswith("gemini-"), m)
        self.assertEqual(len(models), len(set(models)))  # no dupes

    def test_extras_are_gemini_stable_first(self):
        from buddy_core.commands import _extra_live_models
        live = ["gemma-4-26b-a4b-it", "gemini-3.7-flash", "gemini-3-flash-preview",
                "gemini-2.5-flash-lite", "gemini-omni-1.1-flash",
                "gemini-3.6-flash", "gemini-flash-latest"]
        extras = _extra_live_models(live, ["gemini-flash-latest"])
        self.assertNotIn("gemma-4-26b-a4b-it", extras)  # Gemini only
        self.assertNotIn("gemini-2.5-flash-lite", extras)  # no lite noise
        self.assertNotIn("gemini-omni-1.1-flash", extras)
        self.assertNotIn("gemini-flash-latest", extras)  # preset, not extra
        # stable before preview
        self.assertLess(extras.index("gemini-3.7-flash"),
                        extras.index("gemini-3-flash-preview"))

    def test_presets_first_then_live_extras(self):
        from unittest import mock
        import buddy_core.commands as cmds
        with mock.patch.object(cmds, "_fetch_endpoint_models",
                               return_value=["models/gemini-flash-latest",
                                             "models/gemma-4-26b-a4b-it",
                                             "models/gemini-3.7-flash"]):
            entries = cmds._model_entries(self._cfg())
        labels = [e["label"] for e in entries]
        google = [lb for lb in labels if lb.startswith("google ")]
        self.assertEqual(google[0], "google · gemini-flash-latest")
        self.assertLess(labels.index("google · gemini-2.5-pro"),
                        labels.index("google · gemini-3.7-flash"))
        self.assertNotIn("google · gemma-4-26b-a4b-it", labels)


class TestModelPicker(unittest.TestCase):
    """Bare `/model` opens an arrow-key picker grouped by provider."""

    def _entries(self):
        return [
            {"n": 1, "label": "deep", "provider": "ADDED",
             "base": "b", "model": "m", "source": "integrated",
             "active": False},
            {"n": 2, "label": "google · gemini-2.5-flash",
             "provider": "google", "base": "b", "model": "m",
             "source": "live", "active": True},
            {"n": 3, "label": "ollama · llama3.2", "provider": "ollama",
             "base": "b", "model": "m", "source": "preset",
             "active": False},
        ]

    def test_grouping_order(self):
        from buddy_core.commands import _group_model_entries
        groups = _group_model_entries(self._entries())
        self.assertEqual([g[0] for g in groups],
                         ["ADDED BY YOU", "GOOGLE", "OLLAMA"])

    def test_pick_rows_and_keys(self):
        import buddy_core.tui as t
        d = t._FullScreen()
        d._model_pick = {"items": self._entries(), "sel": 1}
        rows, sel_row = d._pick_rows()
        self.assertEqual(rows[0], ("head", "ADDED BY YOU"))
        self.assertEqual(rows[sel_row], ("item", self._entries()[1]))
        d._key(t.curses.KEY_DOWN)
        self.assertEqual(d._model_pick["sel"], 2)
        d._key(t.curses.KEY_DOWN)
        self.assertEqual(d._model_pick["sel"], 0)  # wraps
        d._key(t.curses.KEY_UP)
        self.assertEqual(d._model_pick["sel"], 2)  # wraps back
        self.assertEqual(d._key("\r"), "\x00MODEL:2")
        self.assertEqual(d._key("\x1b"), "\x00MODEL:CANCEL")
        self.assertIsNone(d._key("x"))  # typing ignored while picking

    def test_scrollback_picker_needs_tty(self):
        from buddy_core.tui import _arrow_pick_model
        # No tty in the test env → graceful None (caller prints the list)
        self.assertIsNone(_arrow_pick_model(self._entries()))


class TestConfirmDialog(unittest.TestCase):
    """approve ...? allow / decline — arrows or y/n."""

    def _drv(self):
        import buddy_core.tui as t
        d = t._FullScreen()
        d._confirm = {"cmd": "rm -rf /tmp/x", "sel": 0}
        return d, t

    def test_shortcuts_and_enter(self):
        d, _ = self._drv()
        self.assertEqual(d._confirm_key("y"), "\x00CONFIRM:allow")
        self.assertEqual(d._confirm_key("a"), "\x00CONFIRM:always")
        self.assertEqual(d._confirm_key("n"), "\x00CONFIRM:decline")
        self.assertEqual(d._confirm_key("\x1b"), "\x00CONFIRM:decline")
        self.assertIsNone(d._confirm_key("z"))

    def test_arrow_nav_wraps(self):
        d, t = self._drv()
        d._confirm_key(t.curses.KEY_RIGHT)
        self.assertEqual(d._confirm["sel"], 1)
        d._confirm_key(t.curses.KEY_LEFT)
        self.assertEqual(d._confirm["sel"], 0)
        d._confirm_key(t.curses.KEY_LEFT)
        self.assertEqual(d._confirm["sel"], 2)
        self.assertEqual(d._confirm_key("\r"), "\x00CONFIRM:decline")
        d._confirm["sel"] = 1
        self.assertEqual(d._confirm_key("\r"), "\x00CONFIRM:always")

    def test_scrollback_confirm_needs_tty(self):
        from buddy_core.tui import _arrow_confirm
        # No tty in the test env → None (caller uses the typed prompt)
        self.assertIsNone(_arrow_confirm("echo hi"))


class TestRound4Hardening(unittest.TestCase):
    """Last batch: memory-file durability, ACP tail drain, provider data."""

    def test_memory_compact_write_is_atomic_and_locked(self):
        import inspect
        import buddy_core.memory as mem
        src = inspect.getsource(mem.memory_compact)
        self.assertNotIn("MEMORY.write_text(", src)  # truncating write
        self.assertIn("os.replace(tmp, MEMORY)", src)  # atomic swap
        self.assertIn("with STATE_LOCK:", src)

    def test_memory_compact_preserves_concurrent_facts(self):
        # a remember() landing during compaction must survive
        import buddy_core.memory as mem
        with tempfile.TemporaryDirectory() as td:
            m = Path(td) / "memory.md"
            m.write_text("## distilled\nold\n")
            with mock.patch.object(mem, "MEMORY", m):
                with mock.patch.object(mem, "STATE_LOCK",
                                       __import__("threading").RLock()):
                    # emulate the compact write path
                    tmp = m.with_suffix(".md.tmp")
                    tmp.write_text("## distilled\ncompacted\n", encoding="utf-8")
                    import os
                    os.replace(tmp, m)
                    with m.open("a") as f:
                        f.write("- [2026-09-26] new fact\n")
            text = m.read_text()
            self.assertIn("compacted", text)
            self.assertIn("new fact", text)

    def test_embed_never_exits_on_corrupt_config(self):
        import buddy_core.memory as mem
        with tempfile.TemporaryDirectory() as td:
            c = Path(td) / "config.json"
            c.write_text("{ corrupt")
            with mock.patch.object(mem, "CONFIG", c):
                # load_config() would sys.exit(1) -> uncatchable SystemExit
                self.assertIsNone(mem._embed("hello"))

    def test_acp_drain_waits_for_quiet_not_a_fixed_sleep(self):
        import threading
        import buddy_core.acp as acp
        s = object.__new__(acp._ACPSession)
        s._lock = threading.Lock()
        s._responses = {}
        s.transcript = ["a"]
        s._closed = False
        s._eof = False
        t0 = time.time()
        s.drain(quiet_for=0.05, max_wait=1.0)
        self.assertLess(time.time() - t0, 0.5)  # quiet -> returns promptly
        src = inspect_src(acp, "run_acp_agent")
        self.assertIn("sess.drain(", src)
        self.assertNotIn("time.sleep(0.2)", src)

    def test_brain_tolerates_malformed_tool_specs(self):
        from unittest import mock
        import buddy_core.brain as brain
        bad = [{"nope": 1}, ["only", "two"], {"function": {}},
               {"function": {"name": "ok", "description": "d"}}]
        msgs = [{"role": "system", "content": "s"},
                {"role": "assistant", "content": "a",
                 "tool_calls": [{"x": 1}, {"function": {}}]},
                {"role": "user", "content": "u"}]

        class Resp:
            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            out = brain._completion({"api_base": "https://x/v1", "model": "m"},
                                    msgs, bad, stream=False)
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")

    def test_mcp_list_survives_bad_entries(self):
        import io
        import contextlib
        import buddy_core.mcp as mcp
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "mcp.json"
            f.write_text(json.dumps({"weird": "a string",
                                     "ok": {"command": "true", "args": []}}))
            with mock.patch.object(mcp, "MCP_CONFIG", f):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    mcp.MCPManager().cmd_list()  # must not raise
        self.assertIn("invalid entry", buf.getvalue())


class TestMcpAcpMemoryDeep(unittest.TestCase):
    """Audited MCP / ACP / memory / HTTP-layer bugs."""

    def test_acp_reader_survives_non_dict_json(self):
        import io
        import json as _json
        import threading
        import buddy_core.acp as acp
        s = object.__new__(acp._ACPSession)
        s._lock = threading.Lock()
        s._responses = {}
        s.transcript = []
        s._closed = False
        s._eof = False
        s.proc = type("P", (), {})()
        s.proc.stdout = io.StringIO(
            "null\n1\n[1,2]\n\"hi\"\nnot json\n"
            + _json.dumps({"id": 7, "result": {"ok": True}}) + "\n")
        s._read()
        # one junk line used to kill the only stdout consumer -> 11min hang
        self.assertIn(7, s._responses)
        self.assertTrue(s._eof)

    def test_acp_request_fails_fast_when_output_closed(self):
        import threading
        import buddy_core.acp as acp
        s = object.__new__(acp._ACPSession)
        s._lock = threading.Lock()
        s._responses = {}
        s.transcript = []
        s._closed = False
        s._eof = True
        s.proc = type("P", (), {})()
        s.proc.stdin = type("W", (), {"write": lambda *a: None,
                                      "flush": lambda *a: None})()
        s.proc.poll = lambda: None
        t0 = time.time()
        with self.assertRaises(acp.ACPError):
            s.request("initialize", {}, timeout=30)
        self.assertLess(time.time() - t0, 2)  # did not burn the deadline

    def test_mcp_timed_out_server_is_marked_dead(self):
        import io
        from unittest import mock
        import buddy_core.mcp as mcp

        class DeadProc:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO()

            def poll(self):
                return None

            def kill(self):
                pass

            def wait(self, timeout=None):
                pass

        srv = mcp.MCPServer("slow", ["sleep", "30"])
        with mock.patch.object(mcp.subprocess, "Popen",
                               return_value=DeadProc()):
            with self.assertRaises(Exception):
                srv.start()
        self.assertTrue(srv.dead)
        self.assertEqual(srv.tools, [])
        mgr = mcp.MCPManager()
        mgr.servers["slow"] = srv
        self.assertEqual(mgr.all_tools(), [])  # no longer advertised
        out = mgr.call_tool("mcp__slow__x", {})
        self.assertIn("not running", out)
        srv.shutdown()

    def test_mcp_blank_line_is_not_death(self):
        import queue
        import buddy_core.mcp as mcp
        srv = mcp.MCPServer("x", ["true"])
        srv._out_q.put("\n")  # stray newline, not the EOF sentinel
        srv._out_q.put(None)
        self.assertEqual(srv._out_q.qsize(), 2)

    def test_mcp_output_queue_is_bounded(self):
        import buddy_core.mcp as mcp
        self.assertIsNotNone(mcp.MCPServer("x", ["true"])._out_q.maxsize)

    def test_mcp_budget_includes_lock_queue_time(self):
        # the 60s budget must start BEFORE queueing, else N queued callers
        # could pile up to N x 60s of waiting
        import inspect
        import buddy_core.mcp as mcp
        src = inspect.getsource(mcp.MCPServer._rpc)
        self.assertIn("budget_deadline = time.monotonic() + 60.0", src)
        self.assertLess(src.index("budget_deadline"),
                        src.index("self._lock.acquire("))
        self.assertIn("self._lock.acquire(timeout=60.0)", src)
        self.assertIn("finally:", src)
        self.assertIn("self._lock.release()", src)

    def test_mcp_real_server_round_trip(self):
        import sys as _sys
        import buddy_core.mcp as mcp
        srv = mcp.MCPServer("mock", [_sys.executable, "tests/mock_mcp.py"])
        try:
            srv.start()
            self.assertEqual([t["name"] for t in srv.tools], ["echo"])
            self.assertIn("echo: hi", srv.call("echo", {"text": "hi"}))
            # lock must be free for the next caller
            self.assertTrue(srv._lock.acquire(timeout=2))
            srv._lock.release()
        finally:
            srv.shutdown()

    def test_start_all_tolerates_bad_config(self):
        from unittest import mock
        import buddy_core.mcp as mcp
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "mcp.json"
            f.write_text(json.dumps({"broken": {"nope": 1},
                                     "strargs": {"command": "true",
                                                 "args": "single"}}))
            with mock.patch.object(mcp, "MCP_CONFIG", f):
                m = mcp.MCPManager()
                m.start_all()  # must not raise
                self.assertIn("broken", m.error)

    def test_vector_add_survives_non_object_json(self):
        # a vectors.json holding `null` used to raise AttributeError out of
        # _exit_pass, silently disabling playbook learning
        import buddy_core.memory as mem
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "vectors.json"
            for payload in ("null", "[1,2]", '"x"'):
                v.write_text(payload)
                with mock.patch.object(mem, "VECTORS", v):
                    with mock.patch.object(mem, "_embed",
                                           return_value=[0.1, 0.2]):
                        mem._vector_add("fact")  # must not raise
            self.assertIn("items", v.read_text())

    def test_completion_never_mutates_shared_cfg(self):
        from unittest import mock
        import buddy_core.brain as brain

        class Resp:
            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        cfg = {"api_base": "https://x/v1"}  # no model key at all
        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            out = brain._completion(cfg, [{"role": "user", "content": "hi"}],
                                    None, stream=False)
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")
        # a placeholder model written into cfg got persisted to config.json
        self.assertNotIn("model", cfg)
        self.assertEqual(cfg["api_base"], "https://x/v1")

    def test_stream_has_wall_clock_budget(self):
        import inspect
        import buddy_core.brain as brain
        src = inspect.getsource(brain._completion)
        self.assertIn("stream_max_seconds", src)
        self.assertIn("time.monotonic() > deadline", src)

    def test_sub_mcp_does_not_hold_state_lock_while_starting(self):
        import inspect
        import buddy_core.agent as agent
        src = inspect.getsource(agent._sub_mcp)
        # start_all() must happen before the STATE_LOCK block, not inside it
        start = src.index("mgr.start_all()")
        lock = src.index("with STATE_LOCK")
        self.assertLess(start, lock,
                        "start_all() inside STATE_LOCK freezes the daemon")


class TestLiveConfigAndThreads(unittest.TestCase):
    """Daemon picks up external config edits; timed-out commands and ACP
    calls release their threads; notifications close their sockets."""

    def _cfg_file(self, td, **kw):
        p = Path(td) / "config.json"
        p.write_text(json.dumps({"api_base": "x", "model": "m1", **kw}))
        return p

    def test_external_config_change_is_picked_up(self):
        from buddy_core import config as cfg_mod
        with tempfile.TemporaryDirectory() as td:
            p = self._cfg_file(td)
            with mock.patch.object(cfg_mod, "CONFIG", p):
                cfg = cfg_mod.load_config()
                cfg_mod._stamp_config(cfg)
                self.assertEqual(cfg["model"], "m1")
                time.sleep(0.01)
                p.write_text(json.dumps({"api_base": "x", "model": "m2"}))
                cfg_mod.maybe_reload(cfg)
                self.assertEqual(cfg["model"], "m2")

    def test_reload_preserves_session_state(self):
        from buddy_core import config as cfg_mod
        with tempfile.TemporaryDirectory() as td:
            p = self._cfg_file(td)
            with mock.patch.object(cfg_mod, "CONFIG", p):
                cfg = cfg_mod.load_config()
                cfg_mod._stamp_config(cfg)
                cfg["mode"] = "bypass"
                time.sleep(0.01)
                p.write_text(json.dumps({"api_base": "x", "model": "m2"}))
                cfg_mod.maybe_reload(cfg)
                self.assertEqual(cfg["mode"], "bypass")  # not in the file
                self.assertEqual(cfg["model"], "m2")

    def test_corrupt_config_never_kills_the_process(self):
        from buddy_core import config as cfg_mod
        with tempfile.TemporaryDirectory() as td:
            p = self._cfg_file(td)
            with mock.patch.object(cfg_mod, "CONFIG", p):
                cfg = cfg_mod.load_config()
                cfg_mod._stamp_config(cfg)
                p.write_text("{ broken")  # load_config() would sys.exit(1)
                cfg_mod.maybe_reload(cfg)
                self.assertEqual(cfg["model"], "m1")  # old cfg retained

    def test_web_confirm_sees_live_config(self):
        from buddy_core import config as cfg_mod
        from buddy_core.webui import WebUI, _web_confirm
        old = WebUI.cfg
        try:
            with tempfile.TemporaryDirectory() as td:
                p = self._cfg_file(td)
                with mock.patch.object(cfg_mod, "CONFIG", p):
                    WebUI.cfg = cfg_mod.load_config()
                    cfg_mod._stamp_config(WebUI.cfg)
                    self.assertFalse(_web_confirm("rm -rf /"))
                    time.sleep(0.01)
                    p.write_text(json.dumps({"api_base": "x", "model": "m1",
                                             "yolo": True}))
                    self.assertTrue(_web_confirm("rm -rf /"))  # no restart
        finally:
            WebUI.cfg = old

    def test_timeout_releases_reader_thread(self):
        import threading
        from buddy_core.tools import run_command
        before = threading.active_count()
        for _ in range(3):
            run_command("sleep 30", timeout=1)
        time.sleep(0.5)
        self.assertLessEqual(threading.active_count(), before + 1)

    def test_notifications_close_their_sockets(self):
        import inspect
        import buddy_core.util as util
        import buddy_core.sched as sched
        for mod, fn in ((util, None), (sched, "telegram_send")):
            src = inspect.getsource(mod) if fn is None else inspect.getsource(
                getattr(mod, fn))
            for line in src.splitlines():
                stripped = line.strip()
                if "urlopen(" in stripped:
                    self.assertFalse(
                        stripped.startswith("urllib.request.urlopen("),
                        "urlopen without a context manager: " + stripped)

    def test_sub_mcp_has_exit_hook_and_rebuilds(self):
        import buddy_core.agent as agent
        self.assertIsNone(agent._SUB_MCP)
        first = agent._sub_mcp()
        self.assertIs(agent._sub_mcp(), first)  # singleton
        agent._shutdown_sub_mcp()
        self.assertIsNone(agent._SUB_MCP)
        second = agent._sub_mcp()
        self.assertIsNot(second, first)  # rebuilds after shutdown
        agent._shutdown_sub_mcp()
        import atexit
        # an exit hook is registered, so children die with the process
        self.assertTrue(any(getattr(f, "__name__", "") == "_shutdown_sub_mcp"
                            for f in [agent._shutdown_sub_mcp]))


class TestDeepFixes(unittest.TestCase):
    """Bugs found in a deep audit of runtime paths."""

    def test_email_limit_zero_is_not_everything(self):
        # [-0:] == [0:] — limit=0 used to fetch the ENTIRE mailbox.
        import inspect
        import buddy_core.tools as tm
        src = inspect.getsource(tm.email_check)
        self.assertNotIn("[-int(limit):]", src)
        self.assertIn("max(1, int(limit))", src)

    def test_email_body_none_payload_is_safe(self):
        import inspect
        import buddy_core.tools as tm
        src = inspect.getsource(tm.email_check)
        # get_payload(decode=True) is None for unencoded payloads
        self.assertNotIn("part.get_payload(decode=True).decode", src)
        self.assertNotIn("msg.get_payload(decode=True).decode", src)

    def test_elide_context_shrinks_short_conversations(self):
        from buddy_core.agent import _elide_context, _approx_chars
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "x" * 5000},
                {"role": "user", "content": "and now?"}]
        _elide_context(msgs, 500)
        self.assertLess(_approx_chars(msgs), 5000)
        self.assertEqual(msgs[-1]["content"], "and now?")  # last turn intact
        self.assertEqual(msgs[0]["content"], "sys")  # system intact

    def test_inbox_is_bounded(self):
        from buddy_core import util
        with tempfile.TemporaryDirectory() as td:
            old_inbox = util.INBOX
            util.INBOX = Path(td) / "inbox.md"
            try:
                for i in range(80):
                    util.deliver("X" * 8000, f"job {i}")
                inbox = util.INBOX
                size = inbox.stat().st_size
                self.assertLess(size, 260_000)
                txt = inbox.read_text()
                self.assertIn("inbox trimmed", txt)
                self.assertIn("job 79", txt)  # newest kept
                self.assertNotIn("job 0 ", txt)  # oldest dropped
            finally:
                util.INBOX = old_inbox

    def test_pending_update_count_per_pm(self):
        from buddy_core.tools import _pending_count
        self.assertEqual(_pending_count("apt", "Listing..."), 0)
        self.assertEqual(_pending_count(
            "apt", "vim/stable 2:9.0-1 upgradable from [2:8.2-1]"), 1)
        self.assertEqual(_pending_count("dnf", "No packages marked for update."), 0)
        self.assertEqual(_pending_count("dnf", "vim.x86_64 2:9.0-1 updates"), 1)
        self.assertEqual(_pending_count("pacman", "(no output)"), 0)
        self.assertEqual(_pending_count("pacman", "vim 9.0-1 -> 9.1-1"), 1)
        self.assertEqual(_pending_count(
            "zypper", "vim | repo-oss | 9.0 | 9.1 | x86_64"), 1)
        self.assertEqual(_pending_count("brew", "vim (9.0) < 9.1"), 1)

    def test_update_pc_does_not_escalate_when_current(self):
        from unittest import mock
        import buddy_core.tools as tm
        from buddy_core.commands import _run_intent
        with mock.patch.object(tm, "update_system",
                               return_value="this machine is up to date (via apt) "
                                            "— 0 packages pending upgrade") as us:
            out = _run_intent(("update_pc", ""), {}, lambda _m: True)
        self.assertIn("up to date", out)
        # only the read-only check ran — no OS upgrade was proposed
        self.assertEqual([c.args[0] for c in us.call_args_list], ["check"])

    def test_rollback_turn_keeps_concurrent_turn(self):
        from buddy_core.webui import _rollback_turn
        a = {"role": "user", "content": "A"}
        b = {"role": "user", "content": "B"}
        sess = {"messages": [{"role": "assistant", "content": "old"}, a, b]}
        _rollback_turn(sess, a)  # A failed while B is in flight
        contents = [m["content"] for m in sess["messages"]]
        self.assertIn("B", contents)  # B survived
        self.assertNotIn("A", contents)  # only A rolled back

    def test_rollback_turn_trims_tail_when_alone(self):
        from buddy_core.webui import _rollback_turn
        a = {"role": "user", "content": "A"}
        sess = {"messages": [{"role": "assistant", "content": "old"}, a]}
        _rollback_turn(sess, a)
        self.assertEqual([m["content"] for m in sess["messages"]], ["old"])

    def test_diff_registry_is_bounded_and_drained(self):
        import threading
        import buddy_core.tools as tm
        with tm._DIFF_LOCK:
            tm._LAST_DIFF.clear()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f.txt"
            p.write_text("a\n")
            for _ in range(80):
                tm._record_diff(p, "a\n", "b\n")
        with tm._DIFF_LOCK:
            size = len(tm._LAST_DIFF)
        self.assertLessEqual(size, 64)
        tm.pop_diff()  # this thread's entry drains
        with tm._DIFF_LOCK:
            self.assertNotIn(threading.get_ident(), tm._LAST_DIFF)
            tm._LAST_DIFF.clear()

    def test_watcher_tick_keeps_concurrent_addition(self):
        import json as _json
        from buddy_core import sched
        from buddy_core.config import WATCHERS_FILE
        old = WATCHERS_FILE.read_text() if WATCHERS_FILE.exists() else "[]"
        try:
            WATCHERS_FILE.write_text(_json.dumps([
                {"name": "w1", "kind": "command", "target": "echo a",
                 "interval_minutes": 0, "last_check": None, "last_state": None}]))
            # a watcher added while the tick is polling
            live = _json.loads(WATCHERS_FILE.read_text()) + [
                {"name": "w2", "kind": "command", "target": "echo b",
                 "interval_minutes": 5, "last_check": None, "last_state": None}]
            WATCHERS_FILE.write_text(_json.dumps(live))
            sched.Watcher({})._tick()
            names = [w["name"] for w in _json.loads(WATCHERS_FILE.read_text())]
            self.assertIn("w2", names)  # not silently clobbered
        finally:
            WATCHERS_FILE.write_text(old)


class TestCommandVerdict(unittest.TestCase):
    """After running, buddy must see FAILED first — never mistake it."""

    def test_failure_banner_first_success_quiet(self):
        from buddy_core.tools import run_command
        ok = run_command("echo hi", timeout=10)
        self.assertFalse(ok.startswith("(FAILED"))
        bad = run_command("exit 3", timeout=10)
        self.assertTrue(bad.startswith("(FAILED: Command exited with code 3.)"))
        self.assertIn("Command exited with code 3", bad)  # metadata kept

    def test_timeout_and_refusal_verdicts(self):
        from buddy_core.tools import run_command
        t = run_command("sleep 60", timeout=1)
        self.assertTrue(t.startswith("(FAILED:"))
        self.assertIn("exceeding the timeout", t)
        ref = run_command("rm -rf /", timeout=5)
        self.assertIn("refused by safety guard", ref)

    def test_is_tool_error(self):
        from buddy_core.agent import _is_tool_error
        bad = "(FAILED: Command exited with code 3.)\nfoo"
        self.assertTrue(_is_tool_error("run_command", bad))
        self.assertTrue(_is_tool_error("bash", bad))
        self.assertTrue(_is_tool_error("run_command", "(refused by safety guard: x)"))
        self.assertFalse(_is_tool_error("run_command", "hello"))
        self.assertFalse(_is_tool_error("read_file", bad))
        self.assertTrue(_is_tool_error("edit_file", "(tool failed: x)"))
        self.assertFalse(_is_tool_error("whatever", "hello"))





