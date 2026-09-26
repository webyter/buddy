"""buddy_core.agent — the tool-call loop: run_messages/run_agent, delegation, exit pass, context budget.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .brain import _completion
from .config import MEMORY, STATE_LOCK, TASKS, _TASK_SEQ, _save_config, load_config, secret_set, api_key_for, _needs_key
from .mcp import MCPManager
from .memory import _vector_add
from .prompts import _system_prompt
from .skills import edit_playbook, read_playbook, save_skill
from .tools import all_tool_specs, tool_impl
from .tools import is_dangerous as _dangerous
from .util import _arg_summary, _log_error, _tool_line, deliver, ui_dim

import ast
import base64
import hashlib
import itertools
import json
import operator
import os
import re
import shutil
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
from pathlib import Path

# ---- original buddy.py lines 1107-1110 --------------------------------


# ================================================================== jobs ===

# ---- original buddy.py lines 1111-1186 --------------------------------
def _exit_pass(cfg: dict, transcript: list[dict]) -> None:
    """Single end-of-session LLM pass replacing the old three-call exit
    (session_digest + self_critique + skill_harvest). One completion returns
    a JSON object with all three results; applying them reuses the same
    validation/persistence paths as the originals. Errors go to errors.log,
    never silently vanish. Never raises."""
    try:
        convo = "\n".join(
            f"{m['role']}: {str(m.get('content'))[:400]}"
            for m in transcript
            if m["role"] in ("user", "assistant") and m.get("content")
        )[-8000:]
        if not convo.strip():
            return
        resp = _completion(
            cfg,
            [
                {
                    "role": "system",
                    "content": "End-of-session review. Output ONE JSON object "
                    "(no markdown fences, no commentary) with exactly these "
                    "keys:\n"
                    '"digest": string — durable facts about the user worth '
                    "remembering long-term (preferences, names, plans, "
                    "corrections), one per line prefixed '- '; or null if "
                    "none.\n"
                    '"playbook": string — COMPLETE updated playbook (concise '
                    "bullets, under 4000 chars) if this session revealed a "
                    "flaw or lesson; improving existing lessons beats adding "
                    "new ones; or null to keep the current playbook.\n"
                    '"skill": object {"name", "description", "instructions"} '
                    "if the session demonstrated a reusable multi-step "
                    "procedure (installing/fixing/automating something on the "
                    "host, incl. exact commands and gotchas); or null.\n"
                    "Current playbook:\n" + read_playbook(),
                },
                {"role": "user", "content": convo},
            ],
            tools=None,
            stream=False,
        )
        text = (resp["choices"][0]["message"].get("content") or "").strip()
        # tolerate ```json fences / leading prose before the object
        data = None
        try:
            start = text.index("{")
            data = json.JSONDecoder().raw_decode(text[start:])[0]
        except (ValueError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict):
            _log_error("exit pass: LLM returned unparseable JSON: " + text[:200])
            return
        digest = data.get("digest")
        if isinstance(digest, str) and digest.strip() \
                and not digest.strip().upper().startswith("NOTHING"):
            with MEMORY.open("a") as f:
                f.write(f"### session digest {datetime.now():%Y-%m-%d %H:%M}\n")
                f.write(digest.strip() + "\n")
            for line in digest.splitlines():
                if line.strip().startswith("-"):
                    _vector_add(line.strip()[2:])
        playbook = data.get("playbook")
        if isinstance(playbook, str) and playbook.strip():
            result = edit_playbook(playbook)  # same validation/refusal path
            if result.startswith("(refused"):
                _log_error(f"exit pass: playbook rejected: {result}")
            else:
                print("(playbook updated by self-critique)")
        skill = data.get("skill")
        if isinstance(skill, dict) and skill.get("name") and skill.get("instructions"):
            save_skill(str(skill["name"])[:40],
                       str(skill.get("description") or skill["name"]),
                       str(skill["instructions"]))
            print(f"(learned new skill: {skill['name']})")
    except Exception:
        _log_error(traceback.format_exc())

# ---- original buddy.py lines 2544-2547 --------------------------------


# --- codex: heavy coding muscle via the Codex CLI ----------------------------

# ---- original buddy.py lines 2548-2558 --------------------------------
def codex_run(task: str, timeout: int = 900) -> str:
    if not shutil.which("codex"):
        return ("(codex not installed — `npm install -g @openai/codex` then "
                "`codex login --device-auth` to give buddy a coding muscle)")
    try:
        p = subprocess.run(["codex", "exec", task], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"(codex timed out after {timeout}s)"
    out = (p.stdout + (f"\n[stderr]\n{p.stderr}" if p.stderr else "")).strip()
    return out[:12000] or "(no output)"

# ---- original buddy.py lines 2898-2899 --------------------------------

# Shared MCP manager for subagents/background work. Owned HERE (not
# imported from config): `global` on an imported name rebinds this
# module's binding and leaves config's cell permanently None.
_SUB_MCP: "MCPManager | None" = None


# ---- original buddy.py lines 2900-2905 --------------------------------
def _sub_mcp() -> "MCPManager":
    global _SUB_MCP
    mgr = _SUB_MCP
    if mgr is not None:
        return mgr
    # Build OUTSIDE STATE_LOCK: start_all() spawns processes and waits up
    # to 60s per server for initialize + tools/list. Holding the global
    # lock across that froze every web request and job for minutes.
    mgr = MCPManager()
    mgr.start_all()
    with STATE_LOCK:  # publish atomically; a parallel delegate may build its own
        if _SUB_MCP is None:
            _SUB_MCP = mgr
            import atexit
            atexit.register(_shutdown_sub_mcp)
            return _SUB_MCP
    mgr.shutdown()  # lost the race — don't leak the processes we started
    return _SUB_MCP


def _shutdown_sub_mcp() -> None:
    """Stop the shared subagent MCP manager's child processes at exit."""
    global _SUB_MCP
    mgr = _SUB_MCP
    _SUB_MCP = None
    if mgr is None:
        return
    try:
        mgr.shutdown()
    except Exception:
        pass

# ---- original buddy.py lines 2906-2907 --------------------------------


