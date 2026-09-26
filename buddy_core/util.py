"""buddy_core.util — ANSI colour helpers, width math, panels, deliver/notify, error log, voice, misc.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .config import CONFIG, HOME, INBOX, SPEECH_API, SPEECH_TTS_MODEL, STATE_LOCK, api_key, secret_get

import base64
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

# ---- original buddy.py lines 162-164 ----------------------------------

# ================================================================== TUI look =

# ---- original buddy.py lines 165-165 ----------------------------------
_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# ---- original buddy.py lines 166-166 ----------------------------------


# ---- original buddy.py lines 167-168 ----------------------------------
def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _TTY else s

# -------------------------------------------------------------- theme ----
# The whole UI's palette, tweakable in this one dict. The string roles are
# SGR codes for the ANSI (scrollback) skin; the full-screen driver in
# tui.py maps the "c_*" roles — (256-color value, 8-color fallback) — to
# curses color pairs. Warm orange accent, gray chrome, no rainbow.
_THEME = {
    "accent":   "38;5;208",  # accent — warm orange (256-color terminals)
    "accent8":  "33;1",      # accent fallback for 8-color terminals (amber)
    "dim":      "2",         # meta text, code spans/blocks
    "faint":    "38;5;240",  # chrome gray — borders, rules, descriptions
    "ok":       "32",
    "error":    "31;1",      # tool errors, failures
    "amber":    "38;5;214",  # tips, warm warnings
    "mark":     "38;5;111",  # block-letter wordmark
    "c_accent": (208, 3),    # curses accent (fallback: yellow)
    "c_red":    (196, 1),    # curses errors (fallback: red)
    "c_dim":    (240, 8),    # curses chrome gray
    "c_amber":  (214, 3),    # curses warm warning (context %)
    "c_info":   (110, 6),    # curses code-block tint (fallback: cyan)
    "c_ok":     (114, 2),    # curses success (fallback: green)
}

_256C: "bool | None" = None


def _have256() -> bool:
    """Does the terminal advertise 256-color support? (cached)"""
    global _256C
    if _256C is None:
        t = os.environ.get("TERM", "")
        _256C = "256color" in t or bool(os.environ.get("COLORTERM"))
    return _256C

# ---- original buddy.py lines 170-171 ----------------------------------
def ui_dim(s: str) -> str: return _c(_THEME["dim"], s)

def ui_accent(s: str) -> str: return _c(_THEME["accent"] if _have256()
                                        else _THEME["accent8"], s)

# ---- original buddy.py lines 172-172 ----------------------------------
def ui_bold(s: str) -> str: return _c("1", s)

# ---- original buddy.py lines 173-173 ----------------------------------
def ui_err(s: str) -> str: return _c(_THEME["error"], s)

# ---- original buddy.py lines 174-174 ----------------------------------
def ui_ok(s: str) -> str: return _c(_THEME["ok"], s)

# ---- original buddy.py lines 175-175 ----------------------------------
def ui_faint(s: str) -> str: return _c(_THEME["faint"], s)  # chrome gray

# ---- original buddy.py lines 176-176 ----------------------------------
def ui_amber(s: str) -> str: return _c(_THEME["amber"], s)  # tip-dot amber

# ---- original buddy.py lines 177-177 ----------------------------------
def ui_mark(s: str) -> str: return _c(_THEME["mark"], s)   # wordmark

# ---- original buddy.py lines 178-178 ----------------------------------


# ---- original buddy.py lines 179-180 ----------------------------------
def _strip_ansi(s: str) -> str:
    # Strip ALL terminal escape sequences, not just SGR colors: untrusted
    # model/tool output printed raw could otherwise move the cursor, clear
    # the screen, or spoof the prompt (CSI ..., OSC hyperlinks, charsets).
    s = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", s)  # CSI
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", s)  # OSC
    s = re.sub(r"\x1b[()][0-9A-B]", "", s)  # charset selection
    s = re.sub(r"\x1b[\\]", "", s)  # stray ST terminator
    s = s.replace("\x1b", "")  # any lone ESC left over
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)  # C0 controls
    return s

# ---- original buddy.py lines 181-181 ----------------------------------


# ---- original buddy.py lines 182-191 ----------------------------------
def _vlen(s: str) -> int:
    """Visible terminal width of s: escapes stripped, emoji/wide chars = 2."""
    w = 0
    for ch in _strip_ansi(s):
        o = ord(ch)
        w += 2 if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
                   or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
                   or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
                   or 0x1F300 <= o <= 0x1FAFF) else 1
    return w

# ---- original buddy.py lines 192-192 ----------------------------------


# ---- original buddy.py lines 193-194 ----------------------------------
def _center(s: str, cols: int) -> str:
    return " " * max(0, (cols - _vlen(s)) // 2) + s

# ---- original buddy.py lines 195-195 ----------------------------------


# ---- original buddy.py lines 196-208 ----------------------------------
def _panel(lines: list[str], cols: int | None = None) -> list[str]:
    """Rounded dialog-style panel (OpenCode look): a ╭─╮ box fitted to the
    widest line plus 2-space padding, centered on the terminal. Box-drawing
    chars are plain text; only the border color is ANSI (so it degrades to a
    clean ASCII-free box under NO_COLOR)."""
    cols = max(2, cols or shutil.get_terminal_size().columns)
    lines = list(lines) or [""]
    w = max(2, min(cols, max(_vlen(l) for l in lines) + 8))
    left = " " * max(0, (cols - w) // 2)
    out = [left + ui_faint("╭" + "─" * (w - 2) + "╮")]
    for i, l in enumerate(lines):
        if _vlen(l) > w - 6:  # keep the box aligned on very narrow terminals
            lines[i] = l = _cut(l, w - 9) + "…"
        out.append(f"{left}{ui_faint('│')}  {l}{' ' * (w - 6 - _vlen(l))}  {ui_faint('│')}")
    out.append(left + ui_faint("╰" + "─" * (w - 2) + "╯"))
    return out

# ---- original buddy.py lines 209-209 ----------------------------------


# ---- original buddy.py lines 210-215 ----------------------------------
def _arg_summary(args: dict | None) -> str:
    """First string argument value, whitespace-collapsed, max 40 chars."""
    for v in (args or {}).values():
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", v.strip())[:40]
    return ""

# ---- original buddy.py lines 216-216 ----------------------------------


# ---- original buddy.py lines 217-226 ----------------------------------
def _cut(s: str, width: int) -> str:
    """s truncated to at most `width` visible columns."""
    out, w = [], 0
    for ch in s:
        cw = _vlen(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out)

# ---- original buddy.py lines 227-227 ----------------------------------


# ---- original buddy.py lines 228-253 ----------------------------------
def _tool_line(name: str, arg: str, secs, err: bool = False) -> str:
    """Antigravity (agy)-style tool execution row:
      » name  first-arg  (0.3s)"""
    secs_s = f"({secs:.1f}s)" if secs is not None else ""
    marker = ui_err("× ") if err else ui_accent("» ")
    name_s = ui_err(name) if err else ui_bold(name)
    parts = [marker + name_s]
    if arg:
        parts.append(ui_dim(str(arg)[:60]))
    if secs_s:
        parts.append(ui_faint(secs_s))
    return "  " + "  ".join(parts)

# ---- original buddy.py lines 1309-1310 --------------------------------


# ---- original buddy.py lines 1311-1325 --------------------------------
def claim_inbox() -> str:
    """Atomically claim + clear INBOX, returning its text ("" if empty).

    Uses an atomic rename so two concurrent chats (separate processes) can't
    both read the same entries: exactly one wins, the other gets "".
    Peeking readers (/inbox, web UI) don't consume — only this does."""
    try:
        claimed = INBOX.with_name(f"inbox.claimed.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            os.replace(INBOX, claimed)
        except FileNotFoundError:
            return ""
        except OSError:
            return INBOX.read_text(encoding="utf-8", errors="replace") if INBOX.exists() else ""
        try:
            return claimed.read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass
    except Exception:
        return ""


def _trim_inbox(max_bytes: int = 200_000) -> None:
    """Keep inbox.md bounded (like errors.log/wishes.log). Drops the
    oldest entries once it exceeds `max_bytes` — the daemon appends here
    forever (jobs, watchers, subagents, webhooks), and /api/inbox read
    the whole file on every poll."""
    try:
        if not INBOX.exists() or INBOX.stat().st_size <= max_bytes:
            return
        raw = INBOX.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    # split on entry headers ("## YYYY-MM-DD HH:MM — title"), keep newest
    parts = [p for p in raw.split("\n## ") if p.strip()]
    if not parts:
        return
    parts[0] = parts[0].lstrip("# ").strip() if parts[0].startswith("## ") else parts[0]
    kept: list[str] = []
    size = 0
    for p in reversed(parts):
        chunk = ("## " + p.strip() + "\n\n")
        size += len(chunk)
        if size > max_bytes:
            break
        kept.append(chunk)
    if not kept:
        # one entry bigger than the whole budget would defeat the trimmer
        # forever — hard-cap it to the newest max_bytes
        newest = parts[-1].strip()
        kept = ["## " + newest[-(max_bytes - 100):] + "\n\n"]
    out = "".join(reversed(kept))
    out += (f"\n(inbox trimmed to the newest {len(kept)} entries; "
            f"older ones were dropped)\n")
    try:
        tmp = INBOX.with_suffix(".md.tmp")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, INBOX)
    except OSError:
        pass


