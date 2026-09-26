"""Test hermeticity — MUST be imported before anything from buddy_core.

The suite used to run against the developer's real ~/.buddy: every run created
config.json, system.json, logs/ and outputs/ there, and one test even flipped
the user's configured provider to deepseek (commit 8716756 patched CONFIG to a
tempdir for that single test, which left the suite as a whole unhermetic).

It also sprayed real desktop notifications. deliver() shells out to
notify-send unconditionally, and test_inbox_is_bounded calls deliver() 80 times
in a loop, so each run put 80 "job N" popups full of X's on the user's screen.

Both are fixed at the source (BUDDY_HOME in config.py,
BUDDY_NO_AMBIENT_NOTIFY in util.py). This module sets them for the suite so the
fixes are exercised rather than merely present, and so the guarantees hold even
if someone runs a test module directly with `python3 tests/test_foo.py`.
"""

import os
import tempfile

# Unconditional, NOT setdefault: if the developer already has BUDDY_HOME (or has
# switched the notify gate off) exported, setdefault would adopt those values and
# the suite would write into their real ~/.buddy again -- the exact bug this
# module exists to prevent. The suite must win.
os.environ["BUDDY_NO_AMBIENT_NOTIFY"] = "1"
os.environ["BUDDY_HOME"] = tempfile.mkdtemp(prefix="buddy-test-")
