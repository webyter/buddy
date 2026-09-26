"""buddy_core.config — paths, constants, shared mutable state, config load/save, key & secret storage.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations

import itertools
import json
import os
try:
    import readline  # type: ignore
except ImportError:  # exotic platforms — chat() falls back to plain input()
    readline = None
try:  # command palette: raw-mode tty handling (absent on Windows)
    import select as _select
    import termios as _termios
except ImportError:
    _select = _termios = None
import sys
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only; avoids a runtime circular import
    from .mcp import MCPManager

# ---- original buddy.py lines 62-62 ------------------------------------


# ---- original buddy.py lines 63-63 ------------------------------------
HOME = Path.home() / ".buddy"

# Root directory of buddy's own source code (the directory containing buddy.py
# and the buddy_core/ package). Used for self-editing and hot-reload.
BUDDY_SRC: Path = Path(__file__).resolve().parent.parent

# ---- original buddy.py lines 64-64 ------------------------------------
CONFIG = HOME / "config.json"

# ---- original buddy.py lines 65-65 ------------------------------------
SECRETS = HOME / "secrets.json"

# ---- original buddy.py lines 66-66 ------------------------------------
MEMORY = HOME / "memory.md"

# ---- original buddy.py lines 67-67 ------------------------------------
PLAYBOOK = HOME / "playbook.md"

# ---- original buddy.py lines 68-68 ------------------------------------
JOBS = HOME / "jobs.json"

# ---- original buddy.py lines 69-69 ------------------------------------
INBOX = HOME / "inbox.md"

# ---- original buddy.py lines 70-70 ------------------------------------
MCP_CONFIG = HOME / "mcp.json"

# ---- original buddy.py lines 71-71 ------------------------------------
VECTORS = HOME / "vectors.json"

# ---- original buddy.py lines 72-72 ------------------------------------
SKILLS = HOME / "skills"
CUSTOM_TOOLS = HOME / "tools"

# ---- original buddy.py lines 73-73 ------------------------------------
WORKSPACE = HOME / "workspace"

# ---- original buddy.py lines 74-74 ------------------------------------
DAEMON_PID = HOME / "daemon.pid"

# ---- original buddy.py lines 75-78 ------------------------------------

DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-flash-latest"  # auto-tracks the newest stable flash

# Speech system (TTS + transcription) — LOCKED to Gemini 3.8 Flash via the
# native Gemini API. Deliberately NOT user-configurable: no config key,
# no /model interaction, cfg["tts_model"] is ignored. The user's chat
# model/endpoint can change freely; speech always uses these.
SPEECH_API = "https://generativelanguage.googleapis.com/v1beta"
SPEECH_TTS_MODEL = "gemini-3.8-flash-tts"
SPEECH_STT_MODEL = "gemini-3.8-flash"

FREE_PRESETS = {
    "google": (DEFAULT_API_BASE, DEFAULT_MODEL,
               "Google AI Studio default (aistudio.google.com/apikey)."),
}

# ---- original buddy.py lines 86-86 ------------------------------------


# ---- original buddy.py lines 87-87 ------------------------------------
PORT = 7616

# ---- original buddy.py lines 88-88 ------------------------------------
MAX_WEB_BODY = 1_000_000

# ---- original buddy.py lines 89-89 ------------------------------------
STATE_LOCK = threading.RLock()

# ---- original buddy.py lines 90-90 ------------------------------------
_CONFIG_LOCK = threading.RLock()  # RLock so _save_config() can nest inside existing with-blocks
_SECRETS_LOCK = threading.RLock()  # secrets.json read-modify-write must not lose a concurrent secret

# every file that makes up the program — `buddy.py update` / install.sh fetch
# exactly these (keep in sync with buddy_core.FRAGMENTS)
UPDATE_FILES = [
    "buddy.py",
    "buddy_core/__init__.py",
    "buddy_core/config.py", "buddy_core/util.py", "buddy_core/prompts.py",
    "buddy_core/tui.py", "buddy_core/memory.py", "buddy_core/skills.py",
    "buddy_core/tools.py", "buddy_core/mcp.py", "buddy_core/sched.py",
    "buddy_core/brain.py", "buddy_core/agent.py", "buddy_core/webui.py",
    "buddy_core/commands.py", "buddy_core/acp.py", "buddy_core/theme.py",
]

# ---- original buddy.py lines 254-254 ----------------------------------


# ---- original buddy.py lines 255-255 ----------------------------------
BUDDY_VERSION = "v3"

# ---- original buddy.py lines 722-724 ----------------------------------

# =========================================================== config/secrets =

# ---- original buddy.py lines 725-734 ----------------------------------
def load_config() -> dict:
    if not CONFIG.exists():
        # Zero-config start: don't gate buddy behind setup. Create a default
        # config and run in offline mode (`! cmd`, status/memory/calc, local
        # model) until an API key is added or `buddy.py local` is run.
        try:
            HOME.mkdir(parents=True, exist_ok=True)
            _save_config({})
            print(f"(no config yet — created {CONFIG} with defaults)\n"
                  "(running offline: shell/`!cmd`, memory, calc work now;\n"
                  " add an API key with `python3 buddy.py setup` for full chat)")
        except OSError:
            print("No config yet. Run: python3 buddy.py setup")
            sys.exit(1)
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("configuration must be a JSON object")
        cfg.setdefault("api_base", DEFAULT_API_BASE)
        cfg.setdefault("model", DEFAULT_MODEL)
        cfg.setdefault("auto_upgrade", True)
        cfg.setdefault("auto_repair", True)
        cfg.setdefault("upgrade_hours", 24)
        _plain = cfg.pop("api_key", "")
        if _plain:
            # One-time migration: plaintext key in config.json -> secure store
            # (system keyring first, else ~/.buddy/secrets.json 0600).
            try:
                _store_key(_plain)
                _save_config(cfg)
            except Exception:
                cfg["api_key"] = _plain  # restore: migration must not lose keys
        return cfg
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"ERROR: {CONFIG} is corrupt or unreadable ({e}). "
              f"Fix or remove it, then run: python3 buddy.py setup")
        sys.exit(1)


# ---- original buddy.py lines 725-734 ----------------------------------
_CONFIG_STAMP: list = [0.0]  # mtime of the file `cfg` was loaded from


def _read_config_quiet() -> dict | None:
    """Parse config.json without load_config()'s print+sys.exit.

    maybe_reload() runs INSIDE a live turn: a half-written or corrupt
    file (hand-edit, concurrent _save_config) must not kill the daemon
    or print a setup error — it just means "keep using what we have"."""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(cfg, dict):
        return None
    cfg.setdefault("api_base", DEFAULT_API_BASE)
    cfg.setdefault("model", DEFAULT_MODEL)
    cfg.setdefault("auto_upgrade", True)
    cfg.setdefault("auto_repair", True)
    cfg.setdefault("upgrade_hours", 24)
    cfg.pop("api_key", "")  # never adopt a plaintext key mid-session
    return cfg


def maybe_reload(cfg: dict) -> dict:
    """Return cfg, re-read from disk if config.json changed since load.

    The daemon (and the TUI) hold one config dict for their whole life,
    so `python3 buddy.py model set X`, `/yolo`, or a hand-edit never took
    effect until restart. One stat() per call makes external changes
    visible without a restart; a failed/absent reload keeps the old dict
    so a transient write can never break a running turn."""
    try:
        if not CONFIG.exists():
            return cfg
        stamp = CONFIG.stat().st_mtime
        if stamp == _CONFIG_STAMP[0]:
            return cfg
        fresh = _read_config_quiet()
        if fresh is None:
            return cfg  # unreadable/corrupt — keep serving with the old cfg
        _CONFIG_STAMP[0] = stamp
        # Preserve in-process session state that isn't stored in the file.
        for k in ("mode", "effort", "web_token", "yolo", "theme"):
            if k in cfg and k not in fresh:
                fresh[k] = cfg[k]
        with _CONFIG_LOCK:  # iterating readers must never see a half-cleared dict
            cfg.clear()
            cfg.update(fresh)
        return cfg
    except Exception:
        return cfg


def _stamp_config(cfg: dict) -> dict:
    """Record the current config.json mtime as loaded (call after load_config)."""
    try:
        _CONFIG_STAMP[0] = CONFIG.stat().st_mtime
    except OSError:
        _CONFIG_STAMP[0] = 0.0
    return cfg

# ---- original buddy.py lines 735-736 ----------------------------------


# ---- original buddy.py lines 737-755 ----------------------------------
def api_key(cfg: dict) -> str:
    if os.environ.get("BUDDY_API_KEY"):
        return os.environ["BUDDY_API_KEY"]
    try:
        import keyring  # type: ignore

        k = keyring.get_password("buddy", "api_key")
        if k:
            return k
    except Exception:
        pass
    if SECRETS.exists():
        try:
            data = json.loads(SECRETS.read_text(encoding="utf-8"))
            k = data.get("api_key") if isinstance(data, dict) else ""
            if k:
                return k
        except Exception:
            pass
    return cfg.get("api_key", "")


def provider_key_name(cfg: dict, model: str, base: str) -> str:
    """Secret name holding the key for an integrated provider, e.g. a model
    added as `deepseek` is stored as `api_key_deepseek`."""
    name = ""
    base = (base or "").rstrip("/")
    models = (cfg or {}).get("models")
    if not isinstance(models, dict):
        return ""
    for cand, info in models.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("model") or cand) == model and \
                str(info.get("api_base") or "").rstrip("/") == base:
            name = cand
            break
    return f"api_key_{name}" if name else ""


def api_key_for(cfg: dict, model: str = "", base: str = "") -> str:
    """The key to use for THIS provider.

    Adding a second provider must not send the first provider's key to a
    third-party host: an integrated model with its own `api_key_<name>`
    secret uses that, and only falls back to the shared key when the user
    deliberately added the provider without one."""
    model = model or str((cfg or {}).get("model") or "")
    base = base or str((cfg or {}).get("api_base") or "")
    name = provider_key_name(cfg, model, base)
    if name:
        # An ABSENT own secret means the user deliberately added this
        # provider to reuse the shared key (documented, tested). But None
        # means the vault couldn't be read — fail the call instead of
        # leaking the shared key to this provider's host.
        k = secret_get(name)
        if k is None:
            return ""  # vault unreadable: no key this turn, no leak
        if k:
            return k
    return api_key(cfg)

# ---- original buddy.py lines 756-757 ----------------------------------


# ---- original buddy.py lines 758-762 ----------------------------------
def _needs_key(cfg: dict) -> bool:
    """True if this brain needs an API key stored. CLI sign-in brains don't."""
    if cfg.get("brain") in ("codex", "claude", "gemini", "agy"):
        return False
    return True

