"""buddy_core.commands — the chat() REPL, slash-command dispatch, first-run/doctor/setup CLI helpers.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
import ast
from .agent import _approx_chars, _exit_pass, run_messages
from .config import api_key_for, CONFIG, DEFAULT_API_BASE, DEFAULT_MODEL, HOME, INBOX, MEMORY, PORT, SECRETS, SKILLS, VECTORS, WORKSPACE, _needs_key, _save_config, _stamp_config, _store_key, api_key, load_config, maybe_reload, secret_get, secret_set
from .mcp import MCPManager
from .memory import recall_memory
from .prompts import _system_prompt
from .sched import Scheduler, TelegramBot, Watcher, daemon_running, list_jobs
from .skills import STARTER_SKILLS, _log_wish, evolve_pass, list_skills, read_playbook, save_skill, self_repair, skills_index
from .tools import BASE_TOOLS, BASE_TOOL_SPECS, probe_system, system_summary
from .tui import _Activity, _CHAT_COMMANDS, _arrow_confirm, _arrow_pick_model, _fs_chat, _fullscreen_ok, _repl_input, _save_history, _setup_readline, _status_line, md_print, ui_banner
from . import tui as _tui  # _FS is a mutable module global — access live via _tui._FS
from .util import TTS_VOICES, _arg_summary, _tool_line, mic_record, speak, ui_accent, ui_amber, ui_bold, ui_dim, ui_err, ui_ok

import json
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
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---- original buddy.py lines 27-27 ------------------------------------


def _print_diff_block(name: str, diff: str, args: dict | None,
                      status: str) -> None:
    """opencode-style edit part: a ┃ bar column with a file header
    (+A -D counts), hunk headers, and dual old/new line numbers
    (- red, + green, context dim). Capped so a huge diff can't flood
    the scrollback; the footer reports the full counts."""
    path = (args or {}).get("path", "") or (args or {}).get("target", "")
    base = os.path.basename(path) if path else "(file)"
    verb = "Edit" if name in ("edit_file", "code_edit") else "Write"
    rows = (diff or "").splitlines()
    adds = sum(1 for ln in rows if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in rows if ln.startswith("-") and not ln.startswith("---"))
    print(ui_dim("  ┃"))
    print(ui_dim("  ┃ ") + ("× " if status == "error" else ui_accent("◐ "))
          + ui_bold(f"{verb} {base}")
          + ui_dim(f"  (+{adds} -{dels})"))
    if path:
        print(ui_dim(f"  ┃ {path}"))
    print(ui_dim("  ┃"))
    if not rows or not (adds or dels):
        print(ui_dim("  ┃ (no changes)"))
        print(ui_dim("  ┃"))
        return
    old_no = new_no = 0
    shown = 0
    total = len(rows)
    for ln in rows:
        if ln.startswith(("---", "+++")):
            continue
        if ln.startswith("@@"):
            m_old = re.match(r"@@ -(\d+)", ln)
            m_new = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", ln)
            if m_old:
                old_no = int(m_old.group(1))
            if m_new:
                new_no = int(m_new.group(1))
            # opencode shows the hunk range dimmed above each change block
            print(ui_dim(f"  ┃ … {ln}"))
            continue
        if shown >= 80:
            print(ui_dim(f"  ┃ … {total - shown} more rows (+{adds} -{dels} total)"))
            break
        text = ln[1:] if ln[:1] in ("+", "-", " ") else ln
        if len(text) > 160:  # keep wide diffs from exploding the viewport wrap
            text = text[:157] + "…"
        if ln.startswith("+"):
            print(ui_dim(f"  ┃ {'':>4} {new_no:>4} ") + ui_ok("+ " + text))
            new_no += 1
        elif ln.startswith("-"):
            print(ui_dim(f"  ┃ {old_no:>4} {'':>4} ") + ui_err("- " + text))
            old_no += 1
        elif ln.startswith(" "):
            print(ui_dim(f"  ┃ {old_no:>4} {new_no:>4}   {text}"))
            new_no += 1
            old_no += 1
        else:
            print(ui_dim("  ┃ ") + text)
        shown += 1
    print(ui_dim("  ┃"))


def _print_todo_block(todos, status: str) -> None:
    """opencode todo panel: ┃ bar column with checkbox rows — ✓ green done,
    • amber in-progress, blank pending; in-progress text amber."""
    print(ui_dim("  ┃"))
    print(ui_dim("  ┃ ") + ("× " if status == "error" else ui_accent("» "))
          + ui_bold("Todos"))
    print(ui_dim("  ┃"))
    for t in todos[:20]:
        content = str(t.get("content", ""))[:100]
        st = t.get("status", "pending")
        if st == "completed":
            print(ui_dim("  ┃ ") + ui_dim("[") + ui_ok("✓") + ui_dim("] ")
                  + ui_dim(content))
        elif st == "in_progress":
            print(ui_dim("  ┃ [") + ui_amber("•") + ui_dim("] ")
                  + ui_amber(content))
        else:
            print(ui_dim(f"  ┃ [ ] {content}"))
    print(ui_dim("  ┃"))


def _bad_model_id(m):
    """Reject empty / path-like / whitespace model ids before they poison config."""
    return (not m) or m.strip() != m or m.startswith("/") or " " in m


# ---- provider catalog: /model shows these with their known models ----
# Google only — other providers were removed by request. Users add them
# back anytime with: /model add <name> <base_url> <model_id> (stored in
# cfg["models"] as "integrated" entries, listed first and never touched
# here). The ACTIVE endpoint is also queried live via GET /models so the
# list reflects what your key can actually use right now (marked "live").
# Unreachable endpoints fall back to their preset list silently.
MODEL_PROVIDERS: dict[str, dict] = {
    "google": {
        "base": DEFAULT_API_BASE,
        # Curated: stable, widely used Gemini models only (newest first).
        # "*-latest" aliases always track the newest stable release, so they
        # are the safest defaults. Live-only extras from GET /models are
        # appended after these, so anything else the key can use stays
        # reachable without cluttering the top of the list.
        "models": [
            "gemini-flash-latest",   # newest stable flash (auto-tracked)
            "gemini-3.8-flash",      # newest pinned flash
            "gemini-3.5-flash",      # previous gen, very stable
            "gemini-2.5-flash",      # legacy workhorse
            "gemini-pro-latest",     # newest stable pro (auto-tracked)
            "gemini-3.1-pro-preview",  # strongest pro reasoning
            "gemini-2.5-pro",        # stable pro
        ],
        "note": "Google AI Studio key (aistudio.google.com/apikey)",
    },
}


def _fetch_endpoint_models(cfg: dict, base: str, timeout: int = 5) -> list[str] | None:
    """Live model ids from GET {base}/models (OpenAI-compatible).

    Returns None when unreachable/unauthorized so callers fall back to
    presets — listing must never hang or fail."""
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {api_key_for(cfg, cfg.get('model', ''), base)}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        ids = [str(m.get("id", "")).strip() for m in data.get("data", [])
               if isinstance(m, dict) and str(m.get("id", "")).strip()]
        return ids or None
    except Exception:
        return None


# Live IDs that aren't chat models (Google returns TTS/audio/embedding,
# video/music variants alongside chat ones) — filtered from selection.
_NON_CHAT_HINTS = ("tts", "audio", "embedding", "image", "veo-", "live",
                   "transcribe", "translate", "lyria", "robotics",
                   "computer-use", "deep-research", "banana", "aqa")

# Cap per-provider live rows so one endpoint can't flood the catalog.
_MAX_LIVE_ROWS = 15


def _norm_live_id(mid: str) -> str:
    """Strip provider prefixes so live ids match configured model ids
    (Google returns 'models/gemini-2.5-flash' for 'gemini-2.5-flash')."""
    mid = mid.strip()
    if mid.startswith("models/"):
        mid = mid[len("models/"):]
    return mid


def _is_chat_model(mid: str) -> bool:
    low = mid.lower()
    return not any(h in low for h in _NON_CHAT_HINTS)


def _model_entries(cfg: dict) -> list[dict]:
    """Ordered selectable entries: {n, label, provider, base, model,
    source, active}.

    Integrated (user-added) models first, then provider presets. The
    active endpoint is queried live and its models are marked "live";
    exact (base, model) duplicates collapse into one entry."""
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    active = ((cfg.get("api_base") or "").rstrip("/"), cfg.get("model", ""))

    def _add(label: str, provider: str, base: str, model: str,
             source: str) -> None:
        key = (base.rstrip("/"), model)
        if key in seen:
            return
        seen.add(key)
        entries.append({"n": len(entries) + 1, "label": label,
                        "provider": provider, "base": key[0], "model": model,
                        "source": source,
                        "active": key == (active[0], active[1])})

    for name, info in (cfg.get("models") or {}).items():
        if isinstance(info, dict):
            _add(name, "ADDED", info.get("api_base", ""),
                 info.get("model", name), "integrated")
    live_base = active[0]
    live_raw = _fetch_endpoint_models(cfg, live_base) if live_base else None
    live_all = [_norm_live_id(m) for m in (live_raw or [])]
    live_all = [m for m in live_all if m and _is_chat_model(m)]
    live_set = set(live_all)
    for pname, p in MODEL_PROVIDERS.items():
        is_live = p["base"].rstrip("/") == live_base
        # Curated presets lead (newest/most popular first), each tagged
        # "live" when the key confirms it; then live-only extras so
        # anything else reachable stays pickable without clutter.
        for mid in p["models"]:
            _add(f"{pname} · {mid}", pname, p["base"], mid,
                 "live" if is_live and mid in live_set else "preset")
        if is_live:
            for mid in _extra_live_models(live_all, p["models"])[:_MAX_LIVE_ROWS]:
                _add(f"{pname} · {mid}", pname, p["base"], mid, "live")
    return entries


def _extra_live_models(live_all: list[str], presets: list[str]) -> list[str]:
    """Live models worth showing below the curated presets: Gemini chat
    models only (no gemma/omni/lite variants), stable releases before
    previews, newest-looking version numbers first."""
    def _rank(m: str) -> tuple:
        stable = "preview" not in m
        ver = [int(x) for x in re.findall(r"\d+", m)] or [0]
        return (0 if stable else 1, tuple(-v for v in ver), m)

    extras = [m for m in live_all
              if m not in presets
              and m.startswith("gemini-")
              and "-lite" not in m
              and not m.startswith("gemini-omni")]
    return sorted(extras, key=_rank)


def _group_model_entries(entries: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group entries by provider for the arrow-key picker display:
    [(GROUP_TITLE, [entries...])], integrated ("ADDED") group first."""
    groups: list[tuple[str, list[dict]]] = []
    index: dict[str, list[dict]] = {}
    for e in entries:
        title = "ADDED BY YOU" if e.get("provider") == "ADDED" else str(
            e.get("provider", "")).upper()
        if title not in index:
            index[title] = []
            groups.append((title, index[title]))
        index[title].append(e)
    return groups


