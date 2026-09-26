"""Regression tests for the security/robustness pass of 2026-09-26.

Each test here pins a specific defect that was confirmed live before the fix.
They are deliberately adversarial: they attempt the exploit, not just the
happy path. tests/test_security.py passing is not evidence these are covered —
it passed while all of the below were exploitable.
"""

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # find _hermetic
import _hermetic  # noqa: E402,F401  (MUST precede buddy_core: isolates BUDDY_HOME, silences desktop notify)
del _os, _sys

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@contextlib.contextmanager
def _env(**kw):
    """Set env vars for the block, restoring the exact prior state (including
    absence) afterwards. Plain os.environ.pop() in one test used to delete the
    suite's own BUDDY_NO_AMBIENT_NOTIFY and break unrelated tests."""
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update(kw)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestUnattendedSlashAllowlist(unittest.TestCase):
    """buddy_core/commands.py: the model-reachable slash path was a DENYLIST,
    so /acp (spawns an arbitrary command) and /upgrade (rewrites all of buddy's
    source from GitHub) ran with confirm=None and no confirmation. Two calls
    gave full RCE from one prompt injection."""

    def setUp(self):
        from buddy_core.commands import run_slash_command
        from buddy_core.config import load_config_safe
        self.run = run_slash_command
        self.cfg = load_config_safe()

    def test_acp_add_is_refused_unattended(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "PWNED"
            out = self.run(self.cfg, f"/acp add pwn -- /usr/bin/touch {marker}")
            self.assertIn("needs a human", out)
            self.assertFalse(marker.exists(), "arbitrary command executed")

    def test_acp_run_is_refused_unattended(self):
        out = self.run(self.cfg, "/acp someagent do-something")
        self.assertIn("needs a human", out)

    def test_upgrade_is_refused_unattended(self):
        for cmd in ("/upgrade", "/update"):
            self.assertIn("needs a human", self.run(self.cfg, cmd))

    def test_theme_mutation_is_refused_unattended(self):
        before = json.dumps(self.cfg.get("theme"))
        self.assertIn("needs a human", self.run(self.cfg, "/theme dark"))
        self.assertEqual(json.dumps(self.cfg.get("theme")), before)

    def test_acp_add_does_not_persist_to_config(self):
        from buddy_core.config import CONFIG, _save_config
        _save_config({})
        self.run(self.cfg, "/acp add pwn -- /bin/sh")
        saved = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        self.assertNotIn("pwn", (saved.get("acp_agents") or {}))

    def test_acp_tool_cannot_register_agents(self):
        """The `acp` tool has no command/args channel, so agent="add" must not
        register anything — it falls through to "unknown agent"."""
        from buddy_core.tools import tool_impl
        from buddy_core.config import CONFIG
        with tempfile.TemporaryDirectory() as td:
            out = tool_impl("acp", {"agent": "add", "prompt": "pwn"}, None)
            self.assertIn("unknown ACP agent", out)
            saved = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
            self.assertEqual(saved.get("acp_agents") or {}, {})

    def test_read_only_commands_still_work_unattended(self):
        for cmd, needle in (("/status", "brain="), ("/help", "commands:"),
                            ("/tools", "built-in tools:"), ("/acp", "ACP agents")):
            self.assertIn(needle, self.run(self.cfg, cmd), f"{cmd} regressed")

    def test_allowlist_is_an_allowlist(self):
        """A command nobody classified must be refused, not merely unknown."""
        from buddy_core.commands import _UNATTENDED_SAFE, _UNATTENDED_GATED
        self.assertNotIn("/acp", _UNATTENDED_SAFE)
        self.assertNotIn("/upgrade", _UNATTENDED_SAFE)
        self.assertIn("/status", _UNATTENDED_SAFE)
        # any dispatcher branch not in either set is unreachable from the model
        self.assertIn("/model", _UNATTENDED_GATED)


class TestCommandWatcherGate(unittest.TestCase):
    """sched.py add_watcher had no confirm parameter at all: a `command`
    watcher is a shell=True primitive the daemon re-runs forever, so the model
    could escalate "ask once" to "run unattended indefinitely"."""

    def test_command_watcher_refused_unattended(self):
        from buddy_core import sched
        sched.WATCHERS_FILE = Path(tempfile.mkdtemp()) / "watchers.json"
        out = sched.add_watcher("t", "command", "echo hi", 5)
        self.assertIn("needs a human", out)
        self.assertEqual(sched.load_watchers(), [])

    def test_command_watcher_allowed_with_confirm(self):
        from buddy_core import sched
        sched.WATCHERS_FILE = Path(tempfile.mkdtemp()) / "watchers.json"
        out = sched.add_watcher("t", "command", "echo hi", 5, confirm=lambda _a: True)
        self.assertIn("watcher 't' set", out)
        self.assertEqual(len(sched.load_watchers()), 1)

    def test_command_watcher_cancelled_by_user(self):
        from buddy_core import sched
        sched.WATCHERS_FILE = Path(tempfile.mkdtemp()) / "watchers.json"
        out = sched.add_watcher("t", "command", "echo hi", 5, confirm=lambda _a: False)
        self.assertIn("cancelled", out)
        self.assertEqual(sched.load_watchers(), [])

    def test_url_and_file_watchers_unaffected(self):
        from buddy_core import sched
        sched.WATCHERS_FILE = Path(tempfile.mkdtemp()) / "watchers.json"
        self.assertIn("watcher 'u' set",
                      sched.add_watcher("u", "url", "https://example.com/", 5))
        self.assertIn("watcher 'f' set",
                      sched.add_watcher("f", "file", "/tmp/x", 5))


class TestSensitivePathDenylist(unittest.TestCase):
    """tools.py _sensitive_path_reason only matched SSH/PGP material, so the
    commonest places a plaintext token lives were readable with confirm=None —
    straight into LLM context and the persisted session JSON."""

    GATED = [
        "~/.git-credentials", "~/.netrc", "~/_netrc", "~/.npmrc", "~/.pypirc",
        "~/.htpasswd", "~/.aws/credentials", "~/.aws/config",
        "~/.docker/config.json", "~/.config/gh/hosts.yml", "~/.gem/credentials",
        "~/.config/gcloud/credentials.db", "~/.config/gcloud/access_tokens.db",
        "~/.kube/config", "~/.azure/credentials.json", "~/.password-store/x.gpg",
        "~/.ssh/id_ed25519", "~/.gnupg/secring.gpg",
    ]
    ALLOWED = [
        "~/buddy/buddy_core/tools.py", "~/notes-about-netrc.txt",
        "~/Documents/report.md", "~/projects/app/keyboard-layouts.json",
    ]

    def test_credential_files_are_gated(self):
        from buddy_core.tools import _sensitive_path_reason
        for p in self.GATED:
            with self.subTest(p=p):
                self.assertIsNotNone(
                    _sensitive_path_reason(Path(p).expanduser()),
                    f"{p} must require confirmation")

    def test_ordinary_files_not_gated(self):
        from buddy_core.tools import _sensitive_path_reason
        for p in self.ALLOWED:
            with self.subTest(p=p):
                self.assertIsNone(
                    _sensitive_path_reason(Path(p).expanduser()),
                    f"{p} must NOT be gated (false positive)")

    def test_read_file_of_credentials_requires_confirm(self):
        from buddy_core.tools import read_file
        out = read_file("~/.netrc", None)
        low = out.lower()
        self.assertTrue("cancelled" in low or "refused" in low or "confirm" in low, out)
        self.assertNotIn("password", out.replace("sensitive path", ""))


class TestSsrfGuard(unittest.TestCase):
    """_norm_url returned early for anything already http(s)://, so its
    loopback/LAN logic never applied to a full URL: the model could fetch
    169.254.169.254 or any RFC1918 host and read the body back."""

    BLOCKED = [
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://192.168.1.1/", "http://10.0.0.5/", "http://172.16.0.1/",
        "http://[::1]/", "http://localhost:9999/",
    ]

    def test_web_fetch_refuses_non_public(self):
        from buddy_core.tools import web_fetch
        for u in self.BLOCKED:
            with self.subTest(u=u):
                out = web_fetch(u)
                # refused, and no fetched body came back (the URL is echoed in
                # the refusal, so assert on the verdict, not on substrings)
                self.assertTrue(out.startswith("(refused:"), out)
                self.assertIn("non-public host", out)
                self.assertNotIn("AccessKeyId", out)

    def test_net_open_enforces_policy(self):
        from buddy_core.tools import _net_open
        with self.assertRaises(ValueError):
            _net_open("http://127.0.0.1:1/")

    def test_private_host_reason_classifies(self):
        from buddy_core.tools import _private_host_reason
        self.assertIsNotNone(_private_host_reason("127.0.0.1"))
        self.assertIsNotNone(_private_host_reason("localhost"))
        self.assertIsNotNone(_private_host_reason("169.254.169.254"))
        self.assertIsNone(_private_host_reason("example.com"))

    def test_norm_url_contract_preserved(self):
        """_norm_url must stay a normaliser: existing callers and tests rely on
        bare-host loopback normalisation. The policy lives in _net_open."""
        from buddy_core.tools import _norm_url
        self.assertEqual(_norm_url("localhost:7616"), "http://localhost:7616")
        self.assertEqual(_norm_url("127.0.0.1:7616"), "http://127.0.0.1:7616")
        self.assertEqual(_norm_url("example.com"), "https://example.com")


class TestAutonomousCageBranchCheck(unittest.TestCase):
    """skills.py ignored `git checkout -b buddy/autonomous`'s return code. From
    cycle 2 the branch exists, checkout fails, HEAD stays on the user's branch
    and the commit lands unattended edits straight into their working tree."""

    def _cage(self, cycles):
        """Drive _cage_autonomous_edits over N cycles in a throwaway repo."""
        from buddy_core import skills
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            def g(*a):
                subprocess.run(["git", "-C", str(repo), *a], capture_output=True)
            g("init", "-q", "-b", "main")
            g("config", "user.email", "t@t"); g("config", "user.name", "t")
            (repo / "a.txt").write_text("base\n"); g("add", "-A"); g("commit", "-qm", "base")
            from buddy_core import config as cfgmod
            orig = cfgmod.BUDDY_SRC      # imported locally inside the function
            cfgmod.BUDDY_SRC = repo
            try:
                for n in range(cycles):
                    (repo / "a.txt").write_text(f"edit-{n}\n")
                    skills._cage_autonomous_edits(f"cycle {n}")
            finally:
                cfgmod.BUDDY_SRC = orig
            head = subprocess.run(["git", "-C", str(repo), "rev-parse",
                                   "--abbrev-ref", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                                   capture_output=True, text=True).stdout.strip()
            return head, dirty

    def test_edits_never_land_on_the_working_branch(self):
        head, dirty = self._cage(3)
        self.assertEqual(head, "main", "cage left HEAD off the user's branch")
        self.assertEqual(dirty, "", "cage left unattended edits in the working tree")


class TestLoadConfigNeverKillsDaemonThreads(unittest.TestCase):
    """load_config() calls sys.exit(1) on a corrupt config. SystemExit is not an
    Exception, so the scheduler/watcher/telegram loops' `except Exception` let
    it through: the thread died with no log line and no supervisor."""

    def test_load_config_safe_returns_dict_on_corrupt(self):
        from buddy_core import config as cfgmod
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "config.json"
            bad.write_text("{not json")
            orig = cfgmod.CONFIG
            cfgmod.CONFIG = bad
            try:
                self.assertEqual(cfgmod.load_config_safe(), {})
            finally:
                cfgmod.CONFIG = orig

    def test_daemon_loops_catch_baseexception(self):
        """The loops must not use `except Exception` around tick bodies, or a
        stray SystemExit is fatal to the thread."""
        import inspect
        from buddy_core import sched
        for cls in (sched.Scheduler, sched.Watcher):
            src = inspect.getsource(cls._loop)
            self.assertIn("except BaseException:", src,
                          f"{cls.__name__}._loop must catch BaseException, or a "
                          "SystemExit from load_config() is fatal to the thread")

    def test_scheduler_loop_survives_systemexit(self):
        """A SystemExit raised inside a tick must not end the loop."""
        from unittest import mock
        from buddy_core import sched

        class _StopAfter:
            def __init__(self, n): self.n = n
            def wait(self, _t=None):
                if self.n <= 0: return True
                self.n -= 1; return False
            def is_set(self): return self.n <= 0
            def set(self): self.n = 0

        s = sched.Scheduler.__new__(sched.Scheduler)
        s.cfg = {}; s.mcp = None; s.confirm = None; s._last_compact = None
        s.stop = _StopAfter(3)
        calls = []
        def boom():
            calls.append(1)
            raise SystemExit(1)
        with mock.patch.object(sched, "load_jobs", side_effect=boom):
            s._loop()   # must complete all 3 iterations
        self.assertEqual(len(calls), 3, "scheduler loop aborted on SystemExit")

    def test_watcher_loop_survives_systemexit(self):
        from buddy_core import sched

        class _StopAfter:
            def __init__(self, n): self.n = n
            def wait(self, _t=None):
                if self.n <= 0: return True
                self.n -= 1; return False
            def is_set(self): return self.n <= 0
            def set(self): self.n = 0

        w = sched.Watcher.__new__(sched.Watcher)
        w.cfg = {}; w.stop = _StopAfter(3)
        calls = []
        def boom():
            calls.append(1)
            raise SystemExit(1)
        w._tick = boom
        w._loop()
        self.assertEqual(len(calls), 3, "watcher loop aborted on SystemExit")


class TestAmbientNotifyGate(unittest.TestCase):
    """deliver() shelled out to notify-send unconditionally. test_inbox_is_bounded
    calls it 80 times, so every test run put 80 'job N' desktop popups full of
    X's on the developer's screen."""

    def test_gate_suppresses_notify_send(self):
        from buddy_core import util
        with _env(BUDDY_NO_AMBIENT_NOTIFY="1"):
            self.assertFalse(util._ambient_notify_enabled())
        # the suite itself runs with the gate on, so prove the enabled path by
        # clearing it explicitly rather than assuming it is unset
        with _env(BUDDY_NO_AMBIENT_NOTIFY=""):
            self.assertTrue(util._ambient_notify_enabled())

    def test_deliver_still_writes_inbox_when_gated(self):
        from buddy_core import util
        with tempfile.TemporaryDirectory() as td:
            old = util.INBOX
            util.INBOX = Path(td) / "inbox.md"
            os.environ["BUDDY_NO_AMBIENT_NOTIFY"] = "1"
            try:
                with _env(BUDDY_NO_AMBIENT_NOTIFY="1"):
                    util.deliver("hello", "t")
                written = util.INBOX.read_text()
            finally:
                util.INBOX = old
            self.assertIn("hello", written)

    def test_no_notify_send_when_gated(self):
        """End-to-end: with the gate on, deliver() must not spawn notify-send."""
        from unittest import mock
        from buddy_core import util
        with tempfile.TemporaryDirectory() as td:
            old = util.INBOX
            util.INBOX = Path(td) / "inbox.md"
            try:
                with _env(BUDDY_NO_AMBIENT_NOTIFY="1"), \
                     mock.patch.object(util.subprocess, "run") as m:
                    util.deliver("X" * 8000, "job 0")
                    self.assertFalse(
                        any("notify-send" in " ".join(map(str, c.args[:1]))
                            for c in m.call_args_list),
                        "notify-send ran despite the gate")
            finally:
                util.INBOX = old

    def test_trim_note_does_not_accumulate(self):
        """_trim_inbox appended its note outside every '## ' header, so the next
        trim absorbed it into the last entry and re-emitted it with a header —
        one copy per trim, forever."""
        from buddy_core import util
        with tempfile.TemporaryDirectory() as td:
            old = util.INBOX
            util.INBOX = Path(td) / "inbox.md"
            try:
                for round_ in range(6):
                    with util.INBOX.open("a") as fh:
                        for i in range(40):
                            fh.write(
                                f"## 2026-09-26 10:{i%60:02d} — job {round_}_{i}\n"
                                + ("pad " * 60) + "\n\n")
                    util._trim_inbox(max_bytes=4000)
                notes = util.INBOX.read_text().count("inbox trimmed to the newest")
                self.assertLessEqual(notes, 1, f"trim note duplicated {notes} times")
            finally:
                util.INBOX = old


class TestBuddyHomeOverride(unittest.TestCase):
    """The suite wrote to the real ~/.buddy on every run (config.py hardcoded
    Path.home()/'.buddy'). BUDDY_HOME relocates the whole state dir."""

    def test_home_honours_env(self):
        from buddy_core import config as cfgmod
        with tempfile.TemporaryDirectory() as td:
            import importlib
            try:
                with _env(BUDDY_HOME=td):
                    reloaded = importlib.reload(cfgmod)
                    self.assertEqual(reloaded.HOME, Path(td))
                    self.assertEqual(reloaded.CONFIG.parent, Path(td))
            finally:
                importlib.reload(cfgmod)

    def test_default_is_still_dot_buddy(self):
        import importlib
        from buddy_core import config as cfgmod
        try:
            with _env(BUDDY_HOME=""):
                reloaded = importlib.reload(cfgmod)
                self.assertEqual(reloaded.HOME.name, ".buddy")
        finally:
            importlib.reload(cfgmod)

    def test_suite_env_is_isolated(self):
        self.assertEqual(os.environ.get("BUDDY_NO_AMBIENT_NOTIFY"), "1")
        self.assertTrue(os.environ.get("BUDDY_HOME", "").startswith(
            tempfile.gettempdir()))


class TestRunCommandTimeoutIsEnforced(unittest.TestCase):
    """If the child closed stdout early the reader set `done`, `expired` was
    False, and the bare p.wait() blocked for the child's whole lifetime —
    silently ignoring the caller's timeout."""

    def test_early_stdout_close_still_times_out(self):
        import time
        from buddy_core.tools import run_command
        t0 = time.monotonic()
        run_command('python3 -c "import os,time; os.close(1); time.sleep(600)"',
                    timeout=5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 30, f"run_command blocked {elapsed:.0f}s")


class TestQuotaKeyRobustness(unittest.TestCase):
    """quota.py did key.split("|", 1) unguarded; a hand-edited quota.json
    without the separator raised ValueError out of /quota and `buddy.py quota`."""

    def test_missing_separator_does_not_crash(self):
        from buddy_core import quota
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "quota.json"
            f.write_text(json.dumps({"noseparator": {"ts": 0, "headers": {}}}))
            old = quota.QUOTA_FILE if hasattr(quota, "QUOTA_FILE") else None
            target = getattr(quota, "QUOTA_FILE", None) or getattr(quota, "QUOTA", None)
            self.assertIsNotNone(target, "quota module has no quota file global")
            setattr(quota, target.name if False else "QUOTA_FILE", f)
            try:
                out = quota.report({})
                self.assertIn("noseparator", out)
            finally:
                if old is not None:
                    setattr(quota, "QUOTA_FILE", old)


class TestPendingKeyConfirmFlow(unittest.TestCase):
    """webui popped the pending entry *before* comparing it, so the documented
    're-send the key to confirm' branch could never fire and the second send
    silently fell through as a normal chat turn."""

    def test_resend_same_key_confirms(self):
        from buddy_core import webui
        webui._PENDING_KEYS.clear()
        webui._pending_key_put("s1", "gemini", "AIzaFAKEKEY123")
        self.assertEqual(webui._pending_key_peek("s1"), ("gemini", "AIzaFAKEKEY123"))
        # simulate the re-send path the handler now takes
        pending = webui._pending_key_peek("s1")
        self.assertEqual(pending, ("gemini", "AIzaFAKEKEY123"))
        webui._pending_key_take("s1")
        self.assertIsNone(webui._pending_key_peek("s1"))

    def test_take_is_idempotent_and_locked(self):
        from buddy_core import webui
        webui._PENDING_KEYS.clear()
        self.assertIsNone(webui._pending_key_take("nope"))
        self.assertIsNone(webui._pending_key_peek("nope"))


class TestMemoryIsBounded(unittest.TestCase):
    """remember() appended forever: memory_compact only prunes digest blocks
    older than 30 days, and _memory_context() re-reads the whole file on every
    system-prompt build."""

    def test_fact_lines_are_capped(self):
        from buddy_core import memory
        with tempfile.TemporaryDirectory() as td:
            old = memory.MEMORY
            memory.MEMORY = Path(td) / "memory.md"
            memory.MEMORY.write_text("## Facts\n\n")
            try:
                memory.MAX_FACT_LINES = 50
                for i in range(400):
                    memory.remember(f"fact number {i} " + "x" * 200)
                n = sum(1 for ln in memory.MEMORY.read_text().splitlines()
                        if ln.startswith("- ["))
                self.assertLessEqual(n, 50, f"{n} fact lines survived")
                self.assertIn("## Facts", memory.MEMORY.read_text())
            finally:
                memory.MEMORY = old
                memory.MAX_FACT_LINES = 4000


class TestDeadConfirmParams(unittest.TestCase):
    """confirm was accepted and ignored, so a prompt arranging for a 'confirm
    prompt stays on the main thread' was arranging for nothing."""

    def test_email_send_denies_when_confirm_is_none(self):
        from unittest import mock
        from buddy_core import tools as tm
        with mock.patch.object(tm, "load_config", return_value={
                "smtp_host": "smtp.example.com", "smtp_user": "u"}), \
             mock.patch("smtplib.SMTP") as smtp:
            out = tm.email_send("a@b.c", "s", "b", confirm=None)
            smtp.assert_not_called()
        self.assertIn("needs your confirmation", out)

    def test_email_send_operator_channel_skips_confirm(self):
        from unittest import mock
        from buddy_core import tools as tm
        with mock.patch.object(tm, "load_config", return_value={
                "smtp_host": "smtp.example.com", "smtp_user": "u",
                "smtp_port": 587}), \
             mock.patch.object(tm, "secret_get", return_value="pw"), \
             mock.patch("smtplib.SMTP") as smtp:
            smtp.return_value.__enter__ = lambda s: s
            smtp.return_value.__exit__ = lambda s, *a: False
            out = tm.email_send("ops@x.com", "[buddy] t", "body",
                                confirm=None, operator_channel=True)
        self.assertIn("email sent", out)
        smtp.assert_called_once()

    def test_publish_site_honours_confirm(self):
        from buddy_core import tools as tm
        with tempfile.TemporaryDirectory() as td:
            old = tm.WORKSPACE
            tm.WORKSPACE = Path(td)
            try:
                out = tm.publish_site("x.html", "<p>hi</p>", confirm=lambda _a: False)
                self.assertIn("cancelled", out)
                self.assertFalse((Path(td) / "site" / "x.html").exists())
                out = tm.publish_site("x.html", "<p>hi</p>", confirm=lambda _a: True)
                self.assertIn("published x.html", out)
            finally:
                tm.WORKSPACE = old

    def test_auto_install_honours_callable_confirm(self):
        from unittest import mock
        from buddy_core import commands as cm
        info = {"tools": {"pacman": True}}
        with mock.patch.object(cm, "probe_system", return_value=info), \
             mock.patch.object(cm.subprocess, "run") as sp:
            out = cm.auto_install(["vim"], info, confirm=lambda _a: False)
        self.assertEqual(out, [])
        sp.assert_not_called()


class TestInboxTrimNoteShape(unittest.TestCase):
    def test_note_is_not_absorbed_into_an_entry(self):
        from buddy_core import util
        with tempfile.TemporaryDirectory() as td:
            old = util.INBOX
            util.INBOX = Path(td) / "inbox.md"
            try:
                util.INBOX.write_text(
                    "".join(f"## 2026-09-26 10:{i:02d} — job {i}\n" + "pad " * 80
                            + "\n\n" for i in range(30)))
                util._trim_inbox(max_bytes=2000)
                first = util.INBOX.read_text()
                util._trim_inbox(max_bytes=2000)
                second = util.INBOX.read_text()
                self.assertLessEqual(second.count("inbox trimmed to the newest"), 1)
                self.assertEqual(
                    sum(1 for ln in second.splitlines() if ln.startswith("## ")),
                    sum(1 for ln in first.splitlines() if ln.startswith("## ")))
            finally:
                util.INBOX = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