# ---- original buddy.py lines 2908-2931 --------------------------------
def delegate(task: str, name: str = "") -> str:
    tid = f"{name or 'subagent'}-{next(_TASK_SEQ)}"
    with STATE_LOCK:
        TASKS[tid] = {"task": task, "status": "running", "result": "", "ts": time.time()}
    cfg = load_config()

    def worker():
        try:
            result = run_agent(cfg, _sub_mcp(), task, quiet=True)
            with STATE_LOCK:
                TASKS[tid]["status"] = "done"
                TASKS[tid]["result"] = result
            try:
                deliver(result[:600], title=f"subagent {tid} finished")
            except Exception:
                pass  # the result is stored; a lost notification must not
                      # overwrite it to "failed"
        except Exception as e:
            with STATE_LOCK:
                TASKS[tid]["status"] = "failed"
                TASKS[tid]["result"] = f"(failed: {e})"
            try:
                deliver(f"(subagent {tid} failed: {e})", title=f"subagent {tid} failed")
            except Exception:
                pass  # run_agent already left the traceback in errors.log

    threading.Thread(target=worker, daemon=True, name=tid).start()
    return f"started subagent {tid}: {task[:80]}"

# ---- original buddy.py lines 2932-2933 --------------------------------


# ---- original buddy.py lines 2934-2945 --------------------------------
def tasks_status() -> str:
    with STATE_LOCK:
        cutoff = time.time() - 24 * 3600  # drop finished tasks older than 24h
        for tid in [tid for tid, t in TASKS.items()
                    if t.get("status") in ("done", "failed") and t.get("ts", 0) < cutoff]:
            del TASKS[tid]
        if not TASKS:
            return "(no subagents running)"
        lines = [f"{tid} [{t['status']}] {t['task'][:60]}" for tid, t in TASKS.items()]
        done = [f"{tid}: {t['result'][:400]}" for tid, t in TASKS.items()
                if t["status"] in ("done", "failed") and t["result"]]
    return "\n".join(lines + [""] + [f"  {d}" for d in done])

# ---- original buddy.py lines 3275-3276 --------------------------------


# ---- original buddy.py lines 3277-3279 --------------------------------
def _approx_chars(messages: list[dict]) -> int:
    """Rough context size in characters (tool-call payloads excluded)."""
    return sum(len(str(m.get("content") or "")) for m in messages)

# ---- original buddy.py lines 3280-3281 --------------------------------


# ---- original buddy.py lines 3282-3295 --------------------------------
def _elide_context(messages: list[dict], budget: int) -> None:
    """Shrink the conversation to roughly `budget` chars in place: elide the
    OLDEST tool-role contents first, then old assistant messages, then old
    user pastes. Never touches messages[0] (system) or the last 6 messages
    (which hold the latest user request). Repeats passes until under budget
    or nothing shrinkable remains."""
    if _approx_chars(messages) <= budget:
        return
    while _approx_chars(messages) > budget:
        # keep system + the last 6 intact. With a short conversation
        # (<=7 messages) protected would collapse to 1 and messages[1:1]
        # is empty, so NOTHING was ever elidable (a huge paste in a
        # fresh chat stayed at full size forever). Elide from oldest,
        # but never the final user request.
        keep_tail = 6 if len(messages) > 8 else 1
        protected = max(len(messages) - keep_tail, 1)
        if protected <= 1:
            # Nothing safe to shrink except the oldest message; keep the
            # final user turn (last element) intact.
            protected = max(len(messages) - 1, 1)
        shrunk = False
        for role in ("tool", "assistant", "user"):
            for m in messages[1:protected]:
                if _approx_chars(messages) <= budget:
                    return
                if m.get("role") == role and not str(m.get("content") or "").startswith("(elided:"):
                    c = str(m.get("content") or "")
                    m["content"] = f"(elided: {c[:200]}...)"
                    shrunk = True
        if not shrunk:
            return

# ---- original buddy.py lines 3296-3297 --------------------------------


# Shell result markers that mean the command did NOT do its job. A failed
# shell call returns normal-looking text, so without this the UI (and the
# model) would read it as success.
_SHELL_FAIL_MARKERS = (
    "Command exited with code",
    "exceeding the timeout",
    "refused by safety guard",
)


def _is_tool_error(name: str, result_s: str) -> bool:
    """True when a tool result means failure: tool/MCP dispatch errors,
    plus shell failures (non-zero exit, timeout, safety refusal) which
    hide inside otherwise normal output text."""
    if result_s.startswith(("(tool", "(MCP", "(unknown tool")):
        return True
    if name in ("run_command", "bash"):
        return any(m in result_s for m in _SHELL_FAIL_MARKERS)
    return False


# ---- original buddy.py lines 3298-3309 --------------------------------
def _fire_on_tool(on_tool, name: str, status: str, secs, args=None, diff=None,
                  call_id=None, tail=None, result=None) -> None:
    """Invoke an on_tool callback without ever raising. Callbacks get extra
    positional params by arity: 4th = raw args dict (TUI), 5th = unified file
    diff (web), 6th = the brain's tool_call id (web, disambiguates parallel
    same-named tools), 7th = live output tail from run_command (web progress),
    8th = tool result text (read_file/grep, capped — renders the preview).
    3-arg callbacks (tests) are unchanged."""
    try:
        code = getattr(on_tool, "__code__", None)
        if code is None or code.co_argcount < 4:
            on_tool(name, status, secs)
        elif code.co_argcount == 4:
            on_tool(name, status, secs, args)
        elif code.co_argcount == 5:
            on_tool(name, status, secs, args, diff)
        elif code.co_argcount == 6:
            on_tool(name, status, secs, args, diff, call_id)
        elif code.co_argcount == 7:
            on_tool(name, status, secs, args, diff, call_id, tail)
        else:
            on_tool(name, status, secs, args, diff, call_id, tail, result)
    except Exception:
        pass

# ---- original buddy.py lines 3310-3311 --------------------------------


_OP_MAP = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def _safe_eval_math(expr: str) -> float | int | None:
    try:
        node = ast.parse(expr.strip(), mode="eval").body
        def _eval(n):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
                if isinstance(n.value, int) and n.value.bit_length() > 4096:
                    raise ValueError("number too large")  # str() would hit py3.11's int-to-str limit
                return n.value
            if isinstance(n, ast.BinOp) and type(n.op) in _OP_MAP:
                left, right = _eval(n.left), _eval(n.right)
                if isinstance(n.op, ast.Pow) and abs(right) > 1000:
                    raise ValueError("exponent too large")
                result = _OP_MAP[type(n.op)](left, right)
                if isinstance(result, int) and result.bit_length() > 4096:
                    raise ValueError("result too large")
                return result
            if isinstance(n, ast.UnaryOp) and type(n.op) in _OP_MAP:
                return _OP_MAP[type(n.op)](_eval(n.operand))
            raise ValueError("unsupported node")
        return _eval(node)
    except Exception:
        return None