def _model_catalog_text(cfg: dict) -> str:
    """The /model list: current setup + numbered providers/models to pick."""
    entries = _model_entries(cfg)
    lines = [f"current: {cfg.get('model', DEFAULT_MODEL)}",
             f"endpoint: {cfg.get('api_base', DEFAULT_API_BASE)} "
             f"(brain: {cfg.get('brain', 'api')})",
             "providers & models — select with /model <number|name|id>:"]
    for e in entries:
        mark = "●" if e["active"] else " "
        lines.append(f"  {mark} {e['n']:>2}  {e['label']}  [{e['source']}]")
    lines.append("keys: single shared key (`python3 buddy.py key set`) — "
                 "set the key for a provider before switching to it")
    lines.append("manage: /model add <name> <base_url> <model_id>  ·  "
                 "/model remove <name>  ·  /model <exact-id> switches directly")
    return "\n".join(lines)


def _apply_model_entry(cfg: dict, entry: dict) -> str:
    cfg["api_base"] = entry["base"]
    cfg["model"] = entry["model"]
    _save_config(cfg)
    return f"model switched → {entry['model']} ({entry['base']})"


def _find_model_entry(cfg: dict, target: str) -> "dict | None":
    """Resolve a number, integrated name, catalog label, or model id to
    a catalog entry. Numbers that match nothing return None (caller
    reports them); everything else falls through to raw-id handling."""
    if target.isdigit():
        for e in _model_entries(cfg):
            if e["n"] == int(target):
                return e
        return None
    for e in _model_entries(cfg):
        if target in (e["label"], e["model"]) or e["label"].endswith(" · " + target):
            return e
    return None



# ---- original buddy.py lines 29-29 ------------------------------------


# ---- original buddy.py lines 2581-2584 --------------------------------


# --- desktop launcher ---------------------------------------------------------

# ---- original buddy.py lines 2585-2599 --------------------------------
def install_desktop() -> None:
    me = Path(__file__).resolve()
    term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") \
        or shutil.which("konsole") or shutil.which("xfce4-terminal") or "xterm"
    d = Path.home() / ".local/share/applications"
    d.mkdir(parents=True, exist_ok=True)
    (d / "buddy.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=buddy\n"
        "Comment=your personal assistant\n"
        f"Exec={term} -e \"{sys.executable} {me}\"\n"
        "Icon=utilities-terminal\n"
        "Categories=Utility;\n")
    print(f"Installed {d / 'buddy.desktop'} — buddy appears in your app menu.")

# ---- original buddy.py lines 2733-2734 --------------------------------


_SITE_RE = r"(?:https?://[^\s]+|(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}|localhost)(?::\d+)?(?:/[^\s]*)?)"


def parse_intent(text: str) -> tuple[str, str] | None:
    """Deterministic natural-language intents, handled without the LLM.

    Returns (kind, arg) or None. Conservative: only short, unambiguous
    requests match — everything else falls through to the model.
    Pure function (no I/O), unit-tested.
    """
    low = (text or "").strip().lower()
    if not low or len(low.split()) > 25:
        return None
    if re.search(r"\bupdate\b[^.?!]{0,30}\b(yourself|buddy|your\s*(code|self|software))\b", low):
        return ("upgrade_self", "")
    if re.search(r"\b(updat|upgrad)(e|ing)\b[^.?!]{0,30}\b(this\s+)?(pc|computer|device|machine|laptop|system)\b", low):
        return ("update_pc", "")
    if "stop" in low.split() and "web" in low:
        return ("web_stop", "")
    if (re.search(r"\b(start|open|launch|show|where)\b[^.?!]{0,30}\bweb\b", low)
            or re.search(r"\bweb\s*(ui|interface|dashboard|page)\b", low)):
        return ("web_status", "")
    m = re.search(r"\b(check|open|look at|visit|browse|go to)\s+(" + _SITE_RE + r")", low)
    if m and (low.startswith(("check", "open", "look", "visit", "browse", "go to"))
              or len(low.split()) <= 12):
        return ("check_site", m.group(2))
    m = re.match(r"(run|execute|exec)\s+(.+)$", low)
    if m and not set(m.group(2).split()) & {
            "evolve", "upgrade", "fix", "repair", "schedule", "remember",
            "check", "update", "open", "start", "stop", "web", "model",
            "service", "doctor"}:
        return ("shell", m.group(2).strip())
    m = re.match(r"(remember|note|memorize)\s+(that\s+)?(.+)$", low)
    if m:
        return ("remember", m.group(3).strip())
    if re.search(r"what do you remember|what did i tell you|show( me)? memory|"
                 r"list memories|recall memory", low):
        return ("recall", "")
    # Model switching ("buddy change model to gemini 3.7 flash").
    # Explicit "model" mention always counts; bare "switch/change to X"
    # only when X looks like a model id (digit or provider/model shape)
    # so execution modes ("switch to plan") never match.
    m = re.search(r"\b(change|switch|set|use)\b[^.?!]{0,20}\bmodels?\b\s*(to\s+)?(.+)$", low)
    if m and m.group(3) and m.group(3).strip().rstrip("?!."):
        return ("model", m.group(3).strip().rstrip("?!."))
    m = re.search(r"\b(switch|change)\s+to\s+(.+)$", low)
    if m:
        _mt = m.group(2).strip().rstrip("?!.")
        if _mt and (re.search(r"\d", _mt) or "/" in _mt):
            return ("model", _mt)
    m = re.match(r"(what is|what's|calculate|calc|compute|how much is)\s+(.+)$", low)
    if m and _safe_expr(m.group(2)):
        return ("calc", m.group(2).strip())
    if re.fullmatch(r"[\d\s+\-*/().%^]+", low) and re.search(r"\d", low) \
            and re.search(r"[+\-*/%^]", low) and _safe_expr(low):
        return ("calc", low.strip())
    if re.search(r"what time|current time|today'?s date|what day|the date|what's the time", low):
        return ("time", "")
    if low in ("git status", "git diff", "git log", "git branch", "git show", "git st"):
        return ("shell", low)
    if low in ("uptime", "load", "cpu load", "cpu"):
        return ("shell", "uptime")
    if low in ("df", "disk", "disk space", "disk usage"):
        return ("shell", "df -h")
    if low in ("free", "ram", "free memory", "memory usage"):
        return ("shell", "free -m" if shutil.which("free") else "vm_stat")
    if low in ("sysinfo", "system info", "host info", "machine info"):
        return ("sysinfo", "")
    if low in ("doctor", "diagnose", "health check"):
        return ("doctor", "")
    if low in ("ls", "dir", "list files"):
        return ("list_dir", ".")
    if low.startswith("ls ") or low.startswith("dir "):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            return ("list_dir", parts[1].strip())
    if low.startswith("cat ") or low.startswith("read ") or low.startswith("view ") or low.startswith("show "):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            return ("read_file", parts[1].strip())
    if low in ("who are you", "are you self aware", "are you self-aware", "self aware", "self-aware", "introspect", "self introspect", "telemetry", "tell me about yourself", "what are you"):
        return ("introspect", "")
    return None


