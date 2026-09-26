"""buddy_core.skills — skill store, harvest, playbook, evolve / self-repair / auto-evolve.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .brain import _completion
from .config import HOME, PLAYBOOK, SKILLS, STATE_LOCK
from .mcp import MCPManager
from .util import _log_error, deliver

import os
import re
try:
    import readline  # type: ignore
except ImportError:  # exotic platforms — chat() falls back to plain input()
    readline = None
try:  # command palette: raw-mode tty handling (absent on Windows)
    import select as _select
    import termios as _termios
except ImportError:
    _select = _termios = None
import subprocess
import traceback
import sys
import threading
import time
from datetime import datetime

# ---- original buddy.py lines 884-887 ----------------------------------


# ------------------------------------------------------- self-improvement --

# ---- original buddy.py lines 888-891 ----------------------------------
def read_playbook() -> str:
    if not PLAYBOOK.exists():
        return "(playbook empty — write your first lesson when you learn something)"
    return PLAYBOOK.read_text()

# ---- original buddy.py lines 892-893 ----------------------------------


# ---- original buddy.py lines 894-899 ----------------------------------
def reflect(lesson: str, situation: str = "") -> str:
    with PLAYBOOK.open("a") as f:
        stamp = datetime.now().strftime("%Y-%m-%d")
        ctx = f" (when: {situation})" if situation else ""
        f.write(f"- [{stamp}]{ctx} {lesson}\n")
    return f"Lesson recorded: {lesson}"

# ---- original buddy.py lines 900-901 ----------------------------------


# ---- original buddy.py lines 902-909 ----------------------------------
def edit_playbook(new_playbook: str) -> str:
    text = new_playbook.strip()
    if not text:
        return "(refused: empty playbook)"
    if len(text) > 4000:
        return f"(refused: {len(text)} chars exceeds 4000 — condense it)"
    from .tools import _atomic_write  # tmp + replace: a crash can't truncate it
    _atomic_write(PLAYBOOK, text + "\n")
    return f"Playbook updated ({len(text)} chars)."

# ---- original buddy.py lines 910-911 ----------------------------------


# ---- original buddy.py lines 912-947 ----------------------------------
def self_critique(cfg: dict, transcript: list[dict]) -> None:
    convo = "\n".join(
        f"{m['role']}: {str(m.get('content'))[:400]}"
        for m in transcript
        if m["role"] in ("user", "assistant") and m.get("content")
    )[-6000:]
    if not convo.strip():
        return
    try:
        resp = _completion(
            cfg,
            [
                {
                    "role": "system",
                    "content": "You are reviewing your own performance as an "
                    "assistant to improve yourself. Current playbook:\n"
                    + read_playbook()
                    + "\n\n1) If this session revealed a flaw or a lesson, "
                    "produce a COMPLETE updated playbook (concise bullets, "
                    "under 4000 chars) that incorporates it. Improving existing "
                    "lessons beats adding new ones.\n"
                    "2) Also consider: is your playbook-making process itself "
                    "flawed? Fix that in the playbook too.\n"
                    "If nothing worth changing, output exactly KEEP.",
                },
                {"role": "user", "content": convo},
            ],
            tools=None,
            stream=False,
        )
        text = (resp["choices"][0]["message"].get("content") or "").strip()
        if text and not text.upper().startswith("KEEP") and len(text) <= 4000:
            from .tools import _atomic_write
            _atomic_write(PLAYBOOK, text + "\n")
            print("(playbook updated by self-critique)")
    except Exception:
        pass

# ---- original buddy.py lines 1062-1063 --------------------------------


# ---- original buddy.py lines 1064-1106 --------------------------------
def skill_harvest(cfg: dict, transcript: list[dict]) -> None:
    """Exit pass: did this session produce a reusable procedure worth a skill?"""
    convo = "\n".join(
        f"{m['role']}: {str(m.get('content'))[:300]}"
        for m in transcript
        if m["role"] in ("user", "assistant") and m.get("content")
    )[-6000:]
    if not convo.strip():
        return
    existing = {s["slug"] for s in list_skills()}
    try:
        resp = _completion(
            cfg,
            [
                {
                    "role": "system",
                    "content": "Review this assistant session. If it demonstrated "
                    "a reusable multi-step procedure (installing/fixing/automating "
                    "something on the host), output ONE skill in exactly this "
                    "format:\nSKILL: <short-name>\nDESC: <one line>\nBODY:\n"
                    "<step-by-step instructions for a future assistant on the "
                    "same machine, incl. exact commands and gotchas>\n"
                    "If nothing reusable, output exactly NONE.",
                },
                {"role": "user", "content": convo},
            ],
            tools=None,
            stream=False,
        )
        text = (resp["choices"][0]["message"].get("content") or "").strip()
        if not text.upper().startswith("SKILL:"):
            return
        lines = text.splitlines()
        name = lines[0][6:].strip()[:40]
        desc = next((ln[5:].strip() for ln in lines if ln.startswith("DESC:")), name)
        body_start = next((i for i, ln in enumerate(lines) if ln.strip() == "BODY:"), None)
        body = "\n".join(lines[body_start + 1:]).strip() if body_start is not None else name
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        if slug and slug not in existing:
            save_skill(name, desc, body)
            print(f"(learned new skill: {slug})")
    except Exception:
        pass

# ---- original buddy.py lines 1603-1606 --------------------------------


# ----------------------------------------------------------------- skills --

# ---- original buddy.py lines 1607-1613 --------------------------------
def save_skill(name: str, description: str, instructions: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "skill"
    SKILLS.mkdir(parents=True, exist_ok=True)
    (SKILLS / f"{slug}.md").write_text(
        f"# {name}\n\n{description.strip()}\n\n{instructions.strip()}\n"
    )
    return f"Skill '{slug}' saved. Future sessions will know it."

# ---- original buddy.py lines 1614-1615 --------------------------------


# ---- original buddy.py lines 1616-1625 --------------------------------
def list_skills() -> list[dict]:
    if not SKILLS.exists():
        return []
    out = []
    for p in sorted(SKILLS.glob("*.md")):
        try:
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            continue
        title = lines[0].lstrip("# ").strip() if lines else p.stem
        desc = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
        out.append({"slug": p.stem, "title": title, "desc": desc[:120]})
    return out

# ---- original buddy.py lines 1626-1627 --------------------------------


# ---- original buddy.py lines 1628-1632 --------------------------------
def skills_index() -> str:
    skills = list_skills()
    if not skills:
        return "(no skills learned yet)"
    return "\n".join(f"- {s['slug']}: {s['desc']}" for s in skills)

# ---- original buddy.py lines 1633-1634 --------------------------------


# ---- original buddy.py lines 1635-1642 --------------------------------
def use_skill(slug: str) -> str:
    slug = str(slug).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        return "(invalid skill slug)"
    p = SKILLS / f"{slug}.md"
    if not p.exists():
        return f"(no skill named {slug}; use the index in your prompt)"
    try:
        return p.read_text(errors="replace")
    except OSError as e:
        return f"(couldn't read skill {slug}: {e})"

# ---- original buddy.py lines 2426-2429 --------------------------------


# --- capability garden: log what he wishes he could do -----------------------

# ---- original buddy.py lines 2430-2440 --------------------------------
def _log_wish(note: str) -> None:
    """Record a capability gap (failed tool call, unmet request) so the next
    evolve cycle can grow the missing feature or skill."""
    try:
        wpath = HOME / "wishes.log"
        with wpath.open("a") as f:
            f.write(f"- {datetime.now():%Y-%m-%d %H:%M} {note[:300]}\n")
        if wpath.stat().st_size > 100_000:
            tmp = wpath.with_suffix(".log.tmp")  # atomic, keeps concurrent appends
            tmp.write_text(wpath.read_text()[-100_000:])
            os.replace(tmp, wpath)
    except Exception:
        pass

# ---- original buddy.py lines 2441-2442 --------------------------------


# ---- original buddy.py lines 2443-2467 --------------------------------
def evolve_pass(cfg: dict, mcp: "MCPManager", confirm=None) -> str:
    """One self-improvement cycle: review yourself, make one real improvement."""
    wishes = ""
    try:
        wpath = HOME / "wishes.log"
        if wpath.exists() and wpath.stat().st_size > 0:
            wishes = ("\n\nCapability gaps your human hit (wishes.log — the newest "
                      "entries are the most important):\n" + wpath.read_text()[-3000:])
    except Exception:
        pass
    prompt = (
        "Self-improvement cycle. Review your state: read your playbook (read_file "
        "~/.buddy/playbook.md), list your skills, and recall recent memory." + wishes +
        "\nPick the single most valuable improvement you can make RIGHT NOW and do it, "
        "for example: refine a vague playbook rule with edit_playbook; save or upgrade "
        "a skill with save_skill; improve the web UI by editing the PAGE string in your "
        "own code with code_edit (syntax is checked, backups are automatic); publish or "
        "improve a page with publish_site. If the wishes above show a missing "
        "capability, GROW it: add a brand-new tool to your BASE_TOOLS list and its "
        "implementation in tool_impl via code_edit, or write yourself a new skill with "
        "save_skill. Make exactly one focused improvement, prefer improvements "
        "that reduce token use or failure rate over cosmetic ones, then "
        "summarize what you changed and why in two lines."
    )
    from .agent import run_agent  # lazy: avoids circular import
    return run_agent(cfg, mcp, prompt, quiet=True, confirm=confirm)

# ---- original buddy.py lines 2600-2603 --------------------------------


# --- auto-evolve: scheduled self-improvement ---------------------------------

# ---- original buddy.py lines 2604-2629 --------------------------------
class AutoEvolve:
    """Runs one evolve cycle every `evolve_hours` (config, default 24).
    Disable with config "evolve_hours": 0."""

    def __init__(self, cfg: dict, mcp=None):
        self.cfg = cfg if cfg is not None else {}
        self.mcp = mcp
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            hours = int(float(self.cfg.get("evolve_hours", 24) or 0))
        except (TypeError, ValueError):
            hours = 24
        if hours <= 0:
            return
        while not self.stop.wait(hours * 3600):
            try:
                note = evolve_pass(self.cfg, self.mcp, confirm=None)
                note = _cage_autonomous_edits(note)
                deliver(f"auto-evolve cycle:\n{note}", title="self-improvement")
            except Exception as e:
                _log_error("auto-evolve failed:\n" + traceback.format_exc())
                try:
                    deliver(f"(auto-evolve failed: {e})", title="self-improvement")
                except Exception:
                    pass  # notification is best-effort; the log line is the record


class AutoUpgrade:
    """Daemon-mode thread: periodically checks for upstream codebase upgrades,
    validates syntax and unit tests, applies updates with automatic rollback on
    failure, and hot-reloads modified modules."""

    def __init__(self, cfg: dict, mcp=None):
        self.cfg = cfg if cfg is not None else {}
        self.mcp = mcp
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            hours = int(float(self.cfg.get("upgrade_hours", 24) or 0))
        except (TypeError, ValueError):
            hours = 24
        if hours <= 0 or not self.cfg.get("auto_upgrade", True):
            return
        from .tools import self_update
        while not self.stop.wait(hours * 3600):
            try:
                res = self_update()
                if "updated" in res.lower() and "file(s)" in res.lower():
                    deliver(f"auto-upgrade cycle:\n{res}", title="code upgrade")
            except Exception as e:
                _log_error("auto-upgrade failed:\n" + traceback.format_exc())


# ---- original buddy.py lines 2630-2633 --------------------------------


# --- self-repair: he finds and fixes his own bugs ----------------------------

# ---- original buddy.py lines 2634-2634 --------------------------------
_REPAIR_LOCK = threading.Lock()

# ---- original buddy.py lines 2635-2635 --------------------------------
_last_repair = [0.0]

# ---- original buddy.py lines 2647-2648 --------------------------------


# ---- original buddy.py lines 2649-2671 --------------------------------
def self_repair(cfg: dict, mcp=None, confirm=None, issue: str = "") -> str:
    """One repair cycle: scans for AST syntax bugs, test suite failures,
    crash logs in errors.log, or user-reported issues. Diagnoses, patches
    Buddy's own source via ast-guarded code_edit, runs verification tests,
    and hot-reloads the module."""
    from .config import BUDDY_SRC
    with _REPAIR_LOCK:
        path = HOME / "errors.log"
        raw_recent = path.read_text(encoding="utf-8", errors="replace")[-6000:] if path.exists() else ""
        # Filter out transient external API/network errors (401/429/quota) which are not codebase bugs
        code_crashes = []
        for block in raw_recent.split("## "):
            if not block.strip():
                continue
            if any(transient in block for transient in ("API error 401", "API error 429", "API error 503", "rate limited", "Quota exceeded", "Unauthorized")):
                continue
            if "Traceback" in block and "buddy_core" in block:
                code_crashes.append("## " + block.strip())
        recent = "\n\n".join(code_crashes)

        # 1. AST syntax scan across all source files
        syntax_errors = []
        for p in BUDDY_SRC.rglob("*.py"):
            if any(part in str(p) for part in (".git", "scratch", "__pycache__")):
                continue
            try:
                import ast
                ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
            except SyntaxError as se:
                syntax_errors.append(f"{p.relative_to(BUDDY_SRC)}:{se.lineno} SyntaxError: {se.msg}")

        # 2. Test suite run (if no syntax errors and not in recursive test mode)
        test_failures = ""
        if not syntax_errors and not os.environ.get("BUDDY_TESTING_SELF_REPAIR"):
            try:
                env = dict(os.environ)
                env["BUDDY_TESTING_SELF_REPAIR"] = "1"
                proc = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                    cwd=str(BUDDY_SRC),
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=env,
                )
                if proc.returncode != 0:
                    test_failures = (proc.stderr or proc.stdout)[-4000:]
            except Exception as te:
                test_failures = f"Test execution error: {te}"

        # If clean and no issue specified
        if not issue.strip() and not recent.strip() and not syntax_errors and not test_failures.strip():
            return "(all unit tests pass, syntax is valid across all files, and no errors on record — source code is healthy and bug-free)"

        # Assemble diagnosis context
        diagnostics = []
        if issue.strip():
            diagnostics.append(f"Reported Bug / Problem Statement:\n{issue.strip()}")
        if syntax_errors:
            diagnostics.append("Syntax Errors Detected in Source Code:\n" + "\n".join(syntax_errors))
        if test_failures.strip():
            diagnostics.append(f"Failing Unit Tests in Test Suite:\n{test_failures.strip()}")
        if recent.strip():
            diagnostics.append(f"Recent Runtime Crashes (from errors.log):\n{recent.strip()}")

        # In-function rate limit for automatic (no-issue) triggers: direct
        # `/fix <issue>` from the user always runs, but background/error-log
        # sweeps at most once per 10 minutes to avoid LLM+test loops.
        if not issue.strip() and time.time() - _last_repair[0] < 600:
            return "(self-repair ran recently — waiting before the next automatic cycle)"

        diag_text = "\n\n".join(diagnostics)

        # List buddy's own source files so the agent can navigate them
        src_files = sorted(
            str(p.relative_to(BUDDY_SRC))
            for p in BUDDY_SRC.rglob("*.py")
            if ".git" not in str(p) and "scratch" not in str(p) and "__pycache__" not in str(p)
        )
        src_listing = "\n".join(f"  {f}" for f in src_files[:60])
        from .agent import run_agent, _sub_mcp  # lazy: avoids circular import
        try:
            report = run_agent(
                cfg, mcp or _sub_mcp(),
                f"You are repairing your own source code. Your source files live in: {BUDDY_SRC}\n"
                f"Source layout (buddy_core/ is a Python package — each .py is a module):\n"
                f"{src_listing}\n\n"
                f"Diagnostics and Detected Bugs:\n\n{diag_text}\n\n"
                "Instructions:\n"
                "1. Read the relevant source file with read_file to understand the bug and context.\n"
                "2. Use code_edit to fix the bug (target path: 'buddy_core/<module>.py' or 'buddy.py').\n"
                "   code_edit verifies Python syntax and saves automatic backups in ~/.buddy/backups/.\n"
                "3. Verify your fix by running tests with run_command: python3 -m unittest discover -s tests\n"
                "4. Call hot_reload('<module>') so the fix is active immediately in memory.\n"
                "5. If the error is environmental (e.g. missing external package, network down), explain clearly.\n"
                "End with exactly one verdict line: FIXED, NOT-A-BUG, or NEEDS-HUMAN.",
                quiet=True, confirm=confirm)
            # Consume the log only when the repair claims success — a NEEDS-HUMAN
            # or failed verdict must keep the evidence for the next cycle/human.
            # Match the verdict line exactly: a "FIXED" substring in ordinary
            # prose ("that was already fixed in v2") used to destroy the
            # crash evidence while the bug was still live.
            verdict_fixed = any(
                ln.strip().upper() == "FIXED"
                for ln in report.splitlines())
            if path.exists() and recent.strip() and verdict_fixed:
                try:
                    with STATE_LOCK:
                        with path.open("w"):
                            pass
                except OSError:
                    pass  # keep the evidence if we can't clear it
            deliver(f"self-repair cycle:\n{report}", title="source bug fixed")
            return report
        finally:
            # Cooldown applies even when run_agent raised: an exception must
            # not re-fire the full scan+test+LLM cycle on every 20s tick.
            _last_repair[0] = time.time()

# ---- original buddy.py lines 2672-2673 --------------------------------


# ---- original buddy.py lines 2674-2698 --------------------------------
# Trusted internal self-improvement cycles (ErrorReaper, startup repair,
# `buddy.py fix`) run unattended on buddy's OWN source with syntax checks,
# backups and rollback downstream — they need code_edit/hot_reload, which
# deny confirm=None. This bypass applies ONLY to these internal entry
# points; every model-reachable tool path still gets the real confirm.
TRUSTED_CONFIRM = lambda _m: True  # noqa: E731


def _cage_autonomous_edits(note: str) -> str:
    """Cage for unattended self-modification (deep-check #12 design fix).

    A cycle running with TRUSTED_CONFIRM can edit buddy's own source with
    nobody watching. If the source checkout is a git repo, any edits the
    cycle made are committed to the `buddy/autonomous` branch — the live
    process keeps them (hot_reload applied them in memory), but the working
    tree returns to the previous branch, so a restart lands on reviewed code
    and the changes survive only as a mergeable, reviewable branch.
    Without a git repo, backups + rollback remain the safety net."""
    import subprocess
    from .config import BUDDY_SRC
    try:
        if not (BUDDY_SRC / ".git").exists():
            return note  # not a checkout — nothing to cage into
        g = ["git", "-C", str(BUDDY_SRC)]

        def _run(*args):
            return subprocess.run(g + list(args), capture_output=True,
                                  text=True, timeout=30)

        st = _run("status", "--porcelain")
        if st.returncode != 0 or not st.stdout.strip():
            return note  # repo clean: the cycle edited nothing on disk
        cur = _run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if cur == "buddy/autonomous":
            _run("add", "-A")
            _run("commit", "-m", "autonomous cycle (continued)")
            return note + "\n(caged: committed to buddy/autonomous)"
        _run("checkout", "-b", "buddy/autonomous")
        _run("add", "-A")
        c = _run("commit", "-m",
                 "autonomous cycle: proposed changes awaiting human review")
        if c.returncode == 0:
            _run("checkout", cur)  # tree reverts; edits live on the cage branch
            return note + ("\n(caged: edits committed to buddy/autonomous — "
                           "review the diff, then merge or discard)")
        return note
    except Exception:
        return note  # best-effort; backups/rollback still apply


class ErrorReaper:
    """Daemon-mode thread: when unhandled errors pile up in errors.log,
    gives buddy one repair cycle (at most once per hour)."""

    def __init__(self, cfg: dict, mcp=None):
        self.cfg = cfg if cfg is not None else {}
        self.mcp = mcp
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        path = HOME / "errors.log"
        seen = 0  # consume any pre-restart backlog too: crash, restart, repair
        while not self.stop.wait(20):
            try:
                size = path.stat().st_size if path.exists() else 0
                if size > seen and time.time() - _last_repair[0] > 3600:
                    seen = size
                    _cage_autonomous_edits(
                        self_repair(self.cfg, self.mcp, confirm=TRUSTED_CONFIRM))
                elif not (size > seen and time.time() - _last_repair[0] <= 3600):
                    # while cooling down, DON'T advance `seen`: errors arriving
                    # during the cooldown must still trigger the next repair
                    seen = size
            except Exception:
                _log_error("error reaper failed:\n" + traceback.format_exc())

# ---- original buddy.py lines 2699-2700 --------------------------------


# ---- original buddy.py lines 2701-2720 --------------------------------
STARTER_SKILLS = [
    ("web-research", "How to research a topic properly on the web",
     "1. Start with web_search on the core question — read titles before clicking.\n"
     "2. web_fetch the 2-3 most promising pages, not just the first result.\n"
     "3. Cross-check any surprising fact against a second, independent source.\n"
     "4. If a page needs JavaScript (fetch returns junk), escalate to browse.\n"
     "5. Report findings with sources; flag anything you couldn't verify."),
    ("troubleshoot-linux", "Debug a failing command or service on Linux",
     "1. Run the exact failing command and read the FIRST error line, not the last.\n"
     "2. Check the obvious: does the binary exist (`which x`), is it installed,\n"
     "   is the service running (`systemctl --user status x` / `ps aux | grep x`).\n"
     "3. Permissions: who owns the file, am I root, is the port privileged?\n"
     "4. Logs: journalctl --user -u <name>, /var/log/syslog, stderr.\n"
     "5. Change ONE thing at a time and re-test. Note the fix with save_skill."),
    ("clean-conversation", "Keep long conversations fast and on track",
     "1. If context feels bloated, suggest /new — memory and playbook survive it.\n"
     "2. Offload long findings to workspace files instead of repeating them.\n"
     "3. For multi-step chores, delegate to a subagent instead of doing them inline.\n"
     "4. Recap state in one line before starting a new subtask."),
]

