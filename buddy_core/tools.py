"""buddy_core.tools — built-in tools: shell (sudo modes), files, web/image, email/social, code edits, tool_impl dispatcher.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .brain import _headers
from .config import BACKUPS, CONFIG, CUSTOM_TOOLS, HOME, PORT, SPEECH_API, SPEECH_STT_MODEL, UPDATE_FILES, WORKSPACE, api_key, load_config, secret_get
from .memory import recall_memory, remember, search_memory
from .sched import add_job, add_watcher, list_jobs, list_watchers, remove_watcher
from .skills import _log_wish, edit_playbook, evolve_pass, reflect, save_skill, self_repair, use_skill

import base64
import json
import os
import re
import signal
import threading
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---- original buddy.py lines 1581-1584 --------------------------------


# ============================================================ shell safety =

# ---- original buddy.py lines 1585-1595 --------------------------------
DANGEROUS = [re.compile(p, re.IGNORECASE) for p in [
    r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive)\b",  # rm -rf / -fr / -fir
    r"\brm\s+(?=(?:\S+\s+)*-[a-z]*r)(?=(?:\S+\s+)*-[a-z]*f)\S+(?:\s+\S+)*",  # flags split: rm -r -f
    r"(^|[\s;|&(/])(bin/rm|usr/bin/rm)\b",  # absolute-path rm bypass
    r"\bfind\b[^;|&]*(-delete\b|-exec\s+rm\b)",  # find / -delete / -exec rm
    r"\b(shred|wipefs)\b",  # unrecoverable disk wipe
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+if=",
    r"\bdd\s+of=/dev/(sd|nvme|hd|mmcblk)",            # dd writing a block device directly
    r":\(\)\s*\{.*\};:",                              # fork bomb
    r"\b(shutdown|reboot|halt|poweroff|init\s+[06])\b",
    r"\bsystemctl\s+(poweroff|halt|reboot)\b",
    r">\s*/dev/(sd|nvme|hd)",
    r"\bchmod\s+(-R\s+)?0?7777?\b",  # chmod 777 / 0777 / 7777 / 07777 loosening
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|fi)?sh\b",  # curl | sh (incl. sudo, zsh, fish)
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?python3?\b",  # curl | python3 — same remote-exec shape
    r"\b(ba|z|fi)?sh\s+(-\w+\s+)*[\"']?\$\((curl|wget)\b",  # sh -c "$(curl …)" command substitution
    r"\bscp\b[^;|&]*@[^;|&]*:",  # scp to a remote host (uploads secrets; downloads need confirm too)
    r"\b(ba|z|fi)?sh\s+<\s*\((curl|wget)\b",  # bash <(curl …) process substitution
    r"\bdocker\b[^;|&]*--privileged\b",  # container escape
    r"\bdocker\b[^;|&]*-v\s+/:",  # docker -v /:/host
    r"\b(curl|wget)\b[^;|&]*--data[^;|&]*@[^;|&]*(secrets\.json|id_rsa|\.buddy|\.ssh)",  # secret exfil
    r"\bapt(-get)?\b[^;|&]*\b(dist-upgrade|full-upgrade)\b",  # debian distro upgrade
    r"\b(dnf|yum)\b[^;|&]*\bupgrade\b[^;|&]*(-y|--assumeyes)\b",  # unattended upgrade
    r"\bpacman\b[^;|&]*-Syu\b",  # arch full system upgrade
    r"\bzypper\b[^;|&]*\bdup\b",  # suse distro upgrade
]]

# ---- original buddy.py lines 1596-1597 --------------------------------


# ---- original buddy.py lines 1598-1602 --------------------------------
def is_dangerous(command: str) -> str | None:
    for pat in DANGEROUS:
        if pat.search(command):
            return pat.pattern
    return None

def _sensitive_path_reason(path: Path) -> str | None:
    """Return a reason if `path` is sensitive (keys, secrets, shell rc).
    Write/read via tools to these requires user confirm (None = deny)."""
    try:
        home = Path.home()
    except Exception:
        home = None
    try:
        rp = path.expanduser()
        # resolve symlinks when the target exists so ~/.ssh-link -> ~/.ssh is caught
        if rp.exists():
            try:
                rp = rp.resolve()
            except OSError:
                pass
        s = str(rp)
    except Exception:
        return "unresolvable path"
    low = s.lower()
    # private keys / secrets
    for marker in ("secrets.json", "id_rsa", "id_ed25519", ".gnupg", ".ssh", "authorized_keys", ".pem", ".key"):
        if marker in low and ("buddy" in low or ".ssh" in low or ".gnupg" in low
                              or marker in ("id_rsa", "id_ed25519", "authorized_keys")):
            # narrow: only flag key/ssh material + buddy secrets, not every *.key
            if marker in (".pem", ".key"):
                if ".ssh" in low or "buddy" in low or "id_" in low:
                    return f"sensitive key material: {s}"
                continue
            return f"sensitive path: {s}"
    # shell startup / privilege escalation targets
    if home is not None:
        try:
            rel = rp.relative_to(home) if rp.is_absolute() else None
        except ValueError:
            rel = None
        if rel is not None and len(rel.parts) == 1 and str(rel) in (
                ".bashrc", ".zshrc", ".profile", ".bash_profile"):
            return f"shell startup file: {s}"
    if low in ("/etc/passwd", "/etc/shadow", "/etc/sudoers") or low.startswith(("/etc/sudoers.d/", "/etc/ssh/")):
        return f"system auth file: {s}"
    return None

# ---- original buddy.py lines 1643-1646 --------------------------------


# ---------------------------------------------------------------- web tools

# ---- original buddy.py lines 1647-1647 --------------------------------
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) buddy/3.0"}

# ---- original buddy.py lines 1648-1651 --------------------------------


# --- network layer: proxy-aware, cached, one place to reason about the web --

# ---- original buddy.py lines 1652-1652 --------------------------------
_NET_OPENERS: dict = {}

# ---- original buddy.py lines 1653-1653 --------------------------------
_NET_CACHE: dict = {}
_NET_CACHE_LOCK = threading.Lock()

# ---- original buddy.py lines 1654-1655 --------------------------------


# ---- original buddy.py lines 1656-1676 --------------------------------
def _net_open(url: str, timeout: int = 30, cache_seconds: int = 0) -> bytes:
    """Fetch a URL through the shared opener. Routes via config "proxy" (or
    HTTPS_PROXY env) when set; caches responses briefly to stay fast."""
    if _NET_CACHE:  # evict stale entries so the cache can't grow unbounded
        now_t = time.time()
        with _NET_CACHE_LOCK:  # a concurrent insert during iteration = RuntimeError
            for old_url in [u for u, (t, _) in _NET_CACHE.items() if now_t - t > 300]:
                _NET_CACHE.pop(old_url, None)
    cfg = (load_config() if CONFIG.exists() else {})
    proxy = cfg.get("proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    key = proxy or "-"
    if key not in _NET_OPENERS:
        _NET_OPENERS[key] = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    if cache_seconds and url in _NET_CACHE and time.time() - _NET_CACHE[url][0] < cache_seconds:
        return _NET_CACHE[url][1]
    req = urllib.request.Request(url, headers=UA)
    with _NET_OPENERS[key].open(req, timeout=timeout) as r:
        data = r.read(2_000_000)  # cap: a huge response must not OOM the daemon
    if cache_seconds and len(data) < 2_000_000:
        with _NET_CACHE_LOCK:
            _NET_CACHE[url] = (time.time(), data)
    return data

# ---- original buddy.py lines 1677-1678 --------------------------------


# ---- original buddy.py lines 1679-1693 --------------------------------
def web_search(query: str) -> str:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    html = _net_open(url, timeout=20, cache_seconds=120).decode(errors="replace")
    results = []
    for m in re.finditer(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.S,
    ):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
        results.append(f"{title}\n  {href}")
        if len(results) >= 5:
            break
    return "\n\n".join(results) if results else "(no results)"

# ---- original buddy.py lines 1694-1695 --------------------------------


def _norm_url(url: str) -> str:
    """Accept bare domains from the brain/user ("check football.com").

    Adds https:// (http:// for loopback/LAN hosts). Returns "" when the
    input isn't URL-like at all."""
    u = (url or "").strip()
    u = u.strip("<>\"'")
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        host = u.split("/")[0].split(":")[0].lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host):
            return ""
        if (host in ("localhost",) or host.startswith("127.")
                or host.startswith(("10.", "192.168."))
                or re.fullmatch(r"172\.(1[6-9]|2[0-9]|3[01])\..*", host)):
            u = "http://" + u
        elif "." not in host:
            return ""
        else:
            u = "https://" + u
    return u


# ---- original buddy.py lines 1696-1703 --------------------------------
def web_fetch(url: str) -> str:
    url = _norm_url(url)
    if not url:
        return "(not a valid URL — give a domain like example.com or a full http(s) URL)"
    try:
        html = _net_open(url, timeout=30, cache_seconds=300).decode(errors="replace")
    except Exception:
        if url.startswith("https://"):
            try:  # some hosts only serve plain http
                html = _net_open("http://" + url[len("https://"):],
                                 timeout=30, cache_seconds=300).decode(errors="replace")
            except Exception as e:
                return f"(fetch failed: {e})"
        else:
            return f"(fetch failed: {url})"
        return "(fetched over insecure http — content may not be authentic)\n" + _strip_page(html)
    return _strip_page(html)