def _safe_expr(expr: str) -> bool:
    """True if expr is plain arithmetic (evaluable, no names/calls)."""
    if re.search(r"[a-zA-Z_]", expr):
        return False
    try:
        tree = ast.parse(expr.replace("^", "**"), mode="eval")
    except (SyntaxError, ValueError):  # ValueError: >4300-digit int literal (py3.11)
        return False
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
               ast.Pow, ast.UAdd, ast.USub)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return False
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            return False
    return True



# --- local models (manual) --------------------------------------------------
# Buddy can fail over to any manually registered model on rate/quota errors
# (see _failover_model): add one with `python3 buddy.py model add <name>
# <base> <model>` (e.g. a local ollama server) and it joins the failover
# candidates automatically. There is no bundled installer.


def _confirm_setup(question: str) -> bool:
    try:
        return input(f"{question}? [y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _run_intent(intent: tuple[str, str], cfg: dict, confirm) -> str | None:
    """Execute a parsed intent. Returns summary text, or None to fall through."""
    from .tools import update_system, web_fetch
    kind, arg = intent
    if kind == "web_status":
        return _web_status(cfg)
    if kind == "web_stop":
        if confirm("stop the buddy daemon (web UI + background jobs)?"):
            from .sched import cmd_service
            cmd_service(["stop"])
            return "Daemon stopped — web UI offline. Restart with `python3 buddy.py service start`."
        return "(ok — daemon left running.)"
    if kind == "upgrade_self":
        from .tools import self_update
        return self_update("", confirm=confirm)
    if kind == "update_pc":
        summary = update_system("check")
        # Only escalate to a full OS upgrade when the check actually
        # counted pending packages ("0 packages pending" / "up to date"
        # means there is nothing to install — don't ask to upgrade).
        m = re.match(r"(\d+) package\(s\) pending", summary)
        if m and int(m.group(1)) > 0:
            return summary + "\n\n" + update_system("upgrade", confirm)
        return summary
    if kind == "check_site":
        return web_fetch(arg)
    if kind == "shell":
        from .tools import run_command
        return run_command(arg, 120, confirm)
    if kind == "remember":
        from .memory import remember as _remember
        return _remember(arg)
    if kind == "recall":
        return recall_memory()
    if kind == "model":
        # Natural-language switching ("change model to ..."): reuse the
        # exact /model resolution (number/name/label/id, dash fix).
        return run_slash_command(cfg, ("/model " + arg).strip(), confirm=confirm)
    if kind == "calc":
        try:
            tree = ast.parse(arg.replace("^", "**"), mode="eval")
            for node in ast.walk(tree):  # cap pow before eval OOMs the daemon
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
                    if (isinstance(node.right, ast.Constant)
                            and isinstance(node.right.value, (int, float))
                            and abs(node.right.value) > 1000):
                        raise ValueError("exponent too large")
                    # nested pow ("9**9**9") sneaks past the constant cap and
                    # eval materializes a multi-GB integer — reject it outright
                    if any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow)
                           for n in ast.walk(node.right)):
                        raise ValueError("nested exponent too large")
            val = eval(compile(tree, "<calc>", "eval"),  # noqa: S307
                       {"__builtins__": {}}, {})
            if isinstance(val, int) and val.bit_length() > 14000:
                return "(number too large to display)"
            return f"= {val}"
        except Exception as e:
            return None  # shouldn't happen (_safe_expr passed) — fall through
    if kind == "time":
        from datetime import datetime as _dt
        return _dt.now().strftime("%A, %B %d, %Y — %H:%M")
    if kind == "read_file":
        from .tools import read_file
        return read_file(arg)
    if kind == "list_dir":
        from .tools import list_dir
        return list_dir(arg or ".")
    if kind == "sysinfo":
        from .tools import probe_system, system_summary
        return system_summary(probe_system())
    if kind == "doctor":
        return doctor()
    if kind == "introspect":
        from .tools import introspect
        return introspect("all")
    return None


def _web_status(cfg: dict, mask_token: bool = False) -> str:
    """Web UI address + daemon state for chat (/web) and the brain."""
    if cfg.get("web_auth") is False:
        url = f"http://127.0.0.1:{PORT}/"
    elif mask_token:
        url = f"http://127.0.0.1:{PORT}/#<token> — run /web yourself for the URL"
    else:
        from .config import web_token as _wt
        url = f"http://127.0.0.1:{PORT}/#{_wt(cfg)}"
    try:
        running = daemon_running()
    except Exception:
        running = False
    state = "RUNNING — open the URL in your browser" if running else (
        "STOPPED — start it with `python3 buddy.py service start` (or /service)")
    return f"web UI: {url}\ndaemon: {state}"