# ---- original buddy.py lines 763-764 ----------------------------------


# ---- original buddy.py lines 765-782 ----------------------------------
def _store_key(key: str) -> str:
    """Persist an API key as securely as this machine allows. Returns a short
    human-readable 'where' label ('' if key is empty)."""
    if not key:
        return ""
    try:
        import keyring  # type: ignore

        keyring.set_password("buddy", "api_key", key)
        return "system keyring"
    except Exception:
        pass
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(SECRETS.read_text(encoding="utf-8")) if SECRETS.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["api_key"] = key
    _write_secrets(data)
    return f"{SECRETS} (0600)"

# ---- original buddy.py lines 783-784 ----------------------------------


# ---- original buddy.py lines 785-793 ----------------------------------
def cmd_key_set() -> None:
    import getpass

    key = getpass.getpass("API key: ").strip()
    if not key:
        print("Empty key, nothing stored.")
        return
    where = _store_key(key)
    print(f"Stored in {where}.")

# ---- original buddy.py lines 794-795 ----------------------------------


# ---- original buddy.py lines 796-803 ----------------------------------
def web_token(cfg: dict) -> str:
    with _CONFIG_LOCK:  # first-run: two threads must not mint two tokens
        tok = cfg.get("web_token")
        if not tok:
            tok = uuid.uuid4().hex
            cfg["web_token"] = tok
            _save_config(cfg)
        return tok