def _strip_page(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text[:10000] or "(empty page)"

# ---- original buddy.py lines 1704-1705 --------------------------------


# ---- original buddy.py lines 1706-1729 --------------------------------
def browse(url: str, wait_seconds: int = 3) -> str:
    """Real browser rendering (JS included) via Playwright if installed;
    otherwise falls back to plain fetch."""
    url = _norm_url(url)
    if not url:
        return "(not a valid URL — give a domain like example.com or a full http(s) URL)"
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        result = web_fetch(url)
        return ("(playwright not installed — plain fetch only, JS not rendered. "
                "pip install playwright && playwright install chromium for full browsing)\n\n"
                + result)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_seconds * 1000)
            text = page.inner_text("body")
            title = page.title()
            browser.close()
        return f"# {title}\n\n{text[:12000]}"
    except Exception as e:
        return f"(browser failed: {e})"

# ---- original buddy.py lines 1730-1733 --------------------------------


# ------------------------------------------------------------------ senses =

# ---- original buddy.py lines 1734-1761 --------------------------------
def transcribe_audio(path: str) -> str:
    """Speech-to-text via the locked SPEECH_STT_MODEL (Gemini) — sends the
    audio to :generateContent and returns the transcript. The model is
    NOT user-changeable; /model switches never affect speech."""
    cfg = (load_config() if CONFIG.exists() else {})
    key = api_key(cfg)
    p = Path(path).expanduser()
    if not p.exists():
        return f"(file not found: {p})"
    if p.stat().st_size > 20_000_000:  # inline_data cap: don't base64 a 2GB video
        return "(audio too large to transcribe: 20MB max — trim it first)"
    if not key:
        return "(no API key — run: python3 buddy.py key set)"
    suffix = p.suffix.lower()
    mime = ("audio/wav" if suffix == ".wav" else
            "audio/mp3" if suffix == ".mp3" else
            "application/octet-stream")
    body = json.dumps({
        "contents": [{"parts": [
            {"text": "Transcribe this audio exactly, returning only the transcript."},
            {"inlineData": {"mimeType": mime,
                            "data": base64.b64encode(p.read_bytes()).decode()}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        f"{SPEECH_API}/models/{SPEECH_STT_MODEL}:generateContent",
        data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8", "replace") or "{}")
        texts = []
        for cand in resp.get("candidates", []):
            if not isinstance(cand, dict):
                continue
            for part in cand.get("content", {}).get("parts", []):
                t = part.get("text") if isinstance(part, dict) else ""
                if t:
                    texts.append(t)
        return "\n".join(texts).strip() or "(empty transcript)"
    except Exception as e:
        return f"(transcription failed: {e})"

# ---- original buddy.py lines 1762-1763 --------------------------------


# ---- original buddy.py lines 1764-1788 --------------------------------
def generate_image(prompt: str, out_path: str = "") -> str:
    """Text-to-image via the API images endpoint (if available)."""
    cfg = (load_config() if CONFIG.exists() else {})
    body = json.dumps({"model": cfg.get("image_model", "gpt-image-1"),
                       "prompt": prompt[:4000], "n": 1, "size": "1024x1024"}).encode()
    req = urllib.request.Request(
        cfg.get("api_base", "").rstrip("/").replace("/v1", "") + "/v1/images/generations"
        if "/v1" in cfg.get("api_base", "") else
        cfg.get("api_base", "").rstrip("/") + "/images/generations",
        data=body, headers=_headers(cfg, str(cfg.get("image_model", "gpt-image-1")), cfg.get("api_base", "")),
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read())
        item = resp["data"][0]
        if "b64_json" in item:
            out = Path(out_path).expanduser() if out_path else HOME / f"img_{uuid.uuid4().hex[:6]}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(item["b64_json"]))
            return f"Image saved to {out}"
        if "url" in item:
            return f"Image URL: {item['url']}"
        return "(unexpected API response)"
    except Exception as e:
        return f"(image generation failed: {e} — provider must support the images API)"

# ---- original buddy.py lines 1789-1790 --------------------------------


# ---- original buddy.py lines 1791-1809 --------------------------------
def screenshot(out_path: str = "") -> str:
    out = Path(out_path).expanduser() if out_path else HOME / f"screenshot_{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["grim", str(out)],  # Wayland-native; skipped elsewhere
        ["gnome-screenshot", "-f", str(out)],
        ["scrot", str(out)],
        ["spectacle", "-b", "-n", "-o", str(out)],
        ["import", "-window", "root", str(out)],
        ["maim", str(out)],
    ):
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=15)
            if p.returncode == 0 and out.exists():
                return f"Screenshot saved to {out} — view it with /image {out}"
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return "(no screenshot tool found — install grim, scrot or gnome-screenshot)"

# ---- original buddy.py lines 1810-1811 --------------------------------


# ---- original buddy.py lines 1812-1830 --------------------------------
def clipboard(action: str, text: str = "") -> str:
    if action == "get":
        for cmd in (["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]):
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if p.returncode == 0:
                    return p.stdout or "(clipboard empty)"
            except FileNotFoundError:
                continue
        return "(no working clipboard tool — install xclip or wl-clipboard)"
    if action == "set":
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            try:
                subprocess.run(cmd, input=text, text=True, timeout=5, check=True)
                return f"Copied {len(text)} chars to clipboard"
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
        return "(no working clipboard tool — install xclip or wl-clipboard)"
    return "(action must be 'get' or 'set')"

# ---- original buddy.py lines 1831-1832 --------------------------------


# ---- original buddy.py lines 1833-1843 --------------------------------
def open_url(target: str) -> str:
    """Open a URL or file with the system handler."""
    try:
        p = subprocess.run(["xdg-open", target],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True, timeout=5)
        return f"Opened {target}" if p.returncode == 0 else f"(xdg-open failed on {target})"
    except FileNotFoundError:
        return "(xdg-open not found — is this a desktop machine?)"
    except Exception as e:
        return f"(open failed: {e})"

# ---- original buddy.py lines 2112-2115 --------------------------------


# ==================================================== system self-knowledge =

# ---- original buddy.py lines 2116-2120 --------------------------------
SYS_TOOLS = [
    "python3", "pip3", "node", "npm", "bun", "docker", "git", "curl", "wget",
    "mpv", "ffmpeg", "ffplay", "espeak-ng", "espeak", "notify-send", "piper",
    "apt", "dnf", "pacman", "zypper", "brew", "snap", "flatpak",
    # screens + clipboard (doctor/setup optional extras)
    "grim", "scrot", "gnome-screenshot", "maim", "import",
    "wl-copy", "wl-paste", "xclip", "xsel", "xdg-open", "arecord", "rec",
]

# ---- original buddy.py lines 2121-2122 --------------------------------


# ---- original buddy.py lines 2123-2179 --------------------------------
def probe_system(force: bool = False) -> dict:
    """Fingerprint the machine: OS, package manager, available tools."""
    cache = HOME / "system.json"
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text())
            # re-probe if older than 7 days
            age = time.time() - data.get("probed_at", 0)
            if age < 7 * 86400:
                return data
        except Exception:
            pass

    info: dict = {"probed_at": time.time()}
    try:
        info["kernel"] = os.uname().release
        info["arch"] = os.uname().machine
    except Exception:
        pass
    # distro
    distro = "unknown"
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                distro = line.split("=", 1)[1].strip('"')
                break
    except Exception:
        pass
    info["distro"] = distro
    info["desktop"] = os.environ.get("XDG_CURRENT_DESKTOP", "") or (
        "headless" if not os.environ.get("SSH_CONNECTION") else "headless/ssh"
    )
    # which tools exist
    found = {}
    for tool in SYS_TOOLS:
        try:
            subprocess.run(["which", tool], capture_output=True, check=True)
            found[tool] = True
        except Exception:
            found[tool] = False
    info["tools"] = found
    # resources
    try:
        info["cpu_cores"] = os.cpu_count()
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    info["mem_gb"] = round(int(line.split()[1]) / 1e6, 1)
                    break
    except Exception:
        pass
    # shell
    info["shell"] = os.environ.get("SHELL", "/bin/sh")

    HOME.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(info, indent=2))
    return info

# ---- original buddy.py lines 2180-2181 --------------------------------


# ---- original buddy.py lines 2182-2204 --------------------------------
def system_summary(info: dict) -> str:
    have = sorted(t for t, ok in info.get("tools", {}).items() if ok)
    missing_voice = not ({"mpv", "ffplay"} & set(have))
    lines = [
        f"Host: {info.get('distro', 'unknown')} ({info.get('arch', '?')}), "
        f"{info.get('cpu_cores', '?')} cores, {info.get('mem_gb', '?')} GB RAM, "
        f"desktop: {info.get('desktop', 'unknown')}, shell: {info.get('shell', '?')}",
    ]
    if have:
        lines.append(f"Available tools: {', '.join(have)}")
    # teach package-manager usage
    pm = next((p for p in ("apt", "dnf", "pacman", "zypper", "brew") if info.get("tools", {}).get(p)), None)
    if pm:
        lines.append(
            f"Package manager: {pm}. When installing software for the user, "
            f"use '{pm} install' (with sudo only if needed) rather than guessing."
        )
    if missing_voice and pm:
        lines.append(
            "Note: no audio player found yet — if the user wants voice output, "
            f"suggest installing mpv via {pm}."
        )
    return "\n".join(lines)

# ---- original buddy.py lines 2205-2208 --------------------------------


# ============================================================ built-in tool=

# ---- original buddy.py lines 2209-2238 --------------------------------
# opencode-style live output: run_command streams output tails to a thread-keyed
# sink; the agent loop registers one that forwards to the UI as progress events.
_PROGRESS: dict[int, object] = {}

def set_progress_sink(cb) -> None:
    """Register (per-thread) a callback receiving live output tails from
    run_command. Pass None to unregister."""
    if cb is None:
        _PROGRESS.pop(threading.get_ident(), None)
    else:
        _PROGRESS[threading.get_ident()] = cb

# run_command capture limits (opencode bash.ts parity): keep everything up to
# MAX_CAPTURE, show the model a tail of the output, spill the full capture to a
# file and tell it where the whole log lives.
MAX_CAPTURE_BYTES = 512 * 1024
MAX_OUTPUT_CHARS = 8000

def run_command(command: str, timeout: int = 60, confirm=None, workdir=None) -> str:
    pat = is_dangerous(command)
    if pat:
        if confirm is None or not confirm(command):
            return "(refused by safety guard: matches dangerous pattern " + pat + ")"
    # sudo support: cfg "sudo": "off" (default) | "nopass" (sudoers drop-in)
    # | "password" (sudo_password in secrets.json, piped via sudo -S).
    sudo_mode = (load_config() if CONFIG.exists() else {}).get("sudo", "off")
    stdin_data = None
    if sudo_mode and sudo_mode not in ("off", False, None):
        if sudo_mode is True or sudo_mode == "nopass":
            command = "sudo -n " + command
        elif sudo_mode == "password":
            command = "sudo -S -p '' " + command
            stdin_data = (secret_get("sudo_password") or "") + "\n"
        else:
            return f"(invalid sudo mode {sudo_mode!r} — use off, nopass or password)"
    cwd = None
    if workdir:
        w = Path(workdir).expanduser()
        if not w.is_dir():
            return f"(workdir is not a directory: {w})"
        cwd = str(w)
    try:
        p = subprocess.Popen(
            command, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # opencode: combined output
            text=True, errors="replace",
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            start_new_session=True,  # own process group — timeout kills the whole tree
        )
    except OSError as e:
        return f"(couldn't start command: {e})"
    chunks: list[str] = []
    truncated = {"flag": False}
    done = threading.Event()
    sink = _PROGRESS.get(threading.get_ident())  # caller's thread, not reader's

    def _reader() -> None:
        last_emit = 0.0
        size = 0
        try:
            for line in p.stdout:
                size += len(line)
                if size <= MAX_CAPTURE_BYTES:
                    chunks.append(line)
                else:
                    truncated["flag"] = True
                now = time.monotonic()
                # throttled live tail — the UI watches output as it runs
                if sink is not None and now - last_emit > 0.3:
                    tail = "".join(chunks[-24:])[-2000:]
                    try:
                        sink(tail)
                    except Exception:
                        pass
                    last_emit = now
        except Exception:
            pass
        finally:
            if p.stdout is not None:
                try:
                    p.stdout.close()
                except Exception:
                    pass
            done.set()

    _reader_th = threading.Thread(target=_reader, daemon=True)
    _reader_th.start()
    if stdin_data is not None:
        try:
            p.stdin.write(stdin_data)
            p.stdin.close()
        except OSError:
            pass
    expired = not done.wait(timeout)
    if expired:
        # SIGTERM the process group, grace period, then force-kill (opencode's
        # detached + forceKillAfter pattern)
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if not done.wait(3):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            done.wait(1)
            # The reader can still be parked in a blocking read on the pipe
            # (a grandchild may hold the write end open). Closing stdout
            # unblocks it so the thread — and the chunks list it pins —
            # are released instead of leaking per timeout-kill.
            try:
                if p.stdout is not None:
                    p.stdout.close()
            except Exception:
                pass
        _reader_th.join(timeout=1)
    else:
        # success path: the reader may still hold a buffered tail — join it
        # (with the same stdout-close unblock fallback as the kill path)
        if not _reader_th.join(0.5):
            try:
                if p.stdout is not None:
                    p.stdout.close()
            except Exception:
                pass
            _reader_th.join(timeout=2)
    p.wait()
    out = "".join(chunks).strip()
    if "sudo: a password is required" in out:
        out += ("\n(sudo needs a password — either add a sudoers drop-in for the "
                "commands you allow and set config \"sudo\": \"nopass\", or store "
                "sudo_password in secrets.json and set \"sudo\": \"password\")")
    if not out:
        out = "(no output)"
    meta = []
    verdict = ""
    if expired:
        meta.append(
            f"shell tool terminated the command after exceeding the timeout of "
            f"{timeout}s. If the command is expected to take longer and is not "
            f"waiting for interactive input, retry with a larger timeout value "
            f"in seconds.")
        verdict = ("(FAILED: shell tool terminated the command after exceeding "
                   f"the timeout of {timeout}s)")
    elif p.returncode != 0:
        meta.append(f"Command exited with code {p.returncode}.")
        verdict = f"(FAILED: Command exited with code {p.returncode}.)"
    if truncated["flag"]:
        meta.append("output capture was truncated at the safety limit")
    # model sees the TAIL (recent output matters most); full log spilled to disk
    if len(out) > MAX_OUTPUT_CHARS:
        outdir = Path(HOME) / "outputs"  # HOME is already ~/.buddy
        try:
            outdir.mkdir(parents=True, exist_ok=True)
            log = outdir / (time.strftime("%Y%m%d-%H%M%S") + f"-{p.pid}.log")
            log.write_text(out, encoding="utf-8")
            out = (out[-MAX_OUTPUT_CHARS:]
                   + f"\n\n...output truncated (showing the last {MAX_OUTPUT_CHARS} chars)...\n"
                   f"Full output saved to: {log}")
        except OSError:
            out = out[-MAX_OUTPUT_CHARS:]
    if meta:
        out += "\n\n<shell_metadata>\n" + "\n".join(meta) + "\n</shell_metadata>"
    if verdict:
        # Verdict FIRST: with long output the trailing metadata is easy to
        # miss — the model (and `!` users) must see FAILED before anything.
        out = verdict + "\n" + out
    return out

# ---- original buddy.py lines 2239-2240 --------------------------------


# ---- original buddy.py lines 2241-2248 --------------------------------
def read_file(path: str, confirm=None) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p  # relative paths live in buddy's workspace
    reason = _sensitive_path_reason(p)
    if reason:
        if confirm is None or not confirm(f"read {p} ({reason})"):
            return "(read cancelled by user: sensitive path)"
    if not p.exists():
        return f"(file not found: {p})"
    if not p.is_file():
        return f"(not a regular file: {p})"
    try:
        with open(p, "r", encoding="utf-8", errors="strict") as fh:
            text = fh.read(12001)  # bounded: never loads a multi-GB log whole
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return f"(couldn't read file: {e})"
    return text[:12000] + ("\n...(truncated)" if len(text) > 12000 else "")

# ---- original buddy.py lines 2249-2250 --------------------------------

# thread-keyed last-diff registry: file tools record a unified diff here and
# the agent loop attaches it to the tool-end event (opencode-style diffs)
_LAST_DIFF: dict[int, str | None] = {}
_DIFF_LOCK = threading.RLock()  # written by tool threads, popped by agent loop
_ACTIVE_TODOS: list[dict] = []


def _search_gate(p: Path, confirm) -> str | None:
    """Sensitive-path gate for search/list tools: a grep over ~/.ssh must not
    dump what read_file is gated on. Returns an error string or None."""
    reason = _sensitive_path_reason(p)
    if reason:
        if confirm is None or not confirm(f"search {p} ({reason})"):
            return "(search cancelled by user: sensitive path)"
    return None


def list_dir(path: str = ".") -> str:
    """List directory contents with file types and sizes."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve() if (Path.cwd() / p).exists() else (WORKSPACE / p).resolve()
    if not p.exists():
        return f"(path not found: {p})"
    if not p.is_dir():
        return f"(not a directory: {p})"
    g = _search_gate(p, None)
    if g:
        return g
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except OSError as e:
        return f"(couldn't list directory: {e})"
    lines = []
    for e in entries[:250]:
        try:
            if e.is_dir():
                lines.append(f"📁 {e.name}/")
            else:
                sz = e.stat().st_size
                lines.append(f"📄 {e.name} ({sz:,} bytes)")
        except OSError:
            continue
    out = f"Directory {p} ({len(lines)} items):\n" + "\n".join(lines)
    if len(entries) > 250:
        out += f"\n...({len(entries) - 250} more items not shown)"
    return out


def find_files(pattern: str = "*", path: str = ".") -> str:
    """Find files matching a glob pattern."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve() if (Path.cwd() / p).exists() else (WORKSPACE / p).resolve()
    if not p.exists():
        return f"(path not found: {p})"
    g = _search_gate(p, None)
    if g:
        return g
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".buddy"}
    matches = []
    try:
        for f in p.rglob(pattern):
            if any(part in skip for part in f.parts):
                continue
            if _sensitive_path_reason(f):  # don't enumerate sensitive material
                continue
            if len(matches) >= 200:
                break
            matches.append(str(f.relative_to(p) if p != f else f.name) + ("/" if f.is_dir() else ""))
    except Exception as e:
        return f"(find error: {e})"
    if not matches:
        return f"(no files matching {pattern!r} under {p})"
    return f"Found {len(matches)} matches for {pattern!r}:\n" + "\n".join(matches)


def todo_read() -> str:
    """Return active session todos as JSON."""
    if not _ACTIVE_TODOS:
        return "(no todos recorded for this session)"
    return json.dumps(_ACTIVE_TODOS, indent=2)


def todo_write(todos: list) -> str:
    """Replace the session todo list (opencode todowrite parity). The brain
    sends the FULL list each call; UIs render it from the tool args."""
    global _ACTIVE_TODOS
    if not isinstance(todos, list) or not todos:
        return "(todo_write needs a non-empty todos array)"
    valid = ("pending", "in_progress", "completed")
    clean = []
    for t in todos:
        if not isinstance(t, dict) or not str(t.get("content", "")).strip():
            return "(each todo needs a content and a status)"
        st = t.get("status", "pending")
        if st not in valid:
            return (f"(unknown todo status {st!r} — use pending, "
                    f"in_progress or completed)")
        clean.append({"content": str(t["content"]), "status": st})
    _ACTIVE_TODOS = clean
    done = sum(1 for t in clean if t["status"] == "completed")
    return f"{done}/{len(clean)} todos"

def grep(pattern: str, path: str = ".", include: str = "",
         confirm=None) -> str:
    """Search file contents under path with a regex (opencode grep parity).
    Returns 'N matches' + file:line: text rows. include narrows by glob,
    e.g. include='*.py'. Sensitive files are skipped inside the walk — a
    sensitive ROOT is confirm-gated, but content under it must never leak
    via recursion either."""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"(bad regex: {e})"
    root = Path(path).expanduser()
    if not root.is_absolute():
        root = WORKSPACE / root
    g = _search_gate(root, confirm)
    if g:
        return g
    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = sorted(root.rglob(include or "*"))
    else:
        return f"(path not found: {root})"
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".buddy"}
    rows: list[str] = []
    matches = 0
    truncated = False
    for f in files:
        if len(rows) >= 200:
            truncated = True
            break
        try:
            if not f.is_file() or f.stat().st_size > 1_000_000:
                continue
            if any(part in skip for part in f.parts):
                continue
            if _sensitive_path_reason(f):  # never read sensitive content in a walk
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            if rx.search(ln):
                matches += 1
                if len(rows) < 200:
                    rows.append(f"{f}:{i}: {ln.strip()[:200]}")
                else:
                    truncated = True
                    break
        if truncated:
            break
    if not matches:
        return f"(no matches for {pattern!r} under {root})"
    out = f"{matches} match{'es' if matches != 1 else ''} for {pattern!r} under {root}"
    out += "\n" + "\n".join(rows)
    if truncated:
        out += f"\n...{matches - len(rows)} more matches not shown"
    return out

def _record_diff(path: Path, old_content: str, new_content: str) -> None:
    """Store a unified diff of old_content vs new_content for this thread.
    Callers must capture old_content BEFORE writing the file."""
    import difflib
    diff = "\n".join(difflib.unified_diff(
        old_content.splitlines(), new_content.splitlines(),
        fromfile=str(path) + " (old)", tofile=str(path) + " (new)", lineterm=""))
    with _DIFF_LOCK:
        # Bounded: a caller that records but never pops (or a dead thread
        # ident) can never grow this without limit in the daemon.
        if len(_LAST_DIFF) > 64:
            for k in list(_LAST_DIFF)[:32]:
                _LAST_DIFF.pop(k, None)
        _LAST_DIFF[threading.get_ident()] = diff or None

def pop_diff() -> str | None:
    """Return and clear this thread's last recorded file diff."""
    with _DIFF_LOCK:
        return _LAST_DIFF.pop(threading.get_ident(), None)

def _atomic_write(p, content: str) -> None:
    """tmp + os.replace: a crash mid-write can never leave a truncated file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, p)


def edit_file(path: str, old_text: str, new_text: str, confirm=None) -> str:
    """Replace the first exact occurrence of old_text with new_text in a file."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p
    reason = _sensitive_path_reason(p)
    if reason:
        if confirm is None or not confirm(f"edit {p} ({reason})"):
            return "(edit cancelled by user: sensitive path)"
    if not p.exists():
        return f"(file not found: {p})"
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as e:
        return f"(couldn't read file: {e})"
    if old_text not in content:
        return (f"(old_text not found in {p} — read the file and copy the "
                f"exact text, including whitespace)")
    if content.count(old_text) > 1:
        return (f"(old_text matches {content.count(old_text)} places in {p} — "
                f"include more surrounding text so it matches exactly once)")
    _atomic_write(p, content.replace(old_text, new_text, 1))
    _record_diff(p, content, p.read_text(encoding="utf-8"))
    return f"Edited {p}"

def write_file(path: str, content: str, confirm=None) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p  # relative paths live in buddy's workspace
    reason = _sensitive_path_reason(p)
    if reason:
        if confirm is None or not confirm(f"write {p} ({reason})"):
            return "(write cancelled by user: sensitive path)"
    try:
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        _atomic_write(p, content)
    except OSError as e:
        return f"(couldn't write file: {e})"
    _record_diff(p, old, content)
    return f"Wrote {len(content)} chars to {p}"




def _host_pm() -> str | None:
    """Detected host package manager (apt/dnf/pacman/zypper/brew), or None."""
    try:
        tools = probe_system().get("tools", {})
    except Exception:
        tools = {}
    import shutil as _sh
    for pm in ("apt-get", "apt", "dnf", "pacman", "zypper", "brew"):
        if tools.get(pm) or _sh.which(pm):
            return "apt" if pm in ("apt-get", "apt") else pm
    return None


def _pending_count(pm: str, raw: str) -> int:
    """How many packages the check output lists as upgradable.

    Substring sniffing ("0 upgrades" / "up to date") never matched real
    apt/dnf output, so a fully-up-to-date machine still looked like it
    had pending work. Count the actual "old -> new" package lines per
    package manager instead."""
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    pats = {
        "pacman": r"^\S+\s+\S+\s*->\s*\S+",              # pkg ver -> new
        "apt": r"^\S+/\S+.*upgradable from",             # pkg/stable upgradable from ...
        # zypper: "pkg | repo | old | new | arch" — the header ("S | Repository |…")
        # matches the same shape, so exclude the status-letter first field
        "zypper": r"^(?!S\s*\|)\S+\s*\|\s*\S+\s*\|\s*\S+\s*\|\s*\S+",
        "brew": r"^\S+.*<\s*\S+",                        # name (1.0) < 2.0
    }
    if pm == "dnf":  # "pkg.arch  version  repo"
        return sum(1 for ln in lines
                   if re.match(r"^\S+\.\S+\s+\S+\s+\S+(\s+\S+)*$", ln)
                   and "last metadata" not in ln)
    pat = pats.get(pm)
    if not pat:
        return len(lines)
    return sum(1 for ln in lines if re.match(pat, ln))


def update_system(action: str = "check", confirm=None) -> str:
    """Update THIS machine's OS packages (the device buddy runs on).

    action="check" is read-only: lists pending updates, never changes anything.
    action="upgrade" installs them via the host package manager (needs
    passwordless sudo; asks permission first — confirm=None means deny).
    This is for "update this pc/device/machine". Buddy updating HIMSELF is
    self_update/auto_upgrade instead.
    """
    action = (action or "check").strip().lower()
    if action not in ("check", "upgrade"):
        return "(refused: action must be 'check' or 'upgrade')"
    pm = _host_pm()
    if not pm:
        return ("(no supported package manager found "
                "(apt/dnf/pacman/zypper/brew) — can't update this machine)")
    checks = {
        "apt": "apt list --upgradable 2>/dev/null | head -n 50",
        "dnf": "dnf check-update 2>&1 | head -n 50",
        "pacman": "pacman -Qu 2>&1 | head -n 50",
        "zypper": "zypper list-updates 2>&1 | head -n 50",
        "brew": "brew outdated --verbose 2>&1 | head -n 50",
    }
    upgrades = {
        "apt": "sudo -n apt-get update && sudo -n apt-get upgrade -y",
        "dnf": "sudo -n dnf upgrade -y",
        "pacman": "sudo -n pacman -Syu --noconfirm",
        "zypper": "sudo -n zypper -n up",
        "brew": "brew upgrade",
    }
    if action == "check":
        out = run_command(checks[pm], timeout=120)
        n = _pending_count(pm, out)
        if n <= 0:
            return (f"this machine is up to date (via {pm}) — 0 packages "
                    f"pending upgrade")
        return f"{n} package(s) pending update (via {pm}):\n{out}"
    if confirm is None or not confirm(f"install OS package upgrades via {pm}"):
        return "(system upgrade cancelled by user)"
    out = run_command(upgrades[pm], timeout=1800, confirm=lambda _m: True)
    if "sudo" in out.lower() and ("password" in out.lower() or "permission denied" in out.lower()):
        return (out + "\n(sudo needs passwordless permission for this — "
                "add a sudoers drop-in or run the upgrade yourself in a terminal)")
    return f"system upgrade result (via {pm}):\n{out}"


# ---- original buddy.py lines 2258-2259 --------------------------------


# ---- original buddy.py lines 2260-2342 --------------------------------
BASE_TOOLS = [
    ("run_command", "Run a shell command on the user's PC. Output streams live to the UI while the command runs; the result is the tail of the combined output (full log is saved to a file when very long) plus the exit code. If the command times out, it is killed and you are told to retry with a larger timeout. An optional workdir sets the working directory. Dangerous commands are blocked unless the user confirms. sudo is available when configured (config \"sudo\": \"nopass\" or \"password\").",
     {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 60}, "workdir": {"type": "string", "description": "optional working directory"}},
     ["command"]),
    ("update_system", "Update THIS machine's OS packages (the device buddy runs on — NOT buddy himself). action='check' lists pending updates (safe, read-only); action='upgrade' installs them (asks permission first, can take a while, needs passwordless sudo). 'update this pc/device/machine/system' means THIS tool; 'update yourself/buddy/code' means self_update/auto_upgrade.",
     {"action": {"type": "string", "description": "check or upgrade (default check)"}}, []),
    ("read_file", "Read a text file from the user's PC.",
     {"path": {"type": "string"}}, ["path"]),
    ("grep", "Search file contents under a path with a regex. Returns 'file:line: text' rows plus the match count. Use include to narrow file types, e.g. include='*.py'.",
     {"pattern": {"type": "string"}, "path": {"type": "string", "description": "file or directory to search (default: workspace)"}, "include": {"type": "string", "description": "glob filter like '*.py'"}},
     ["pattern"]),
    ("write_file", "Write (or overwrite) a text file on the user's PC.",
     {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    ("edit_file", "Replace the first exact occurrence of old_text with new_text in a file. Prefer this over write_file for small changes.",
     {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, ["path", "old_text", "new_text"]),
    ("todo_write", "Write the full todo list for the current task (replaces the previous list). Use it to plan multi-step work and keep the user posted: call it early with pending items, flip items to in_progress as you start them and completed as you finish, re-calling this tool with the FULL updated list each time.",
     {"todos": {"type": "array", "items": {"type": "object", "properties": {
         "content": {"type": "string"},
         "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
         "required": ["content", "status"]}}}, ["todos"]),
    ("web_search", "Search the web (DuckDuckGo) and return titles + URLs. Fast — use this to FIND pages when you don't have a URL.",
     {"query": {"type": "string"}}, ["query"]),
    ("web_fetch", "Fetch ONE known URL or bare domain (e.g. football.com) and return its readable text and structure — this is how you CHECK/LOOK AT a website. Cheap and fast. Bare domains auto-gain https://.",
     {"url": {"type": "string"}}, ["url"]),
    ("browse", "Open a URL in a real headless browser (renders JavaScript). Costs ~10s per call — reserve for JS-heavy pages web_fetch can't render. Falls back to plain fetch if playwright isn't installed.",
     {"url": {"type": "string"}, "wait_seconds": {"type": "integer", "default": 3}},
     ["url"]),
    ("transcribe", "Transcribe an audio/video file to text.",
     {"path": {"type": "string"}}, ["path"]),
    ("generate_image", "Generate an image from a text prompt and save it as a PNG.",
     {"prompt": {"type": "string"}, "out_path": {"type": "string"}}, ["prompt"]),
    ("screenshot", "Take a screenshot of the user's desktop.",
     {"out_path": {"type": "string"}}, []),
    ("clipboard", "Read ('get') or write ('set') the desktop clipboard.",
     {"action": {"type": "string"}, "text": {"type": "string"}}, ["action"]),
    ("open_url", "Open a URL or file with the system default app (xdg-open).",
     {"target": {"type": "string"}}, ["target"]),
    ("remember", "Save a durable fact about the user to persistent memory.",
     {"fact": {"type": "string"}}, ["fact"]),
    ("slash_command",
     "Run a chat REPL command on the user's behalf when they ask in plain "
     "language. Allowed: /status, /memory, /inbox, /jobs, /skills, /tools, "
     "/model <preset-name|model-id>, /fix (self-repair from error logs), "
     "/evolve (one self-improvement cycle), /clear. "
     "Session-only commands (/forget /quit /yolo) are NOT allowed.",
     {"command": {"type": "string", "description": "the slash command, e.g. '/model google'"}},
     ["command"]),
    ("recall", "Read the full flat memory file.", {}, []),
    ("search_memory", "Semantically search remembered facts by meaning.",
     {"query": {"type": "string"}}, ["query"]),
    ("schedule", "Schedule a job. Use every_minutes for recurring, or at='HH:MM' for daily. Results go to the inbox.",
     {"prompt": {"type": "string"}, "every_minutes": {"type": "integer"}, "at": {"type": "string"}},
     ["prompt"]),
    ("list_jobs", "List scheduled jobs.", {}, []),
    ("reflect", "Record a lesson you learned into your own playbook. Optionally note the situation it applies to.",
     {"lesson": {"type": "string"}, "situation": {"type": "string"}}, ["lesson"]),
    ("edit_playbook", "Rewrite your entire playbook of self-written operating rules. Max 4000 chars.",
     {"new_playbook": {"type": "string"}}, ["new_playbook"]),
    ("sys_detect", "Re-scan the host machine (OS, tools, package manager) and return an updated profile. Use after installing software.",
     {}, []),
    ("save_skill", "Save a reusable procedure you figured out as a named skill. Write instructions to future-you: steps, commands, gotchas.",
     {"name": {"type": "string"}, "description": {"type": "string"}, "instructions": {"type": "string"}},
     ["name", "description", "instructions"]),
    ("use_skill", "Load the full instructions of a learned skill by slug (see the skill index in your prompt).",
     {"slug": {"type": "string"}}, ["slug"]),
    ("delegate", "Spawn a background subagent (a copy of you) that works on a task in parallel while you keep going. It reports to the inbox when done. Use for research, long jobs, or running several things at once.",
     {"task": {"type": "string"}, "name": {"type": "string"}}, ["task"]),
    ("tasks_status", "Check the status/results of your background subagents.", {}, []),
    ("acp", "Drive an external coding agent over the Agent Client Protocol (ACP) — e.g. Claude Code. Pass agent='list' to see configured agents, or agent=<name>, prompt=<what to ask it>, cwd=<working dir>.",
     {"agent": {"type": "string"}, "prompt": {"type": "string"}, "cwd": {"type": "string", "default": ""}},
     ["agent", "prompt"]),
    ("add_watcher", "Watch a URL, shell command, or file and get notified when it changes. Runs while you're open.",
     {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["url", "command", "file"]},
      "target": {"type": "string"}, "interval_minutes": {"type": "number", "default": 5}},
     ["name", "kind", "target"]),
    ("list_watchers", "List active watchers.", {}, []),
    ("remove_watcher", "Remove a watcher by name.", {"name": {"type": "string"}}, ["name"]),
    ("email_send", "Send an email (SMTP configured in config). Asks the user's permission first when in chat.",
     {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
     ["to", "subject", "body"]),
    ("email_check", "Read the latest emails via IMAP.",
     {"folder": {"type": "string", "default": "INBOX"}, "limit": {"type": "integer", "default": 5}}, []),
    ("post_social", "Post to social media (bluesky or mastodon). Asks permission first — it's public.",
     {"platform": {"type": "string"}, "text": {"type": "string"}}, ["platform", "text"]),
    ("code_edit", "Make a surgical edit to any file — including YOUR OWN source code. Python files are syntax-checked and backed up automatically. Improvements to yourself apply on next restart.",
     {"target": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
     ["target", "old_text", "new_text"]),
    ("publish_site", "Publish or update a page on your website, served at /site/ on your web UI. Re-publish to improve pages over time.",
     {"filename": {"type": "string"}, "html": {"type": "string"}}, ["filename", "html"]),
    ("self_update", "Check GitHub for a newer buddy.py and install it (backed up, test-verified, syntax-checked).",
     {"repo": {"type": "string"}}, []),
    ("auto_upgrade", "Automatically check for, validate, and apply the latest upgrades to Buddy's codebase with regression test validation and automatic rollback.",
     {"repo": {"type": "string", "description": "Optional upstream GitHub repo slug (default: config or webyter/buddy)"}}, []),
    ("evolve", "Run one self-improvement cycle: review your playbook/skills/code and make exactly one real improvement.",
     {}, []),
    ("codex", "Delegate a coding task to the Codex CLI agent (if installed on this machine). Best for heavy multi-file coding jobs; can take minutes.",
     {"task": {"type": "string"}, "timeout": {"type": "integer", "default": 900}}, ["task"]),
    ("self_repair", "Run one self-repair cycle: diagnose AST syntax errors, failing tests, crash logs, or user-reported bugs, and patch your own source code (with automatic test verification and backups).",
     {"issue": {"type": "string", "description": "Optional bug description or problem statement to diagnose and fix"}}, []),
    ("hot_reload", "Hot-reload a buddy_core module after a self-edit so the fix applies IMMEDIATELY without restarting. Pass e.g. 'buddy_core.tools' or just 'tools'.",
     {"module_name": {"type": "string"}}, ["module_name"]),
    ("integrate_tool", "Create and permanently register a custom Python tool for Buddy when the user asks you to add or integrate a tool. The code must provide the function implementation. The tool is immediately loaded and persists in ~/.buddy/tools/.",
     {"name": {"type": "string", "description": "tool function name, e.g. 'get_weather'"},
      "description": {"type": "string", "description": "clear description of what the tool does"},
      "parameters": {"type": "object", "description": "JSON Schema properties dict for arguments"},
      "code": {"type": "string", "description": "Python source code defining the function"}},
     ["name", "description", "code"]),
    ("remove_custom_tool", "Remove a user-integrated custom tool from ~/.buddy/tools/.",
     {"name": {"type": "string", "description": "custom tool name to delete"}}, ["name"]),
    ("introspect", "Perform an introspective self-diagnostic of your own live cognitive state, active model, loaded tools, memory count, playbook lessons, operational health, and host environment. Use when asked about your internal state, capabilities, or performance.",
     {"focus": {"type": "string", "enum": ["all", "model", "memory", "tools", "health"], "default": "all"}},
     []),
]

# ---- original buddy.py lines 2347-2348 --------------------------------


# ---- original buddy.py lines 2349-2355 --------------------------------

def _resolve_path(path: str) -> Path:
    """Resolve a path against cwd if it exists or points into cwd; otherwise WORKSPACE."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    cwd_p = (Path.cwd() / p).resolve()
    if cwd_p.exists():
        return cwd_p
    return (WORKSPACE / p).resolve()

def _backup(path: Path) -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS / f"{path.name}.{stamp}.bak"
    if path.exists():
        dest.write_bytes(path.read_bytes())
    return dest

# ---- original buddy.py lines 2356-2357 --------------------------------


# ---- original buddy.py lines 2358-2382 --------------------------------
def code_edit(target: str, old_text: str, new_text: str, confirm=None) -> str:
    """Surgical edit of any file — including buddy's own source code.
    Python files are syntax-checked before saving; broken edits are refused.
    Paths are resolved in this order:
      1. Absolute path → used as-is.
      2. buddy.py or buddy_core/… → resolved relative to BUDDY_SRC (buddy's
         own install directory) so self-edits always hit the live source.
      3. Anything else → resolved relative to WORKSPACE.
    After a successful self-edit, call hot_reload('buddy_core.<module>') to
    apply the change instantly without restarting.
    """
    from .config import BUDDY_SRC
    p = Path(target).expanduser()
    if p.is_absolute():
        path = p
    elif target == "buddy.py" or target.startswith("buddy_core/") or target.startswith("buddy_core\\"):
        path = BUDDY_SRC / target
    else:
        path = _resolve_path(target)
    if not path.exists():
        return f"(file not found: {path})"
    current = path.read_text(encoding="utf-8")
    if old_text not in current:
        return "(old_text not found in file — nothing changed; read the file first and retry with exact text)"
    if current.count(old_text) > 1:
        return "(old_text matches multiple places — include more surrounding lines to make it unique)"
    updated = current.replace(old_text, new_text, 1)
    if path.suffix == ".py":
        try:
            import ast as _ast
            _ast.parse(updated)
        except SyntaxError as e:
            return f"(refused: edit would break Python syntax: {e})"
    # NOTE: confirm=None means DENY. Syntax is validated first so broken
    # edits are refused even without a confirmer, and we never prompt for
    # an edit that would not apply.
    if confirm is None or not confirm(f"edit {path}"):
        return "(edit cancelled by user)"
    _backup(path)
    path.write_text(updated, encoding="utf-8")
    _record_diff(path, current, updated)
    # Tell buddy whether the change is to his own code
    if str(path).startswith(str(BUDDY_SRC)):
        rel = path.relative_to(BUDDY_SRC)
        mod = str(rel).replace("/", ".").removesuffix(".py")
        return (f"edited {rel} ✓ (backup saved). "
                f"Call hot_reload('{mod}') to apply immediately, or restart buddy.")
    return f"edited {path} ✓ (backup saved in ~/.buddy/backups)"


def hot_reload(module_name: str, confirm=None) -> str:
    """Hot-reload a buddy_core module so a self-edit applies immediately
    without restarting.  module_name should be e.g. 'buddy_core.tools' or
    just 'tools' (buddy_core prefix is added automatically).
    Returns 'OK' or an error message."""
    if confirm is None or not confirm(f"hot-reload {module_name}"):
        return "(hot-reload cancelled by user)"
    import importlib, sys
    if "." not in module_name:
        module_name = f"buddy_core.{module_name}"
    try:
        if module_name not in sys.modules:
            importlib.import_module(module_name)
        else:
            importlib.reload(sys.modules[module_name])
        return f"hot_reload OK: {module_name} is live"
    except Exception as e:
        return f"(hot_reload failed for {module_name}: {e})"



# ---- original buddy.py lines 2383-2384 --------------------------------


# ---- original buddy.py lines 2385-2392 --------------------------------
def publish_site(filename: str, html: str, confirm=None) -> str:
    """Publish a page to buddy's website, served at /site/ on his web UI."""
    filename = re.sub(r"[^a-zA-Z0-9._-]", "", filename)
    if filename in ("", ".", ".."):
        filename = "index.html"
    site = WORKSPACE / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / filename).write_text(html)
    return (f"published {filename} — live at http://127.0.0.1:{PORT}/site/{filename} "
            f"(web UI token required). Improve pages over time by re-publishing.")

# ---- original buddy.py lines 2393-2394 --------------------------------


# ---- original buddy.py lines 2395-2425 --------------------------------
def self_update(repo: str = "", confirm=None) -> str:
    """Pull the latest buddy (buddy.py + the buddy_core/ package) from GitHub
    (the repo it came from), back up changed copies, verify test health,
    swap in if they parse, and hot-reload updated modules."""
    import re as _re
    cfg0 = (load_config() if CONFIG.exists() else {})
    configured = (cfg0.get("repo") or "").strip()
    slug = (repo or "").strip() or configured or "webyter/buddy"
    if not _re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        return f"(refused: bad repo slug {slug!r} — expected owner/repo)"
    # Model-controlled slug pointing away from the configured/default repo is a
    # supply-chain risk: require user confirm (None = deny). Explicit CLI
    # (`upgrade <repo>`) passes confirm; background daemon uses configured slug.
    if (repo or "").strip() and (repo or "").strip() != configured and (repo or "").strip() != "webyter/buddy":
        if confirm is None or not confirm(f"self-update from repo {slug}"):
            return "(update cancelled by user: untrusted repo)"
    base = f"https://raw.githubusercontent.com/{slug}/main"
    try:
        new_srcs = {f: _net_open(f"{base}/{f}", timeout=30).decode()
                    for f in UPDATE_FILES}
    except Exception:
        # private repo: raw.githubusercontent.com needs the OAuth token
        tok = secret_get("github_token")
        if not tok:
            return ("(update failed: couldn't fetch the repo. If it's private, run "
                    "`python3 buddy.py oauth github` once so I can authenticate.)")
        try:
            new_srcs = {}
            for f in UPDATE_FILES:
                req = urllib.request.Request(f"{base}/{f}", headers={"Authorization": f"Bearer {tok}"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    new_srcs[f] = r.read().decode()
        except Exception as e:
            return f"(update failed: {e})"
    root = Path(__file__).resolve().parent.parent  # repo root, not buddy_core/
    if root.name == "buddy_core":  # belt and braces: never write inside the package
        root = root.parent
    def _changed(f, s):
        p = root / f
        if not p.exists():
            return True
        try:
            return p.read_text(encoding="utf-8", errors="replace") != s
        except OSError:
            return True  # unreadable: treat as changed and overwrite
    changed = [f for f, s in new_srcs.items() if _changed(f, s)]
    if not changed:
        return "already on the latest version."
    try:
        import ast
        for f, s in new_srcs.items():
            try:
                ast.parse(s)
            except SyntaxError as e:
                return f"(refused: downloaded code doesn't parse in {f}: {e})"
    except SyntaxError as e:
        return f"(refused: downloaded code doesn't parse: {e})"

    # Save backups before writing changes
    saved_backups: dict[str, Path] = {}
    created: list[str] = []
    for f in changed:
        p = root / f
        if p.exists():
            saved_backups[f] = _backup(p)
        else:
            created.append(f)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_srcs[f], encoding="utf-8")

    # Post-upgrade verification: run test suite (or compile-check when the
    # repo ships no tests — "discover" exits 4 and every upgrade would roll
    # back as a phantom failure)
    import subprocess, sys
    try:
        if (root / "tests").is_dir():
            v_run = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            why, detail = "broke test suite", (v_run.stderr or v_run.stdout)[:300]
        else:
            v_run = subprocess.run(
                [sys.executable, "-m", "py_compile", *UPDATE_FILES],
                cwd=str(root), capture_output=True, text=True, timeout=60)
            why, detail = "did not compile", (v_run.stderr or "")[:300]
        if v_run.returncode != 0:
            # Automatic rollback on test failure!
            for f, bak in saved_backups.items():
                if bak and bak.exists():
                    (root / f).write_bytes(bak.read_bytes())
            for f in created:
                try:
                    (root / f).unlink()
                except OSError:
                    pass
            return (f"(update aborted & rolled back: downloaded code {why}:\n"
                    f"{detail})")
    except Exception as e:
        for f, bak in saved_backups.items():
            if bak and bak.exists():
                try:
                    (root / f).write_bytes(bak.read_bytes())
                except OSError:
                    pass
        for f in created:
            try:
                (root / f).unlink()
            except OSError:
                pass
        return f"(update aborted & rolled back: verification could not run: {e})"

    # Hot-reload any changed buddy_core modules (internal: post-verification)
    reloaded = []
    for f in changed:
        if f.startswith("buddy_core/") and f.endswith(".py"):
            mod = f.replace("/", ".").removesuffix(".py")
            try:
                hot_reload(mod, confirm=lambda _msg: True)
                reloaded.append(mod)
            except Exception:
                pass

    reload_note = f" (hot-reloaded {len(reloaded)} modules: {', '.join(reloaded)})" if reloaded else ""
    return (f"updated {len(changed)}/{len(UPDATE_FILES)} file(s) from {slug} "
            f"({', '.join(changed)}; backups in ~/.buddy/backups).{reload_note}")


def auto_upgrade(repo: str = "", confirm=None) -> str:
    """Automatically check for upstream upgrades, validate syntax & test suite,
    apply updates with rollback on failure, and hot-reload modules."""
    return self_update(repo, confirm)

# ---- original buddy.py lines 2721-2721 --------------------------------


# ---- original buddy.py lines 2722-2732 --------------------------------
BASE_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": n,
            "description": d,
            "parameters": {"type": "object", "properties": p, "required": r},
        },
    }
    for (n, d, p, r) in BASE_TOOLS
]

_CUSTOM_TOOLS: dict[str, tuple[dict, any]] = {}
_CUSTOM_TOOLS_LOCK = threading.Lock()  # readers snapshot under it: no clear/update gap

def load_custom_tools(force: bool = False) -> list[dict]:
    """Scan ~/.buddy/tools/ for user-defined Python tools (.py files)."""
    with _CUSTOM_TOOLS_LOCK:
        if _CUSTOM_TOOLS and not force:
            return [spec for spec, _ in _CUSTOM_TOOLS.values()]
    fresh: dict = {}  # build aside, swap in whole under the lock
    if not CUSTOM_TOOLS.exists():
        with _CUSTOM_TOOLS_LOCK:
            _CUSTOM_TOOLS.clear()
        return []
    import importlib.util
    for p in sorted(CUSTOM_TOOLS.glob("*.py")):
        if p.name.startswith(("_", ".")):
            continue
        try:
            mod_name = f"buddy_custom_tool_{p.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, p)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            tool_dict = getattr(mod, "TOOL", None)
            if isinstance(tool_dict, dict) and "name" in tool_dict and "handler" in tool_dict:
                t_name = tool_dict["name"]
                t_desc = tool_dict.get("description", "")
                t_params = tool_dict.get("parameters", {})
                t_req = tool_dict.get("required", list(t_params.keys()))
                t_spec = {
                    "type": "function",
                    "function": {
                        "name": t_name,
                        "description": t_desc,
                        "parameters": {"type": "object", "properties": t_params, "required": t_req},
                    },
                }
                fresh[t_name] = (t_spec, tool_dict["handler"])
            else:
                fn = getattr(mod, p.stem, None) or getattr(mod, "handler", None)
                if callable(fn):
                    t_name = p.stem
                    t_desc = (fn.__doc__ or f"Custom tool {t_name}").strip()
                    import inspect
                    sig = inspect.signature(fn)
                    t_params = {}
                    t_req = []
                    for param_name, param in sig.parameters.items():
                        if param_name in ("self", "cls"):
                            continue
                        p_type = "string"
                        if param.annotation is int:
                            p_type = "integer"
                        elif param.annotation is float:
                            p_type = "number"
                        elif param.annotation is bool:
                            p_type = "boolean"
                        elif param.annotation is list:
                            p_type = "array"
                        elif param.annotation is dict:
                            p_type = "object"
                        t_params[param_name] = {"type": p_type}
                        if param.default is inspect.Parameter.empty:
                            t_req.append(param_name)
                    t_spec = {
                        "type": "function",
                        "function": {
                            "name": t_name,
                            "description": t_desc,
                            "parameters": {"type": "object", "properties": t_params, "required": t_req},
                        },
                    }
                    fresh[t_name] = (t_spec, fn)
        except Exception as e:
            try:
                from .util import _log_error as _le
                _le("load_custom_tools", e)
            except Exception:
                pass
    with _CUSTOM_TOOLS_LOCK:
        _CUSTOM_TOOLS.clear()
        _CUSTOM_TOOLS.update(fresh)
        return [spec for spec, _ in _CUSTOM_TOOLS.values()]


def all_tool_specs() -> list[dict]:
    """Return all available tool specifications: built-in and user custom tools."""
    custom = load_custom_tools()
    return list(BASE_TOOL_SPECS) + custom


def integrate_tool(name: str, description: str, parameters: dict | None, code: str, confirm=None) -> str:
    """Create and register a custom Python tool for Buddy."""
    # Persistent code exec: model path must have user confirm (None = deny).
    # CLI `tools add` copies a user-chosen file directly and does not go through here.
    if confirm is None or not confirm(f"integrate tool {name}"):
        return "(integrate cancelled by user)"
    import ast
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip()).strip("_")
    if not name:
        return "(error: invalid tool name)"
    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"(syntax error in tool code: {e})"
    CUSTOM_TOOLS.mkdir(parents=True, exist_ok=True)
    target = CUSTOM_TOOLS / f"{name}.py"
    if "TOOL =" not in code and f"def {name}" in code:
        params_json = json.dumps(parameters or {}, indent=4)
        req_list = json.dumps(list((parameters or {}).keys()))
        wrapper_code = (
            f"{code}\n\n"
            f"TOOL = {{\n"
            f'    "name": {json.dumps(name)},\n'
            f'    "description": {json.dumps(description)},\n'
            f'    "parameters": {params_json},\n'
            f'    "required": {req_list},\n'
            f'    "handler": {name},\n'
            f"}}\n"
        )
    else:
        wrapper_code = code

    target.write_text(wrapper_code, encoding="utf-8")
    load_custom_tools(force=True)
    if name in _CUSTOM_TOOLS:
        return f"Tool '{name}' successfully integrated and active! Description: {description}"
    return f"Tool '{name}' written to {target}, but could not find a valid handler function."


def remove_custom_tool(name: str) -> str:
    """Remove a custom user tool from ~/.buddy/tools/."""
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", str(name).strip()).strip("_")
    target = CUSTOM_TOOLS / f"{name}.py"
    if not name or target.resolve().parent != CUSTOM_TOOLS.resolve():
        return "(error: invalid tool name)"
    if target.exists():
        target.unlink()
        load_custom_tools(force=True)
        return f"Tool '{name}' removed."
    return f"Tool '{name}' not found."


def introspect(focus: str = "all") -> str:
    """Perform a deep introspective self-diagnostic of Buddy's live operational state,
    cognitive context, active capabilities, memory, health, and environment."""
    from .config import CONFIG, DEFAULT_API_BASE, DEFAULT_MODEL, HOME, MEMORY, PLAYBOOK, SKILLS, load_config
    cfg = load_config() if CONFIG.exists() else {}

    sections = []

    # Cognitive & Model State
    brain = cfg.get("brain", "api")
    model = cfg.get("model", DEFAULT_MODEL)
    base = cfg.get("api_base", DEFAULT_API_BASE)
    mode = cfg.get("mode", "accept-edits")
    effort = cfg.get("effort", "medium")
    yolo = bool(cfg.get("yolo", False))

    sections.append(
        "🧠 [Cognitive & Model State]\n"
        f"  • Identity: Buddy v3 (Local AI Assistant & Autonomous Operator)\n"
        f"  • Active Engine: {model} (brain: {brain})\n"
        f"  • API Endpoint: {base}\n"
        f"  • Execution Mode: {mode} (effort: {effort}, confirmations: {'disabled/YOLO' if yolo else 'strict'})"
    )

    # Knowledge & Metacognition
    mem_count = len(MEMORY.read_text(encoding="utf-8").splitlines()) if MEMORY.exists() else 0
    playbook_text = PLAYBOOK.read_text(encoding="utf-8").strip() if PLAYBOOK.exists() else ""
    playbook_rules = len([ln for ln in playbook_text.splitlines() if ln.strip().startswith("-")])
    skills_count = len(list(SKILLS.glob("*.md"))) if SKILLS.exists() else 0

    sections.append(
        "📚 [Knowledge & Playbook]\n"
        f"  • Long-term Memories: {mem_count} facts retained (~/.buddy/memory.md)\n"
        f"  • Learned Skills: {skills_count} operational procedures (~/.buddy/skills/)\n"
        f"  • Self-training Playbook: {playbook_rules} active lessons (~/.buddy/playbook.md)"
    )

    # Toolchain & Integrations
    load_custom_tools()
    custom_count = len(_CUSTOM_TOOLS)
    builtin_count = len(BASE_TOOLS)
    acp_agents = len(cfg.get("acp_agents") or {})
    mcp_servers = len(cfg.get("mcp_servers") or {})

    sections.append(
        "🛠️ [Toolchain & Integrations]\n"
        f"  • Built-in Tools: {builtin_count} active\n"
        f"  • User Custom Tools: {custom_count} loaded (~/.buddy/tools/)\n"
        f"  • External ACP Agents: {acp_agents} configured\n"
        f"  • Connected MCP Servers: {mcp_servers} configured"
    )

    # Operational Health & Telemetry
    err_file = HOME / "errors.log"
    err_count = 0
    recent_err = "None"
    if err_file.exists():
        err_lines = err_file.read_text(encoding="utf-8", errors="replace").splitlines()
        err_count = len([ln for ln in err_lines if ln.startswith("## ")])
        if err_lines:
            recent_err = err_lines[-1][:120]

    from .sched import daemon_running
    daemon_st = "RUNNING" if daemon_running() else "STOPPED"
    from .sched import list_watchers
    w = list_watchers()
    w_count = len(w) if isinstance(w, list) else 0

    sections.append(
        "⚡ [Operational Health & Telemetry]\n"
        f"  • Daemon Status: {daemon_st}\n"
        f"  • Active Watchers: {w_count}\n"
        f"  • Error History: {err_count} logged issues (latest: {recent_err})\n"
        f"  • Workspace: {HOME / 'workspace'}"
    )

    focus_lower = (focus or "all").lower().strip()
    if focus_lower in ("cognitive", "model", "brain"):
        return sections[0]
    if focus_lower in ("knowledge", "memory", "playbook", "skills"):
        return sections[1]
    if focus_lower in ("toolchain", "tools", "mcp", "acp"):
        return sections[2]
    if focus_lower in ("telemetry", "health", "daemon", "status"):
        return sections[3]

    return "\n\n".join(sections)

# ---- original buddy.py lines 2772-2773 --------------------------------


# ---- original buddy.py lines 2774-2855 --------------------------------
def tool_impl(name: str, args: dict, confirm=None) -> str:
    try:
        return _tool_impl(name, args, confirm)
    except KeyError as e:
        # a missing required argument used to surface as a raw KeyError
        return f"(missing required argument for {name}: {e})"


def _tool_impl(name: str, args: dict, confirm=None) -> str:
    if name == "run_command":
        return run_command(args["command"], args.get("timeout", 60), confirm,
                           workdir=args.get("workdir"))
    if name == "slash_command":
        from .commands import run_slash_command
        return run_slash_command(load_config(), str(args.get("command", "")).strip())
    if name == "update_system":
        return update_system(args.get("action", "check"), confirm)
    if name == "read_file":
        return read_file(args["path"], confirm)
    if name == "grep":
        return grep(str(args.get("pattern", "")), str(args.get("path", ".")),
                    str(args.get("include", "")), confirm)
    if name == "find_files":
        return find_files(args.get("pattern", "*"), args.get("path", "."))
    if name == "list_dir":
        return list_dir(args.get("path", "."))
    if name == "write_file":
        return write_file(args["path"], args["content"], confirm)
    if name == "edit_file":
        return edit_file(args["path"], args["old_text"], args["new_text"], confirm)
    if name == "todo_write":
        return todo_write(args.get("todos"))
    if name == "todo_read":
        return todo_read()
    if name == "web_search":
        try:
            return web_search(args["query"])
        except Exception as e:
            return f"(search failed: {e})"
    if name == "web_fetch":
        try:
            return web_fetch(args["url"])
        except Exception as e:
            return f"(fetch failed: {e})"
    if name == "browse":
        return browse(args["url"], args.get("wait_seconds", 3))
    if name == "transcribe":
        return transcribe_audio(args["path"])
    if name == "generate_image":
        return generate_image(args["prompt"], args.get("out_path", ""))
    if name == "screenshot":
        return screenshot(args.get("out_path", ""))
    if name == "clipboard":
        return clipboard(args["action"], args.get("text", ""))
    if name == "open_url":
        return open_url(args["target"])
    if name == "remember":
        return remember(args["fact"])
    if name == "recall":
        return recall_memory()
    if name == "search_memory":
        return search_memory(args["query"])
    if name == "schedule":
        return add_job(args["prompt"], args.get("every_minutes"), args.get("at"))
    if name == "list_jobs":
        return list_jobs()
    if name == "reflect":
        return reflect(args["lesson"], args.get("situation", ""))
    if name == "edit_playbook":
        return edit_playbook(args["new_playbook"])
    if name == "sys_detect":
        return system_summary(probe_system(force=True))
    if name == "save_skill":
        return save_skill(args["name"], args["description"], args["instructions"])
    if name == "use_skill":
        return use_skill(args["slug"])
    if name == "delegate":
        from .agent import delegate
        return delegate(args["task"], args.get("name", ""))
    if name == "tasks_status":
        from .agent import tasks_status
        return tasks_status()
    if name == "acp":
        from .acp import acp_tool
        return acp_tool(load_config(), args.get("agent", ""), args.get("prompt", ""),
                        args.get("cwd", ""))
    if name == "add_watcher":
        return add_watcher(args["name"], args["kind"], args["target"],
                           args.get("interval_minutes", 5))
    if name == "list_watchers":
        return list_watchers()
    if name == "remove_watcher":
        return remove_watcher(args["name"])
    if name == "email_send":
        return email_send(args["to"], args["subject"], args["body"], confirm)
    if name == "email_check":
        return email_check(args.get("folder", "INBOX"), args.get("limit", 5))
    if name == "post_social":
        return post_social(args["platform"], args["text"], confirm)
    if name == "code_edit":
        return code_edit(args["target"], args["old_text"], args["new_text"], confirm)
    if name == "publish_site":
        return publish_site(args["filename"], args["html"])
    if name in ("self_update", "auto_upgrade"):
        return self_update(args.get("repo", ""), confirm)
    if name == "evolve":
        return evolve_pass(load_config(), __import__('buddy_core.agent', fromlist=['_sub_mcp'])._sub_mcp(), confirm)
    if name == "codex":
        from .agent import codex_run
        return codex_run(args["task"], args.get("timeout", 900))
    if name == "self_repair":
        return self_repair(load_config(), __import__('buddy_core.agent', fromlist=['_sub_mcp'])._sub_mcp(), confirm, issue=str(args.get("issue", "")))
    if name == "hot_reload":
        return hot_reload(args["module_name"], confirm)
    if name == "integrate_tool":
        return integrate_tool(
            str(args.get("name", "")),
            str(args.get("description", "")),
            args.get("parameters"),
            str(args.get("code", "")),
            confirm,
        )
    if name == "remove_custom_tool":
        return remove_custom_tool(str(args.get("name", "")))
    if name == "introspect":
        return introspect(str(args.get("focus", "all")))
    load_custom_tools()
    with _CUSTOM_TOOLS_LOCK:
        registered = name in _CUSTOM_TOOLS
        if registered:
            _, handler = _CUSTOM_TOOLS[name]
    if registered:
        try:
            res = handler(**args) if isinstance(args, dict) else handler()
            return str(res)
        except Exception as e:
            return f"(custom tool '{name}' error: {e})"
    _log_wish(f"called a tool that doesn't exist: {name} {json.dumps(args)[:200]}")
    return f"(unknown tool {name})"

# ---- original buddy.py lines 3082-3085 --------------------------------


# --- email (smtp/imap, all stdlib) ------------------------------------------

# ---- original buddy.py lines 3086-3114 --------------------------------
def email_send(to: str, subject: str, body: str, confirm=None) -> str:
    cfg = load_config()
    host = cfg.get("smtp_host")
    user = cfg.get("smtp_user")
    if not host or not user:
        return ('(email not configured — set "smtp_host", "smtp_port", "smtp_user" '
                'in ~/.buddy/config.json and "smtp_pass" via `python3 buddy.py secret set smtp_pass`)')
    if confirm is None or not confirm(f"send email to {to}: {subject}"):
        return "(email cancelled by user)"
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = cfg.get("smtp_from", user)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(cfg.get("smtp_port", 587)), timeout=30) as s:
            s.ehlo()
            try:
                s.starttls()
            except smtplib.SMTPException:
                # server already TLS or local relay — never send a password plaintext
                if host not in ("localhost", "127.0.0.1"):
                    return "(smtp requires TLS — refusing to send password in plaintext)"
            pw = secret_get("smtp_pass")
            if pw:
                s.login(user, pw)
            s.send_message(msg)
    except Exception as e:
        return f"(email failed: {e})"
    return f"email sent to {to}"

# ---- original buddy.py lines 3115-3116 --------------------------------


# ---- original buddy.py lines 3117-3151 --------------------------------
def email_check(folder: str = "INBOX", limit: int = 5) -> str:
    cfg = load_config()
    host = cfg.get("imap_host")
    user = cfg.get("imap_user", cfg.get("smtp_user"))
    if not host or not user:
        return ('(imap not configured — set "imap_host", "imap_port", "imap_user" '
                'in ~/.buddy/config.json and "imap_pass" via `python3 buddy.py secret set imap_pass`)')
    import imaplib
    import email as _email
    try:
        m = imaplib.IMAP4_SSL(host, int(cfg.get("imap_port", 993)))
    except Exception as e:
        return f"(imap connection failed: {e})"
    try:
        try:
            m.login(user, secret_get("imap_pass") or secret_get("smtp_pass"))
        except Exception as e:
            return f"(imap login failed: {e})"
        m.select(folder)
        _, data = m.search(None, "ALL")
        # limit<=0 meant "everything" by accident: [-0:] == [0:] == all ids.
        try:
            n = max(1, int(limit))
        except (TypeError, ValueError):
            n = 5
        ids = (data[0].split() if data and data[0] else [])[-n:]
        out = []
        for mid in reversed(ids):
            _, d = m.fetch(mid, "(BODY.PEEK[])")
            if not d or not isinstance(d[0], (tuple, list)) or len(d[0]) < 2:
                continue
            msg = _email.message_from_bytes(d[0][1])
            snippet = ""
            try:
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            # get_payload(decode=True) is None for payloads
                            # without an encoding (e.g. nested multiparts).
                            raw_part = part.get_payload(decode=True)
                            if raw_part:
                                snippet = raw_part.decode("utf-8", "replace")
                                break
                else:
                    raw_msg = msg.get_payload(decode=True)
                    if raw_msg:
                        snippet = (raw_msg.decode("utf-8", "replace")
                                   if isinstance(raw_msg, bytes)
                                   else str(raw_msg))
            except Exception as e:
                snippet = f"(could not read body: {e})"
            out.append(f"From: {msg['From']}\nSubject: {msg['Subject']}\n"
                       f"Date: {msg['Date']}\n{snippet[:500]}")
        return "\n\n---\n\n".join(out) or "(mailbox empty)"
    except Exception as e:  # select/search/fetch errors: report, never leak raw
        return f"(imap read failed: {e})"
    finally:
        try:
            m.logout()
        except Exception:
            pass

# ---- original buddy.py lines 3152-3155 --------------------------------


# --- social posting (bluesky + mastodon, both plain REST) -------------------

# ---- original buddy.py lines 3156-3187 --------------------------------
def post_social(platform: str, text: str, confirm=None) -> str:
    platform = platform.lower().strip()
    if confirm is None or not confirm(f"post publicly to {platform}: {text[:80]!r}"):
        return "(post cancelled by user)"
    cfg = load_config()
    def _post(url: str, payload: dict, headers: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    if platform == "bluesky":
        handle = cfg.get("bluesky_handle")
        pw = secret_get("bluesky_app_password")
        if not handle or not pw:
            return '(set "bluesky_handle" in config and "bluesky_app_password" via secret set)'
        try:
            session = _post("https://bsky.social/xrpc/com.atproto.server.createSession",
                            {"identifier": handle, "password": pw}, {})
            if not isinstance(session, dict) or not session.get("did") or not session.get("accessJwt"):
                return "(bluesky login failed: bad session response)"
            record = {"repo": session["did"], "collection": "app.bsky.feed.post",
                      "record": {"text": text, "createdAt": datetime.now(timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%S.000Z")}}
            _post("https://bsky.social/xrpc/com.atproto.repo.createRecord", record,
                  {"Authorization": f"Bearer {session['accessJwt']}"})
        except Exception as e:
            return f"(bluesky post failed: {e})"
        return f"posted to Bluesky as {handle}"
    if platform == "mastodon":
        token = secret_get("mastodon_token")
        base = (cfg.get("mastodon_instance") or "https://mastodon.social").rstrip("/")
        if not token:
            return '(set "mastodon_token" via secret set, optionally "mastodon_instance" in config)'
        try:
            _post(f"{base}/api/v1/statuses", {"status": text},
                  {"Authorization": f"Bearer {token}"})
        except Exception as e:
            return f"(mastodon post failed: {e})"
        return f"posted to Mastodon ({base})"
    return "(unsupported platform: try 'bluesky' or 'mastodon')"

