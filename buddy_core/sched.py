"""buddy_core.sched — jobs, scheduler daemon, watchers, telegram bot, service install.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .config import DAEMON_PID, JOBS, PORT, STATE_LOCK, WATCHERS_FILE, _save_config, load_config, secret_get
from .mcp import MCPManager
from .memory import memory_compact
from .util import _log_error, deliver

import hashlib
import getpass
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
import traceback
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ---- original buddy.py lines 1253-1254 --------------------------------


# ---- original buddy.py lines 1255-1265 --------------------------------
def load_jobs() -> list:
    with STATE_LOCK:
        if not JOBS.exists():
            return []
        try:
            data = json.loads(JOBS.read_text())
            if not isinstance(data, list):
                return []
            return [j for j in data if isinstance(j, dict) and j.get("id")]
        except (OSError, json.JSONDecodeError):
            return []

# ---- original buddy.py lines 1266-1267 --------------------------------


# ---- original buddy.py lines 1268-1271 --------------------------------
def save_jobs(jobs: list) -> None:
    with STATE_LOCK:
        JOBS.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOBS.with_suffix(".json.tmp")  # atomic: a crash mid-write must
        tmp.write_text(json.dumps(jobs, indent=2))  # not truncate jobs.json
        os.replace(tmp, JOBS)

# ---- original buddy.py lines 1272-1273 --------------------------------


# ---- original buddy.py lines 1274-1297 --------------------------------
def add_job(prompt: str, every_minutes: int | None, at: str | None,
            in_minutes: int | None = None) -> str:
    if not prompt.strip():
        return "(refused: job prompt is empty)"
    if (every_minutes is None) + (at is None) + (in_minutes is None) != 2:
        return "(refused: specify exactly one of every_minutes, at, or in_minutes)"
    if every_minutes is not None and (isinstance(every_minutes, bool)
                                        or not isinstance(every_minutes, int)
                                        or every_minutes <= 0):
        return "(refused: every_minutes must be a positive integer)"
    if at is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at):
        return "(refused: at must be HH:MM in 24-hour time)"
    if in_minutes is not None and (isinstance(in_minutes, bool)
                                   or not isinstance(in_minutes, int)
                                   or in_minutes <= 0 or in_minutes > 60 * 24 * 7):
        return "(refused: in_minutes must be 1..10080)"
    with STATE_LOCK:
        jobs = load_jobs()
        job = {
            "id": uuid.uuid4().hex[:8],
            "prompt": prompt,
            "created": datetime.now().isoformat(timespec="seconds"),
            "last_run": None,
        }
        if every_minutes is not None:
            job["every_minutes"] = every_minutes
        elif at is not None:
            job["at"] = at
        else:
            job["in_minutes"] = in_minutes
        jobs.append(job)
        save_jobs(jobs)
    if in_minutes is not None:
        return (f"Timer {job['id']} set: {prompt!r} in {in_minutes} min "
                f"(fires once, then deletes itself)")
    return f"Scheduled job {job['id']}: {prompt!r}"


def cancel_job(job_id: str) -> str:
    """Remove a scheduled job by id (works offline, no LLM)."""
    job_id = job_id.strip().lower()
    with STATE_LOCK:
        jobs = load_jobs()
        kept = [j for j in jobs if not (isinstance(j, dict) and j.get("id") == job_id)]
        if len(kept) == len(jobs):
            return f"(no job with id {job_id!r} — see `jobs`)"
        save_jobs(kept)
    return f"Cancelled job {job_id}."

# ---- original buddy.py lines 1298-1299 --------------------------------


# ---- original buddy.py lines 1300-1308 --------------------------------
def list_jobs() -> str:
    jobs = [j for j in load_jobs() if isinstance(j, dict)]
    if not jobs:
        return "(no scheduled jobs)"
    rows = []
    for j in jobs:
        sched = (f"every {j['every_minutes']}min" if j.get("every_minutes")
                 else (f"daily @{j['at']}" if j.get("at")
                       else (f"once in {j['in_minutes']}min" if j.get("in_minutes")
                             else "(unscheduled)")))
        rows.append(f"{j.get('id', '?')}: {str(j.get('prompt', ''))[:70]} {sched}")
    return "\n".join(rows)

# ---- original buddy.py lines 1326-1327 --------------------------------


# ---- original buddy.py lines 1328-1341 --------------------------------
def _job_due(job: dict, now: datetime) -> bool:
    """Return whether a persisted job is due using local wall-clock time."""
    def local_time(value) -> datetime:
        if not isinstance(value, str):
            raise ValueError("last_run must be an ISO timestamp")
        parsed = datetime.fromisoformat(value)
        return (parsed.astimezone().replace(tzinfo=None)
                if parsed.tzinfo is not None else parsed)

    if job.get("last_run"):
        if job.get("every_minutes"):
            last = local_time(job["last_run"])
            return (now - last).total_seconds() >= job["every_minutes"] * 60
        last = local_time(job["last_run"])
        if last.date() == now.date():
            return False
        hh, mm = map(int, job["at"].split(":"))
        return (now.hour, now.minute) >= (hh, mm)
    # backlog deferral: not due until the defer window passes (last_run is
    # deliberately untouched — the job never ran)
    if job.get("deferred_until"):
        if now < local_time(job["deferred_until"]):
            return False
    if job.get("in_minutes"):
        created = local_time(job["created"])
        return (now - created).total_seconds() >= job["in_minutes"] * 60
    if job.get("at"):
        hh, mm = map(int, job["at"].split(":"))
        return (now.hour, now.minute) >= (hh, mm)
    return True

# ---- original buddy.py lines 1342-1343 --------------------------------


# ---- original buddy.py lines 1344-1352 --------------------------------
def _pid_is_buddy(pid: int) -> bool:
    """Best-effort pid-reuse guard: True if pid looks like a buddy daemon."""
    if pid == os.getpid():
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        return "buddy" in cmd
    except OSError:
        return True  # non-Linux or unreadable: fall back to kill(pid, 0)


def daemon_running() -> bool:
    if not DAEMON_PID.exists():
        return False
    try:
        pid = int(DAEMON_PID.read_text().strip())
        os.kill(pid, 0)
        if not _pid_is_buddy(pid):
            try:
                DAEMON_PID.unlink()  # stale: pid recycled by another program
            except OSError:
                pass
            return False
        return True
    except Exception:
        try:
            DAEMON_PID.unlink()
        except OSError:
            pass
        return False

# ---- original buddy.py lines 1353-1354 --------------------------------


# ---- original buddy.py lines 1355-1398 --------------------------------
class Scheduler:
    def __init__(self, cfg: dict, mcp: "MCPManager", confirm=None):
        self.cfg = cfg
        self.mcp = mcp
        self.confirm = confirm  # shell-confirm callback; None = auto-deny
        self.stop = threading.Event()
        self._last_compact: "datetime.date | None" = None  # memory housekeeping

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        # After downtime every overdue job is due at once — cap executions per
        # tick so the backlog drains gradually instead of stampeding.
        _MAX_JOBS_PER_TICK = 3
        while not self.stop.wait(20):
          try:
            now = datetime.now()
            _ran = 0
            for job in load_jobs():
                try:
                    due = _job_due(job, now)
                except Exception:
                    # mark it so a permanently-broken job reports once, not
                    # every 20-second tick forever
                    with STATE_LOCK:
                        fresh = load_jobs()
                        for current in fresh:
                            if current.get("id") == job.get("id"):
                                current["last_run"] = now.isoformat(timespec="seconds")
                                current["broken"] = True
                                save_jobs(fresh)
                                break
                    deliver("(job has invalid schedule data)", title=f"job {job.get('id', '?')}")
                    continue
                if not due:
                    continue
                if _ran >= _MAX_JOBS_PER_TICK:
                    # Defer WITHOUT stamping last_run: stamping here marked
                    # never-run jobs as run — one-shot in_minutes timers
                    # became permanently broken (no "at" for _job_due to
                    # parse) and deferred daily jobs silently skipped a day.
                    with STATE_LOCK:
                        jobs = load_jobs()
                        for current in jobs:
                            if current.get("id") == job.get("id"):
                                current["deferred_until"] = (
                                    now + timedelta(minutes=5)).isoformat(timespec="seconds")
                                save_jobs(jobs)
                                break
                    continue
                _ran += 1
                try:
                    from .agent import run_agent  # lazy: agent.py imports tools imports sched
                    # Pick up config.json edits (model switch, yolo, limits)
                    # without waiting for a daemon restart.
                    from .config import maybe_reload
                    maybe_reload(self.cfg)
                    result = run_agent(
                        self.cfg, self.mcp, job["prompt"], quiet=True,
                        confirm=self.confirm,
                    )
                    deliver(result, title=f"job {job['id']}")
                except Exception as e:
                    deliver(f"(job failed: {e})", title=f"job {job['id']}")
                with STATE_LOCK:
                    if job.get("in_minutes"):
                        # one-shot timer: fire once, then remove (success or fail)
                        fresh = load_jobs()
                        save_jobs([j for j in fresh
                                   if not (isinstance(j, dict) and j.get("id") == job.get("id"))])
                    else:
                        jobs = load_jobs()
                        for current in jobs:
                            if current.get("id") == job.get("id"):
                                current["last_run"] = now.isoformat(timespec="seconds")
                                save_jobs(jobs)
                                break
            # built-in daily housekeeping: fold stale session digests
            if now.date() != self._last_compact:
                self._last_compact = now.date()
                try:
                    memory_compact(self.cfg)
                except Exception:
                    _log_error("memory compact failed:\n" + traceback.format_exc())
          except Exception:
            _log_error("scheduler tick failed:\n" + traceback.format_exc())
            continue  # self-heal: one bad tick never kills all job execution

# ---- original buddy.py lines 2475-2476 --------------------------------


# ---- original buddy.py lines 2477-2485 --------------------------------
def telegram_send(chat_id: str, text: str) -> None:
    token = secret_get("telegram_token")
    if not token or not chat_id:
        return
    payload = json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
        method="POST", headers={"Content-Type": "application/json"})
    # `with` — a bare urlopen left the response (and its socket) to
    # refcounting on a path that runs for every notification.
    with urllib.request.urlopen(req, timeout=20) as _r:
        _r.read()

# ---- original buddy.py lines 2486-2487 --------------------------------


# ---- original buddy.py lines 2488-2543 --------------------------------
class TelegramBot:
    """Long-polls Telegram and answers messages from the paired chat only.
    Pair by messaging the bot once — it learns your chat id on first contact."""

    def __init__(self, cfg: dict):
        self.cfg = cfg if cfg is not None else {}
        self.stop = threading.Event()
        self.offset = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self._poll()
            except Exception:
                _log_error("telegram poll failed:\n" + traceback.format_exc())
                self.stop.wait(5)  # self-heal: network blips never kill the bot

    def _poll(self):
        token = secret_get("telegram_token")
        if not token:
            self.stop.wait(60)
            return
        url = (f"https://api.telegram.org/bot{token}/getUpdates"
               f"?timeout=50&offset={self.offset}")
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.loads(r.read())
        for upd in data.get("result", []):
            self.offset = max(self.offset, upd.get("update_id", 0) + 1)
            msg = upd.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if not chat or not text:
                continue
            allowed = self.cfg.get("telegram_chat_id")
            if allowed and chat != str(allowed):
                continue  # not you — ignore
            if not allowed:
                # No auto-pair: first messager must prove local access by sending
                # the pairing code from `python3 buddy.py web` (the web_token).
                # Anyone else just gets pairing instructions, never an agent turn.
                # With web_auth=false there is no token: pair manually by setting
                # "telegram_chat_id" in ~/.buddy/config.json instead.
                try:
                    if self.cfg.get("web_auth") is False:
                        pairing = ""
                    else:
                        from .config import web_token as _web_token
                        pairing = _web_token(self.cfg)
                except Exception:
                    pairing = ""
                if pairing and text.strip() == pairing:
                    self.cfg["telegram_chat_id"] = chat
                    try:
                        disk = load_config()
                        disk["telegram_chat_id"] = chat
                        _save_config(disk)
                    except Exception:
                        _log_error("telegram pairing save failed:\n" + traceback.format_exc())
                    telegram_send(chat, "paired. I'll answer here from now on.")
                else:
                    try:
                        if self.cfg.get("web_auth") is False:
                            telegram_send(chat, "not paired. Ask the owner to set telegram_chat_id in config.")
                        else:
                            telegram_send(chat, "not paired. Send the pairing code from `python3 buddy.py web` on your machine.")
                    except Exception:
                        pass
                continue
            try:
                from .agent import run_agent, _sub_mcp  # lazy: avoids circular import
                reply = run_agent(self.cfg, _sub_mcp(), text, quiet=True, confirm=None)
            except Exception as e:
                reply = f"(something broke: {e})"
            try:
                telegram_send(chat, reply)
            except Exception:
                _log_error("telegram send failed:\n" + traceback.format_exc())

# ---- original buddy.py lines 2951-2952 --------------------------------


# ---- original buddy.py lines 2953-2960 --------------------------------
def load_watchers() -> list:
    with STATE_LOCK:
        if not WATCHERS_FILE.exists():
            return []
        try:
            rows = json.loads(WATCHERS_FILE.read_text())
        except Exception:
            return []
    # a hand-edited/corrupt row must not kill the tick loop
    return [w for w in rows if isinstance(w, dict)] if isinstance(rows, list) else []

# ---- original buddy.py lines 2961-2962 --------------------------------


# ---- original buddy.py lines 2963-2968 --------------------------------
def save_watchers(watchers: list) -> None:
    with STATE_LOCK:
        WATCHERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WATCHERS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(watchers, indent=2))
        os.replace(tmp, WATCHERS_FILE)

# ---- original buddy.py lines 2969-2970 --------------------------------


# ---- original buddy.py lines 2971-2984 --------------------------------
def _watch_snapshot(kind: str, target: str) -> str:
    if kind == "url":
        # Only real web URLs: the default opener also handles file:// (and
        # other schemes), which would read local files into the change log.
        scheme = urllib.parse.urlparse(target).scheme.lower()
        if scheme not in ("http", "https"):
            return "(refused: watcher url must be http:// or https://)"
        from .tools import _net_open  # lazy: avoids circular import
        data = _net_open(target, timeout=20)  # no cache: change detection needs live data
        return hashlib.sha256(data).hexdigest() + "|" + data[:400].decode("utf-8", "replace")
    if kind == "command":
        from .tools import is_dangerous  # lazy: avoids circular import
        pat = is_dangerous(target)
        if pat:
            return f"(refused by safety guard: matches dangerous pattern {pat})"
        import signal as _signal
        proc = subprocess.Popen(target, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            # kill the whole process group: shell children/grandchildren must
            # not outlive the watcher tick (they used to leak)
            try:
                os.killpg(proc.pid, _signal.SIGKILL)
            except Exception:
                proc.kill()
            out, err = proc.communicate()
        return ((out or "") + (err or ""))[:10000].strip()  # bounded capture
    if kind == "file":
        path = Path(target).expanduser()
        from .tools import _sensitive_path_reason  # lazy: avoids circular import
        reason = _sensitive_path_reason(path)
        if reason:
            return f"(refused by safety guard: {reason})"
        if not path.exists():
            return "(missing)"
        try:
            if path.stat().st_size > 5_000_000:
                return "(file too large to watch)"
        except OSError:
            return "(unreadable)"
        return hashlib.sha256(path.read_bytes()).hexdigest() + "|" + path.read_text(errors="replace")[:400]
    raise ValueError(f"unknown watcher kind: {kind}")

# ---- original buddy.py lines 2985-2986 --------------------------------


# ---- original buddy.py lines 2987-2995 --------------------------------
def add_watcher(name: str, kind: str, target: str, interval_minutes: float = 5.0) -> str:
    if not str(name or "").strip():
        return "(refused: watcher name required)"
    if kind not in ("url", "command", "file"):
        return f"(refused: unknown watcher kind {kind!r} — use url, command, or file)"
    if not str(target or "").strip():
        return "(refused: watcher target required)"
    try:
        interval = float(interval_minutes)
    except (TypeError, ValueError):
        return f"(refused: bad interval {interval_minutes!r})"
    if not (0.5 <= interval <= 43200):
        return f"(refused: interval {interval} out of range 0.5..43200 minutes)"
    if kind == "command":
        from .tools import is_dangerous  # lazy: avoids circular import
        pat = is_dangerous(target)
        if pat:
            return f"(refused by safety guard: matches dangerous pattern {pat})"
    if kind == "url":
        # Gate at registration too: the snapshot check is the backstop, but a
        # prompt-injected add should be refused outright.
        scheme = urllib.parse.urlparse(str(target)).scheme.lower()
        if scheme not in ("http", "https"):
            return "(refused: watcher url must be http:// or https://)"
    if kind == "file":
        from .tools import _sensitive_path_reason  # lazy: avoids circular import
        reason = _sensitive_path_reason(Path(str(target)).expanduser())
        if reason:
            return f"(refused by safety guard: {reason})"
    watchers = [w for w in load_watchers() if isinstance(w, dict) and w.get("name") != name]
    watchers.append({"name": str(name), "kind": kind, "target": str(target),
                     "interval_minutes": interval,
                     "last_state": None, "last_check": None})
    save_watchers(watchers)
    return (f"watcher '{name}' set: {kind} {target} every {interval} min. "
            f"Runs while chat/daemon is open; changes land in your inbox.")

# ---- original buddy.py lines 2996-2997 --------------------------------


# ---- original buddy.py lines 2998-3004 --------------------------------
def list_watchers() -> str:
    watchers = [w for w in load_watchers() if isinstance(w, dict)]
    if not watchers:
        return "(no watchers set)"
    return "\n".join(
        f"{w.get('name','?')}: {w.get('kind','?')} {w.get('target','?')} every {w.get('interval_minutes','?')} min"
        for w in watchers)

# ---- original buddy.py lines 3005-3006 --------------------------------


# ---- original buddy.py lines 3007-3011 --------------------------------
def remove_watcher(name: str) -> str:
    watchers = [w for w in load_watchers() if isinstance(w, dict)]
    left = [w for w in watchers if w.get("name") != name]
    save_watchers(left)
    return f"removed {len(watchers) - len(left)} watcher(s) named '{name}'"

# ---- original buddy.py lines 3012-3013 --------------------------------


# ---- original buddy.py lines 3014-3056 --------------------------------
class Watcher:
    """Background poller; wakes buddy's inbox when watched things change."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.wait(20):
            try:
                self._tick()
            except Exception:
                _log_error("watcher cycle failed:\n" + traceback.format_exc())
                continue  # self-heal: a bad cycle never kills the watcher

    def _tick(self):
        # NO outer STATE_LOCK here: _watch_snapshot does network/command I/O
        # (up to 60s timeouts) and holding the global state lock for it stalls
        # every other state op (web sessions, jobs, deliver). load_watchers/
        # save_watchers/deliver each take STATE_LOCK themselves.
        now = datetime.now()
        watchers = load_watchers()
        touched = False
        for w in watchers:
            try:
                if w.get("last_check"):
                    last = datetime.fromisoformat(w["last_check"])
                    if (now - last).total_seconds() < w["interval_minutes"] * 60:
                        continue
                snap = _watch_snapshot(w["kind"], w["target"])
                w["last_check"] = now.isoformat(timespec="seconds")
                if w.get("last_state") is not None and snap != w["last_state"]:
                    preview = snap.split("|", 1)[1][:400] if "|" in snap else snap[:400]
                    deliver(f"watcher '{w['name']}' saw a change:\n{preview}",
                            title=f"watcher {w['name']}")
                w["last_state"] = snap
                touched = True
            except Exception as e:
                deliver(f"watcher '{w.get('name', '?')}' error: {e}", title="watcher error")
                w["last_check"] = now.isoformat(timespec="seconds")
                touched = True
        if touched:
            # Re-read before writing: the snapshot above can be minutes old
            # (network/command watchers), so a watcher added or removed
            # mid-tick would be silently overwritten by this stale list.
            # Merge + save under the lock so two concurrent ticks can't
            # interleave read-modify-write (the merge itself is fast; the
            # slow network I/O above stays outside it).
            with STATE_LOCK:
                current = {(w.get("name"), w.get("kind"), w.get("target")): w
                           for w in load_watchers()}
                for w in watchers:
                    key = (w.get("name"), w.get("kind"), w.get("target"))
                    live = current.get(key)
                    if live is None:
                        continue  # removed while we were polling — drop it
                    live["last_check"] = w.get("last_check")
                    if "last_state" in w:
                        live["last_state"] = w.get("last_state")
                save_watchers(list(current.values()))