# ---- original buddy.py lines 804-805 ----------------------------------


# ---- original buddy.py lines 806-828 ----------------------------------
def setup() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    print("Setting up buddy v3.\n")
    base = input(f"API base URL [{DEFAULT_API_BASE}]: ").strip() or DEFAULT_API_BASE
    model = input(f"Model [{DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL
    embed = input(
        "Embedding model for semantic memory [text-embedding-3-small, empty=skip]: "
    ).strip()
    # Merge with existing config so re-running setup never drops
    # web_token/models/theme/mode/etc.
    existing: dict = {}
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
        except (OSError, json.JSONDecodeError, ValueError):
            existing = {}
    merged = {
        **existing,
        "api_base": base,
        "model": model,
        **({"embed_model": embed} if embed else {}),
    }
    _save_config(merged)
    print(f"\nSaved {CONFIG}.")
    print("Now store your key:  python3 buddy.py key set")
    print("Chat:                python3 buddy.py")
    print("Always-on + web UI:  python3 buddy.py serve")

# ---- original buddy.py lines 2343-2345 --------------------------------

# --- self-improvement: buddy can edit code, publish a site, update himself --

# ---- original buddy.py lines 2346-2346 --------------------------------
BACKUPS = HOME / "backups"

# ---- original buddy.py lines 2468-2471 --------------------------------


# --- telegram: text buddy from your phone -----------------------------------

# ---- original buddy.py lines 2472-2474 --------------------------------
def _save_config(cfg: dict) -> None:
    """Atomic + 0600 config write. Never leave a truncated world-readable config."""
    with _CONFIG_LOCK:
        tmp = CONFIG.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(cfg, indent=2))
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        os.replace(tmp, CONFIG)
        try:
            os.chmod(CONFIG, 0o600)
        except OSError:
            pass