# ---- original buddy.py lines 2735-2771 --------------------------------
def run_slash_command(cfg: dict, raw: str, confirm=None) -> str:
    """Let the brain run the safe subset of REPL commands on the user's behalf.
    confirm=None means unattended (tool path): model-config mutations are denied
    — a prompt-injected brain must not be able to point buddy at a new API
    endpoint, where api_key_for's fallback would hand it the shared key."""
    parts = raw.split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/status":
        # Quick observability view: one glance at every subsystem. Safe on
        # the unattended path (read-only, no network, ~ms).
        import shutil as _shutil
        rows = [f"brain={cfg.get('brain', 'api')} · model={cfg.get('model', '?')} · "
                f"mode={cfg.get('mode', 'chat')}"
                + (" · YOLO" if cfg.get("yolo") else "")]
        mem_lines = len(MEMORY.read_text().splitlines()) if MEMORY.exists() else 0
        rows.append(f"memory: {mem_lines} lines")
        try:
            from .sched import load_jobs, load_watchers
            jobs = load_jobs()
            watchers = load_watchers()
            rows.append(f"jobs: {len(jobs)} · watchers: {len(watchers)}")
        except Exception:
            rows.append("jobs/watchers: unavailable (scheduler not loaded)")
        inbox_n = 0
        if INBOX.exists():
            try:
                inbox_n = sum(1 for line in INBOX.read_text().splitlines() if line.strip())
            except OSError:
                pass
        rows.append(f"inbox: {inbox_n} entries")
        err = HOME / "errors.log"
        if err.exists() and err.stat().st_size > 0:
            try:
                last = err.read_text(errors="replace").strip().splitlines()
                rows.append(f"errors.log: {err.stat().st_size} bytes — last: {last[-1][:100]}")
            except OSError:
                pass
        else:
            rows.append("errors.log: clean")
        try:
            du = _shutil.disk_usage(Path.home())
            free_gb = du.free / 2**30
            rows.append(f"disk: {free_gb:.1f} GB free"
                        + (" ⚠ low" if free_gb < 1 else ""))
        except Exception:
            pass
        return "\n".join(rows)
    if cmd == "/memory":
        return recall_memory()
    if cmd == "/inbox":
        return INBOX.read_text() if INBOX.exists() else "(inbox empty)"
    if cmd == "/jobs":
        return list_jobs()
    if cmd == "/skills":
        return skills_index() or "(no skills yet)"
    if cmd == "/tools":
        from .tools import _CUSTOM_TOOLS, load_custom_tools
        load_custom_tools()
        base_names = [n for n, *_ in BASE_TOOLS]
        custom_names = list(_CUSTOM_TOOLS.keys())
        out = ["built-in tools: " + ", ".join(base_names)]
        if custom_names:
            out.append("custom tools: " + ", ".join(custom_names))
        return "\n".join(out)
    if cmd == "/model":
        models = cfg.get("models") or {}
        parts = arg.split()

        def _model_guard(action: str) -> str | None:
            """Gate config-mutating /model ops: interactive users pass a real
            confirm; the unattended tool path (confirm=None) is denied."""
            if confirm is None:
                return (f"(model config changes need a human — run `{action}` "
                        "as a chat command yourself)")
            return None if confirm(action) else "(cancelled by user)"

        if not arg or arg == "list":
            return _model_catalog_text(cfg)
        if parts[0] == "add" and len(parts) >= 4:
            m_name, m_base, m_id = parts[1], parts[2], parts[3]
            if _bad_model_id(m_name) or _bad_model_id(m_id):
                return f"(rejected {m_id!r}: not a valid model id)"
            if not (m_base.startswith("http://") or m_base.startswith("https://")):
                return f"(rejected {m_base!r}: api_base must start with http:// or https://)"
            guarded = _model_guard(f"/model add {m_name} {m_base} {m_id}")
            if guarded:
                return guarded
            if "models" not in cfg or not isinstance(cfg["models"], dict):
                cfg["models"] = {}
            cfg["models"][m_name] = {"api_base": m_base, "model": m_id}
            _save_config(cfg)
            out = f"model '{m_name}' integrated ({m_base} / {m_id})"
            # Optional 5th arg / --key: that provider's own key, so buddy
            # never sends this provider's key to a third-party host.
            key = ""
            _kf = next((f for f in ("--key", "-k") if f in parts), None)
            if len(parts) >= 5 and parts[4] not in ("--key", "-k"):
                key = parts[4]
            elif _kf:
                i = parts.index(_kf)
                key = parts[i + 1] if i + 1 < len(parts) else ""
                if not key:
                    return f"(error: {_kf} given without a value)"
            if key:
                where = secret_set(f"api_key_{m_name}", key)
                out += f"\n{where}"
            else:
                out += (f"\n(no key given — buddy would reuse the shared key "
                        f"for {m_base}; add one with: "
                        f"`python3 buddy.py secret set api_key_{m_name}`)")
            return out
        if parts[0] == "remove" and len(parts) >= 2:
            name = parts[1]
            guarded = _model_guard(f"/model remove {name}")
            if guarded:
                return guarded
            if name in models and isinstance(models, dict):
                del cfg["models"][name]
                _save_config(cfg)
                return f"model '{name}' removed."
            return f"model '{name}' not found in integrated models."
        if parts[0] == "set" and len(parts) < 2:
            return ("switch with: /model <number|name|id>  ·  "
                    "integrate with: /model add <name> <base_url> <model_id>")
        target = parts[1] if parts[0] == "set" and len(parts) >= 2 else arg
        target = target.strip().rstrip("?!.")
        entry = _find_model_entry(cfg, target)
        if entry is not None:
            guarded = _model_guard(f"switch model to {target}")
            if guarded:
                return guarded
            return _apply_model_entry(cfg, entry)
        if target.isdigit():
            return f"(no model #{target} — see /model)"
        if " " in target:
            # Spoken ids ("gemini 3.7 flash") → canonical dashes.
            entry = _find_model_entry(cfg, re.sub(r"\s+", "-", target))
            if entry is not None:
                guarded = _model_guard(f"switch model to {target}")
                if guarded:
                    return guarded
                return _apply_model_entry(cfg, entry)
        if _bad_model_id(target):
            return f"(rejected {target!r}: not a valid model id)"
        guarded = _model_guard(f"switch model to {target}")
        if guarded:
            return guarded
        cfg["model"] = target
        _save_config(cfg)
        return f"model → {target}"
    if cmd == "/acp":
        from .acp import acp_tool, add_acp_agent
        parts = arg.split()
        if not arg or parts[0] == "list":
            return acp_tool(cfg, "list", "")
        if parts[0] == "add" and "--" in parts:
            idx = parts.index("--")
            # Expected: add <name> -- <cmd> [args...]
            if len(parts) < 4 or idx < 2 or idx + 1 >= len(parts):
                return "usage: /acp [list | add <name> -- <cmd> [args...] | <name> <prompt>]"
            agent_name = parts[1]
            if not agent_name or agent_name == "--":
                return "usage: /acp [list | add <name> -- <cmd> [args...] | <name> <prompt>]"
            c = parts[idx + 1]
            a = parts[idx + 2:]
            return add_acp_agent(cfg, agent_name, c, a)
        if parts[0] == "run" and len(parts) >= 3:
            return acp_tool(cfg, parts[1], " ".join(parts[2:]))
        if len(parts) >= 2:
            return acp_tool(cfg, parts[0], " ".join(parts[1:]))
        return "usage: /acp [list | add <name> -- <cmd> [args...] | <name> <prompt>]"
    if cmd == "/service":
        from .sched import service_status_summary
        return service_status_summary(cfg)
    if cmd == "/web":
        if confirm is None:
            # unattended (brain/tool) path: the bearer token must not reach
            # the LLM — a prompt-injected turn could exfiltrate it
            return _web_status(cfg, mask_token=True)
        return _web_status(cfg)
    if cmd in ("/introspect", "/self"):
        from .tools import introspect
        return introspect(arg.strip() if arg else "all")
    if cmd == "/theme":
        from . import theme as _theme
        if not arg:
            return ("theme: " + _theme.CURRENT +
                    " · available: " + ", ".join(_theme.list_themes()) +
                    " · set with /theme <name>")
        if arg not in _theme.list_themes():
            return f"(unknown theme {arg!r} — available: {', '.join(_theme.list_themes())})"
        _theme.apply(arg)
        cfg["theme"] = arg
        _save_config(cfg)
        return f"theme → {arg}"
    if cmd == "/fix":
        # interactive /fix runs with the chat confirm; the tool path (None)
        # stays gated — a prompt-injected brain can't trigger source edits
        return self_repair(cfg, confirm=confirm, issue=arg.strip())
    if cmd in ("/upgrade", "/update"):
        from .tools import self_update
        return self_update(arg.strip())
    if cmd == "/evolve":
        from .agent import _sub_mcp  # lazy: avoids circular import
        return evolve_pass(cfg, _sub_mcp(), confirm=confirm)
    if cmd == "/clear":
        return "(cleared screen)"
    if cmd == "/doctor":
        return doctor()
    if cmd == "/playbook":
        return read_playbook()
    if cmd == "/help":
        return ("commands: " + " ".join(_CHAT_COMMANDS) +
                "\n  • switch models: /model <name|id>" +
                "\n  • inspect health & tools: /introspect" +
                "\n  • manage background services: /service" +
                "\n  • web UI address & daemon state: /web" +
                "\n  • drive ACP coding agents: /acp" +
                "\n  • view tools: /tools")
    if cmd == "/say":
        return f"voice mode: {cfg.get('tts', 'off')} (set with /say off|api|espeak)"
    return ("(command not allowed for me: "
            f"{raw!r} — allowed: /help /status /memory /inbox /jobs /skills /tools /model /acp /service /web /introspect /theme /fix /upgrade /evolve /clear /doctor /playbook /wish /say)")

# ---- original buddy.py lines 3188-3191 --------------------------------


# --- managed OAuth: GitHub device flow (tokens live in buddy's own vault) ----