# ---- original buddy.py lines 4601-4602 --------------------------------


# ---- original buddy.py lines 4603-4620 --------------------------------
def install_service() -> None:
    """Write the user unit, enable + start it, and enable linger so the
    daemon survives logouts. Reports what failed instead of guessing."""
    # __file__ inside fragments resolves to the buddy.py entrypoint (the
    # fragments are exec'd into its namespace), so try several candidates.
    cands = [Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None,
             Path(__file__).resolve().parent / "buddy.py",
             Path(__file__).resolve().parent.parent / "buddy.py",
             Path.cwd() / "buddy.py"]
    entry = next((c for c in cands if c and c.name == "buddy.py" and c.exists()), None)
    if entry is None:
        print("(could not locate buddy.py — run this from the directory containing it)")
        return
    entry = entry.resolve()
    unit = f"""[Unit]
Description=buddy personal assistant daemon
After=network.target

[Service]
Type=simple
WorkingDirectory={entry.parent}
ExecStart="{sys.executable}" "{entry}" serve
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
    d = Path.home() / ".config/systemd/user"
    d.mkdir(parents=True, exist_ok=True)
    (d / "buddy.service").write_text(unit)
    print(f"Installed {d / 'buddy.service'} (entrypoint: {entry})")
    steps = (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "buddy"],
        ["loginctl", "enable-linger", getpass.getuser()],
    )
    failed = False
    for cmd in steps:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except FileNotFoundError:
            print(f"  (systemd not available — run manually: {' '.join(steps[1])})")
            return
        except Exception as e:
            print(f"  failed: {' '.join(cmd)} ({e})")
            failed = True
            continue
        if p.returncode == 0:
            print(f"  ok: {' '.join(cmd)}")
        else:
            print(f"  failed: {' '.join(cmd)}\n    {p.stderr.strip()[:200]}")
            failed = True
    if not failed:
        print("buddy daemon is now installed and running at boot")
    print("check status:  systemctl --user status buddy")


def service_status_summary(cfg: dict | None = None) -> str:
    from .config import HOME, PORT, DAEMON_PID
    lines = []
    pid = None
    if DAEMON_PID.exists():
        try:
            pid = int(DAEMON_PID.read_text().strip())
            os.kill(pid, 0)
            lines.append(f"• Buddy Daemon: RUNNING (pid {pid}, web port {PORT})")
        except (ValueError, OSError):
            lines.append("• Buddy Daemon: STOPPED (stale pid file)")
    else:
        lines.append("• Buddy Daemon: STOPPED")

    try:
        p = subprocess.run(["systemctl", "--user", "is-active", "buddy"], capture_output=True, text=True, timeout=5)
        st = p.stdout.strip()
        lines.append(f"• Systemd Service: {st.upper() if st else 'INACTIVE'}")
    except Exception:
        lines.append("• Systemd Service: unavailable")

    w = list_watchers()
    lines.append(f"• Active Watchers: {len(w) if isinstance(w, list) else 0}")

    s_dir = HOME / "services"
    if s_dir.exists():
        c_services = list(s_dir.glob("*"))
        lines.append(f"• User Services (~/.buddy/services/): {len(c_services)}")
    return "\n".join(lines)


def cmd_service(rest: list[str]) -> None:
    import signal
    from .config import HOME, DAEMON_PID
    sub = rest[0] if rest else "status"
    if sub in ("status", "list"):
        print("Buddy Services Status:")
        print(service_status_summary())
        print("\nCommands:")
        print("  python3 buddy.py service start      # start daemon / systemd service")
        print("  python3 buddy.py service stop       # stop daemon / systemd service")
        print("  python3 buddy.py service restart    # restart daemon")
        print("  python3 buddy.py service install    # install systemd user unit")
        print("  python3 buddy.py service logs       # show systemd service logs")
        print("  python3 buddy.py service add <name> <script>  # install user service")
    elif sub == "install":
        install_service()
    elif sub == "start":
        try:
            p = subprocess.run(["systemctl", "--user", "start", "buddy"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0:
                print("Started buddy service via systemctl.")
                return
        except Exception:
            pass
        if daemon_running():
            print("Daemon is already running.")
            return
        cand = Path(__file__).resolve().parent.parent / "buddy.py"
        entry = cand if cand.exists() else Path.cwd() / "buddy.py"
        log = HOME / "daemon.log"
        try:
            lf = open(log, "ab")
        except OSError:
            lf = subprocess.DEVNULL  # fall back: never fail the start itself
        subprocess.Popen([sys.executable, "-u", str(entry), "serve"], cwd=str(entry.parent),
                         stdout=lf, stderr=subprocess.STDOUT,
                         start_new_session=True)
        # Verify the child actually came up (bind failures exit fast) instead
        # of declaring success unconditionally after 0.5s.
        up = False
        for _ in range(30):
            time.sleep(0.2)
            if daemon_running():
                up = True
                break
        if up:
            print(f"Buddy daemon started in background. (log: {log})")
        else:
            print(f"Daemon failed to start — see {log} (port {PORT} in use?)")
    elif sub == "stop":
        stopped = False
        try:
            p = subprocess.run(["systemctl", "--user", "stop", "buddy"], capture_output=True, text=True, timeout=10)
            if p.returncode == 0:
                print("Stopped buddy service via systemctl.")
                stopped = True
        except Exception:
            pass
        if DAEMON_PID.exists():
            try:
                pid = int(DAEMON_PID.read_text().strip())
                if not _pid_is_buddy(pid):
                    print(f"pid {pid} is not buddy (recycled after reboot?) — leaving it alone.")
                else:
                    os.kill(pid, signal.SIGTERM)
                    print(f"Terminated buddy daemon pid {pid}.")
                    stopped = True
            except Exception:
                pass
            try:
                DAEMON_PID.unlink()
            except OSError:
                pass
        if not stopped:
            print("Buddy service was not running.")
    elif sub == "restart":
        cmd_service(["stop"])
        time.sleep(1)
        cmd_service(["start"])
    elif sub == "logs":
        try:
            p = subprocess.run(["journalctl", "--user", "-u", "buddy", "-n", "30", "--no-pager"],
                               capture_output=True, text=True, timeout=10)
            print(p.stdout or "(no logs recorded)")
        except Exception as e:
            print(f"(could not fetch logs: {e})")
    elif sub == "add" and len(rest) >= 3:
        name = Path(rest[1]).name  # never let a service name escape the dir
        if not name or name.startswith("."):
            print("Error: invalid service name")
            return
        src = Path(rest[2])
        if not src.exists():
            print(f"Error: file not found: {src}")
            return
        s_dir = HOME / "services"
        s_dir.mkdir(parents=True, exist_ok=True)
        dest = s_dir / f"{name}{src.suffix}"
        try:
            shutil.copyfile(src, dest)
            os.chmod(dest, 0o755)
        except OSError as e:
            print(f"Error: could not install service: {e}")
            return
        print(f"User service '{name}' installed at {dest}.")
    else:
        print("usage: python3 buddy.py service [status | start | stop | restart | install | logs | add <name> <file>]")