def deliver(text: str, title: str = "buddy") -> None:
    try:
        with STATE_LOCK:
            with INBOX.open("a", encoding="utf-8", errors="replace") as f:
                f.write(f"## {datetime.now():%Y-%m-%d %H:%M} — {title}\n{text}\n\n")
            _trim_inbox()
    except OSError:
        try:
            from .sched import _log_error
            import traceback as _tb
            _log_error("inbox write failed:\n" + _tb.format_exc())
        except Exception:
            pass
    try:
        subprocess.run(
            ["notify-send", title, text[:200]], timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    try:
        _notify_router(text, title)
    except Exception:
        pass

# ---- original buddy.py lines 2042-2045 --------------------------------


# ================================================================ speaking =

# ---- original buddy.py lines 2046-2046 --------------------------------
TTS_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]

# ---- original buddy.py lines 2047-2048 --------------------------------


# ---- original buddy.py lines 2049-2066 --------------------------------
def _play_file(path: Path) -> bool:
    for cmd in (
        ["mpv", "--no-video", "--really-quiet", str(path)],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
        ["paplay", str(path)],
        ["aplay", str(path)],
        ["afplay", str(path)],
    ):
        try:
            p = subprocess.run(cmd, timeout=300,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if p.returncode == 0:
                return True
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False

# ---- original buddy.py lines 2067-2068 --------------------------------


# ---- original buddy.py lines 2069-2111 --------------------------------
# Locked speech voice handling: the MODEL is fixed (SPEECH_TTS_MODEL) —
# only the voice is selectable. OpenAI voice names map to Gemini
# prebuilt voices; genuine Gemini voice names pass through; anything
# else falls back to Kore so a stale config can never break speech.
_TTS_VOICE_MAP = {
    "alloy": "Kore", "ash": "Puck", "ballad": "Leda", "coral": "Aoede",
    "echo": "Charon", "fable": "Fenrir", "nova": "Kore", "onyx": "Orus",
    "sage": "Umbriel", "shimmer": "Zephyr",
}
_GEMINI_VOICES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Alnilam",
    "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemium", "Sulafat", "Sadachbia", "Sadaltager",
})


