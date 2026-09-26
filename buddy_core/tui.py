"""buddy_core.tui — terminal UI: splash/banner, readline + command palette, markdown printing, spinner, status line.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .config import BUDDY_VERSION, HOME
from .skills import list_skills
from .util import _THEME, _TTY, _c, _strip_ansi, ui_accent, ui_amber, ui_bold, ui_dim, ui_faint

import builtins
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
try:  # full-screen mode: curses (absent on some Windows shells)
    import curses
except ImportError:  # pragma: no cover
    curses = None
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

# ---- original buddy.py lines 256-258 ----------------------------------

# block-letter 'buddy' wordmark (ANSI-Shadow style), rendered once at startup
# authentic ANSI-Shadow glyphs, b u d d y (8+9+8+8+9 + 4 spaces = 46 cols)

# ---- original buddy.py lines 259-287 ----------------------------------
_WORDMARK_ROWS = (
    '██████╗ ',
    '██╔══██╗',
    '██████╔╝',
    '██╔══██╗',
    '██████╔╝',
    '╚═════╝ '
), (
    '██╗   ██╗',
    '██║   ██║',
    '██║   ██║',
    '██║   ██║',
    '╚██████╔╝',
    ' ╚═════╝ '
), (
    '██████╗ ',
    '██╔══██╗',
    '██║  ██║',
    '██║  ██║',
    '██████╔╝',
    '╚═════╝ '
), (
    '██████╗ ',
    '██╔══██╗',
    '██║  ██║',
    '██║  ██║',
    '██████╔╝',
    '╚═════╝ '
), (
    '██╗   ██╗',
    '╚██╗ ██╔╝',
    ' ╚████╔╝ ',
    '  ╚██╔╝  ',
    '   ██║   ',
    '   ╚═╝   '
)

# ---- original buddy.py lines 288-289 ----------------------------------
_WORDMARK = "\n".join(" ".join(parts).rstrip().ljust(46)
                     for parts in zip(*_WORDMARK_ROWS))

# ---- original buddy.py lines 290-291 ----------------------------------

# rotating startup tip ('● Tip <text>'), cycled by hour

# ---- original buddy.py lines 292-297 ----------------------------------
_TIPS = (
    "Run /connect to add an AI provider and start coding",
)

# ---- original buddy.py lines 298-298 ----------------------------------


# ---- original buddy.py lines 299-299 ----------------------------------
_GIT_BRANCH: str | None = None

# ---- original buddy.py lines 300-300 ----------------------------------


# ---- original buddy.py lines 301-311 ----------------------------------
def _git_branch() -> str:
    """Current git branch (cached), '' outside a repo or if git is missing."""
    global _GIT_BRANCH
    if _GIT_BRANCH is None:
        try:
            r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                               capture_output=True, text=True, timeout=2)
            _GIT_BRANCH = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            _GIT_BRANCH = ""
    return _GIT_BRANCH

# ---- original buddy.py lines 312-312 ----------------------------------


# ---- original buddy.py lines 313-324 ----------------------------------
def _short_cwd() -> str:
    """cwd with the home directory collapsed to ~."""
    try:
        cwd = os.getcwd()
    except OSError:
        return "?"
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd or "/"


def _sessions_recent(max_n: int = 14) -> list:
    """Recent sessions for the TUI sidebar: (id, title, updated_ts), most
    recently updated first. Tolerates corrupt/missing files; returns []."""
    try:
        sdir = HOME / "sessions"
        def _mtime(p):
            try:
                return p.stat().st_mtime  # deleted mid-glob: sort it last
            except OSError:
                return 0.0
        files = sorted(sdir.glob("*.json"), key=_mtime,
                       reverse=True)[:max_n]
    except Exception:
        return []
    out = []
    for p in files:
        try:
            st = p.stat()
            d = json.loads(p.read_text() or "{}")
            title = str(d.get("title") or "(untitled)")
            out.append((p.stem, title, st.st_mtime))
        except Exception:
            continue
    return out


def _age_s(ts: float | str) -> str:
    """Compact human age for the sidebar: 5m · 2h · 3d · 2w."""
    if isinstance(ts, str):
        try:
            ts = float(ts)
        except ValueError:
            try:
                dt = datetime.fromisoformat(ts)
                ts = dt.timestamp()
            except Exception:
                return "1d"
    try:
        d = max(0, time.time() - ts)
    except Exception:
        return "1d"
    for lim, div, suf in ((90, 60, "m"), (48 * 3600, 3600, "h"),
                          (14 * 86400, 86400, "d")):
        if d < lim:
            return f"{max(1, int(d // div))}{suf}"
    return f"{int(d // (7 * 86400))}w"

# ---- original buddy.py lines 325-325 ----------------------------------


# ---- original buddy.py lines 326-342 ----------------------------------
def _status_line(cfg: dict, messages: list[dict]) -> None:
    """Antigravity (agy)-style status bar divider after turn."""
    cols = max(40, shutil.get_terminal_size().columns)
    model = cfg.get("model", "Gemini 2.5 Flash")
    if model == "gemini-2.5-flash":
        model = "Gemini 2.5 Flash"
    effort = str(cfg.get("effort", "medium")).lower()
    mode = cfg.get("mode", "accept-edits")
    left = "? for shortcuts"
    right = f"{mode} · {model} · {effort} · AI: Ready"
    pad = max(2, cols - len(left) - len(right))
    print(ui_dim("─" * cols))
    print(ui_dim(left) + " " * pad + ui_dim(right))


# ---- original buddy.py lines 343-343 ----------------------------------


# ---- original buddy.py lines 344-366 ----------------------------------
def ui_banner(cfg: dict, where: str, n_mcp: int) -> None:
    """Antigravity (agy)-style splash: chevron logo, CLI version, active model,
    workspace info, divider rule, and statusline."""
    cols = max(40, shutil.get_terminal_size().columns)
    model = cfg.get("model", "Gemini 2.5 Flash")
    if model == "gemini-2.5-flash":
        model = "Gemini 2.5 Flash"
    effort = str(cfg.get("effort", "medium")).capitalize()
    cwd = _short_cwd()
    user_id = os.environ.get("USER", "developer") + "@localhost"
    mode = cfg.get("mode", "accept-edits")

    logo_lines = [
        "       ██       ",
        "      ████      ",
        "     ██  ██     ",
        "    ██    ██    ",
    ]
    info_lines = [
        ui_bold(f"Buddy CLI {BUDDY_VERSION}"),
        ui_dim(user_id),
        f"{model} ({effort})",
        ui_dim(cwd),
    ]
    print()
    for logo, info in zip(logo_lines, info_lines):
        print(f"  {ui_accent(logo)}  {info}")
    print()
    print(ui_dim("─" * cols))
    left = "? for shortcuts"
    right = f"{mode} · {model} · {effort.lower()} · AI: Ready"
    pad = max(2, cols - len(left) - len(right))
    print(ui_dim(left) + " " * pad + ui_dim(right))
    print()

# ---- original buddy.py lines 367-370 ----------------------------------


# =========================================================== readline input =

# ---- original buddy.py lines 371-374 ----------------------------------
_CHAT_COMMANDS = ("/new", "/clear", "/mode", "/effort", "/model", "/tools", "/acp", "/service",
                  "/web", "/introspect",
                  "/memory", "/forget", "/jobs", "/inbox", "/quota",
                  "/playbook", "/skills", "/evolve", "/fix", "/upgrade",
                  "/theme", "/wish", "/mic", "/image", "/say", "/yolo", "/help",
                  "/status", "/quit", "/exit")

# ---- original buddy.py lines 375-376 ----------------------------------


# ---- original buddy.py lines 377-388 ----------------------------------
def _make_completer(slugs: list[str]):
    """Tab-completion: slash commands, and skill slugs after '/skills '."""
    def complete(text: str, state: int):
        line = readline.get_line_buffer() if readline else ""
        if text.startswith("/"):
            matches = [c for c in _CHAT_COMMANDS if c.startswith(text)]
        elif line.startswith("/skills ") or text:
            matches = [s for s in slugs if s.startswith(text)]
        else:
            matches = []
        return matches[state] if state < len(matches) else None
    return complete

# ---- original buddy.py lines 389-390 ----------------------------------


# ---- original buddy.py lines 391-413 ----------------------------------
def _setup_readline() -> None:
    """Arrow keys, persistent history (last 500 lines) and tab-completion
    for the chat REPL. Adds standard shell word-navigation and word-delete
    bindings so the input line feels like a proper shell prompt. Skips
    silently where readline is unavailable."""
    if readline is None:
        return
    hist = HOME / "history"
    try:
        hist.parent.mkdir(parents=True, exist_ok=True)
        readline.read_history_file(hist)
    except OSError:
        pass
    readline.set_history_length(500)
    try:
        readline.set_completer(_make_completer([s["slug"] for s in list_skills()]))
        readline.parse_and_bind("tab: complete")
        readline.parse_and_bind('"\\C-i": menu-complete')  # Tab cycles full matches
        readline.parse_and_bind("set completion-query-items 0")
        readline.set_completer_delims(" \t")
        # Esc-Esc clears the line; Ctrl-C during a RUNNING turn cancels it (chat())
        readline.parse_and_bind('"\\e\\e": "kill-whole-line"')
        # Standard shell word-navigation / deletion
        readline.parse_and_bind('"\\e[1;5D": backward-word')   # Ctrl-Left
        readline.parse_and_bind('"\\e[1;5C": forward-word')    # Ctrl-Right
        readline.parse_and_bind('"\\eb": backward-word')       # Alt-b
        readline.parse_and_bind('"\\ef": forward-word')        # Alt-f
        readline.parse_and_bind('"\\C-w": unix-word-rubout')   # Ctrl-W delete word left
        readline.parse_and_bind('"\\ed": kill-word')           # Alt-d  delete word right
        readline.parse_and_bind('"\\C-u": unix-line-discard')  # Ctrl-U kill line left
        readline.parse_and_bind('"\\C-k": kill-line')          # Ctrl-K kill line right
        # Bracketed paste: let the terminal delimit pastes so multi-line
        # paste doesn't submit line-by-line; markers are stripped in
        # _input_line as a fallback for readline builds without support.
        try:
            readline.parse_and_bind("set enable-bracketed-paste on")
        except Exception:
            pass
    except Exception:
        pass

# ---- original buddy.py lines 414-415 ----------------------------------


# ---- original buddy.py lines 416-424 ----------------------------------
def _save_history() -> None:
    if readline is None:
        return
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        readline.set_history_length(500)
        readline.write_history_file(HOME / "history")
    except OSError:
        pass

# ---- original buddy.py lines 425-426 ----------------------------------


# Compiled once: matches all common ANSI/VT escape sequences for _prompt_str.
# CSI (ESC [ … letter), OSC (ESC ] … BEL/ST), and charset designators.
_ANSI_ESC_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[A-Za-z]"      # CSI sequences  ESC [ ... letter
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)" # OSC sequences  ESC ] ... BEL or ST
    r"|[()][A-Za-z0-9])"               # charset desig  ESC ( A  etc.
)

# ---- original buddy.py lines 427-433 ----------------------------------
def _prompt_str(s: str) -> str:
    """Make an ANSI-colored input() prompt readline-safe: wrap every escape
    sequence in \\001/\\002 (RL_PROMPT_START_IGNORE / END_IGNORE) so that
    readline knows the display width is zero for those bytes.  Without this,
    arrow-key editing redraws the prompt at the wrong column and corrupts the
    visible line.

    Uses a regex so that *all* escape sequences are wrapped — not just SGR
    resets — which the original simple str.replace missed for chained codes."""
    if not _TTY or readline is None:
        return s
    return _ANSI_ESC_RE.sub(lambda m: "\x01" + m.group(0) + "\x02", s)

# ---- original buddy.py lines 434-435 ----------------------------------


# ---- original buddy.py lines 436-447 ----------------------------------
def _input_line(prompt: str) -> str:
    """input() with a readline-safe prompt; a line ending in '\\\\' reads
    another line (up to 10 continuations) and joins them with newlines."""
    line = input(_prompt_str(prompt))
    # Fallback: strip bracketed-paste markers if the terminal sent them but
    # this readline build didn't consume them.
    if "\x1b[200~" in line or "\x1b[201~" in line:
        line = line.replace("\x1b[200~", "").replace("\x1b[201~", "")
    parts: list[str] = []
    while line.rstrip().endswith("\\") and len(parts) < 10:
        parts.append(line.rstrip()[:-1])
        line = input(_prompt_str(ui_dim("  … ")))
    if parts:
        parts.append(line)
        return "\n".join(parts)
    return line

# ---- original buddy.py lines 448-451 ----------------------------------


# ================================================== command palette ('/) ====

# ---- original buddy.py lines 452-473 ----------------------------------
_COMMAND_HELP = {
    "/new": "fresh chat (clears context)",
    "/clear": "fresh chat (clears context)",
    "/mode": "switch execution mode (accept-edits/plan/bypass)",
    "/effort": "set reasoning effort (low/medium/high/max)",
    "/model": "manage / switch LLM models",
    "/tools": "list available tools",
    "/acp": "manage / drive ACP external agents",
    "/service": "view / control background services",
    "/web": "web UI address & daemon state",
    "/introspect": "operational self-awareness & telemetry",
    "/memory": "view memory",
    "/forget": "wipe all memory",
    "/jobs": "list scheduled jobs",
    "/inbox": "read messages sent while away",
    "/playbook": "view the self-training playbook",
    "/skills": "list learned skills",
    "/evolve": "run a self-improvement cycle",
    "/fix": "autonomous bug diagnosis & source repair",
    "/upgrade": "check & apply latest code upgrades",
    "/wish": "record a wish for the next evolve",
    "/mic": "talk instead of typing",
    "/image": "attach an image <path> [question]",
    "/say": "voice mode off/api/espeak",
    "/yolo": "toggle command confirmations",
    "/help": "show commands",
    "/status": "session and memory status",
    "/quit": "exit buddy",
    "/exit": "exit buddy",
}

# ---- original buddy.py lines 474-477 ----------------------------------

# commands that still need an argument: selecting them in the palette hands
# the line back to the normal readline prompt, pre-filled with the command
# + space, so the argument goes through the exact same input flow

# ---- original buddy.py lines 478-478 ----------------------------------
_ARG_COMMANDS = ("/model", "/acp", "/service", "/introspect", "/fix", "/upgrade", "/mode", "/effort", "/wish", "/say", "/image", "/mic")

# ---- original buddy.py lines 479-480 ----------------------------------


# ---- original buddy.py lines 481-490 ----------------------------------
def _palette_ok() -> bool:
    """The palette needs a real terminal on both ends plus termios and
    readline (to hand pre-filled lines back). Piped input, Windows and other
    unsupported setups skip it silently — everything works as before."""
    if _termios is None or _select is None or readline is None:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False

# ---- original buddy.py lines 491-492 ----------------------------------


# ---- original buddy.py lines 493-530 ----------------------------------
def _palette_key(fd: int) -> str:
    """One logical keypress from the tty in cbreak mode: a printable
    character, a control character, or a named key
    ('up' / 'down' / 'bs' / 'esc' / 'eof'). Reads a byte at a time via
    os.read so nothing extra is buffered away from readline."""
    b = os.read(fd, 1)
    if not b:
        return "eof"
    c = b[0]
    if c == 0x1B:  # Esc — lone keypress, or the start of an escape sequence
        r, _, _ = _select.select([fd], [], [], 0.05)
        if not r:
            return "esc"
        c2 = os.read(fd, 1)
        if c2 == b"[":
            r2, _, _ = _select.select([fd], [], [], 0.05)
            if not r2:
                return "esc"
            c3 = os.read(fd, 1)
            if c3 == b"2":  # bracketed paste start: ESC[200~ ... ESC[201~
                r3, _, _ = _select.select([fd], [], [], 0.05)
                if r3 and os.read(fd, 1) == b"0":
                    r4, _, _ = _select.select([fd], [], [], 0.05)
                    if r4 and os.read(fd, 1) == b"0":
                        r5, _, _ = _select.select([fd], [], [], 0.05)
                        if r5 and os.read(fd, 1) == b"~":
                            out = b""
                            while not out.endswith(b"\x1b[201~"):
                                r6, _, _ = _select.select([fd], [], [], 0.5)
                                if not r6:
                                    break
                                out += os.read(fd, 4096)
                            end = out.rfind(b"\x1b[201~")
                            return out[:end].decode("utf-8", "replace")
                return "unknown"  # stray sequence — never cancel the palette
            if c3 == b"A":
                return "up"
            if c3 == b"B":
                return "down"
            if c3 == b"C":
                return "right"
            if c3 == b"D":
                return "left"
            if c3 == b"H":
                return "home"
            if c3 == b"F":
                return "end"
            if c3 == b"3":  # Del == ESC [ 3 ~
                r3, _, _ = _select.select([fd], [], [], 0.05)
                if r3 and os.read(fd, 1) == b"~":
                    return "del"
            return "unknown"  # never cancel the palette on stray sequences
        if c2 == b"O":  # SS3: Home/End on some terminals
            r2, _, _ = _select.select([fd], [], [], 0.05)
            if r2:
                c3 = os.read(fd, 1)
                if c3 == b"H":
                    return "home"
                if c3 == b"F":
                    return "end"
            return "unknown"
        return "esc"
    if c in (0x08, 0x7F):
        return "bs"
    if c == 0x04:   # Ctrl-D
        return "eof"
    if c == 0x10:   # Ctrl-P
        return "up"
    if c == 0x0E:   # Ctrl-N
        return "down"
    n = 4 if c >= 0xF0 else 3 if c >= 0xE0 else 2 if c >= 0xC0 else 1
    out = b
    while len(out) < n:  # UTF-8 continuation bytes are usually queued already
        r, _, _ = _select.select([fd], [], [], 0.1)  # slow terminals, not 20ms
        if not r:
            break
        out += os.read(fd, 1)
    return out.decode("utf-8", "replace")

# ---- original buddy.py lines 531-532 ----------------------------------


# ---- original buddy.py lines 533-559 ----------------------------------
def _palette_draw(query: str, ms: list, sel: int, drawn: int) -> int:
    """Render the palette inline below the prompt (erasing the previous
    frame). Returns the number of lines drawn, for the next erase."""
    out = sys.stdout
    if drawn:
        out.write(f"\x1b[{drawn}A\r\x1b[J")  # back to the prompt line, clear below
    out.write("\r\x1b[K" + ui_accent("> ") + "/" + query
              + ui_faint("▏") + "\n")
    if not ms:
        out.write("\x1b[K  " + ui_faint("(no matching command)") + "\n")
        out.flush()
        return 2
    pad = max(len(c) for c in _CHAT_COMMANDS)
    start = max(0, min(sel - 4, max(0, len(ms) - 10)))  # 10-row scroll window
    end = min(start + 10, len(ms))
    for i in range(start, end):
        desc = _COMMAND_HELP.get(ms[i], "")
        updown = "↑" if i == start and start > 0 else "↓" if i == end - 1 and end < len(ms) else " "
        if i == sel:
            row = "  " + ui_accent(">") + " " + ui_bold(ms[i]) + " " * (pad - len(ms[i])) \
                  + "  " + ui_dim(desc)
        else:
            row = "  " + updown + " " + ms[i] + " " * (pad - len(ms[i])) \
                  + "  " + ui_faint(desc)
        out.write("\x1b[K" + row + "\n")
    out.flush()
    return 1 + (end - start)

# ---- original buddy.py lines 560-561 ----------------------------------


# ---- original buddy.py lines 562-600 ----------------------------------
def _palette_run(fd: int):
    """The palette loop. Returns the chosen command, or None on cancel
    (Esc / Ctrl-C / Ctrl-D / Enter with no matches)."""
    query, sel, drawn = "", 0, 0
    sys.stdout.write("\x1b[?25l")  # cursor hidden — '▏' marks the edit point
    sys.stdout.write("\x1b[?2004h")  # bracketed paste: pastes arrive as one blob
    try:
        while True:
            q = query.strip().lower()
            ms = list(_CHAT_COMMANDS) if not q else [
                c for c in _CHAT_COMMANDS if q in c[1:]]
            sel = min(sel, len(ms) - 1) if ms else 0
            drawn = _palette_draw(query, ms, sel, drawn)
            key = _palette_key(fd)
            if key in ("esc", "eof"):
                return None
            if key in ("left", "right", "home", "end", "unknown"):
                pass  # single-line filter: no mid-line cursor to move
            elif key == "up" and ms:
                sel = (sel - 1) % len(ms)
            elif key == "down" and ms:
                sel = (sel + 1) % len(ms)
            elif key in ("bs", "del"):
                query = query[:-1]
                sel = 0
            elif key == "\t":  # complete the highlighted command, keep editing
                if ms:
                    query = ms[sel][1:] + " "
            elif key in ("\r", "\n"):
                if q and ms:
                    return ms[sel]
                return None  # empty filter / no matches — back out
            elif key.isprintable():
                query += key
                sel = 0
    except KeyboardInterrupt:  # Ctrl-C backs out to the prompt
        return None
    finally:
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}A\r\x1b[J")  # erase the palette
        sys.stdout.write("\x1b[?2004l")  # bracketed paste OFF
        sys.stdout.write("\x1b[?25h")  # cursor back
        sys.stdout.flush()

# ---- original buddy.py lines 601-602 ----------------------------------


# ---- original buddy.py lines 603-635 ----------------------------------
def _palette_probe() -> tuple:
    """Peek at the first keystroke of the next prompt (tty only). If it is
    '/', run the command palette. Returns (command, prefill):
      command  — a full command to run as if typed ('/help')
      prefill  — text to seed the normal readline prompt with ('' = nothing)
    """
    if not _palette_ok():
        return None, ""
    fd = sys.stdin.fileno()
    try:
        old = _termios.tcgetattr(fd)
    except Exception:
        return None, ""
    try:
        # char-at-a-time, no echo — via TCSANOW: tty.setcbreak() would be
        # simpler but defaults to TCSAFLUSH, which DISCARDS input queued
        # while buddy was still starting up (type-ahead would be lost)
        mode = _termios.tcgetattr(fd)
        mode[3] &= ~(_termios.ECHO | _termios.ICANON)  # 3 = LFLAG
        _termios.tcsetattr(fd, _termios.TCSANOW, mode)
        first = _palette_key(fd)
        if first == "eof":
            raise EOFError
        if first in ("\r", "\n"):
            return None, "\n"
        if first in ("esc", "up", "down", "bs", "\t") or (
                not first or len(first) > 1 or not first.isprintable()):
            return None, ""  # stray/named key at an empty prompt — ignore
        if first != "/":
            return None, first  # normal typing — pass the char through
        return _palette_run(fd), ""
    finally:
        try:
            _termios.tcsetattr(fd, _termios.TCSADRAIN, old)
        except Exception:
            pass

# ---- original buddy.py lines 636-637 ----------------------------------


# ---- model picker for the scrollback REPL (raw-tty arrow keys) --------
def _arrow_pick_model(entries: list) -> "dict | None":
    """Arrow-key provider/model picker for bare `/model` outside the
    full-screen TUI. Returns the chosen entry dict, or None on cancel
    (Esc / Ctrl-C / Ctrl-D) or when no tty is available (caller falls
    back to the printed numbered list)."""
    from .commands import _group_model_entries
    items = list(entries or [])
    if not items or not _palette_ok():
        return None
    sel = next((i for i, e in enumerate(items) if e.get("active")), 0)
    rows: list = []
    for title, group in _group_model_entries(items):
        rows.append(("head", title))
        for e in group:
            rows.append(("item", e))

    def _sel_row() -> int:
        for k, row in enumerate(rows):
            if row[0] == "item" and row[1].get("n", 0) - 1 == sel:
                return k
        return 0

    def _draw(drawn: int) -> int:
        out = sys.stdout
        if drawn:
            out.write(f"\x1b[{drawn}A\r\x1b[J")
        out.write("\r\x1b[K" + ui_accent("models")
                  + ui_dim(" — ↑/↓ select · enter use · esc cancel") + "\n")
        sr = _sel_row()
        first = max(0, min(sr - 4, max(0, len(rows) - 10)))
        for kind, payload in rows[first:first + 10]:
            if kind == "head":
                out.write("\x1b[K" + ui_dim(f"  ─ {payload} ─") + "\n")
            else:
                mark = "> " if payload.get("n", 0) - 1 == sel else "  "
                dot = "● " if payload.get("active") else "○ "
                line = f"  {mark}{dot}{payload.get('label', '')}"
                if payload.get("n", 0) - 1 == sel:
                    line = ui_accent(">") + line[2:]
                    out.write("\x1b[K" + ui_bold(line) + "\n")
                else:
                    out.write("\x1b[K" + line + "\n")
        out.flush()
        return 1 + min(10, len(rows) - first)

    fd = sys.stdin.fileno()
    try:
        old = _termios.tcgetattr(fd)
    except Exception:
        return None
    drawn = 0
    sys.stdout.write("\x1b[?25l")
    try:
        mode = _termios.tcgetattr(fd)
        mode[3] &= ~(_termios.ECHO | _termios.ICANON)
        _termios.tcsetattr(fd, _termios.TCSANOW, mode)
        while True:
            drawn = _draw(drawn)
            key = _palette_key(fd)
            if key in ("esc", "eof"):
                return None
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key in ("\r", "\n"):
                return items[sel]
    except KeyboardInterrupt:
        return None
    finally:
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}A\r\x1b[J")
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        try:
            _termios.tcsetattr(fd, _termios.TCSADRAIN, old)
        except Exception:
            pass


# ---- approve/decline confirm for the scrollback REPL (raw-tty) --------
def _arrow_confirm(cmd: str, mode: str = "") -> "str | None":
    """Arrow-key approve/decline dialog: ←/→ move between
    allow · always · decline, y/n/a answer directly, Enter confirms,
    Esc declines. Returns "allow"/"always"/"decline", or None when no
    tty is available (caller falls back to a typed prompt)."""
    if not _palette_ok():
        return None
    opts = ["allow", "always", "decline"]
    sel = 0
    lines = cmd.splitlines()[:4]

    def _draw(drawn: int) -> int:
        out = sys.stdout
        if drawn:
            out.write(f"\x1b[{drawn}A\r\x1b[J")
        title = ui_amber("⚠ approve?") + (ui_dim(f" ({mode})") if mode else "")
        out.write("\r\x1b[K" + title + "\n")
        for ln in lines:
            out.write("\x1b[K" + ui_dim("  $ ") + ui_bold(ln[:120]) + "\n")
        row = ""
        for i, opt in enumerate(opts):
            tag = f"[{opt}]"
            if i == sel:
                tag = ui_accent(f">[{opt}]")
            row += tag + "   "
        out.write("\x1b[K" + row.rstrip() + "\n")
        out.write("\x1b[K" + ui_dim("←/→ select · y/n/a shortcut · enter ok · esc no") + "\n")
        out.flush()
        return 3 + len(lines)

    fd = sys.stdin.fileno()
    try:
        old = _termios.tcgetattr(fd)
    except Exception:
        return None
    drawn = 0
    sys.stdout.write("\x1b[?25l")
    try:
        tty_mode = _termios.tcgetattr(fd)
        tty_mode[3] &= ~(_termios.ECHO | _termios.ICANON)
        _termios.tcsetattr(fd, _termios.TCSANOW, tty_mode)
        while True:
            drawn = _draw(drawn)
            key = _palette_key(fd)
            if isinstance(key, str) and len(key) == 1:
                low = key.lower()
                if low == "y":
                    return "allow"
                if low == "a":
                    return "always"
                if low == "n":
                    return "decline"
            if key in ("esc", "eof"):
                return "decline"
            if key in ("left", "right", "up", "down"):
                d = -1 if key in ("left", "up") else 1
                sel = (sel + d) % len(opts)
            elif key in ("\r", "\n"):
                return opts[sel]
    except KeyboardInterrupt:
        return "decline"
    finally:
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}A\r\x1b[J")
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        try:
            _termios.tcsetattr(fd, _termios.TCSADRAIN, old)
        except Exception:
            pass


# ---- original buddy.py lines 638-663 ----------------------------------
def _repl_input(prompt: str) -> str:
    """The chat prompt: on a tty the first keystroke is peeked — '/' opens
    the command palette, any other key is handed to readline pre-filled so
    ordinary typing (and '/' mid-line) behaves exactly as before. Cancelling
    the palette re-arms it; without a tty this is just _input_line."""
    if not _palette_ok():
        return _input_line(prompt)
    while True:
        sys.stdout.write("\r\x1b[K" + prompt)  # placeholder while peeking
        sys.stdout.flush()
        pal_cmd, prefill = _palette_probe()
        sys.stdout.write("\r\x1b[K")  # placeholder or palette residue — clear
        sys.stdout.flush()
        if pal_cmd:
            return pal_cmd  # runs through the same path as a typed command
        if prefill == "\n":
            return ""
        if prefill:
            if readline is not None:
                def _seed(s=prefill, _rl=readline):
                    _rl.set_startup_hook(None)  # self-clearing
                    _rl.insert_text(s)
                readline.set_startup_hook(_seed)
            try:
                return _input_line(prompt)
            finally:
                if readline is not None:
                    try:
                        readline.set_startup_hook(None)
                    except Exception:
                        pass

# ---- original buddy.py lines 664-668 ----------------------------------
        # palette cancelled (Esc / Ctrl-C / no matches) — re-arm and wait again


# ================================================================ markdown =

# ---- original buddy.py lines 669-721 ----------------------------------
def md_print(text: str) -> None:
    """Print markdown-ish text with ANSI styling (honors _TTY/NO_COLOR):
    headings bold+accent, **bold**, *em*/_em_ italic, `code` dim,
    fenced blocks in a dim ┌─/└─ frame with dim content, '- ' bullets as
    '·' with hanging indent, numbered lists kept, [text](url) links,
    blockquotes (> …), horizontal rules (---/===), and pipe tables."""
    if not text or not text.strip():
        return
    # Model output is untrusted: drop escape sequences before WE apply our
    # own styling below (prompt spoofing / cursor hijack otherwise).
    text = _strip_ansi(text)
    # model-supplied NULs would collide with our stash markers below and
    # corrupt output (or crash the index restore)
    text = text.replace("\x00", "")

    def inline(s: str) -> str:
        stash: list[str] = []

        def keep(m: "re.Match[str]") -> str:
            stash.append(ui_dim(m.group(1)))  # `code` dim (theme)
            return f"\x00{len(stash) - 1}\x00"

        s = re.sub(r"`([^`\n]+)`", keep, s)
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                   lambda m: m.group(1) + ui_dim(f" ({m.group(2)})"), s)
        s = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: ui_bold(m.group(1)), s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?!\*)",
                   lambda m: _c("3", m.group(1)), s)
        s = re.sub(r"(?<![\w_])_([^_\n]+)_(?!\w)",
                   lambda m: _c("3", m.group(1)), s)
        return re.sub(r"\x00(\d+)\x00",
                      lambda m: stash[int(m.group(1))]
                      if int(m.group(1)) < len(stash) else "", s)

    def _is_pipe_row(line: str) -> bool:
        s = line.strip()
        return s.startswith("|") and s.endswith("|") and s.count("|") >= 2

    def _is_sep_row(line: str) -> bool:
        return _is_pipe_row(line) and bool(re.match(r"^[|\-: ]+$", line.strip()))

    def _print_pipe_row(line: str, is_header: bool) -> None:
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        row = ui_faint("│")
        for c in cols:
            styled = inline(c)
            pad = " " * max(1, 10 - len(_strip_ansi(styled)))
            cell = (ui_bold(styled) if is_header else styled) + pad
            row += " " + cell + ui_faint("│")
        print(row)

    fence_open = False
    all_lines = text.splitlines()
    i = 0
    while i < len(all_lines):
        raw = all_lines[i]
        stripped = raw.strip()

        # fenced code blocks
        if stripped.startswith("```"):
            fence_open = not fence_open
            lang = stripped[3:].strip()
            print(ui_dim("  ┌─" + (f"─ {lang}" if lang else "──")) if fence_open
                  else ui_dim("  └──"))
            i += 1
            continue
        if fence_open:
            print("  " + ui_dim(raw))
            i += 1
            continue

        # horizontal rules: three or more -, *, or _ alone on a line
        if re.match(r"^\s*[-*_]{3,}\s*$", stripped):
            cols = max(40, shutil.get_terminal_size().columns - 2)
            print(ui_faint("─" * min(cols, 72)))
            i += 1
            continue

        # ATX headings
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            print(ui_bold(ui_accent(m.group(2))))
            i += 1
            continue

        # blockquotes
        if stripped.startswith(">"):
            content = stripped[1:].lstrip(" ")
            print(ui_faint("▌ ") + ui_dim(inline(content)))
            i += 1
            continue

        # pipe tables (header row followed by separator row)
        if _is_pipe_row(raw) and not _is_sep_row(raw):
            if i + 1 < len(all_lines) and _is_sep_row(all_lines[i + 1]):
                _print_pipe_row(raw, is_header=True)
                i += 2  # skip separator row
                while i < len(all_lines) and _is_pipe_row(all_lines[i]):
                    _print_pipe_row(all_lines[i], is_header=False)
                    i += 1
                continue
            _print_pipe_row(raw, is_header=False)
            i += 1
            continue

        # bullets with arbitrary nesting indent
        m = re.match(r"^(\s*)[-*]\s+(.*)$", raw)
        if m:
            depth = len(m.group(1))
            bullet = "  " * (depth // 2) + "  · "
            print(bullet + inline(m.group(2)))
            i += 1
            continue

        # numbered lists
        m = re.match(r"^(\s*)(\d+[.])\s+(.*)$", raw)
        if m:
            print(f"{m.group(1)}{m.group(2)} {inline(m.group(3))}")
            i += 1
            continue

        # blank line
        if not stripped:
            print()
            i += 1
            continue

        print(inline(raw))
        i += 1

# ---- original buddy.py lines 4792-4792 --------------------------------


# ---- original buddy.py lines 4793-4852 --------------------------------
class _Activity:
    """Live 'working · N steps · elapsed' status line for the terminal REPL.

    A threading.Lock guards every stdout write so that pause() is
    *synchronous*: it waits for the spin thread to finish its current
    write before clearing the line and returning.  This prevents the
    spinner from overwriting streaming delta text when _on_delta calls
    pause() immediately before printing.

    In full-screen mode the display is owned by the _FullScreen driver;
    _Activity then only forwards events and never touches stdout.
    """

    FR = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self.steps = 0
        self.last = ""
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._th: threading.Thread | None = None
        self._lock = threading.Lock()   # serializes stdout writes with pause/end
        self._t0: float = 0.0

    def _enabled(self):
        if _FS is not None:  # full-screen driver draws its own indicator
            return False
        return sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def reset(self):
        # Stop any running thread first so we don't double-start on the next call
        if self._th is not None:
            self._stop.set()
            self._th.join(timeout=1)
            self._th = None
        self.steps = 0
        self.last = ""
        self._stop.clear()
        self._pause.clear()
        self._t0 = 0.0

    def start(self):
        if _FS is not None:
            _FS.working_start()
            return
        if self._enabled() and not self._th:
            self._t0 = time.monotonic()
            self._th = threading.Thread(target=self._spin, daemon=True)
            self._th.start()

    def _spin(self):
        i = 0
        while not self._stop.wait(0.10):
            if self._pause.is_set():
                continue
            f = self.FR[i % len(self.FR)]
            i += 1
            elapsed = time.monotonic() - self._t0
            label = f"working · {self.steps} steps · {elapsed:.0f}s"
            if self.last:
                label += f" · 🔧 {self.last}"
            with self._lock:
                if not self._pause.is_set() and not self._stop.is_set():
                    sys.stdout.write(f"\r\033[K  {f} {label}")
                    sys.stdout.flush()

    def tick(self, last=None):
        if _FS is not None:
            _FS.tool_tick(last or "")
            return
        self.steps += 1
        if last:
            self.last = last
        self._pause.clear()

    def pause(self):
        """Synchronously pause the spinner and clear the status line.

        Acquiring _lock guarantees the spin thread has finished its
        current write before we clear, so streaming text is never
        corrupted by a concurrent spinner update.
        """
        if _FS is not None:
            return
        self._pause.set()
        with self._lock:          # wait for any in-flight spin write to finish
            if self._enabled():
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()

    def end(self):
        if _FS is not None:
            _FS.working_end()
            return
        self._stop.set()
        with self._lock:          # wait for any in-flight write
            pass
        if self._th:
            self._th.join(timeout=1)
            self._th = None
        if self._enabled():
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


# ======================================================= full-screen curses =

# _FS is the live _FullScreen driver while full-screen mode runs (None when
# the scrollback REPL is active). _Activity above consults it to hand the
# working/spinner events over to the driver.

_FS = None


def _fullscreen_ok() -> bool:
    """Full-screen mode needs a real terminal on both ends, a usable TERM
    and the curses module. Piped input, dumb terminals and Windows-without-
    curses fall back to the classic scrollback REPL — everything still
    works, it just scrolls like a normal shell command. BUDDY_TUI=plain
    forces the scrollback REPL on a real terminal."""
    if os.environ.get("BUDDY_TUI") == "plain":
        return False
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    if curses is None:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _fs_chat(*args, **kwargs) -> None:
    """Run the shared REPL loop (_chat_loop in commands) on the full-screen
    driver. Terminal setup/teardown is bracketed by try/finally so the
    terminal is ALWAYS restored — even on SystemExit (missing key) or a
    crash mid-turn."""
    drv = _FullScreen()
    if not drv.start():        # curses could not start (bad TERM, tiny tty)
        from .commands import _chat_loop
        _chat_loop(None, *args, **kwargs)
        return
    try:
        from .commands import _chat_loop
        _chat_loop(drv, *args, **kwargs)
    except BaseException:
        drv._explode = True    # replay the viewport tail after restore so
        raise                  # exit messages stay readable in scrollback
    finally:
        drv.close()


def _char_width(ch: str) -> int:
    if not ch:
        return 0
    cp = ord(ch)
    if cp < 32 or (0x7F <= cp < 0xA0):
        return 0
    w = unicodedata.east_asian_width(ch)
    return 2 if w in ("W", "F") else 1


def _str_width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def _wrap_runs(runs: list, width: int) -> list:
    """Word-wrap styled runs ((text, attr) pairs) to a cell width. Returns
    a list of rows, each a list of runs. Fully handles emojis and CJK characters."""
    out, row, x = [], [], 0
    max_w = max(1, width)
    for text, attr in runs:
        if not text:
            continue
        cur_text = []
        for ch in text:
            cw = _char_width(ch)
            if x + cw > max_w and (row or cur_text):
                if cur_text:
                    row.append(("".join(cur_text), attr))
                    cur_text = []
                out.append(row)
                row, x = [], 0
            cur_text.append(ch)
            x += cw
        if cur_text:
            row.append(("".join(cur_text), attr))
    if row or not out:
        out.append(row)
    return out


# Sentinel returned by _input_box while the model picker is open.
_MODEL_PICK = "\x00MODEL:"

# Sentinel returned by _input_box while the approve/decline dialog is open.
_CONFIRM = "\x00CONFIRM:"


class _FullScreen:
    """Antigravity (AGY) full-screen terminal UI built on curses.

    Replicates the exact layout, visual aesthetics, and interaction model of
    Google Antigravity CLI (`agy`):

    Initial Empty State:
        ┌──────────────────────────────────────────────────────────────────────────┐
        │       ▄▀▀▄        Buddy CLI v3                                          │
        │      ▀▀▀▀▀▀       user@localhost                                        │
        │     ▀▀▀▀▀▀▀▀      Gemini 3.5 Flash (Medium)                             │
        │    ▄▀▀    ▀▀▄     ~/buddy                                               │
        │   ▄▀▀      ▀▀▄                                                          │
        │                                                                          │
        │ ──────────────────────────────────────────────────────────────────────── │
        │ > Accept-edits mode: file edits auto-approved (shift+tab to cycle)       │
        │ ──────────────────────────────────────────────────────────────────────── │
        │ ? for shortcuts                     accept-edits · Gemini 3.5 Flash · AI: Ready │
        └──────────────────────────────────────────────────────────────────────────┘

    Active Conversation State:
        - Viewport: lines scroll smoothly above the pinned divider.
        - Pinned Dividers: clean horizontal rules framing the `> ` prompt.
        - Modes: `accept-edits`, `plan`, `bypass` (cycled with Shift+Tab or Tab).
        - Slash Palette: clean command list below divider with `esc to cancel`.
        - Shortcuts Modal: opened with `?` at empty prompt.
    """

    _PAIRS: dict = {}
    _NEXT = [1]

    _CSI = re.compile(
        r"\x1b\[([0-9;?]*)([A-Za-z])"
        r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
        r"|\x1b[()][A-Za-z0-9]"
    )

    _LOGO = [
        " ____  _    _ _____  _______     __",
        "|  _ \\| |  | |  __ \\|  __ \\ \\   / /",
        "| |_) | |  | | |  | | |  | \\ \\_/ /",
        "|  _ <| |  | | |  | | |  | |\\   /",
        "| |_) | |__| | |__| | |__| | | |",
        "|____/ \\____/|_____/|_____/  |_|",
    ]

    _MODES = ["accept-edits", "plan", "bypass"]

    @property
    def mode(self) -> str:
        return self._MODES[self.mode_idx]

    @mode.setter
    def mode(self, val: str) -> None:
        if val in self._MODES:
            self.mode_idx = self._MODES.index(val)
            self.dirty = True

    def __init__(self):
        self.lines: list = []
        self._pend: list = []
        self._pen = (0, False, False, False)
        self._code = False  # inside a ``` fenced code block (for tinting)
        self.scroll = 0

        self.lock = threading.RLock()
        self.dirty = True
        self.working = False
        self.steps = 0
        self.tool = ""
        self.frame = "⠋"
        self._t0 = 0.0

        self.session: dict = {"cfg": {}, "messages": []}
        self.history: list = []

        self._stdscr = None
        self._real = None
        self._old_input = None
        self._colors = False
        self._old_mouse = None
        self._mouse_on = False
        self._mouse_full = False
        self._base_mask = 0
        self._selecting = None  # {"anchor": (row, col), "current": (row, col)}
        self._wrapcache: dict = {}
        self._last_draw = 0.0
        self._size = (0, 0)
        self._in_box = False
        self._explode = False

        self._buf = ""
        self._pos = 0
        self._sel = 0
        self._hist_idx: int | None = None  # None = not browsing history

        self._popup = None
        self._model_pick = None  # {"items": [...], "sel": idx} while picking
        self._confirm = None  # {"cmd": str, "sel": 0|1|2} while confirming
        self._dialog = None
        self._shortcuts_open = False

        # agy mode: accept-edits (default), plan, bypass
        self.mode_idx = 0

        self._stop = threading.Event()
        self._anim: threading.Thread | None = None
        self._old_sigwinch = None

    # ─────────────────────────────────────────────────────────── lifecycle

    def start(self) -> bool:
        global _FS
        os.environ.setdefault("ESCDELAY", "30")
        try:
            # bracketed paste ON: the terminal wraps pastes in ESC[200~…
            # ESC[201~ so the input loop can coalesce them — a pasted
            # newline must never submit (or execute) anything
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()
            self._stdscr = curses.initscr()
            self._colors = False
            if curses.has_colors() and not os.environ.get("NO_COLOR"):
                curses.start_color()
                try:
                    curses.use_default_colors()
                except curses.error:
                    pass
                self._colors = True
            curses.noecho()
            curses.cbreak()
            self._stdscr.keypad(True)
            self._stdscr.nodelay(True)
            # Wheel-only reporting by default: the full-screen UI owns the
            # screen, so without it the wheel/trackpad cannot scroll chat.
            # Buttons stay with the terminal (select/copy, right-click menu,
            # middle-click paste). F9 switches to the full in-app mouse.
            self._mouse_enable(full=False)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        except Exception:
            self._stdscr = None
            try:
                curses.endwin()
            except Exception:
                pass
            return False

        self._real = sys.stdout
        self._old_input = builtins.input
        sys.stdout = self
        try:
            # If setup after the stdout swap raises, the terminal would be
            # left hijacked (stdout replaced, input hooked, no cursor).
            builtins.input = self._input_hook
            _FS = self

            import signal
            def _winch(sig, frame):
                try:
                    import fcntl, struct, termios as _t
                    buf = fcntl.ioctl(sys.stderr.fileno(), _t.TIOCGWINSZ, b"\x00" * 8)
                    rows, cols = struct.unpack("HHHH", buf)[:2]
                    if rows > 0 and cols > 0:
                        curses.resizeterm(rows, cols)
                        with self.lock:
                            self.dirty = True
                            self._wrapcache.clear()
                except Exception:
                    pass
            self._old_sigwinch = signal.signal(signal.SIGWINCH, _winch)

            self._anim = threading.Thread(target=self._anim_spin, daemon=True)
            self._anim.start()
        except Exception:
            self.close()
            raise
        return True

    def close(self) -> None:
        global _FS
        _FS = None
        self._stop.set()
        if self._anim:
            self._anim.join(timeout=1)
            self._anim = None

        if self._old_sigwinch is not None:
            try:
                import signal
                signal.signal(signal.SIGWINCH, self._old_sigwinch)
            except Exception:
                pass

        if self._old_input is not None:
            builtins.input = self._old_input
            self._old_input = None
        if self._real is not None:
            sys.stdout = self._real
        self._save_history()
        if self._stdscr is not None:
            try:
                self._mouse_disable()  # stop wheel/click reports
                self._stdscr.erase()
                self._stdscr.refresh()
                curses.endwin()
            except Exception:
                pass
            self._stdscr = None
        try:
            sys.stdout.write("\x1b[?2004l")  # bracketed paste OFF
            sys.stdout.flush()
        except Exception:
            pass
        if self._explode:
            self._replay_tail()
        self._real = None

    def _save_history(self) -> None:
        if not self.history:
            return
        try:
            hist = HOME / "history"
            hist.parent.mkdir(parents=True, exist_ok=True)
            try:
                old = [l for l in hist.read_text(encoding="utf-8",
                                                  errors="replace").splitlines() if l]
            except OSError:
                old = []
            merged = (old[-490:] + self.history)[-500:]
            hist.write_text("\n".join(merged) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _replay_tail(self) -> None:
        try:
            rows = ["".join(t for t, _ in runs)[-200:] for runs in self.lines[-40:]]
            tail = "\n".join(rows).strip("\n")
            if tail and self._real:
                self._real.write(tail + "\n")
                self._real.flush()
        except Exception:
            pass

    # ──────────────────────────────────── sys.stdout capture

    def write(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            self._feed(text)
            if self._in_box:
                self.dirty = True
            else:
                now = time.time()
                if now - self._last_draw >= 0.05:
                    self._draw()
                else:
                    self.dirty = True

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"

    def _feed(self, text: str) -> None:
        pos = 0
        for m in self._CSI.finditer(text):
            if m.start() > pos:
                self._text(text[pos:m.start()])
            if m.group(2) == "m":
                self._sgr(m.group(1))
            pos = m.end()
        if pos < len(text):
            self._text(text[pos:])

    def _text(self, s: str) -> None:
        if not s:
            return
        attr = self._cattr()
        for i, seg in enumerate(s.split("\n")):
            if i:
                if self._pend or not self._last_blank():
                    self.lines.append(self._pend or [("", 0)])
                self._pend = []
                if len(self.lines) > 8000:
                    del self.lines[:1000]
                    self._wrapcache.clear()
            seg = "".join(c for c in seg.replace("\t", "    ") if ord(c) >= 32)
            if not seg:
                continue
            # Fenced code blocks + markdown headings get theme tint:
            # the TUI is fed plain streamed text, so the tint is applied
            # at the line level here rather than by the producer.
            stripped = seg.lstrip()
            if stripped.startswith("```"):
                self._code = not self._code
                self._pend.append((seg, self._role("c_faint", curses.A_DIM)))
                continue
            if self._code:
                self._pend.append((seg, attr | self._role("c_info")))
                continue
            if re.match(r"#{1,4}\s+\S", stripped):
                self._pend.append((seg, attr | self._role("c_accent")
                                   | curses.A_BOLD))
                continue
            self._pend.append((seg, attr))

    def _last_blank(self) -> bool:
        if not self.lines:
            return False
        r = self.lines[-1]
        return not r or all(not t.strip() for t, _ in r)

    def _sgr(self, params: str) -> None:
        # Extended colors: util._THEME emits "38;5;N[;1]" (and possibly
        # "38;2;r;g;b"), so parse positionally instead of per-code.
        fg, bold, dim, italic = self._pen
        try:
            codes = [int(x) if x else 0 for x in params.split(";")]
        except ValueError:
            return
        i = 0
        while i < len(codes):
            p = codes[i]
            if p == 0:
                fg, bold, dim, italic = 0, False, False, False
            elif p == 1:
                bold = True
            elif p == 2:
                dim = True
            elif p == 3:
                italic = True
            elif p == 4:
                bold = True  # no underline pen: nearest is bold
            elif p == 22:
                bold = dim = False
            elif p == 23:
                italic = False
            elif p == 24:
                pass  # underline off (folded into bold above)
            elif p == 38:
                # extended foreground: 38;5;N | 38;2;r;g;b
                if i + 2 < len(codes) and codes[i + 1] == 5:
                    fg = codes[i + 2]
                    i += 2
                elif i + 4 < len(codes) and codes[i + 1] == 2:
                    try:
                        from .theme import _hex_to_256 as _h256
                        fg = _h256("#%02x%02x%02x" % (
                            codes[i + 2], codes[i + 3], codes[i + 4]))
                    except Exception:
                        pass
                    i += 4
            elif p == 39:
                fg = 0
            elif p == 48:
                # extended background: consume 48;5;N | 48;2;r;g;b (ignored)
                if i + 2 < len(codes) and codes[i + 1] == 5:
                    i += 2
                elif i + 4 < len(codes) and codes[i + 1] == 2:
                    i += 4
            elif 30 <= p <= 37:
                fg = p - 30
            elif 40 <= p <= 47:
                pass  # background ignored in fullscreen pen
            elif 90 <= p <= 97:
                fg = p - 90 + 8
            elif 100 <= p <= 107:
                pass  # bright background ignored
            i += 1
        self._pen = (fg, bold, dim, italic)

    def _cattr(self) -> int:
        fg, bold, dim, italic = self._pen
        attr = 0
        if bold:
            attr |= curses.A_BOLD
        if dim:
            attr |= curses.A_DIM
        if fg and self._colors:
            attr |= self._color_pair(fg, -1)
        return attr

    def _color_pair(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        if key not in self._PAIRS:
            n = self._NEXT[0]
            self._NEXT[0] += 1
            try:
                curses.init_pair(n, fg, bg)
                self._PAIRS[key] = curses.color_pair(n)
            except curses.error:
                self._PAIRS[key] = 0
        return self._PAIRS[key]

    def _role(self, role: str, extra: int = 0) -> int:
        if not self._colors:
            return extra
        theme = _THEME
        val = theme.get(role, "")
        if not val:
            return extra
        try:
            if isinstance(val, (tuple, list)):
                # (xterm-256 index, 8-color fallback) as written by theme.apply
                idx = int(val[0])
                return self._color_pair(idx, -1) | extra
            from .theme import _hex_to_256 as _h256_role
            idx = _h256_role(val)
            return self._color_pair(idx, -1) | extra
        except Exception:
            return extra

    # ──────────────────────────────────── public API

    def working_start(self) -> None:
        with self.lock:
            self.working = True
            self.steps = 0
            self.tool = ""
            self._t0 = time.time()
            self._draw()

    def working_end(self) -> None:
        with self.lock:
            self.working = False
            self.steps = 0
            self.tool = ""
            self._draw()

    def tool_tick(self, name: str) -> None:
        with self.lock:
            self.steps += 1
            self.tool = name

    def status(self, cfg: dict, messages: list) -> None:
        with self.lock:
            self.session = {"cfg": cfg, "messages": messages}
            self._draw()

    def welcome(self, cfg: dict, where: str, n_mcp: int) -> None:
        with self.lock:
            self.session = {"cfg": cfg, "messages": []}
            self._draw()
        # config "mouse": true = full in-app mouse from the start (drag
        # highlight + click-paste). Default stays wheel-only so selection,
        # the right-click menu and middle-click paste remain the terminal's.
        # (status() deliberately doesn't re-apply: an F9 toggle mid-session
        # must stick.)
        try:
            if (cfg or {}).get("mouse") is True and not self._mouse_full:
                self._mouse_enable(full=True)
        except Exception:
            pass

    def redraw(self) -> None:
        with self.lock:
            self._draw()

    def input_line(self, prompt: str = "") -> str:
        line = self._input_box()
        self.scroll = 0
        self.history.append(line)
        if line:
            with self.lock:
                self._rhythm()
                self.write(ui_accent("> ") + ui_bold(line.replace("\x1b", "")) + "\n")
        return line

    def _input_hook(self, prompt: str = "") -> str:
        if prompt:
            with self.lock:
                self._rhythm()
            self.write(prompt)
        ans = self._input_box()
        self.scroll = 0
        self.history.append(ans)
        self.write(ans + "\n")
        return ans

    def _rhythm(self) -> None:
        if self.lines and not self._last_blank():
            self.lines.append([("", 0)])
            self._wrapcache.clear()

    def _scroll_by(self, delta: int) -> None:
        """Scroll the viewport by `delta` logical lines (+ = older/up).

        Clamped so the window never runs past the newest line (0) or
        beyond the oldest buffered line. New chat input resets to 0
        (jump to bottom) in input_line/_input_hook."""
        with self.lock:
            src_n = len(self.lines) + (1 if self._pend else 0)
            self.scroll = max(0, min(max(0, src_n), self.scroll + delta))
            self._selecting = None  # scrolling invalidates the highlight
            self.dirty = True

    def _mouse_scroll(self) -> None:
        """Handle one KEY_MOUSE event: wheel scrolls, button-1 drag
        highlights text to copy (see _mouse_event).

        Returns None so the input loop redraws and keeps waiting — a
        mouse event never submits the prompt."""
        return self._mouse_event()

    def _wheel_bits(self) -> tuple:
        _up = (getattr(curses, "BUTTON4_PRESSED", 0)
               | getattr(curses, "BUTTON4_CLICKED", 0)
               | getattr(curses, "BUTTON4_DOUBLE_CLICKED", 0)
               | getattr(curses, "BUTTON4_TRIPLE_CLICKED", 0))
        _down = (getattr(curses, "BUTTON5_PRESSED", 0)
                 | getattr(curses, "BUTTON5_CLICKED", 0)
                 | getattr(curses, "BUTTON5_DOUBLE_CLICKED", 0)
                 | getattr(curses, "BUTTON5_TRIPLE_CLICKED", 0))
        return _up, _down

    def _mouse_event(self) -> None:
        """Dispatch one KEY_MOUSE event: wheel → scroll, left-drag →
        highlight-to-copy. Never submits the prompt (returns None)."""
        if curses is None:
            return None
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except Exception:
            return None
        _up, _down = self._wheel_bits()
        if _up and (bstate & _up):
            if not self._selecting:
                self._scroll_by(3)
            return None
        if _down and (bstate & _down):
            if not self._selecting:
                self._scroll_by(-3)
            return None
        # Right/middle-click: paste the clipboard at the input cursor
        # (press/click only — not release, so one click pastes once).
        _paste_bits = 0
        for _n in (2, 3):
            for _s in ("PRESSED", "CLICKED", "DOUBLE_CLICKED",
                       "TRIPLE_CLICKED"):
                _paste_bits |= getattr(curses, f"BUTTON{_n}_{_s}", 0)
        if _paste_bits and (bstate & _paste_bits):
            if self._selecting:
                self._selection_cancel()
            self._paste_from_clipboard()
            return None
        _b1_rel = (getattr(curses, "BUTTON1_RELEASED", 0)
                   | getattr(curses, "BUTTON1_CLICKED", 0)
                   | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
                   | getattr(curses, "BUTTON1_TRIPLE_CLICKED", 0))
        _b1_press = getattr(curses, "BUTTON1_PRESSED", 0)
        if _b1_rel and (bstate & _b1_rel):
            if self._selecting:
                self._selection_finish()
            else:
                self._selection_cancel()
            return None
        # Any other button-1 event while selecting is a drag update;
        # outside a selection a press starts one.
        if self._selecting:
            self._selection_update(mx, my)
        elif _b1_press and (bstate & _b1_press):
            self._selection_begin(mx, my)
        return None

    # ─────────────────── highlight-to-copy selection ─────────────

    def _display_model(self) -> tuple:
        """Rows currently shown in the viewport, for mouse mapping.

        Returns (rows, vw, vh); rows[k] is one screen row (list of
        (text, attr)), display index k == screen y. Empty when there
        is nothing selectable."""
        H, W = self._size if self._size != (0, 0) else (None, None)
        if H is None and self._stdscr is not None:
            try:
                H, W = self._stdscr.getmaxyx()
            except Exception:
                return [], 0, 0
        if not H or not W or H < 10 or W < 20:
            return [], 0, 0
        if not self.lines and not self._pend:
            return [], 0, 0
        popup_h = min(6, len(self._popup[0])) + 3 if self._popup else 0
        prompt_top = max(1, H - 4 - popup_h)
        vh = max(1, prompt_top)
        vw = max(1, W - 2)
        with self.lock:
            src_n = len(self.lines) + (1 if self._pend else 0)
            start = max(0, src_n - 1 - max(0, self.scroll))
            rows = []
            for i in range(max(0, start - vh), start + 1):
                rows.extend(self._wrapped(i, vw))
        return rows[-vh:], vw, vh

    def _point_to_cell(self, mx: int, my: int) -> "tuple | None":
        """Map a mouse (x, y) to a (display row, cell col), or None."""
        rows, _vw, _vh = self._display_model()
        if not rows:
            return None
        return (max(0, min(my, len(rows) - 1)), max(0, mx - 1))

    def _selection_mask(self, on: bool) -> None:
        """Add motion reports while an in-app drag is active. Only
        meaningful in full mouse mode — in wheel-only mode we never claim
        button 1, so the terminal keeps doing its own selection."""
        if curses is None or self._stdscr is None or not self._mouse_full:
            return
        try:
            mask = self._base_mask
            if on:
                mask |= getattr(curses, "REPORT_MOUSE_POSITION", 0)
            curses.mousemask(mask)
        except Exception:
            pass

    def _selection_begin(self, mx: int, my: int) -> None:
        pt = self._point_to_cell(mx, my)
        if pt is None:
            return
        with self.lock:
            self._selecting = {"anchor": pt, "current": pt}
            self.dirty = True
        self._selection_mask(True)

    def _selection_update(self, mx: int, my: int) -> None:
        pt = self._point_to_cell(mx, my)
        if pt is None:
            return
        with self.lock:
            if self._selecting is not None:
                self._selecting["current"] = pt
                self.dirty = True

    def _selection_cancel(self) -> None:
        with self.lock:
            self._selecting = None
            self.dirty = True
        self._selection_mask(False)

    def _selection_span(self) -> "tuple | None":
        sel = self._selecting
        if sel is None:
            return None
        (ar, ac), (cr, cc) = sel["anchor"], sel["current"]
        if (ar, ac) > (cr, cc):
            (ar, ac), (cr, cc) = (cr, cc), (ar, ac)
        return (ar, ac, cr, cc)

    def _selection_text(self) -> str:
        """Plain text inside the current highlight (cell-precise)."""
        span = self._selection_span()
        if span is None:
            return ""
        ar, ac, cr, cc = span
        rows, _vw, _vh = self._display_model()
        if not rows:
            return ""
        ar, cr = max(0, ar), min(len(rows) - 1, cr)
        out = []
        for k in range(ar, cr + 1):
            line = "".join(t for t, _ in rows[k])
            start_c = ac if k == ar else 0
            end_c = cc if k == cr else 10 ** 9
            chars, cx = [], 0
            for ch in line:
                w = _char_width(ch)
                if cx + w > start_c and cx < end_c:
                    chars.append(ch)
                cx += w
            out.append("".join(chars))
        return "\n".join(out).strip("\n")

    def _selection_finish(self) -> None:
        text = self._selection_text()
        self._selection_cancel()
        if not text.strip():
            return
        self.write(ui_dim(self._to_clipboard(text)) + "\n")

    # ─────────────────── right-click paste ───────────────────────

    @staticmethod
    def _read_clipboard() -> "str | None":
        """Clipboard text, '' when empty, None when no tool exists."""
        for cmd in (["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]):
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=5)
                if p.returncode == 0:
                    return p.stdout
            except (FileNotFoundError, OSError, subprocess.SubprocessError):
                continue
            except Exception:
                continue
        return None

    def _paste_from_clipboard(self) -> None:
        """Right/middle-click: insert clipboard text at the input cursor."""
        if self._shortcuts_open:
            return
        text = self._read_clipboard()
        if text is None:
            self.write(ui_dim("(paste failed — install wl-clipboard or xclip)") + "\n")
            return
        # Single-line prompt: flatten newlines/tabs, drop control chars.
        text = (text.replace("\x1b", "").replace("\r\n", " ")
                    .replace("\r", " ").replace("\n", " ")
                    .replace("\t", "    "))
        text = "".join(c for c in text if ord(c) >= 32)
        if not text:
            return
        text = text[:50000]  # safety cap
        with self.lock:
            self._buf = self._buf[:self._pos] + text + self._buf[self._pos:]
            self._pos += len(text)
            self._hist_idx = None
            self._update_popup()
            self.dirty = True

    # ─────────────────────────── mouse reporting + copying ──────

    def _wheel_mask(self) -> int:
        """Report ONLY the wheel (buttons 4/5).

        This is the sweet spot: the full-screen UI owns the screen, so
        without wheel reports the terminal has no scrollback to scroll and
        the wheel/trackpad does nothing. But claiming button 1/2/3 as well
        breaks the things users expect from a terminal — drag-to-select,
        the right-click menu, middle-click paste. Terminals gate each
        button on whether it is reported, so asking for the wheel alone
        gives working scrolling AND normal selection/copy/paste."""
        if curses is None:
            return 0
        mask = 0
        for n in (4, 5):
            for suffix in ("PRESSED", "RELEASED", "CLICKED",
                           "DOUBLE_CLICKED", "TRIPLE_CLICKED"):
                mask |= getattr(curses, f"BUTTON{n}_{suffix}", 0)
        return mask

    def _mouse_enable(self, full: bool = False) -> None:
        """Enable mouse reports. `full` also claims the buttons (in-app
        drag-highlight + click-paste); default is wheel-only."""
        if curses is None or self._stdscr is None:
            return
        try:
            if full:
                self._base_mask = (curses.ALL_MOUSE_EVENTS
                                   & ~curses.REPORT_MOUSE_POSITION)
            else:
                self._base_mask = self._wheel_mask()
            self._mouse_full = bool(full)
            self._old_mouse = curses.mousemask(self._base_mask)
            curses.mouseinterval(0)
            self._mouse_on = True
        except Exception:
            self._base_mask = 0
            self._mouse_on = False

    def _mouse_disable(self) -> None:
        """Disable all mouse reports so the terminal selects text normally."""
        self._selection_cancel()
        if curses is None or self._stdscr is None:
            return
        try:
            curses.mousemask(0)
        except Exception:
            pass
        self._mouse_on = False
        self._mouse_full = False
        self._base_mask = 0

    def _mouse_toggle(self) -> None:
        """F9: wheel-only <-> full in-app mouse (drag highlight, click paste).

        Wheel scrolling stays available in both modes; F9 is only about
        whether buddy claims the mouse buttons itself."""
        if self._mouse_full:
            self._mouse_enable(full=False)
            self.write(ui_dim("(mouse: wheel only — select/copy and right-click "
                              "are the terminal's again)") + "\n")
        else:
            self._mouse_enable(full=True)
            self.write(ui_dim("(mouse: full — drag to highlight & copy, "
                              "right-click to paste; F9 back to wheel only)") + "\n")

    def _viewport_text(self, n: int = 150) -> str:
        """Plain text of the last `n` chat lines (+ any pending output)."""
        with self.lock:
            rows = list(self.lines[-n:])
            if self._pend:
                rows = rows + [self._pend]
        return "\n".join("".join(t for t, _ in runs) for runs in rows).strip("\n")

    def _to_clipboard(self, text: str) -> str:
        """Copy text out: OSC52 now (fast), native tools in a background
        thread — this runs INSIDE the display lock, and a 5s×2 wl-copy/xclip
        timeout used to freeze rendering for ~10s."""
        clipped = text[:100000]  # safety cap
        try:  # OSC52: works over SSH, no tools needed (terms may confirm/ignore)
            if self._real is not None:
                import base64 as _b64
                self._real.write("\x1b]52;c;" + _b64.b64encode(
                    clipped.encode("utf-8")).decode() + "\x07")
                self._real.flush()
        except Exception:
            pass

        def _native_copy():
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
                try:
                    p = subprocess.run(cmd, input=clipped, text=True, timeout=5)
                    if p.returncode == 0:
                        return
                except (FileNotFoundError, OSError, subprocess.SubprocessError):
                    continue
                except Exception:
                    continue
            try:  # file fallback — always works
                d = HOME / "copies"
                d.mkdir(parents=True, exist_ok=True)
                fp = d / (time.strftime("%Y%m%d-%H%M%S") + ".txt")
                fp.write_text(clipped, encoding="utf-8")
            except OSError:
                pass  # OSC52 may still have delivered it

        threading.Thread(target=_native_copy, daemon=True).start()
        return f"(copied {len(clipped)} chars to clipboard)"

    def _copy_viewport(self) -> None:
        text = self._viewport_text()
        if not text.strip():
            self.write(ui_dim("(nothing to copy yet)") + "\n")
            return
        self.write(ui_dim(self._to_clipboard(text)) + "\n")

    # ─────────────────────────── model picker (arrow-key) ─────────────

    def pick_model(self, entries: list) -> "dict | None":
        """Arrow-key provider/model picker for bare `/model`. Returns the
        chosen entry dict, or None on cancel/empty. Echoes the choice
        into the transcript."""
        items = list(entries or [])
        if not items:
            return None
        sel = next((i for i, e in enumerate(items) if e.get("active")), 0)
        self._model_pick = {"items": items, "sel": sel}
        self._buf, self._pos, self._sel = "", 0, 0
        self._popup = None
        try:
            res = self._input_box()
        finally:
            self._model_pick = None
            self._buf, self._pos, self._sel = "", 0, 0
        if isinstance(res, str) and res.startswith(_MODEL_PICK):
            n = res[len(_MODEL_PICK):]
            if n == "CANCEL":
                return None
            try:
                return items[int(n)]
            except (ValueError, IndexError):
                return None
        return None

    def _pick_rows(self) -> tuple:
        """Picker display rows + selected row: ([("head"|"item", payload)],
        sel_row). Headers are not selectable."""
        from .commands import _group_model_entries
        pick = self._model_pick or {"items": [], "sel": 0}
        rows: list = []
        sel_row = 0
        for title, group in _group_model_entries(pick["items"]):
            rows.append(("head", title))
            for e in group:
                if e.get("n", 0) - 1 == pick["sel"]:
                    sel_row = len(rows)
                rows.append(("item", e))
        return rows, sel_row

    def _pick_key(self, ch) -> "str | None":
        """Keys while the model picker is open: arrows move, Enter picks,
        Esc/Ctrl-C/Ctrl-D cancels; anything else is ignored."""
        pick = self._model_pick
        items = pick["items"]
        if isinstance(ch, str):
            if ch in ("\r", "\n"):
                return _MODEL_PICK + str(pick["sel"])
            if ch in ("\x1b", "\x03"):
                return _MODEL_PICK + "CANCEL"
            return None
        if ch == curses.KEY_UP:
            pick["sel"] = (pick["sel"] - 1) % len(items)
            return None
        if ch == curses.KEY_DOWN:
            pick["sel"] = (pick["sel"] + 1) % len(items)
            return None
        if ch in (curses.KEY_ENTER, 10, 13):
            return _MODEL_PICK + str(pick["sel"])
        if ch == curses.KEY_PPAGE:
            pick["sel"] = max(0, pick["sel"] - 10)
            return None
        if ch == curses.KEY_NPAGE:
            pick["sel"] = min(len(items) - 1, pick["sel"] + 10)
            return None
        if ch == 4:  # Ctrl-D
            return _MODEL_PICK + "CANCEL"
        return None

    # ─────────────────── approve/decline confirm dialog ─────────────

    _CONFIRM_OPTS = ("allow", "always", "decline")

    def ask_confirm(self, cmd: str) -> str:
        """Interactive approve/decline dialog for one shell command.

        Arrow keys (or ←/→) move between allow · always · decline,
        y/n/a answer directly, Enter confirms, Esc declines. Returns
        "allow", "always", or "decline" (never raises)."""
        self._confirm = {"cmd": cmd, "sel": 0}
        self._buf, self._pos, self._sel = "", 0, 0
        self._popup = None
        try:
            res = self._input_box()
        finally:
            self._confirm = None
            self._buf, self._pos, self._sel = "", 0, 0
        if isinstance(res, str) and res.startswith(_CONFIRM):
            ans = res[len(_CONFIRM):]
            if ans in self._CONFIRM_OPTS:
                return ans
        return "decline"

    def _confirm_key(self, ch) -> "str | None":
        """Keys while the confirm dialog is open."""
        n = len(self._CONFIRM_OPTS)
        if isinstance(ch, str):
            low = ch.lower()
            if low == "y":
                return _CONFIRM + "allow"
            if low == "a":
                return _CONFIRM + "always"
            if low == "n":
                return _CONFIRM + "decline"
            if ch in ("\r", "\n"):
                return _CONFIRM + self._CONFIRM_OPTS[self._confirm["sel"]]
            if ch in ("\x1b", "\x03"):
                return _CONFIRM + "decline"
            return None
        if ch in (curses.KEY_LEFT, curses.KEY_UP):
            self._confirm["sel"] = (self._confirm["sel"] - 1) % n
            return None
        if ch in (curses.KEY_RIGHT, curses.KEY_DOWN):
            self._confirm["sel"] = (self._confirm["sel"] + 1) % n
            return None
        if ch in (curses.KEY_ENTER, 10, 13):
            return _CONFIRM + self._CONFIRM_OPTS[self._confirm["sel"]]
        if ch == 4:  # Ctrl-D
            return _CONFIRM + "decline"
        return None

    # ──────────────────────────────────── animation

    def _anim_spin(self) -> None:
        frames = _Activity.FR
        i = 0
        while not self._stop.wait(0.12):
            if not self.working or self._in_box:
                continue
            with self.lock:
                self.frame = frames[i % len(frames)]
                i += 1
                self._draw()

    def _poll_repaint(self) -> bool:
        with self.lock:
            try:
                size = self._stdscr.getmaxyx()
            except Exception:
                return False
        if size != self._size or self.dirty:
            self._size = size
            return True
        return False

    # ──────────────────────────────────── input loop

    def _paste_wch(self, scr):
        """One logical keypress, with bracketed-paste coalescing: ESC[200~
        …ESC[201~ returns as ONE multi-char string. _key inserts those
        literally and never treats them as Enter — a pasted newline must
        not submit (or execute) anything."""
        ch = scr.get_wch()
        if ch != "\x1b":
            return ch
        seq = "\x1b"
        for _ in range(5):  # probe for a paste marker after the Esc
            try:
                nxt = scr.get_wch()
            except curses.error:
                return "\x1b"  # lone Esc keypress
            seq += str(nxt)
            if seq == "\x1b[200~":
                pasted = ""
                while not pasted.endswith("\x1b[201~"):
                    try:
                        pasted += str(scr.get_wch())
                    except curses.error:
                        time.sleep(0.01)
                end = pasted.rfind("\x1b[201~")
                return pasted[:end]
            if not "\x1b[200~".startswith(seq):
                # some other escape sequence — push it back and let the
                # normal handlers see it
                for c in reversed(seq[1:]):
                    try:
                        curses.ungetwch(c)
                    except curses.error:
                        pass
                return "\x1b"
        return "\x1b"

    def _input_box(self) -> str:
        scr = self._stdscr
        self._buf, self._pos, self._sel = "", 0, 0
        self._in_box = True
        self._shortcuts_open = False
        try:
            while True:
                try:
                    if self._poll_repaint():
                        with self.lock:
                            self._draw()
                    try:
                        ch = self._paste_wch(scr)
                    except curses.error:
                        time.sleep(0.03)
                        continue
                    if isinstance(ch, int) and ch == -1:
                        time.sleep(0.03)
                        continue
                    with self.lock:
                        done = self._key(ch)
                        if done is None:
                            self._draw()
                    if done is not None:
                        return done
                except KeyboardInterrupt:
                    if self.working:
                        raise
                    self._buf, self._pos, self._sel = "", 0, 0
                    self._shortcuts_open = False
                    with self.lock:
                        self._draw()
                    continue
        finally:
            self._in_box = False
            self._buf, self._pos, self._sel = "", 0, 0

    def _palette_matches(self, buf: str) -> list:
        q = buf[1:].strip().lower()
        if not q:
            return list(_CHAT_COMMANDS)
        ms = [c for c in _CHAT_COMMANDS if q in c[1:]]
        ms.sort(key=lambda c: not c[1:].startswith(q))
        return ms

    def _update_popup(self) -> None:
        if self._buf.startswith("/") and " " not in self._buf:
            ms = self._palette_matches(self._buf)
            if ms:
                self._popup = (ms, min(self._sel, len(ms) - 1))
                return
        self._popup = None

    def _key(self, ch) -> "str | None":
        buf, pos = self._buf, self._pos

        # Model picker overlay takes over all input except mouse wheel
        # scroll (arrows move, Enter picks, Esc cancels, rest ignored).
        if self._model_pick is not None:
            return self._pick_key(ch)

        # Approve/decline dialog: arrows + y/n/a + Enter/Esc only.
        if self._confirm is not None:
            return self._confirm_key(ch)

        # Mouse/trackpad: wheel scrolls, left-drag highlights to copy.
        # (curses.KEY_MOUSE only arrives when mousemask is enabled in start.)
        if curses is not None and ch == curses.KEY_MOUSE:
            return self._mouse_event()

        # Shift+Up / Shift+Down (KEY_SR / KEY_SF where the terminfo
        # entry exists): line-wise viewport scroll without touching
        # history — keyboard fallback when the wheel isn't available.
        if curses is not None and ch == getattr(curses, "KEY_SR", -999):
            self._scroll_by(1)
            return None
        if curses is not None and ch == getattr(curses, "KEY_SF", -998):
            self._scroll_by(-1)
            return None

        # F9 / Ctrl+G: toggle mouse reporting. Reporting ON = wheel
        # scrolls but the terminal can't select text; OFF = normal
        # drag-to-select & copy (Shift+drag bypasses in most terms anyway).
        _f9 = getattr(curses, "KEY_F9", -997) if curses is not None else -997
        if ch == _f9 or ch in (7, "\x07"):
            self._mouse_toggle()
            return None

        # Ctrl+O: copy recent chat to clipboard (OSC52 + wl-copy/xclip),
        # falling back to ~/.buddy/copies/ when no clipboard tool exists.
        if ch in (15, "\x0f"):
            self._copy_viewport()
            return None

        # Shortcuts modal view controls
        if self._shortcuts_open:
            if ch in ("\x1b", "\x03", "?", "q"):
                self._shortcuts_open = False
                return None
            return None

        # Ctrl-U: clear input line
        if ch in (21, "\x15"):
            self._buf, self._pos, self._sel = "", 0, 0
            self._update_popup()
            return None

        # Ctrl-P: open slash command palette
        if ch in (16, "\x10"):
            if not self._buf.startswith("/"):
                self._buf = "/" + self._buf
                self._pos = len(self._buf)
            self._update_popup()
            return None

        # Ctrl-L: clear viewport
        if ch in (12, "\x0c"):
            self.lines.clear()
            self._wrapcache.clear()
            self.dirty = True
            return None

        # Mode cycle: Shift+Tab (KEY_BTAB) or Tab on empty prompt
        if ch == curses.KEY_BTAB or (ch == "\t" and not buf):
            self.mode_idx = (self.mode_idx + 1) % len(self._MODES)
            return None

        # '?' at empty prompt opens shortcuts
        if ch == "?" and not buf:
            self._shortcuts_open = True
            return None

        popup = self._popup is not None
        ms = self._popup[0] if popup else []

        if isinstance(ch, str):
            if len(ch) > 1:
                # bracketed paste: insert literally — pasted newlines become
                # spaces and must NEVER submit (a pasted "/forget" or "/yolo"
                # would otherwise execute with zero keystrokes)
                chunk = ch.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
                for c in chunk:
                    if ord(c) >= 32:
                        buf = self._buf
                        self._buf = buf[:self._pos] + c + buf[self._pos:]
                        self._pos += 1
                self._hist_idx = None
                self._update_popup()
                return None
            if ch in ("\r", "\n"):
                if popup and ms:
                    if buf == "/":
                        return None
                    pick = ms[self._popup[1]]
                    if pick in _ARG_COMMANDS and buf != pick:
                        self._buf, self._pos, self._sel = pick + " ", len(pick) + 1, 0
                        self._update_popup()
                        return None
                    return pick
                if buf == "/":
                    return None
                return buf

            if ch == "\x03":  # Ctrl-C
                if self.working:
                    raise KeyboardInterrupt
                self._buf, self._pos, self._sel = "", 0, 0
                self._update_popup()
                return None

            if ch == "\x1b":  # Esc
                self._buf, self._pos, self._sel = "", 0, 0
                self._update_popup()
                return None

            if ch in ("\x7f", "\x08"):
                if pos > 0:
                    self._buf = buf[:pos - 1] + buf[pos:]
                    self._pos -= 1
                    self._update_popup()
                return None

            if ch == "\t":
                if popup and ms:
                    self._sel = (self._popup[1] + 1) % len(ms)
                    self._update_popup()
                return None

            if ord(ch) >= 32:
                self._buf = buf[:pos] + ch + buf[pos:]
                self._pos += 1
                self._hist_idx = None
                self._update_popup()
            return None

        # curses special keys
        page = max(1, self._size[0] - 10)
        if ch == curses.KEY_UP:
            if popup and ms:
                self._sel = max(0, self._popup[1] - 1)
                self._update_popup()
            elif self.history:
                if self._hist_idx is None:
                    self._hist_idx = len(self.history) - 1
                else:
                    self._hist_idx = max(0, self._hist_idx - 1)
                self._buf = self.history[self._hist_idx]
                self._pos = len(self._buf)
                self._update_popup()
        elif ch == curses.KEY_DOWN:
            if popup and ms:
                self._sel = min(len(ms) - 1, self._popup[1] + 1)
                self._update_popup()
            elif self._hist_idx is not None:
                if self._hist_idx >= len(self.history) - 1:
                    self._hist_idx = None
                    self._buf, self._pos = "", 0
                else:
                    self._hist_idx += 1
                    self._buf = self.history[self._hist_idx]
                    self._pos = len(self._buf)
                self._update_popup()
        elif ch == curses.KEY_PPAGE:
            self._scroll_by(page)
        elif ch == curses.KEY_NPAGE:
            self._scroll_by(-page)
        elif ch == curses.KEY_LEFT:
            self._pos = max(0, pos - 1)
        elif ch == curses.KEY_RIGHT:
            self._pos = min(len(buf), pos + 1)
        elif ch in (curses.KEY_HOME,):
            self._pos = 0
        elif ch in (curses.KEY_END,):
            self._pos = len(buf)
        elif ch in (curses.KEY_BACKSPACE, 8, 127, 263):
            if pos > 0:
                self._buf = buf[:pos - 1] + buf[pos:]
                self._pos -= 1
                self._update_popup()
        elif ch == curses.KEY_DC:
            self._buf = buf[:pos] + buf[pos + 1:]
            self._update_popup()
        elif ch in (curses.KEY_ENTER, 10, 13):
            if popup and ms:
                if buf == "/":
                    return None
                pick = ms[self._popup[1]]
                if pick in _ARG_COMMANDS and buf != pick:
                    self._buf, self._pos, self._sel = pick + " ", len(pick) + 1, 0
                    self._update_popup()
                    return None
                return pick
            if buf == "/":
                return None
            return buf
        elif ch == 4:  # Ctrl-D
            raise EOFError
        return None

    # ──────────────────────────────────── drawing helpers

    def _draw(self) -> None:
        scr = self._stdscr
        if scr is None:
            return
        self._last_draw = time.time()
        self.dirty = False
        try:
            H, W = scr.getmaxyx()
            scr.erase()
            if H >= 10 and W >= 20:
                self._paint(scr, H, W)
            scr.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass

    def _put(self, scr, y: int, x: int, s: str, attr: int, clip: int) -> None:
        if not s or y < 0 or x >= clip:
            return
        try:
            H, W = scr.getmaxyx()
            if y >= H:
                return
            max_c = min(clip, W - 1) if y == H - 1 else min(clip, W)
            if x >= max_c:
                return
            scr.addnstr(y, x, s, max(0, max_c - x), attr)
        except curses.error:
            pass

    def _wrapped(self, i: int, W: int) -> list:
        pend = i >= len(self.lines)
        if not pend:
            got = self._wrapcache.get((W, i))
            if got is not None:
                return got
        runs = self._pend if pend else self.lines[i]
        got = _wrap_runs(runs, max(1, W))
        if not pend:
            if len(self._wrapcache) > 20000:
                self._wrapcache.clear()
            self._wrapcache[(W, i)] = got
        return got

    # ──────────────────────────────────── main paint

    def _paint(self, scr, H: int, W: int) -> None:
        cfg = self.session.get("cfg") or {}
        model = cfg.get("model", "Gemini 2.5 Flash")
        if model == "gemini-2.5-flash":
            model = "Gemini 2.5 Flash"
        dim = curses.A_DIM
        bright = curses.A_BOLD
        accent = self._role("c_accent", bright)

        mode_name = self._MODES[self.mode_idx]

        # Check if conversation is active
        empty = not self.lines and not self._pend

        # ── Shortcuts Modal view ──────────────────────────────────────
        if self._shortcuts_open:
            self._paint_shortcuts_view(scr, H, W)
            return

        # ── Calculate prompt box position ─────────────────────────────
        popup_h = min(6, len(self._popup[0])) + 3 if self._popup else 0
        if self._model_pick is not None:
            rows, _sel = self._pick_rows()
            pick_h = min(12, len(rows)) + 2
        else:
            pick_h = 0
        confirm_h = 7 if self._confirm is not None else 0
        if empty and not self._popup and self._model_pick is None and self._confirm is None:
            prompt_top = min(8, max(1, H - 4))
        else:
            prompt_top = max(1, H - 4 - popup_h - pick_h - confirm_h)

        # ── Header / Logo in empty state ──────────────────────────────
        if empty:
            user_id = os.environ.get("USER", "developer") + "@localhost"
            cwd = _short_cwd()
            effort = str(cfg.get("effort", "medium")).capitalize()
            logo_attr = self._role("c_accent", bright)
            logo_w = max(len(line) for line in self._LOGO)
            tx = 3 + logo_w + 3  # info column, clear of the wordmark
            for r_idx, line in enumerate(self._LOGO):
                self._put(scr, 1 + r_idx, 3, line, logo_attr, W)
            self._put(scr, 1, tx, f"Buddy CLI {BUDDY_VERSION}", bright, W)
            self._put(scr, 2, tx, user_id, dim, W)
            self._put(scr, 3, tx, f"{model} ({effort})", 0, W)
            self._put(scr, 4, tx, cwd, dim, W)
            self._put(scr, 6, tx, "offline-ready — type help", dim, W)
        else:
            # ── Viewport: messages scroll above the prompt top divider
            vh = max(1, prompt_top)
            vw = max(1, W - 2)
            src_n = len(self.lines) + (1 if self._pend else 0)
            start = max(0, src_n - 1 - max(0, self.scroll))
            rows = []
            for i in range(max(0, start - vh), start + 1):
                rows.extend(self._wrapped(i, vw))
            y = 0
            span = self._selection_span()
            disp_rows = rows[-vh:]
            # k counts from the top of the visible window (== screen y),
            # the same convention _point_to_cell uses for mouse mapping.
            for k, row in enumerate(disp_rows):
                x = 1
                if span is not None:
                    ar, ac, cr, cc = span
                    if ar <= k <= cr:
                        for t, a in row:
                            cx = x - 1
                            for ch in t:
                                w = _char_width(ch)
                                s0 = ac if k == ar else 0
                                s1 = cc if k == cr else 10 ** 9
                                attr = (a | curses.A_REVERSE
                                        if cx + w > s0 and cx < s1 else a)
                                self._put(scr, y, x, ch, attr, W - 1)
                                x += w
                                cx += w
                        y += 1
                        continue
                for t, a in row:
                    self._put(scr, y, x, t, a, W - 1)
                    x += _str_width(t)
                y += 1
            if self.scroll > 0:
                hint = " ↑ scrollback — wheel / PgDn to bottom "
                self._put(scr, 0, max(0, W - len(hint) - 1), hint, dim, W - 1)

        # ── Top Divider Rule ──────────────────────────────────────────
        self._put(scr, prompt_top, 0, "─" * W, dim, W)

        # ── Prompt Row ────────────────────────────────────────────────
        prompt_y = prompt_top + 1
        self._put(scr, prompt_y, 0, "> ", accent, W)
        clip = W - 1
        budget = max(1, W - 4)

        if self._buf:
            pre, post = self._buf[:self._pos], self._buf[self._pos:]
            plain = pre + "▏" + post
            shift = max(0, len(plain) - budget)
            shown = plain[shift:]
            self._put(scr, prompt_y, 2, shown[:budget], bright, clip)
        else:
            # AGY Mode Placeholders
            if mode_name == "accept-edits":
                ph = "Accept-edits mode: file edits auto-approved (shift+tab to cycle)"
            elif mode_name == "plan":
                ph = "Plan mode: plan & explore without modifying code (shift+tab to cycle)"
            else:
                ph = "Bypass mode: dangerous commands auto-approved (shift+tab to cycle)"
            self._put(scr, prompt_y, 2, ph[:budget], dim, clip)

        # ── Bottom Divider Rule ───────────────────────────────────────
        bottom_div_y = prompt_top + 2
        self._put(scr, bottom_div_y, 0, "─" * W, dim, W)

        # ── Slash Palette Overlay (below bottom divider) ──────────────
        current_y = bottom_div_y + 1
        if self._popup:
            ms, sel = self._popup
            first = max(0, min(sel - 2, max(0, len(ms) - 5)))
            shown_cmds = ms[first:first + 5]
            for idx, c in enumerate(shown_cmds):
                if current_y >= H - 2:
                    break
                gi = first + idx
                desc = _COMMAND_HELP.get(c, "")
                is_sel = (gi == sel)
                marker = "> " if is_sel else "  "
                attr = accent if is_sel else 0
                self._put(scr, current_y, 0, f"{marker}{c:<22} {desc}", attr, W - 1)
                current_y += 1
            remain = len(ms) - (first + len(shown_cmds))
            if remain > 0 and current_y < H - 2:
                self._put(scr, current_y, 3, f"↓ {remain} more", dim, W - 1)
                current_y += 1
            if current_y < H - 1:
                self._put(scr, current_y, 2, "↑/↓ Navigate · enter Select · tab Complete", dim, W - 1)

        # ── Model Picker Overlay (below bottom divider) ─────────────────
        if self._model_pick is not None:
            rows, sel_row = self._pick_rows()
            first = max(0, min(sel_row - 4, max(0, len(rows) - 10)))
            for row in rows[first:first + 10]:
                if current_y >= H - 2:
                    break
                kind, payload = row
                if kind == "head":
                    self._put(scr, current_y, 0, f"  ─ {payload} ─", dim, W - 1)
                else:
                    is_sel = (payload.get("n", 0) - 1
                              == self._model_pick["sel"])
                    marker = "> " if is_sel else "  "
                    dot = "● " if payload.get("active") else "○ "
                    src = payload.get("source", "")
                    attr = accent if is_sel else 0
                    self._put(scr, current_y, 0,
                              f"{marker}{dot}{payload.get('label', '')}",
                              attr, W - 1)
                    if src:
                        self._put(scr, current_y, W - len(src) - 9,
                                  f"[{src}]", dim, W - 1)
                current_y += 1
            remain = len(rows) - len(rows[first:first + 10])
            if remain > 0 and current_y < H - 2:
                self._put(scr, current_y, 3, f"↓ {remain} more", dim, W - 1)
                current_y += 1
            if current_y < H - 1:
                self._put(scr, current_y, 2, "↑/↓ select · enter use · esc cancel", dim, W - 1)

        # ── Approve/Decline Dialog (below bottom divider) ───────────────
        if self._confirm is not None:
            cmd = self._confirm["cmd"]
            sel = self._confirm["sel"]
            mode_name_cf = self._MODES[self.mode_idx]
            self._put(scr, current_y, 0,
                      f"  ⚠ approve? ({mode_name_cf})", accent, W - 1)
            current_y += 1
            for ln in cmd.splitlines()[:3]:
                if current_y >= H - 2:
                    break
                self._put(scr, current_y, 0, f"  $ {ln}"[:W - 1], bright, W - 1)
                current_y += 1
            if current_y < H - 2:
                x = 2
                for i, opt in enumerate(self._CONFIRM_OPTS):
                    label = f"[{opt}]"
                    attr = accent if i == sel else dim
                    if i == sel:
                        label = f">[{opt}]"
                    self._put(scr, current_y, x, label, attr, W - 1)
                    x += len(label) + 3
                current_y += 1
            if current_y < H - 1:
                self._put(scr, current_y, 2, "←/→ select · y/n/a shortcut · enter ok · esc no", dim, W - 1)

        # ── Status Bar Row (always anchored at H - 1) ─────────────────
        status_y = H - 1
        if self._popup or self._model_pick is not None or self._confirm is not None or self.working:
            left_status = "esc to cancel"
        else:
            left_status = "? for shortcuts"
        self._put(scr, status_y, 0, left_status, dim, W - 1)

        # Right status: mode · model · effort · AI state, each with its own
        # color — mode is semantic (bypass red, plan amber, edits accent).
        mode_attr = (self._role("c_red") if mode_name == "bypass"
                     else self._role("c_amber") if mode_name == "plan"
                     else self._role("c_accent"))
        effort_s = str(cfg.get("effort", "medium")).lower()
        if self.working:
            elapsed = time.time() - self._t0 if self._t0 else 0
            segments = [(f"{self.frame} working", self._role("c_accent", bright)),
                        (f"{self.steps} steps", dim), (f"{elapsed:.0f}s", dim)]
        else:
            segments = []
        # AI state mirrors the quota circuit breaker: a cooldown shows amber
        try:
            from .agent import _quota_open as _q_open
            ai_open = _q_open(cfg)
        except Exception:
            ai_open = True
        segments.append((mode_name, mode_attr))
        segments.append((model, dim))
        if not self.working:
            segments.append((effort_s, dim))
        if ai_open:
            segments.append(("AI: Ready", self._role("c_ok", dim)))
        else:
            segments.append(("AI: quota cooldown", self._role("c_amber", dim)))
        right_text = " · ".join(t for t, _ in segments)
        right_x = max(len(left_status) + 2, W - len(right_text) - 2)
        x = right_x
        for i, (t, a) in enumerate(segments):
            self._put(scr, status_y, x, t, a, W - 1 - x)
            x += len(t)
            if i < len(segments) - 1:
                self._put(scr, status_y, x, " · ", dim, W - 1 - x)
                x += 3

    # ──────────────────────────────────── shortcuts view (AGY style)

    def _paint_shortcuts_view(self, scr, H: int, W: int) -> None:
        dim = curses.A_DIM
        bright = curses.A_BOLD

        # Header bar
        self._put(scr, 1, 0, "Buddy CLI   general    commands    shortcuts   (←/→ or tab to cycle)", bright, W)
        self._put(scr, 2, 0, "Keyboard Shortcuts", bright, W)
        self._put(scr, 3, 2, "/help to view commands", dim, W)

        shortcuts = [
            ("> /", "Open slash commands"),
            (r"  \ + enter", "Insert newline fallback"),
            ("  shift+tab, tab", "Cycle execution mode (accept-edits / plan)"),
            ("  ctrl+c, esc", "Go back / dismiss"),
            ("  ctrl+d", "Exit CLI"),
            ("  ctrl+p", "Command palette"),
            ("  ctrl+l", "Clear CLI screen"),
            ("  ctrl+o", "Copy recent chat to clipboard"),
            ("  f9 / ctrl+g", "Mouse: wheel-only <-> full (drag copy, click paste)"),
            ("  wheel", "Scroll chat (works in both mouse modes)"),
            ("  drag / right-click", "Terminal's own select & paste (wheel-only mode)"),
            ("  ↑/↓", "Navigate history / popup"),
            ("  /model", "Pick provider/model with ↑/↓"),
            ("  pgup/pgdn · wheel", "Scroll viewport"),
        ]

        y = 5
        for key, desc in shortcuts:
            if y >= H - 3:
                break
            self._put(scr, y, 0, f"{key:<32} {desc}", 0, W)
            y += 1

        self._put(scr, H - 2, 0, "Keyboard: ↑/↓ Navigate  esc Close", dim, W)