def offline_task_handler(text: str, cfg: dict | None = None, confirm=None) -> str | None:
    """Handle simple offline tasks without contacting the LLM API."""
    raw = text.strip()
    if not raw:
        return None
    low = raw.lower().strip()

    # 1. Shell commands starting with ! or $ or "run <cmd>" or "exec <cmd>".
    # Specific phrases first: "run tests" must reach the test runner,
    # not run_command("tests").
    if low in ("test", "run tests", "run test", "pytest", "unit tests", "run unit tests"):
        from .tools import run_command
        return run_command(f"{sys.executable} -m unittest discover -s tests", confirm=confirm)
    if raw.startswith("!") or raw.startswith("$"):
        cmd = raw[1:].strip()
        if cmd:
            from .tools import run_command
            return run_command(cmd, confirm=confirm)
    if low.startswith("run ") or low.startswith("exec ") or low.startswith("sh ") or low.startswith("bash "):
        cmd = raw.split(maxsplit=1)[1].strip()
        if cmd:
            from .tools import run_command
            return run_command(cmd, confirm=confirm)
    if low.startswith("git "):
        from .tools import run_command
        return run_command(raw, confirm=confirm)
    if low.startswith("which "):
        tool_name = raw.split(maxsplit=1)[1].strip()
        p = shutil.which(tool_name)
        return f"{tool_name}: {p}" if p else f"{tool_name}: not found in PATH"

    # 2. Date & Time
    if low in ("time", "date", "what time is it", "current time", "what day is it", "today", "clock"):
        now = datetime.now()
        return f"📅 {now.strftime('%A, %B %d, %Y %H:%M:%S')}"

    # 3. System & Host Diagnostics
    if low in ("pwd", "cwd", "where am i", "current dir", "current directory"):
        return f"📂 Current directory: {os.getcwd()}"
    if low in ("uptime", "load", "cpu"):
        from .tools import run_command
        return run_command("uptime", confirm=confirm)
    if low in ("df", "disk", "disk space", "disk usage"):
        from .tools import run_command
        return run_command("df -h", confirm=confirm)
    if low in ("free", "ram", "memory usage"):
        from .tools import run_command
        return run_command("free -m" if shutil.which("free") else "vm_stat", confirm=confirm)
    if low in ("sysinfo", "system info", "host", "machine", "os"):
        from .tools import probe_system, system_summary
        return system_summary(probe_system())
    if low in ("doctor", "diagnose", "health check"):
        from .commands import doctor
        doctor()
        return "Doctor diagnostic complete."

    # 4. File reading & directory listing
    if (low.startswith("cat ") or low.startswith("read ") or low.startswith("view ") or low.startswith("show ")) \
            and not low.startswith(("read http://", "read https://")):
        parts = raw.split(maxsplit=1)
        if len(parts) > 1:
            p_str = parts[1].strip()
            from .tools import read_file
            return read_file(p_str)
    if low.startswith("head "):
        parts = raw.split(maxsplit=1)
        if len(parts) > 1:
            p_str = parts[1].strip()
            from .tools import read_file
            res = read_file(p_str)
            lines = res.splitlines()[:20]
            return "\n".join(lines)
    if low.startswith("tail "):
        parts = raw.split(maxsplit=1)
        if len(parts) > 1:
            p_str = parts[1].strip()
            from .tools import read_file
            res = read_file(p_str)
            lines = res.splitlines()[-20:]
            return "\n".join(lines)
    if low.startswith("wc ") or low.startswith("count lines "):
        parts = raw.split(maxsplit=2 if low.startswith("count lines ") else 1)
        target = parts[-1].strip()
        p = Path(target)
        if p.is_file():
            try:
                content = p.read_text(errors="replace")
                lines = len(content.splitlines())
                words = len(content.split())
                chars = len(content)
                return f"📄 {target}: {lines} lines, {words} words, {chars} characters"
            except Exception as e:
                return f"(error reading {target}: {e})"
        return f"(file not found: {target})"
    if low in ("ls", "dir", "list files", "files"):
        from .tools import list_dir
        return list_dir(".")
    if low.startswith("ls ") or low.startswith("dir "):
        parts = raw.split(maxsplit=1)
        if len(parts) > 1:
            from .tools import list_dir
            return list_dir(parts[1].strip())
    if low.startswith("grep "):
        pat = raw[5:].strip()
        if pat:
            from .tools import grep
            return grep(pat, ".")
    if low.startswith("find ") and not low.startswith("find files"):
        pat = raw[5:].strip()
        if pat:
            from .tools import find_files
            return find_files(pat, ".")

    # 5. Buddy features, memory & offline notes
    if low.startswith("remember ") or low.startswith("note ") or low.startswith("save note "):
        fact = raw.split(maxsplit=1 if not low.startswith("save note ") else 2)[-1].strip()
        if fact:
            from .memory import remember
            return remember(fact)
    if low in ("are you self aware", "are you self-aware", "self aware", "self-aware", "introspect", "self introspect", "telemetry", "who are you", "status", "version", "info"):
        from .tools import introspect
        return introspect("all")
    if low in ("tools", "list tools", "what tools", "available tools"):
        from .tools import BASE_TOOL_SPECS, _CUSTOM_TOOLS, load_custom_tools
        load_custom_tools()
        out = ["Built-in Tools: " + ", ".join(s["function"]["name"] for s in BASE_TOOL_SPECS)]
        if _CUSTOM_TOOLS:
            out.append("Custom Tools: " + ", ".join(_CUSTOM_TOOLS.keys()))
        return "\n".join(out)
    if low in ("jobs", "list jobs", "scheduled jobs"):
        from .sched import list_jobs
        return list_jobs()
    if low in ("inbox", "read inbox", "messages"):
        from .config import INBOX
        return INBOX.read_text() if INBOX.exists() else "(inbox empty)"
    if low in ("memory", "show memory", "recall memory"):
        from .memory import recall_memory
        return recall_memory()
    if low in ("playbook", "show playbook"):
        from .skills import read_playbook
        return read_playbook()
    if low in ("skills", "list skills"):
        from .skills import skills_index
        return skills_index() or "(no skills saved yet)"
    if low in ("introspect", "self introspect", "telemetry"):
        from .tools import introspect
        return introspect("all")

    # 5b. Offline scheduling & reminders (work with a dead API — jobs run
    # on the local scheduler, no LLM needed to create them).
    m = re.fullmatch(r"timer\s+(\d+)\s*(minutes?|mins?|seconds?|secs?|hours?|hrs?)\b.*", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("s"):
            n = max(1, n // 60) or 1
            minutes = n
        else:
            minutes = n * 60 if unit.startswith(("hour", "hr")) else n
        from .sched import add_job
        return add_job("Timer — time is up", every_minutes=None, at=None,
                       in_minutes=minutes)
    m = re.search(r"remind me (?:to )?(.+?) in (\d+)\s*(minutes?|mins?|hours?|hrs?)\b", low)
    if m:
        from .sched import add_job
        n, unit = int(m.group(2)), m.group(3)
        minutes = n * 60 if unit.startswith(("hour", "hr")) else n
        what = re.sub(r"^to\s+", "", m.group(1).strip(" ."))
        return add_job(f"Reminder: {what}", every_minutes=None, at=None,
                       in_minutes=minutes)
    m = re.search(r"cancel (?:job|reminder|timer)\s+([0-9a-f]{4,8})\b", low)
    if m:
        from .sched import cancel_job
        return cancel_job(m.group(1))
    m = re.search(r"remind me (?:to )?(.+?) every (\d+)\s*(minutes?|mins?|hours?|hrs?)\b", low)
    if m:
        what, n, unit = m.group(1), int(m.group(2)), m.group(3)
        minutes = n * 60 if unit.startswith(("hour", "hr")) else n
        from .sched import add_job
        return add_job(f"Reminder: {what.strip(' .')}", every_minutes=minutes, at=None)
    m = re.search(r"remind me (?:to )?(.+?) at ([01]\d|2[0-3]):([0-5]\d)", low)
    if m:
        from .sched import add_job
        return add_job(f"Reminder: {m.group(1).strip(' .')}", every_minutes=None,
                       at=f"{m.group(2)}:{m.group(3)}")
    if low.startswith("open ") and (raw[5:].strip().startswith(("http://", "https://", "www."))):
        import webbrowser
        url = raw[5:].strip()
        if url.startswith("www."):
            url = "https://" + url
        webbrowser.open(url)
        return f"🌐 opened {url}"

    # 6. Basic math & encoding utilities
    if low.startswith("base64 encode "):
        val = raw[14:].strip()
        return base64.b64encode(val.encode()).decode()
    if low.startswith("base64 decode "):
        val = raw[14:].strip()
        try:
            return base64.b64decode(val.encode()).decode(errors="replace")
        except Exception as e:
            return f"(invalid base64: {e})"
    if low.startswith("sha256 "):
        val = raw[7:].strip()
        return hashlib.sha256(val.encode()).hexdigest()
    if low.startswith("md5 "):
        val = raw[4:].strip()
        return hashlib.md5(val.encode()).hexdigest()

    # 5c. Network helpers — quota exhaustion kills the LLM, not the internet.
    if low in ("ip", "my ip", "what is my ip", "whats my ip"):
        import urllib.request as _u
        try:
            with _u.urlopen("https://api.ipify.org", timeout=10) as r:
                return f"🌐 your public IP: {r.read().decode().strip()}"
        except Exception as e:
            return f"(could not reach the IP service: {e})"
    if low.startswith("weather"):
        import urllib.request as _u
        city = raw[7:].strip().replace(" ", "%20") if len(raw) > 7 else ""
        url = f"https://wttr.in/{city}?format=3" if city else "https://wttr.in?format=3"
        try:
            req = _u.Request(url, headers={"User-Agent": "curl/8.0"})
            with _u.urlopen(req, timeout=15) as r:
                return f"🌤️ {r.read().decode().strip()}"
        except Exception as e:
            return f"(weather lookup failed: {e})"
    if low.startswith("ping "):
        host = raw.split(maxsplit=1)[1].strip()
        if host and re.fullmatch(r"[a-zA-Z0-9.\-]+", host):
            from .tools import run_command
            return run_command(f"ping -c 4 {host}", confirm=confirm)

    # 5d. Generators & converters
    if low.startswith("password") or low.startswith("passphrase"):
        import secrets as _s
        import string as _st
        n = 20
        m = re.search(r"(\d+)", low)
        if m:
            n = min(int(m.group(1)), 128)
        alphabet = _st.ascii_letters + _st.digits + "!@#$%^&*"
        return "🔑 " + "".join(_s.choice(alphabet) for _ in range(n))
    if low in ("uuid", "guid"):
        import uuid as _uuid
        return f"🆔 {_uuid.uuid4()}"
    _TEMP = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*°?\s*(c|f|celsius|fahrenheit)\s*(to|in)\s*°?\s*(c|f|celsius|fahrenheit)", low)
    if _TEMP:
        val = float(_TEMP.group(1))
        src, dst = _TEMP.group(2)[0], _TEMP.group(4)[0]
        if src == dst:
            res = val
        elif src == "c":
            res = val * 9 / 5 + 32
        else:
            res = (val - 32) * 5 / 9
        unit = "°C" if dst == "c" else "°F"
        return f"🌡️ {val:g}°{src.upper()} = {res:g}{unit}"
    _CONV = {"km": ("mi", 0.621371), "mi": ("km", 1.60934), "miles": ("km", 1.60934),
             "kg": ("lb", 2.20462), "lb": ("kg", 0.453592), "lbs": ("kg", 0.453592),
             "m": ("ft", 3.28084), "ft": ("m", 0.3048), "cm": ("in", 0.393701),
             "in": ("cm", 2.54)}
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(km|miles|mi|m|ft|cm|in|kg|lbs|lb)\s*(?:to|in)\s*(km|miles|mi|m|ft|cm|in|kg|lbs|lb)\b", low)
    if m and m.group(2) in _CONV and m.group(3) == _CONV[m.group(2)][0]:
        val, (dst, factor) = float(m.group(1)), _CONV[m.group(2)]
        return f"📏 {m.group(1)} {m.group(2)} = {val * factor:g} {dst}"
    if low.startswith("url encode "):
        from urllib.parse import quote
        return quote(raw[11:].strip())
    if low.startswith("url decode "):
        from urllib.parse import unquote
        return unquote(raw[11:].strip())
    if low.startswith("hex encode "):
        return raw[11:].strip().encode().hex()
    if low.startswith("hex decode "):
        try:
            return bytes.fromhex(raw[11:].strip()).decode(errors="replace")
        except ValueError as e:
            return f"(invalid hex: {e})"

    # 5f. API/config status — zero LLM involvement, always safe offline.
    if low in ("api status", "api", "model status", "quota", "key status", "brain"):
        cfg = cfg or {}
        lines = [f"brain: {cfg.get('brain', 'api')}",
                 f"model: {cfg.get('model', '?')}",
                 f"api_base: {cfg.get('api_base', '?')}"]
        try:
            from .config import api_key_for as _akf
            lines.append("api_key: " + ("configured (hidden)" if _akf(cfg) else "NOT SET — `python3 buddy.py setup`"))
        except Exception:
            lines.append("api_key: unknown")
        lines.append("failover: " + ("off" if cfg.get("failover") is False else "on"))
        extra = [n for n, i in (cfg.get("models") or {}).items() if isinstance(i, dict)]
        if extra:
            lines.append("other models: " + ", ".join(extra))
        lines.append("offline mode: `! <command>`, reminders, weather, fx, wiki, `help`")
        return "🧠 " + "\n   ".join(lines)

    # 5e. Knowledge lookups — free keyless APIs, internet only, no LLM.
    if low.startswith(("read http://", "read https://", "fetch ", "get http")):
        url = raw.split(maxsplit=1)[1].strip()
        if url.startswith("get "):
            url = url[4:].strip()
        import urllib.request as _u
        import re as _re
        if not url.startswith(("http://", "https://")):
            return "(give a full URL, e.g. read https://example.com)"
        try:
            req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0 (buddy offline reader)"})
            with _u.urlopen(req, timeout=20) as r:
                ctype = r.headers.get("Content-Type", "")
                body = r.read(400_000).decode("utf-8", errors="replace")
            if "html" in ctype or body.lstrip()[:1] == "<":
                body = _re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", body)
                body = _re.sub(r"(?s)<[^>]+>", " ", body)
                body = _re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&#\d+;",
                               lambda m: {"&nbsp;": " ", "&amp;": "&", "&lt;": "<",
                                          "&gt;": ">"}.get(m.group(0), " "), body)
                body = _re.sub(r"[ \t]+", " ", body)
                body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
            return body[:4000] or "(empty page)"
        except Exception as e:
            return f"(fetch failed: {e})"
    m = re.fullmatch(r"(?:define|meaning of|dictionary)\s+([a-zA-Z\-']+)", low)
    if m:
        import urllib.request as _u
        import json as _json
        word = m.group(1)
        try:
            with _u.urlopen(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=15) as r:
                entry = _json.loads(r.read())
            phon = entry[0].get("phonetic", "")
            out = [f"📖 {word} {phon}".rstrip()]
            for meaning in entry[0].get("meanings", [])[:3]:
                out.append(f"• ({meaning.get('partOfSpeech', '?')}) " +
                           (meaning.get("definitions") or [{}])[0].get("definition", ""))
            return "\n".join(out)
        except Exception:
            return f"(no dictionary entry for {word!r})"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]{3})\s*(?:to|in)\s*([a-z]{3})", low)
    if m:
        import urllib.request as _u
        import json as _json
        amt, src, dst = float(m.group(1)), m.group(2).upper(), m.group(3).upper()
        rates = offline_task_handler.__dict__.setdefault("_fx_cache", {})
        import time as _time
        now_ts = _time.time()
        if src not in rates or now_ts - rates[src][1] > 3600:
            try:
                with _u.urlopen(f"https://open.er-api.com/v6/latest/{src}", timeout=15) as r:
                    data = _json.loads(r.read())
                if data.get("result") != "success":
                    raise ValueError(data.get("message", "rate fetch failed"))
                rates[src] = (data["rates"], now_ts)
            except Exception as e:
                return f"(exchange rate lookup failed: {e})"
        rate = rates[src][0].get(dst)
        if not rate:
            return f"(no rate for {src} → {dst})"
        return f"💱 {amt:g} {src} = {amt * rate:,.2f} {dst}"
    m = re.fullmatch(r"(?:wiki|wikipedia)\s+(.+)", low)
    if m:
        import urllib.request as _u
        import json as _json
        query = m.group(1).strip()
        from urllib.parse import quote as _quote
        query = _quote(query)
        try:
            with _u.urlopen(_u.Request(f"https://en.wikipedia.org/api/rest_v1/page/summary/{query}",
                                       headers={"User-Agent": "buddy/3 (offline helper)"}), timeout=15) as r:
                data = _json.loads(r.read())
            if data.get("type") == "disambiguation":
                return f"🔎 {data.get('title', '?')} is ambiguous — be more specific."
            return f"📚 {data.get('title', '?')}: {data.get('extract', '(no summary)')}"
        except Exception:
            return f"(no wikipedia article found for {m.group(1).strip()!r})"

    math_candidate = None
    if low.startswith("calc ") or low.startswith("math ") or low.startswith("eval "):
        math_candidate = raw.split(maxsplit=1)[1]
    elif low.startswith("what is ") and any(op in low for op in ("+", "-", "*", "/", "%", "**")):
        math_candidate = raw[8:].rstrip("?").strip()
    elif re.fullmatch(r"[\d\s+\-*/%.()^]+", raw) and any(op in raw for op in ("+", "-", "*", "/")):
        math_candidate = raw
    if math_candidate:
        ans = _safe_eval_math(math_candidate)
        if ans is not None:
            return f"🔢 {math_candidate.strip()} = {ans}"

    # 7. Help & Offline Guide
    if low in ("help", "offline help", "what can you do", "commands"):
        return (
            "🛠️ **Buddy Offline Mode Tasks:**\n"
            "• **Run shell command**: `! <cmd>` or `run <cmd>` (e.g. `! git status`, `! pytest`)\n"
            "• **Git commands**: `git status`, `git diff`, `git log`, `git branch`\n"
            "• **Read / View file**: `read <path>`, `cat <path>`, `head <path>`, `tail <path>`, `wc <path>`\n"
            "• **List files / search**: `ls`, `grep <pat>`, `find <pat>`, `pwd`, `which <tool>`\n"
            "• **System info**: `time`, `disk`, `free`, `cpu`, `sysinfo`, `doctor`\n"
            "• **API state**: `api status` (brain/model/key — no key is ever shown)\n"
            "• **Buddy state & notes**: `remember <note>`, `memory`, `status`, `tools`, `jobs`, `inbox`, `playbook`, `skills`\n"
            "• **Reminders (offline)**: `remind me to stretch every 30 minutes`, `remind me to stretch at 14:30`, `remind me to check the oven in 10 minutes`, `timer 5 minutes`, `cancel timer <id>`, `jobs`\n"
            "• **Open a link**: `open https://...`\n"
            "• **Network**: `ip`, `weather <city>`, `ping <host>` (needs internet, not the API)\n"
            "• **Knowledge (keyless)**: `read <url>`, `define <word>`, `100 usd to ghs`, `wiki <topic>`\n"
            "• **Generators & converters**: `password 24`, `uuid`, `100 km to mi`, `30 c to f`, `url encode/decode <str>`, `hex encode/decode <str>`\n"
            "• **Calculator & Utils**: `calc 1024 * 768`, `base64 encode/decode <str>`, `sha256 <str>`\n"
            "• **Switch model / API endpoint**: `/model <name>` or `/model add ...`"
        )

    return None