def _gemini_voice(name: str | None) -> str:
    for v in _GEMINI_VOICES:
        if v.lower() == (name or "").lower():
            return v
    return _TTS_VOICE_MAP.get((name or "").lower(), "Kore")


def _gemini_tts_wav(text: str, key: str, voice: str) -> Path | None:
    """Synthesize `text` with the locked SPEECH_TTS_MODEL. Returns the WAV
    path, or None when the response carries no audio. Raises on HTTP errors."""
    body = json.dumps({
        "contents": [{"parts": [
            {"text": f"Say in a natural, conversational voice: {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }).encode()
    req = urllib.request.Request(
        f"{SPEECH_API}/models/{SPEECH_TTS_MODEL}:generateContent",
        data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode("utf-8", "replace") or "{}")
    for cand in resp.get("candidates", []):
        if not isinstance(cand, dict):
            continue
        for part in cand.get("content", {}).get("parts", []):
            inline = (part.get("inlineData") or {}) if isinstance(part, dict) else {}
            if not inline.get("data"):
                continue
            pcm = base64.b64decode(inline["data"])
            rate = 24000
            m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
            if m:
                rate = int(m.group(1))
            import uuid as _uuid
            tmp = HOME / f"tts_{_uuid.uuid4().hex[:8]}.wav"
            import wave
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm)
            out = HOME / "last_tts.wav"
            os.replace(tmp, out)  # atomic: concurrent plays keep their inode
            return out
    return None


def speak(text: str, cfg: dict) -> None:
    """Speak text aloud — always via the locked SPEECH_TTS_MODEL (Gemini).

    cfg["tts_model"] is IGNORED (locked, not user-changeable); only the
    voice (cfg["tts_voice"]) is selectable. Falls back to espeak when
    the API is unreachable."""
    # strip markdown noise so it isn't read aloud
    clean = re.sub(r"[`*_#\[\]]", "", text)
    clean = re.sub(r"https?://\S+", "(link)", clean)
    clean = clean.strip()
    if not clean:
        return

    mode = cfg.get("tts", "api")
    if mode in ("api", "tts"):
        try:
            key = api_key(cfg)
            if key:
                out = _gemini_tts_wav(clean[:4000], key,
                                      _gemini_voice(cfg.get("tts_voice", "nova")))
                if out is not None and _play_file(out):
                    return
        except Exception:
            pass  # fall through to espeak
    try:
        subprocess.run(
            ["espeak-ng", "-s", "160", clean[:3000]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
    except FileNotFoundError:
        try:
            subprocess.run(
                ["espeak", clean[:3000]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
            )
        except Exception:
            print(ui_dim("(couldn't speak: install mpv/ffplay or espeak-ng)"))
    except Exception as e:
        print(ui_dim(f"(couldn't speak: {e})"))

# ---- original buddy.py lines 2559-2562 --------------------------------


# --- voice input: record from the microphone, transcribe via locked speech model ----

# ---- original buddy.py lines 2563-2580 --------------------------------
def mic_record(seconds: int = 5) -> str:
    out = str(HOME / "last_mic.wav")
    Path(out).unlink(missing_ok=True)  # a stale recording must not answer
    for argv in (
        ["arecord", "-d", str(seconds), "-f", "cd", "-q", out],
        ["rec", "-q", out, "trim", "0", str(seconds)],
        ["sox", "-d", out, "trim", "0", str(seconds)],
    ):
        try:
            subprocess.run(argv, timeout=seconds + 5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            continue
        if Path(out).exists() and Path(out).stat().st_size > 1000:
            try:
                from .tools import transcribe_audio
                return transcribe_audio(out).strip() or "(didn't catch that — silence?)"
            except Exception as e:
                return f"(recorded but transcription failed: {e})"
    return "(no microphone recorder found — install sox or alsa-utils)"

# ---- original buddy.py lines 2636-2637 --------------------------------


# ---- original buddy.py lines 2638-2646 --------------------------------
def _log_error(trace: str) -> None:
    try:
        path = HOME / "errors.log"
        with path.open("a") as f:
            f.write(f"\n## {datetime.now():%Y-%m-%d %H:%M:%S}\n{trace[-4000:]}\n")
        if path.stat().st_size > 200_000:
            tmp = path.with_suffix(".log.tmp")
            tmp.write_text(path.read_text()[-200_000:])
            os.replace(tmp, path)
    except Exception:
        pass

# ---- original buddy.py lines 3057-3060 --------------------------------


# --- notification router: one deliver() fans out to every channel ----------

# ---- original buddy.py lines 3061-3081 --------------------------------
def _notify_router(text: str, title: str) -> None:
    # local parse instead of load_config(): load_config() calls sys.exit(1) on a
    # corrupt config, and SystemExit escapes deliver()'s except Exception handlers
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
    hook = cfg.get("notify_webhook")
    if hook:
        try:
            body = json.dumps({"title": title, "text": text}).encode()
            req = urllib.request.Request(hook, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as _r:
                _r.read()
        except Exception:
            pass
    if cfg.get("smtp_host") and cfg.get("notify_email"):
        try:
            from .tools import email_send
            email_send(cfg["notify_email"], f"[buddy] {title}", text, confirm=None)
        except Exception:
            pass
    if cfg.get("telegram") and cfg.get("telegram_chat_id") and secret_get("telegram_token"):
        try:
            from .sched import telegram_send
            telegram_send(str(cfg["telegram_chat_id"]), f"🔔 {title}\n{text[:1000]}")
        except Exception:
            pass