# ---- original buddy.py lines 3192-3225 --------------------------------
def cmd_oauth(provider: str) -> None:
    cfg = load_config()
    if provider != "github":
        print("supported providers: github")
        sys.exit(1)
    cid = cfg.get("github_client_id")
    if not cid:
        print('First: create an OAuth app at https://github.com/settings/developers')
        print('(check "Enable Device Flow"), then put its client id in ~/.buddy/config.json:')
        print('  "github_client_id": "Iv1.xxxx"')
        sys.exit(1)
    def _post(url, payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    try:
        resp = _post("https://github.com/login/device/code",
                     {"client_id": cid, "scope": "repo,read:user"})
    except urllib.error.HTTPError as e:
        print(f"OAuth failed: {e}")
        sys.exit(1)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"OAuth failed: cannot reach github.com ({e})")
        sys.exit(1)
    if resp.get("error") or not resp.get("verification_uri"):
        print(f"OAuth failed: {resp.get('error_description', resp.get('error', 'no verification_uri in response'))}")
        sys.exit(1)
    print(f"Open {resp['verification_uri']} and enter code: {resp['user_code']}")
    deadline = time.time() + resp.get("expires_in", 900)
    interval = int(resp.get("interval", 5))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            tok = _post("https://github.com/login/oauth/access_token",
                        {"client_id": cid, "device_code": resp["device_code"],
                         "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        except urllib.error.HTTPError as e:
            print(f"OAuth failed: {e}")
            sys.exit(1)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            print(f"OAuth failed: connection lost ({e})")
            sys.exit(1)
        if tok.get("access_token"):
            print(secret_set("github_token", tok["access_token"]))
            return
        if tok.get("error") == "slow_down":
            interval += 5  # RFC 8628: back off or GitHub locks the client
            continue
        if tok.get("error") != "authorization_pending":
            print(f"OAuth failed: {tok.get('error_description', tok.get('error'))}")
            sys.exit(1)
    print("Timed out waiting for authorization.")
    sys.exit(1)

# ---- original buddy.py lines 3226-3227 --------------------------------


# ---- original buddy.py lines 3228-3244 --------------------------------
def cmd_secret(rest: list) -> None:
    if len(rest) >= 2 and rest[0] == "set":
        import getpass
        value = getpass.getpass(f"value for {rest[1]}: ").strip()
        if value:
            print(secret_set(rest[1], value))
        return
    if len(rest) >= 1 and rest[0] == "list":
        names = set()
        try:
            if SECRETS.exists():
                names |= set(json.loads(SECRETS.read_text()).keys())
        except Exception:
            pass
        print("\n".join(sorted(names - {"api_key"})) or "(no extra secrets stored)")
        return
    print("usage: buddy.py secret set <name> | secret list")


def cmd_tools(rest: list) -> None:
    from .tools import BASE_TOOLS, _CUSTOM_TOOLS, load_custom_tools
    from .config import CUSTOM_TOOLS
    load_custom_tools()
    sub = rest[0] if rest else "list"
    if sub == "list":
        print(f"Custom tools directory: {CUSTOM_TOOLS}")
        custom = load_custom_tools(force=True)
        if custom:
            print("\nUser custom tools:")
            for t_spec, _ in _CUSTOM_TOOLS.values():
                fn = t_spec["function"]
                print(f"  • {fn['name']}: {fn.get('description', '')[:80]}")
        else:
            print("\n  (no custom tools installed yet — drop Python files into ~/.buddy/tools/)")
        print("\nBuilt-in tools:")
        for n, d, *_ in BASE_TOOLS:
            print(f"  • {n}: {d[:80]}...")
    elif sub == "add" and len(rest) >= 3:
        name, src_path = rest[1], rest[2]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            print("Error: tool name may only contain letters, digits, '-' and '_'")
            return
        p = Path(src_path)
        if not p.is_file():
            print(f"Error: file not found: {src_path}")
            return
        CUSTOM_TOOLS.mkdir(parents=True, exist_ok=True)
        dest = CUSTOM_TOOLS / f"{name}.py"
        shutil.copyfile(p, dest)
        load_custom_tools(force=True)
        print(f"Tool '{name}' installed in {dest}.")
    elif sub == "remove" and len(rest) >= 2:
        name = rest[1]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            print("Error: tool name may only contain letters, digits, '-' and '_'")
            return
        target = CUSTOM_TOOLS / f"{name}.py"
        if target.exists():
            target.unlink()
            load_custom_tools(force=True)
            print(f"Tool '{name}' removed.")
        else:
            print(f"Custom tool '{name}' not found.")
    else:
        print("usage: python3 buddy.py tools [list | add <name> <file.py> | remove <name>]")


def cmd_model(rest: list[str]) -> None:
    from .config import DEFAULT_API_BASE, load_config
    cfg = load_config()
    models = cfg.get("models") or {}
    sub = rest[0] if rest else "list"
    if sub == "list":
        print(_model_catalog_text(cfg))
        print("\nCommands:")
        print("  python3 buddy.py model <number|name|id>   # pick from the list")
        print("  python3 buddy.py model add <name> <api_base> <model_id>")
        print("  python3 buddy.py model set <name|model_id>")
        print("  python3 buddy.py model remove <name>")
    elif sub.isdigit():
        for e in _model_entries(cfg):
            if e["n"] == int(sub):
                print(_apply_model_entry(cfg, e))
                break
        else:
            print(f"(no model #{sub} — see: python3 buddy.py model list)")
    elif sub == "add" and len(rest) >= 4:
        name, base, model_id = rest[1], rest[2], rest[3]
        if _bad_model_id(name) or _bad_model_id(model_id):
            print(f"(rejected {model_id!r}: not a valid model id)")
            return
        if not (base.startswith("http://") or base.startswith("https://")):
            print(f"(rejected {base!r}: api_base must start with http:// or https://)")
            return
        if "models" not in cfg or not isinstance(cfg["models"], dict):
            cfg["models"] = {}
        cfg["models"][name] = {"api_base": base, "model": model_id}
        _save_config(cfg)
        print(f"Model '{name}' integrated: {model_id} at {base}")
        key = ""
        _kf = next((f for f in ("--key", "-k") if f in rest), None)
        if len(rest) >= 5 and rest[4] not in ("--key", "-k"):
            key = rest[4]
        elif _kf:
            i = rest.index(_kf)
            key = rest[i + 1] if i + 1 < len(rest) else ""
            if not key:
                print(f"  (error: {_kf} given without a value)")
                return
        if key:
            print(secret_set(f"api_key_{name}", key))
        else:
            print(f"  (no key given — buddy would reuse the shared key for "
                  f"{base}; add one with: "
                  f"`python3 buddy.py secret set api_key_{name}`)")
    elif sub == "set" and len(rest) >= 2:
        target = rest[1]
        if target in models:
            m_info = models[target]
            cfg["api_base"] = m_info.get("api_base", cfg.get("api_base", DEFAULT_API_BASE))
            cfg["model"] = m_info.get("model", target)
            _save_config(cfg)
            print(f"Active model switched to '{target}': {cfg['model']} ({cfg['api_base']})")
        else:
            if _bad_model_id(target):
                print(f"(rejected {target!r}: not a valid model id)")
                return
            cfg["model"] = target
            _save_config(cfg)
            print(f"Active model set to {target} (using active endpoint {cfg.get('api_base', DEFAULT_API_BASE)})")
    elif sub == "remove" and len(rest) >= 2:
        name = rest[1]
        if name in models:
            del models[name]
            _save_config(cfg)
            print(f"Model '{name}' removed from integrated models.")
        else:
            print(f"Model '{name}' not found in configured models.")
    else:
        print("usage: python3 buddy.py model [list | add <name> <api_base> <model_id> | set <name|id> | remove <name>]")


def cmd_introspect(rest: list[str]) -> None:
    from .tools import introspect
    focus = rest[0] if rest else "all"
    print(introspect(focus))


def cmd_fix(rest: list[str]) -> None:
    cfg = load_config() if CONFIG.exists() else {}
    issue = " ".join(rest).strip() if rest else ""
    print("Running autonomous bug diagnosis & source code repair...")
    from .skills import self_repair, TRUSTED_CONFIRM
    report = self_repair(cfg, confirm=TRUSTED_CONFIRM, issue=issue)  # direct CLI user
    print(report)


def cmd_upgrade(rest: list[str]) -> None:
    repo = rest[0] if rest else ""
    from .tools import self_update
    print("Checking for upstream upgrades...")
    # Explicit local CLI invocation: user typed the repo themselves.
    print(self_update(repo, confirm=lambda _msg: True))

# ---- original buddy.py lines 4621-4624 --------------------------------


# ==================================================================== CLI ==

# ---- original buddy.py lines 4625-4641 --------------------------------
def _api_check(cfg: dict) -> str:
    """Try GET /models; return status string."""
    if cfg.get("brain", "api") != "api":
        return f"n/a — brain is '{cfg['brain']}' (no API key needed)"
    base = (cfg.get("api_base") or "").rstrip("/")
    if not base:
        return "no api_base configured"
    req = urllib.request.Request(
        base + "/models",
        headers={"Authorization": f"Bearer {api_key_for(cfg, cfg.get('model', ''), base)}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 200:
                return "OK"
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "BAD KEY (auth failed)"
        return f"HTTP {e.code}"
    except Exception as e:
        return f"UNREACHABLE ({e})"

# ---- original buddy.py lines 4642-4643 --------------------------------


# ---- original buddy.py lines 4644-4665 --------------------------------
def auto_install(packages: list[str], info: dict, confirm) -> list[str]:
    """Try to install optional packages via the detected package manager."""
    pm = next((p for p in ("apt", "dnf", "pacman", "zypper", "brew") if info.get("tools", {}).get(p)), None)
    if not pm:
        return []
    if pm == "apt":
        cmd = ["sudo", "-n", "apt-get", "install", "-y"] + packages
    elif pm == "dnf":
        cmd = ["sudo", "-n", "dnf", "install", "-y"] + packages
    elif pm == "pacman":
        cmd = ["sudo", "-n", "pacman", "-S", "--noconfirm"] + packages
    elif pm == "zypper":
        cmd = ["sudo", "-n", "zypper", "-n", "in"] + packages
    else:  # brew: no sudo
        cmd = ["brew", "install"] + packages
    # Binary names that differ from their package names.
    cmd = [ {"wl-copy": "wl-clipboard"}.get(p, p) for p in cmd ]
    print(f"  installing: {', '.join(packages)} (via {pm})")
    try:
        subprocess.run(cmd, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"  (install failed: {e})")
    refreshed = probe_system(force=True)
    return [p for p in packages if refreshed.get("tools", {}).get(p)]

# ---- original buddy.py lines 4666-4667 --------------------------------


# ---- original buddy.py lines 4668-4696 --------------------------------


# ---- original buddy.py lines 4697-4698 --------------------------------


# ---- original buddy.py lines 4699-4768 --------------------------------
def first_run() -> dict:
    """Self-setup: detect machine, configure, secure key, install extras."""
    import getpass

    HOME.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    SKILLS.mkdir(parents=True, exist_ok=True)
    if not any(SKILLS.iterdir()):  # born with a few starter skills
        for name, desc, body in STARTER_SKILLS:
            save_skill(name, desc, body)
    welcome = WORKSPACE / "welcome.txt"
    if not welcome.exists():
        welcome.write_text(
            "This is buddy's workspace — his notes, projects and scratch files live here.\n"
            "Relative file paths in chat resolve into this directory.\n"
        )
    print("First run — setting myself up.\n")

    info = probe_system()
    print(f"Detected: {system_summary(info).splitlines()[0]}")

    missing = [t for t in ("mpv", "espeak-ng", "grim", "wl-copy") if not info.get("tools", {}).get(t)]
    if missing:
        try:
            ans = input(f"Install optional extras ({', '.join(missing)})? [Y/n] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower() in ("", "y", "yes"):
            installed = auto_install(missing, info, None)
            print(f"  installed: {', '.join(installed)}" if installed else "  (none installed — may need sudo or manual install)")

    try:
        base = input(f"API base URL [{DEFAULT_API_BASE}]: ").strip() or DEFAULT_API_BASE
        model = input(f"Model [{DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL
    except EOFError:
        print("\nSetup cancelled — run `python3 buddy.py` to try again anytime.")
        sys.exit(0)
    try:
        brain = input("Brain — API key, sign in with gemini/agy/codex/claude CLI? [api/gemini/agy/codex/claude, default api]: ").strip().lower() or "api"
    except EOFError:
        brain = "api"
    # Merge with any existing config so re-running first-run never drops
    # web_token/models/theme/mode and friends.
    existing: dict = {}
    if CONFIG.exists():
        try:
            _data = json.loads(CONFIG.read_text(encoding="utf-8"))
            if isinstance(_data, dict):
                existing = _data
        except (OSError, ValueError):
            existing = {}
    cfg = {**existing, "api_base": base, "model": model}
    if brain in ("codex", "claude", "gemini", "agy"):
        cfg["brain"] = brain
        key = ""
        print(f"Brain: {brain} (sign-in, no API key). Install/sign in with: "
              + ("npm install -g @openai/codex && codex login --device-auth" if brain == "codex"
                 else f"{brain} auth / login (then install the {brain} CLI if needed)"))
    else:
        key = getpass.getpass("API key (input hidden): ").strip()

    # store key as securely as this machine allows (no-op for empty key)
    where = _store_key(key)
    if where:
        print(f"Key stored in {where}.")

    _save_config(cfg)

    if cfg.get("brain", "api") == "api":
        print("\nChecking API connection...", end=" ", flush=True)
        status = _api_check(cfg)
        print(status)
        if status != "OK":
            print("(chat will still start, but check the key/base if calls fail)")

    print("\nSetup complete. Starting chat...\n")
    return cfg

# ---- original buddy.py lines 4769-4770 --------------------------------


# ---- original buddy.py lines 4771-4791 --------------------------------
def doctor() -> None:
    cfg = load_config()
    checks = []
    checks.append(("config", bool(cfg.get("api_base") and cfg.get("model"))))
    if cfg.get("brain", "api") == "api":
        checks.append(("api key", bool(api_key(cfg))))
        # An added provider without its own key would be sent the main
        # provider's key — worth surfacing rather than discovering via 401.
        try:
            from .config import secret_get as _sg
            _shared = []
            for _n, _i in (cfg.get("models") or {}).items():
                if isinstance(_i, dict) and not _sg(f"api_key_{_n}"):
                    _shared.append(_n)
            if _shared:
                checks.append((f"provider key(s) missing: {', '.join(_shared)}",
                               False))
        except Exception:
            pass
    else:
        import shutil as _sh
        if cfg.get("brain") == "acp":
            agents = cfg.get("acp_agents") or {}
            entry = agents.get(str(cfg.get("acp_brain") or "claude")) or {}
            cmd = str(entry.get("command") or "")
            checks.append((f"ACP brain agent ({cmd or 'not configured'})",
                           bool(cmd) and bool(_sh.which(cmd))))
        else:
            checks.append((f"brain CLI ({cfg['brain']})", bool(_sh.which(cfg["brain"]))))
    info = probe_system()
    tools = info.get("tools", {})
    checks.append(("voice playback (mpv/ffplay)", any(tools.get(t) for t in ("mpv", "ffplay"))))
    checks.append(("offline voice (espeak)", any(tools.get(t) for t in ("espeak-ng", "espeak"))))
    checks.append(("screenshot (grim/scrot/gnome)", any(tools.get(t) for t in ("grim", "scrot", "gnome-screenshot"))))
    checks.append(("clipboard (xclip/wl)", any(tools.get(t) for t in ("xclip", "wl-copy", "wl-paste"))))
    checks.append(("desktop (xdg-open)", bool(tools.get("xclip") or tools.get("scrot") or os.environ.get("DISPLAY"))))
    print("buddy doctor:")
    for name, ok in checks:
        print(f"  [{'ok' if ok else 'MISSING'}] {name}")
    print(f"  [..] API connection: {_api_check(cfg)}")
    print(f"  daemon: {'running' if daemon_running() else 'not running (python3 buddy.py serve)'}")

# ---- original buddy.py lines 4853-4854 --------------------------------


# ---- original buddy.py lines 4855-5173 --------------------------------
def chat() -> None:
    """Enter chat. On a real terminal (stdin AND stdout ttys, curses
    available) buddy takes over the whole screen with the full-screen UI;
    piped input, dumb terminals and BUDDY_TUI=plain get the classic scrollback
    REPL. Both modes run the SAME loop — _chat_loop owns all command dispatch
    and turn logic, so nothing is duplicated; the full-screen driver just
    captures print() into its viewport and answers input() at the prompt."""
    if _fullscreen_ok():
        _fs_chat()
        return
    _chat_loop(None)


def _chat_loop(tui) -> None:
    """The chat REPL — one implementation for both UI modes. `tui` is the
    _FullScreen driver in full-screen mode, or None for the scrollback
    REPL (identical behavior to pre-fullscreen buddy)."""
    global _CFG
    cfg = load_config()
    _stamp_config(cfg)  # baseline so maybe_reload() sees later edits
    _CFG = cfg
    from .theme import apply as _apply_theme
    _apply_theme(cfg.get("theme", "opencode"))  # recolors ui_* + curses chrome
    if not api_key(cfg) and _needs_key(cfg):
        print(ui_amber("⚠ No API key stored — running in offline mode."))
        print(ui_dim("  (Local tasks like !, git, cat, ls, grep, calc, time, sysinfo, and slash commands are active.)"))
        print(ui_dim("  — add an API key anytime with `python3 buddy.py key set` or switch models with `/model`\n"))
    mcp = MCPManager()
    mcp.start_all()
    for name, err in mcp.error.items():
        print(f"(MCP {name} failed to start: {err[:120]})")
    sched = None
    if not daemon_running():
        sched = Scheduler(cfg, mcp, confirm=None)
        sched.start()
        Watcher(cfg).start()          # the daemon runs its own watcher
        if cfg.get("telegram") and secret_get("telegram_token"):
            TelegramBot(cfg).start()  # the daemon runs its own bot (avoid 409 Conflict)
        from .skills import AutoUpgrade, ErrorReaper
        if cfg.get("auto_repair", True):
            ErrorReaper(cfg, mcp).start()
        if cfg.get("auto_upgrade", True):
            AutoUpgrade(cfg, mcp).start()

    # Autonomous startup checks (non-blocking background threads)
    if cfg.get("auto_repair", True):
        def _bg_startup_repair():
            try:
                p = HOME / "errors.log"
                if p.exists() and p.stat().st_size > 0:
                    from .skills import self_repair, TRUSTED_CONFIRM
                    self_repair(cfg, mcp, confirm=TRUSTED_CONFIRM)
            except Exception:
                pass
        threading.Thread(target=_bg_startup_repair, daemon=True).start()

    if cfg.get("auto_upgrade", True):
        def _bg_startup_upgrade():
            try:
                from .tools import self_update
                from .util import deliver as _deliver
                res = self_update()
                if "updated" in res.lower() and "file(s)" in res.lower():
                    _deliver(f"Auto-upgrade successful on startup:\n{res}", title="code upgrade")
            except Exception:
                pass
        threading.Thread(target=_bg_startup_upgrade, daemon=True).start()

    messages: list[dict] = [{"role": "system", "content": _system_prompt()}]
    yolo = bool(cfg.get("yolo"))

    _allowed_prefixes: set[str] = set()  # "always allow" this session

    def confirm(cmd: str) -> bool:
        active_mode = _tui._FS.mode if _tui._FS is not None else cfg.get("mode", "accept-edits")
        if yolo or active_mode == "bypass":
            return True
        prefix = " ".join(cmd.split()[:2])
        if prefix and prefix in _allowed_prefixes:
            return True
        if _tui._FS is not None:
            ans = _tui._FS.ask_confirm(cmd)
        else:
            ans = _arrow_confirm(cmd, active_mode)
            if ans is None:
                # Piped / no-tty: classic typed prompt.
                try:
                    print(ui_dim("  ┃"))
                    print(ui_dim("  ┃ ") + ui_amber("⚠ Permission required") + ui_dim(f" ({active_mode})"))
                    print(ui_dim("  ┃"))
                    for ln in cmd.splitlines():
                        print(ui_dim("  ┃ ") + "$ " + ui_bold(ln))
                    print(ui_dim("  ┃"))
                    raw = input(ui_dim("  ┃   allow? ")
                                + ui_dim("[y]es · [a]lways (this session) · [N]o — "))
                    print(ui_dim("  ┃"))
                except EOFError:
                    return False
                t = raw.strip().lower()
                ans = "always" if t == "a" else ("allow" if t in ("y", "yes") else "decline")
        if ans == "always":
            _allowed_prefixes.add(prefix)
            return True
        return ans == "allow"

    from .util import claim_inbox  # lazy: avoids circular import
    pending = claim_inbox().strip()  # atomic: concurrent chats can't double-read
    if pending:
        print(f"{ui_dim('--- inbox since last session ---')}\n{pending}\n{ui_dim('-------------------------------')}\n")

    where = "daemon handles jobs" if sched is None else "local scheduler"
    if tui is None:
        ui_banner(cfg, where, len(mcp.servers))
        _setup_readline()
    else:
        tui.welcome(cfg, where, len(mcp.servers))
    cancel_event = threading.Event()

    act = _Activity()
    _delta_started: list[bool] = [False]  # mutable box for closure

    _live_out: dict[str, str] = {}  # call_id -> output tail already printed
    _bash_block: set[str] = set()   # call_ids with a live ┃ block on screen

    def on_tool(name: str, status: str, secs, args: dict | None = None,
                diff: str | None = None, call_id: str | None = None,
                tail: str | None = None, result: str | None = None) -> None:
        if status == "start":
            # Any content that streamed before this point was already
            # terminated by run_messages' print() at agent.py:282.
            # Reset the flag here so the "ok"/"error" path never adds
            # a spurious extra newline.
            _delta_started[0] = False
            act.tick(name)  # live "working · N steps" line
            if name == "run_command" and args and args.get("command"):
                # opencode bash part: a ┃ bar column, command row prefixed $
                key = call_id or name
                _bash_block.add(key)
                act.pause()
                for ln in args["command"].splitlines():
                    print(ui_dim("  ┃ ") + "$ " + ln)
                act._pause.clear()
            return
        if status == "progress":
            # opencode-style live output: print only the lines that appeared
            # since the last progress event, inside the ┃ bar column
            if not tail:
                return
            key = call_id or name
            prev = _live_out.get(key, "")
            new = tail[len(prev):] if tail.startswith(prev) else tail
            _live_out[key] = tail
            if not new.strip():
                return
            act.pause()
            for ln in new.splitlines():
                print(ui_dim("  ┃ ") + ln)
            act._pause.clear()
            return
        # Pause the spinner synchronously (acquires _lock) before printing
        # so the spin thread cannot overwrite our tool line mid-write.
        act.pause()
        if name == "run_command" and (call_id or name) in _bash_block:
            # the ┃ block IS the display — close it with an empty bar row and
            # only surface a note when something went wrong
            _bash_block.discard(call_id or name)
            print(ui_dim("  ┃"))
            if status == "error":
                print("  " + _tool_line(name, _arg_summary(args), secs, err=True))
        elif diff and name in ("edit_file", "write_file", "code_edit"):
            # opencode edit part: ┃ bar column, "◐ Edit file" title, then
            # line-numbered diff rows (- red, + green, context dim)
            _print_diff_block(name, diff, args, status)
        elif name == "read_file" and status == "ok":
            p = str((args or {}).get("path", "")).rstrip("/")
            base = p.split("/")[-1] if p else "file"
            print("  " + ui_accent("» ") + ui_dim(f"Read {base}"))
        elif name == "grep" and status == "ok":
            pat = str((args or {}).get("pattern", ""))
            pth = str((args or {}).get("path", "."))
            m = re.match(r"(\d+) match", result or "")
            count = f" ({m.group(1)} match{'es' if m and m.group(1) != '1' else ''})" if m else ""
            print("  " + ui_accent("» ") + ui_dim(f"Grep \"{pat}\" in {pth}{count}"))
        elif name == "todo_write" and args and args.get("todos"):
            # opencode todo panel: ┃ bar column, checkbox rows — ✓ green,
            # • amber (in progress), space for pending
            _print_todo_block(args["todos"], status)
        else:
            print("  " + _tool_line(name, _arg_summary(args), secs,
                                    err=(status == "error")))
        # Un-pause so the spinner resumes for any subsequent tool calls
        act._pause.clear()

    def _on_delta(txt) -> None:
        # AGY-style: accumulate silently — spinner stays visible during the
        # API stream; md_print renders the complete styled response after.
        _delta_started[0] = True


    # status line: pinned curses bar in full-screen, ANSI rule in scrollback
    status_bar = tui.status if tui is not None else _status_line

    try:
        while True:
            try:
                user = (tui.input_line() if tui is not None else
                        _repl_input(ui_accent("> "))).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user:
                continue
            if user in ("/quit", "/exit"):
                break
            if user.startswith("!"):
                from .tools import run_command, set_progress_sink
                cmd_to_run = user[1:].strip()
                if not cmd_to_run:
                    print("(no command)")
                    continue
                # Live tail while the command runs (same ┃ style as the
                # agent's run_command block); without a sink a long
                # command looks hung until it finishes.
                _seen = {"tail": ""}

                def _direct_prog(tail: str) -> None:
                    prev = _seen["tail"]
                    new = tail[len(prev):] if tail.startswith(prev) else tail
                    _seen["tail"] = tail
                    if new.strip():
                        for ln in new.splitlines():
                            print(ui_dim("  ┃ ") + ln)

                set_progress_sink(_direct_prog)
                try:
                    print(run_command(cmd_to_run, confirm=confirm))
                finally:
                    set_progress_sink(None)
                continue
            if user in ("/new", "/clear"):
                messages = [{"role": "system", "content": _system_prompt()}]
                print("(fresh context)")
                status_bar(cfg, messages)
                continue
            if user == "/mode" or user.startswith("/mode "):
                arg = user[5:].strip().lower()
                if arg in ("accept-edits", "plan", "bypass"):
                    cfg["mode"] = arg
                    _save_config(cfg)
                    if _tui._FS is not None:
                        _tui._FS.mode = arg
                    print(f"Mode set to: {arg}")
                else:
                    curr = _tui._FS.mode if _tui._FS is not None else cfg.get("mode", "accept-edits")
                    print(f"Current mode: {curr}. Available modes: accept-edits, plan, bypass (use shift+tab to cycle)")
                continue
            if user == "/effort" or user.startswith("/effort "):
                arg = user[7:].strip().lower()
                if arg in ("low", "medium", "high", "max"):
                    cfg["effort"] = arg
                    _save_config(cfg)
                    print(f"Reasoning effort set to: {arg}")
                else:
                    curr = cfg.get("effort", "medium")
                    print(f"Current reasoning effort: {curr} (options: low, medium, high, max)")
                continue
            if user == "/memory":
                md_print(recall_memory())
                continue
            if user == "/playbook":
                md_print(read_playbook())
                continue
            if user == "/skills":
                idx = list_skills()
                print(skills_index() if idx else "(no skills yet)")
                continue
            if user == "/evolve":
                print("(running a self-improvement cycle…)")
                try:
                    print(evolve_pass(cfg, mcp, confirm=confirm))
                except Exception as e:
                    print(ui_err(f"(evolve failed: {str(e)[:300]})"))
                continue
            if user == "/fix" or user.startswith("/fix "):
                arg_fix = user[4:].strip()
                print("(running autonomous bug diagnosis & self-repair…)")
                try:
                    print(self_repair(cfg, mcp, confirm=confirm, issue=arg_fix))
                except Exception as e:
                    print(ui_err(f"(self-repair failed: {str(e)[:300]})"))
                continue
            if user == "/upgrade" or user.startswith("/upgrade ") or user == "/update" or user.startswith("/update "):
                arg_up = user.split(maxsplit=1)[1] if " " in user else ""
                print("(checking and applying codebase upgrades…)")
                try:
                    from .tools import self_update
                    print(self_update(arg_up, confirm=confirm))
                except Exception as e:
                    print(ui_err(f"(upgrade failed: {str(e)[:300]})"))
                continue
            if user == "/model" or user.startswith("/model "):
                if user == "/model":
                    # Bare /model: arrow-key picker grouped by provider.
                    pick = None
                    if tui is not None:
                        pick = tui.pick_model(_model_entries(cfg))
                    else:
                        pick = _arrow_pick_model(_model_entries(cfg))
                    if pick is None:
                        # Cancelled, or no tty for the scrollback picker —
                        # fall back to the printed numbered list.
                        if tui is not None:
                            print("(model unchanged)")
                            continue
                    else:
                        print(_apply_model_entry(cfg, pick))
                        continue
                print(run_slash_command(cfg, user, confirm=lambda _m: True))  # user-typed
                continue
            if user == "/acp" or user.startswith("/acp "):
                print(run_slash_command(cfg, user, confirm=lambda _m: True))
                continue
            if user == "/service" or user.startswith("/service "):
                from .sched import service_status_summary
                print(service_status_summary(cfg))
                continue
            if user == "/web":
                print(_web_status(cfg))
                continue
            if user.startswith("/wish "):
                _log_wish(f"human request: {user[6:]}")
                print("(wish recorded — the next evolve cycle will consider it)")
                continue
            if user == "/mic" or user.startswith("/mic "):
                parts = user.split()
                if len(parts) > 1 and parts[1].isdigit():
                    secs = min(60, max(1, int(parts[1])))
                else:
                    secs = 5
                print(f"(listening for {secs}s…)")
                heard = mic_record(secs)
                print(f"  [heard] {heard}")
                if heard.startswith("("):
                    continue  # error message — don't send it to the model
                user = heard  # fall through and treat as a normal message
            if user == "/forget":
                MEMORY.unlink(missing_ok=True)
                VECTORS.unlink(missing_ok=True)
                messages[0]["content"] = _system_prompt()
                print("memory wiped")
                continue
            if user == "/tools":
                from .tools import _CUSTOM_TOOLS, load_custom_tools
                load_custom_tools()
                for t in BASE_TOOL_SPECS:
                    print(" -", t["function"]["name"])
                for t in mcp.all_tools():
                    print(" -", t["function"]["name"], "(mcp)")
                for name in _CUSTOM_TOOLS:
                    print(" -", name, "(custom)")
                continue
            if user == "/jobs":
                md_print(list_jobs())
                continue
            if user == "/inbox":
                md_print(INBOX.read_text() if INBOX.exists() else "(inbox empty)")
                continue
            if user == "/help":
                print(ui_dim("  commands: " + " ".join(_CHAT_COMMANDS)))
                print(ui_dim("  / at an empty prompt opens the command palette · "
                             "Tab cycles suggestions · end a line with \\ to continue"))
                continue
            if user == "/status":
                mem_lines = len(MEMORY.read_text().splitlines()) if MEMORY.exists() else 0
                from .theme import CURRENT as _cur_theme
                print(ui_dim(f"  {len(messages)} messages · ~{_approx_chars(messages)} chars "
                             f"context · {mem_lines} memory lines · {len(mcp.servers)} MCP servers "
                             f"· theme {_cur_theme}"))
                continue
            if user == "/introspect" or user.startswith("/introspect ") or user == "/self" or user.startswith("/self "):
                from .tools import introspect
                focus = user.split(maxsplit=1)[1] if " " in user else "all"
                print()
                print(introspect(focus))
                print()
                continue
            if user.startswith("/theme"):
                from . import theme as _theme
                arg_t = user[6:].strip()
                if not arg_t:
                    print(f"  theme: {_theme.CURRENT} · available: " + ", ".join(_theme.list_themes()))
                    continue
                if arg_t not in _theme.list_themes():
                    print(ui_err(f"(unknown theme {arg_t!r} — available: " + ", ".join(_theme.list_themes()) + ")"))
                    continue
                _theme.apply(arg_t)
                cfg["theme"] = arg_t
                _save_config(cfg)
                print(f"  theme → {arg_t}")
                continue
            if user == "/yolo":
                yolo = not yolo
                print(f"dangerous-command confirm {'disabled (YOLO)' if yolo else 'enabled'}")
                continue
            if user.startswith("/agent"):
                arg = user[6:].strip().lower()
                current = str(cfg.get("agent", "build"))
                if not arg:
                    new_agent = "plan" if current == "build" else "build"
                elif arg in ("build", "plan", "general"):
                    new_agent = arg
                else:
                    print(ui_err(f"(unknown agent '{arg}' — available: build, plan, general)"))
                    continue
                cfg["agent"] = new_agent
                _save_config(cfg)
                print(f"(agent → {new_agent})")
                continue
            if user.startswith("/say"):
                arg = user[4:].strip().lower()
                if arg in ("off", "api", "espeak"):
                    cfg["tts"] = arg
                    _save_config(cfg)
                    print(f"voice: {arg}" + (f" (voice={cfg.get('tts_voice', 'nova')})" if arg == "api" else ""))
                elif arg.startswith("voice "):
                    cfg["tts_voice"] = arg[6:].strip()
                    _save_config(cfg)
                    print(f"voice set to {cfg['tts_voice']} (api mode)")
                    speak(f"Hello. I am buddy, speaking as {cfg['tts_voice']}.", cfg)
                elif arg == "":
                    print(f"voice mode: {cfg.get('tts', 'off')} — /say off|api|espeak, /say voice <name>")
                    print("api voices: " + ", ".join(TTS_VOICES))
                else:
                    print("(unknown /say option)")
                continue
            if user.startswith("/image "):
                rest_arg = user[7:].strip()
                question = ""
                if " " in rest_arg:
                    p_str, question = rest_arg.split(" ", 1)
                else:
                    p_str = rest_arg
                p = Path(p_str).expanduser()
                if not p.exists():
                    print("(image file not found)")
                    continue
                try:
                    if p.stat().st_size > 2_000_000:
                        print("(image too large: 2MB max — shrink it first)")
                        continue
                except OSError as e:
                    print(ui_err(f"(couldn't read image: {e})"))
                    continue
                import base64

                try:
                    b64 = base64.b64encode(p.read_bytes()).decode()
                except Exception as e:
                    print(ui_err(f"(couldn't read image: {e})"))
                    continue
                mime = {"png": "image/png", "gif": "image/gif",
                        "webp": "image/webp", "bmp": "image/bmp"}.get(
                            p.suffix.lstrip(".").lower(), "image/jpeg")
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question or f"(user shared image {p.name})"},
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                })
                print()
                _delta_started[0] = False
                n = len(messages)
                act.reset(); act.start()
                try:
                    answer = run_messages(cfg, mcp, messages, quiet=False,
                                          confirm=confirm, cancel=cancel_event,
                                          on_tool=on_tool, on_delta=(_on_delta if cfg.get('brain', 'api') == 'api' else None))
                except KeyboardInterrupt:
                    cancel_event.set()
                    act.end()
                    _delta_started[0] = False
                    print(ui_dim("\n(cancelled)"))
                    del messages[n:]
                    cancel_event.clear()
                    print()
                    continue
                except Exception as e:
                    answer = f"({str(e)[:300]})"
                    del messages[n:]
                act.end()
                _delta_started[0] = False
                if answer and answer.startswith("("):
                    print(ui_err(f"\nbuddy› {answer}"))
                elif answer and answer.strip():
                    md_print(answer)
                print()
                status_bar(cfg, messages)
                print()
                continue



            # Pick up `buddy.py model set ...` / hand-edits between turns
            maybe_reload(cfg)

            _intent = parse_intent(user)
            if _intent is not None:
                # Deterministic intent: handled without the LLM (works even
                # when the API is down). Kept in history so follow-ups work.
                _summary = _run_intent(_intent, cfg, confirm)
                if _summary is not None:
                    messages.append({"role": "user", "content": user})
                    messages.append({"role": "assistant", "content": _summary})
                    md_print(_summary)
                    print()
                    status_bar(cfg, messages)
                    print()
                    continue

            messages.append({"role": "user", "content": user})
            if cfg.get("brain", "api") == "api":
                print()  # breathing room before streamed response
            _delta_started[0] = False
            n = len(messages)
            act.reset(); act.start()
            try:
                answer = run_messages(cfg, mcp, messages, quiet=False,
                                      confirm=confirm, cancel=cancel_event,
                                      on_tool=on_tool, on_delta=(_on_delta if cfg.get('brain', 'api') == 'api' else None))
            except KeyboardInterrupt:
                # Ctrl-C (or Esc-Esc → SIGINT) mid-turn: cancel, keep the session
                cancel_event.set()
                act.end()
                _delta_started[0] = False
                print(ui_dim("\n(cancelled)"))
                del messages[n:]
                cancel_event.clear()
                print()
                continue
            except Exception as e:
                answer = f"({str(e)[:300]})"
                del messages[n:]
            act.end()
            _delta_started[0] = False
            if answer:
                messages.append({"role": "assistant", "content": answer})
            if answer and answer.startswith("("):
                print(ui_err(f"\nbuddy› {answer}"))
            elif answer and answer.strip():
                md_print(answer)
            print()
            status_bar(cfg, messages)
            print()
            if answer and not answer.startswith("(") and cfg.get("tts", "off") != "off":
                threading.Thread(target=speak, args=(answer, cfg), daemon=True).start()



    finally:
        if tui is None:
            _save_history()  # full-screen mode saves its own history
        if sched:
            sched.stop.set()
        # one LLM exit pass (digest + critique + harvest) in the background so
        # /quit and /exit return immediately; their own errors go to errors.log
        _exit_th = threading.Thread(target=_exit_pass, args=(cfg, messages),
                                    daemon=False)  # non-daemon: exit must wait
        _exit_th.start()
        mcp.shutdown()
        _exit_th.join(timeout=45)  # give the LLM round-trip a real chance

# ---- original buddy.py lines 5174-5175 --------------------------------


# ---- original buddy.py lines 5233-5233 --------------------------------