# ---- original buddy.py lines 3312-3390 --------------------------------
# Transient, model-scoped API failures: another model will likely answer.
_FAILOVER_ERRORS = ("API error 503", "API error 500", "API error 502",
                    "API error 504", "high demand", "overloaded",
                    "UNAVAILABLE", "RESOURCE_EXHAUSTED: per-day")
# A 429 that is really the PROJECT's quota (verified: every model on the
# key returns it) — another model will not help, so don't waste a retry.
_PROJECT_QUOTA = ("exceeded your current quota", "quota exceeded",
                  "check your plan and billing", "free tier",
                  "api quota exhausted")  # the compact 429 message _api_err
# emits — daemon threads see this, not the raw body
# Never worth retrying on a different model.
_NO_FAILOVER = ("API error 401", "API error 403", "API key rejected",
                "model not found", "API error 404", "malformed API response")


def _is_project_quota(err_msg: str) -> bool:
    low = err_msg.lower()
    return any(q in low for q in _PROJECT_QUOTA)


# --- quota circuit breaker -----------------------------------------------
# A dead quota stays dead for minutes. Don't re-pay a doomed API call on
# every turn: after a quota kill with no failover alternative, turns go
# straight to offline mode for a cooldown, then probe the API again.
# 'quota_cooldown': 0 in config disables the breaker.
_QUOTA_CB = {"until": 0.0}  # time.monotonic() stamp; 0 = breaker closed
_QUOTA_CB_LOCK = threading.Lock()


