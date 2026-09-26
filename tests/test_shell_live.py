"""run_command opencode-parity tests: exit codes, timeout kill, tail truncation
with saved full log, and live progress streaming."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import buddy_core.tools as tools  # noqa: E402
from buddy_core.tools import run_command, set_progress_sink  # noqa: E402


class TestExitCode(unittest.TestCase):
    def test_success_has_no_exit_meta(self):
        out = run_command("true", timeout=10)
        self.assertIn("(no output)", out)
        self.assertNotIn("shell_metadata", out)

    def test_failure_reports_exit_code(self):
        out = run_command("exit 3", timeout=10)
        self.assertIn("Command exited with code 3", out)

    def test_stderr_combined(self):
        out = run_command("echo err-line >&2", timeout=10)
        self.assertIn("err-line", out)


class TestTimeout(unittest.TestCase):
    def test_timeout_message_and_kill(self):
        t0 = time.time()
        out = run_command("sleep 60", timeout=1)
        took = time.time() - t0
        self.assertIn("exceeding the timeout of 1s", out)
        self.assertIn("larger timeout", out)
        # SIGTERM should kill sleep promptly — no 60s hang
        self.assertLess(took, 15)

    def test_process_group_killed(self):
        # children spawned by the shell must die with it, not outlive the timeout
        # unique sleep length per process: parallel suites must not see
        # each other's sleepers through the system-wide pgrep
        secs = 51 + ((os.getpid() + int(time.time())) % 30)
        run_command(f"(sleep {secs} & wait) ; sleep {secs}", timeout=1)
        time.sleep(0.3)
        out = run_command(f"pgrep -f 'slee[p] {secs}' | wc -l", timeout=10)
        self.assertIn("0", out.split("\n")[0].strip())


class TestTruncation(unittest.TestCase):
    def test_tail_kept_and_full_log_saved(self):
        out = run_command("seq 1 3000", timeout=30)
        self.assertIn("output truncated", out)
        self.assertIn("Full output saved to: ", out)
        path = out.split("Full output saved to: ")[1].strip().split("\n")[0]
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            full = f.read()
        self.assertIn("1\n", full.split("\n")[0] + "\n")  # head preserved on disk
        self.assertIn("3000", out)  # tail shown to the model

    def test_short_output_untouched(self):
        out = run_command("echo hello", timeout=10)
        self.assertEqual(out.split("\n\n")[0], "hello")


class TestProgress(unittest.TestCase):
    def test_progress_streams_tails(self):
        got = []
        ev = threading.Event()

        def sink(tail):
            got.append(tail)
            if len(got) >= 2:
                ev.set()

        set_progress_sink(sink)
        try:
            run_command("echo one; sleep 0.6; echo two; sleep 0.6; echo three",
                        timeout=15)
        finally:
            set_progress_sink(None)
        self.assertGreaterEqual(len(got), 1)
        joined = "".join(got)
        self.assertIn("one", joined)

    def test_no_sink_is_fine(self):
        out = run_command("echo quiet", timeout=10)
        self.assertIn("quiet", out)


class TestGuard(unittest.TestCase):
    def test_dangerous_still_refused(self):
        out = run_command("rm -rf /", timeout=5)
        self.assertIn("refused by safety guard", out)


if __name__ == "__main__":
    unittest.main()