# ---- original buddy.py lines 2856-2859 --------------------------------


# ========================================================= platform pack ==

# ---- original buddy.py lines 2860-2875 --------------------------------
def secret_get(name: str) -> str:
    """The stored secret, '' when absent, None when the vault is unreadable
    (keyring raised AND the secrets file had nothing) — api_key_for treats
    None as 'fail this call' so a transient vault error can't leak the
    shared key to a third-party provider."""
    _vault_error = False
    try:
        import keyring  # type: ignore
        k = keyring.get_password("buddy", name)
        if k:
            return k
    except Exception:
        _vault_error = True
    if SECRETS.exists():
        try:
            data = json.loads(SECRETS.read_text(encoding="utf-8"))
            k = data.get(name) if isinstance(data, dict) else ""
            if k:
                return k
        except Exception:
            pass
    return None if _vault_error else ""

# ---- original buddy.py lines 2876-2877 --------------------------------


# ---- original buddy.py lines 2878-2890 --------------------------------
def secret_set(name: str, value: str) -> str:
    try:
        import keyring  # type: ignore
        keyring.set_password("buddy", name, value)
        return f"stored {name} in system keyring"
    except Exception:
        pass
    HOME.mkdir(parents=True, exist_ok=True)
    with _SECRETS_LOCK:  # read-modify-write must not lose a concurrent secret
        try:
            data = json.loads(SECRETS.read_text(encoding="utf-8")) if SECRETS.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[name] = value
        _write_secrets(data)
    return f"keyring unavailable; stored {name} in {SECRETS} (0600)"



def _write_secrets(data: dict) -> None:
    """Write secrets atomically and private from the first byte on disk."""
    tmp = SECRETS.with_suffix(".json.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp, SECRETS)
    except BaseException:
        try:
            tmp.unlink()  # never leave a plaintext-secret tmp file behind
        except OSError:
            pass
        raise

# ---- original buddy.py lines 2891-2894 --------------------------------


# --- subagents: buddy multitasks by delegating to himself ------------------

# ---- original buddy.py lines 2895-2895 --------------------------------
TASKS: dict[str, dict] = {}

# ---- original buddy.py lines 2896-2896 --------------------------------
_TASK_SEQ = itertools.count(1)

# ---- original buddy.py lines 2897-2897 --------------------------------
_SUB_MCP: "MCPManager | None" = None

# ---- original buddy.py lines 2946-2949 --------------------------------


# --- watchers: poll a url / command / file, get notified on change ----------

# ---- original buddy.py lines 2950-2950 --------------------------------
WATCHERS_FILE = HOME / "watchers.json"

# ---- original buddy.py lines 3402-3405 --------------------------------


# ============================================== web sessions / cancellation =

# ---- original buddy.py lines 3406-3406 --------------------------------
SESSIONS_DIR = HOME / "sessions"

# ---- original buddy.py lines 3407-3407 --------------------------------
MAX_SESSIONS = 20

# ---- original buddy.py lines 3408-3408 --------------------------------
SESSIONS: dict = {}   # id -> {"id","title","created","updated","messages":[]}

# ---- original buddy.py lines 3409-3409 --------------------------------
CANCELS: dict = {}    # session_id -> threading.Event (live turns)

# ---- original buddy.py lines 5230-5231 --------------------------------


# ---- original buddy.py lines 5232-5232 --------------------------------
_CFG: dict | None = None