def _quota_open(cfg: dict) -> bool:
    """True while turns may hit the API (breaker closed or expired)."""
    try:
        if float(cfg.get("quota_cooldown", 300)) <= 0:
            return True  # breaker disabled
    except (TypeError, ValueError):
        pass
    with _QUOTA_CB_LOCK:
        return time.monotonic() >= _QUOTA_CB["until"]


def _trip_quota_breaker(cfg: dict) -> float:
    """Open the breaker for the cooldown; returns the cooldown seconds."""
    try:
        cooldown = float(cfg.get("quota_cooldown", 300))
    except (TypeError, ValueError):
        cooldown = 300.0
    cooldown = max(0.0, cooldown)
    with _QUOTA_CB_LOCK:
        _QUOTA_CB["until"] = time.monotonic() + cooldown
    return cooldown


def _failover_model(cfg: dict, err_msg: str) -> str | None:
    """Pick another model to retry this turn on, or None to give up.

    Only for rate-limit/overload style errors. Candidates come from the
    user's own catalog (`cfg["models"]` + the built-in Gemini presets) and
    must not be the model that just failed. A 429/quota error may cross
    API bases (e.g. an out-of-quota Gemini key falling back to a local
    ollama model); other errors stay on the same endpoint."""
    if cfg.get("failover") is False:
        return None
    if any(bad in err_msg for bad in _NO_FAILOVER):
        return None
    cross_base_ok = "429" in err_msg or _is_project_quota(err_msg)
    if not cross_base_ok and not any(good in err_msg for good in _FAILOVER_ERRORS):
        return None
    current = str(cfg.get("model") or "")
    base = str(cfg.get("api_base") or "").rstrip("/")
    # Explicit candidates: models the USER added. Same endpoint always
    # qualifies; a different endpoint only on rate/quota errors.
    explicit: list[tuple[str, str]] = []
    for name, info in (cfg.get("models") or {}).items():
        if not isinstance(info, dict):
            continue
        m = str(info.get("model") or name)
        mbase = str(info.get("api_base") or "").rstrip("/")
        same = (not mbase or not base or mbase == base)
        if m and m != current and (same or cross_base_ok):
            if not same:
                # cross-provider retry is only useful if that provider has
                # its OWN key — falling back to the shared main key just
                # buys a guaranteed 401 from a host that never saw it.
                # Keyless local endpoints (ollama on loopback) are exempt.
                local = mbase.startswith(("http://127.0.0.1", "http://localhost",
                                          "http://[::1]"))
                if not local:
                    from .config import provider_key_name, secret_get
                    if not secret_get(provider_key_name(cfg, m, mbase)):
                        continue
            explicit.append((m, mbase))
    # on rate/quota errors prefer a different base (a same-base retry with
    # an exhausted key just 429s again)
    explicit.sort(key=lambda t: (not (cross_base_ok and t[1] and t[1] != base),
                                 t[0] != current))
    if explicit:
        return explicit[0][0]
    # Presets are only safe when they live on the same endpoint — never
    # send the user's key to a host they didn't configure. And on a project
    # quota they're pointless anyway: same key, same exhausted quota.
    if _is_project_quota(err_msg):
        return None
    try:
        from .commands import MODEL_PROVIDERS
        for p in MODEL_PROVIDERS.values():
            if base and str(p["base"]).rstrip("/") != base:
                continue
            for m in p["models"]:
                if m and m != current:
                    return m
    except Exception:
        pass
    return None


def _failover_base(cfg: dict, model: str) -> str | None:
    """The api_base a failover candidate lives on (None = current base)."""
    for name, info in (cfg.get("models") or {}).items():
        if isinstance(info, dict) and str(info.get("model") or name) == model:
            return str(info.get("api_base") or "").rstrip("/") or None
    return None


def _short_err(err_msg: str) -> str:
    """One short clause for the 'retrying on X' notice."""
    if "429" in err_msg or "rate limit" in err_msg.lower() or "quota" in err_msg.lower():
        return "rate limited"
    if "503" in err_msg or "high demand" in err_msg.lower() or "UNAVAILABLE" in err_msg:
        return "model overloaded"
    return "API error"


def _last_user_text(messages: list) -> str:
    """The newest user text in a message list (web turns use content lists)."""
    texts = [m.get("content") for m in messages if m.get("role") == "user"]
    last = texts[-1] if texts else ""
    if isinstance(last, list):
        last = " ".join(p.get("text", "") for p in last
                        if isinstance(p, dict) and p.get("text"))
    return str(last)


def run_messages(cfg: dict, mcp: MCPManager, messages: list[dict], quiet: bool,
                 confirm=None, stream: bool = True,
                 cancel: threading.Event | None = None,
                 on_tool=None, on_delta=None) -> str:
    tools = all_tool_specs() + (mcp.all_tools() if mcp else [])
    # "0" or a negative max_rounds means unlimited — buddy grinds until done
    max_rounds = int(cfg.get("max_rounds", 40))
    rounds = range(max_rounds) if max_rounds > 0 else itertools.count()
    event_lock = threading.Lock()  # serializes tool events / prints across workers
    # on_delta sink: forwards content deltas to the caller (web SSE) instead of
    # the terminal. Track whether any streamed, so a CLI / non-streamed brain
    # can still emit its whole reply as one delta.
    streamed = {"any": False}
    sink = None
    if on_delta is not None:
        def sink(text: str) -> None:
            streamed["any"] = True
            on_delta(text)
    # Rate-limit / overload failover: a 429 or 503 is a property of THIS
    # model right now, not of the whole account, so retry the turn once on
    # another model that answers. Scoped to the turn (a local override) so
    # the user's configured model is never silently rewritten, and tried
    # once only so a genuinely dead provider can't loop. Set
    # "failover": false in config.json to turn it off.
    failover_from = {"model": None, "base": None}
    # Zero-config: nothing to authenticate with. Never waste the turn on a
    # guaranteed 401 — if a local model is installed, run the turn on it
    # (ollama needs no key); otherwise answer offline with a notice that
    # says how to get more.
    keyless = _needs_key(cfg) and not api_key_for(cfg)
    if keyless:
        local = (cfg.get("models") or {}).get("local")
        if not isinstance(local, dict):
            local = {}
        on_local = bool(local) and (str(cfg.get("api_base") or "").rstrip("/")
                                    == str(local.get("api_base") or "").rstrip("/"))
        if not on_local and cfg.get("failover") is not False \
                and local.get("model") and local.get("api_base"):
            failover_from["model"] = str(local["model"])
            failover_from["base"] = str(local["api_base"]).rstrip("/")
            if not quiet:
                print(ui_dim("  (no API key configured — running this turn on "
                             "the local model; `python3 buddy.py setup` to add "
                             "a key)"))
        elif not on_local:
            _last = _last_user_text(messages)
            _offline = offline_task_handler(_last, cfg=cfg, confirm=confirm) if _last else None
            reply = (f"[offline mode]\n\n{_offline}" if _offline is not None else
                     "(no API key configured — running in offline mode.\n"
                     " `! <command>`, reminders, notes, calc, files work now;\n"
                     " type `help` for everything. For full chat, add a key\n"
                     " with `python3 buddy.py setup`.)")
            if sink is not None:
                sink(reply)
            return reply
    for _ in rounds:
        if cancel and cancel.is_set():
            return "(turn cancelled)"
        budget = int(cfg.get("context_budget", 40000))
        _elide_context(messages, budget)
        if not _quota_open(cfg) and failover_from["model"] is None:
            # circuit breaker open: quota died recently and no alternative
            # model exists — answer offline instantly, no doomed API call
            _last = _last_user_text(messages)
            _offline = offline_task_handler(_last, cfg=cfg, confirm=confirm) if _last else None
            reply = (f"[offline mode]\n\n{_offline}" if _offline is not None else
                     "(API quota cooling down — offline mode active: `! <command>`,"
                     " reminders, weather, fx, wiki all work. `help` for the list.)")
            if sink is not None:
                sink(reply)
            return reply
        try:
            resp = _completion(cfg, messages, tools,
                               stream=(stream and not quiet) or sink is not None,
                               on_delta=sink, model=failover_from["model"],
                               base=failover_from["base"])
        except Exception as e:
            err_msg = str(e)
            # Try one alternate model before declaring the API offline.
            alt = _failover_model(cfg, err_msg) if failover_from["model"] is None else None
            if alt:
                failover_from["model"] = alt
                failover_from["base"] = _failover_base(cfg, alt)
                if not quiet:
                    print(ui_dim(f"  ({_short_err(err_msg)} — retrying this "
                                 f"turn on {alt})"))
                continue
            if "429" in err_msg or _is_project_quota(err_msg):
                # no failover alternative: quota is dead account-wide —
                # open the breaker so later turns skip the doomed call
                _trip_quota_breaker(cfg)
            last_text = _last_user_text(messages)
            offline_result = offline_task_handler(last_text, cfg=cfg, confirm=confirm) if last_text else None
            if offline_result is not None:
                reply = f"[offline mode]\n\n{offline_result}"
                if sink is not None:
                    sink(reply)
                return reply
            reply = (f"(API offline: {err_msg})"
                     "\nOffline mode still works: `! <command>`, reminders, notes, calc, files, `jobs` — type `help` for the full list."
                     "\nNo API key? `python3 buddy.py setup` adds one.")
            if sink is not None:
                sink(reply)
            return reply
        msg = resp["choices"][0]["message"]
        if not msg.get("tool_calls"):
            reply = msg.get("content") or "(empty response)"
            if sink is not None and not streamed["any"] and reply:
                on_delta(reply)  # CLI / non-streamed brain: one whole-reply delta
            return reply
        if not quiet and msg.get("content"):
            print()  # ensure tool logs start on a fresh line
        messages.append(msg)
        parsed = []  # (idx, tc, name, args) — runnable calls
        for idx, tc in enumerate(msg["tool_calls"]):
            name = (tc.get("function") or {}).get("name", "")
            if not name:
                # the assistant message is already appended — leave a tool-role
                # reply so the next request is protocol-valid instead of 400
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id") or f"call_{idx}",
                     "content": "(tool call rejected: missing function name)"}
                )
                continue
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError as e:
                result = (f"(tool call rejected: could not parse arguments as "
                          f"JSON — {e}. Re-issue the tool call with valid JSON.)")
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id") or f"call_{idx}",
                     "content": result}
                )
                continue
            parsed.append((idx, tc, name, args))

        # Parallel execution is opt-in: the system prompt tells the brain to
        # only batch tool calls when the user explicitly asked to run things
        # at the same time. "parallel_tools": false in config disables it
        # entirely. Batches containing dangerous commands or sensitive
        # side-effecting tools fall back to sequential so the confirm prompt
        # stays on the main thread (never prompt from worker threads).
        # NOTE: confirm=None means DENY (see tools.code_edit/email_send/
        # post_social/run_command). Parallel workers get None, so any
        # sensitive tool left in a parallel batch would be denied rather
        # than auto-allowed — the fallback below avoids that denial by
        # forcing sequential execution where a real prompt is possible.
        _SENSITIVE_PARALLEL = frozenset({
            "code_edit", "email_send", "post_social",
            "integrate_tool", "self_update", "auto_upgrade",
            "self_repair", "hot_reload", "publish_site",
            "update_system",
        })
        parallel = (len(parsed) > 1
                    and cfg.get("parallel_tools", True)
                    and not any(((name in ("run_command", "bash") and _dangerous(args.get("command", "")))
                                 or (name in _SENSITIVE_PARALLEL))
                                for _, _, name, args in parsed))

        def run_one(idx, tc, name, args):
            cid = tc.get("id") or f"call_{idx}"
            # opencode-style live output: while bash/run_command executes, its
            # output tail streams to the UI as "progress" tool events
            if on_tool is not None and name in ("run_command", "bash"):
                def _prog(tail, _name=name, _cid=cid, _args=args):
                    with event_lock:
                        _fire_on_tool(on_tool, _name, "progress", None, _args,
                                      call_id=_cid, tail=tail)
                from .tools import set_progress_sink
                set_progress_sink(_prog)
            # start events stay silent — the tool block prints on completion
            if on_tool is not None:
                with event_lock:
                    _fire_on_tool(on_tool, name, "start", None, args, call_id=cid)
            t0 = time.time()
            if name.startswith("mcp__"):
                try:
                    result = mcp.call_tool(name, args)
                except Exception as e:
                    result = f"(MCP error: {e})"
            else:
                try:
                    # Parallel workers pass confirm=None (= DENY for
                    # confirm-gated tools) to avoid prompting off-thread.
                    result = tool_impl(name, args, confirm if not parallel else None)
                except Exception as e:
                    result = (f"(tool '{name}' failed: {type(e).__name__}: {e}. "
                              f"Read the error, adapt, and try again or use a "
                              f"neighboring tool.)")
                finally:
                    if on_tool is not None and name in ("run_command", "bash"):
                        from .tools import set_progress_sink
                        set_progress_sink(None)
            secs = time.time() - t0
            result_s = str(result)
            is_err = _is_tool_error(name, result_s)
            # Always drain this thread's pending diff, even with no on_tool
            # (scheduled jobs / subagents / self-repair): leaving it keyed
            # by a dead thread ident leaked a full diff per edit in the
            # 24/7 daemon, and a recycled ident could hand a stale diff to
            # an unrelated later tool call.
            from .tools import pop_diff
            _diff = pop_diff()
            if on_tool is not None:
                with event_lock:
                    _fire_on_tool(on_tool, name, "error" if is_err else "ok", secs,
                                  args, diff=_diff, call_id=cid,
                                  result=(result_s[:4000]
                                          if name in ("read_file", "read", "grep") else None))
            elif not quiet:
                with event_lock:
                    print("  " + _tool_line(name, _arg_summary(args), secs, err=is_err))
            return {"role": "tool", "tool_call_id": tc.get("id") or f"call_{idx}",
                    "content": str(result)}

        if parallel:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, len(parsed))) as pool:
                futs = [pool.submit(run_one, *p) for p in parsed]
                for (_, tc, _, _), fut in zip(parsed, futs):
                    try:
                        messages.append(fut.result())
                    except Exception as e:  # never let a worker kill the turn
                        _log_error(f"parallel tool '{tc.get('function', {}).get('name')}' worker: {e}")
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.get("id") or "call_?",
                             "content": f"(tool failed: {e})"})
        else:
            for p in parsed:
                messages.append(run_one(*p))
    return "(gave up after too many tool rounds)"

# ---- original buddy.py lines 3391-3392 --------------------------------


# ---- original buddy.py lines 3393-3401 --------------------------------
# --- API-key auto-detection ------------------------------------------------
# Paste a key into chat and buddy offers to store it and switch providers —
# no need to remember `secret set` / `model add` syntax.

_API_KEY_PATTERNS = [
    # order matters: specific prefixes before the generic sk- catch-all
    ("Anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("OpenRouter", re.compile(r"\bsk-or-[A-Za-z0-9_\-]{20,}\b")),
    ("OpenAI", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("Gemini", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Groq", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("GitHub", re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b")),
]

_PROVIDER_DEFAULTS = {
    "Gemini": {"api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-3.8-flash"},
    "OpenAI": {"api_base": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "OpenRouter": {"api_base": "https://openrouter.ai/api/v1", "model": None},
    "Groq": {"api_base": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile"},
}


def detect_api_key(text: str):
    """Return (provider, key) if the text contains a recognizable API key."""
    if not text:
        return None
    for provider, rx in _API_KEY_PATTERNS:
        m = rx.search(text)
        if m:
            return provider, m.group(0)
    return None


def maybe_setup_api_key(text: str, cfg: dict, confirm=None) -> str | None:
    """If the message carries an API key, offer to store + activate it.
    Returns a reply string (turn handled) or None (continue normally)."""
    hit = detect_api_key(text)
    if not hit:
        return None
    provider, key = hit
    if confirm is None or not confirm(
            f"that looks like a {provider} API key (…{key[-6:]}) — store it and switch to {provider}?"):
        # declined or no interactive confirm: if the message is nothing but
        # the key, answer with the how-to instead of posting the key to the LLM
        if text.strip() == key:
            return (f"that looks like a {provider} API key. Say 'yes' to store it, "
                    f"or run: python3 buddy.py secret set api_key")
        return None
    return _store_provider_key(provider, key, cfg)


def _store_provider_key(provider: str, key: str, cfg: dict) -> str:
    """Store the key and switch config to the provider's defaults."""
    where = secret_set("api_key", key)
    for k, v in _PROVIDER_DEFAULTS.get(provider, {}).items():
        if v:
            cfg[k] = v
    cfg["brain"] = "api"
    _save_config(cfg)
    try:  # don't let maybe_reload's stale stamp view undo this switch
        from .config import _stamp_config
        _stamp_config(cfg)
    except Exception:
        pass
    model = cfg.get("model") or "its default model"
    return (f"stored your {provider} key ({where}) and switched to {provider} "
            f"({model}). Say anything to test it.")


def run_agent(cfg: dict, mcp: MCPManager, user_input: str, quiet: bool = False,              confirm=None, cancel: threading.Event | None = None) -> str:
    setup = maybe_setup_api_key(user_input, cfg, confirm=confirm)
    if setup is not None:
        if not quiet:
            print(setup)
        return setup
    # a detected-but-declined key must not reach the LLM (and from there, the
    # model's own logs): redact it out of the message before anything else
    hit = detect_api_key(user_input)
    if hit:
        user_input = user_input.replace(hit[1], f"…redacted {hit[0]} key…")
    messages = [{"role": "system", "content": _system_prompt()},
                {"role": "user", "content": user_input}]
    try:
        return run_messages(cfg, mcp, messages, quiet, confirm, cancel=cancel)
    except Exception:
        _log_error(traceback.format_exc())  # leave evidence for self-repair
        raise

