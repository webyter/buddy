"""buddy_core.webui — local web UI: sessions, inbox cards, embedded page, HTTP server, serve().

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .agent import _sub_mcp, run_agent, run_messages
from .config import CANCELS, DAEMON_PID, HOME, INBOX, MAX_SESSIONS, MAX_WEB_BODY, PORT, SESSIONS, SESSIONS_DIR, STATE_LOCK, WORKSPACE, _stamp_config, maybe_reload, secret_get, web_token
from .mcp import MCPManager
from .sched import Scheduler, TelegramBot, Watcher, _pid_is_buddy
from .skills import AutoEvolve, AutoUpgrade, ErrorReaper, read_playbook
from .util import _log_error, deliver

import hmac
import json
import os
import re
import socket
try:
    import readline  # type: ignore
except ImportError:  # exotic platforms — chat() falls back to plain input()
    readline = None
try:  # command palette: raw-mode tty handling (absent on Windows)
    import select as _select
    import termios as _termios
except ImportError:
    _select = _termios = None
import traceback
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SSE_LOCK = threading.Lock()  # parallel tool workers share one SSE stream

# ---- original buddy.py lines 3410-3410 --------------------------------


# ---- original buddy.py lines 3411-3412 --------------------------------
def _session_path(sid: str) -> Path:
    return SESSIONS_DIR / (sid + ".json")


def _valid_session_id(sid: object) -> bool:
    """Web clients may supply an id: never let it escape SESSIONS_DIR."""
    return isinstance(sid, str) and bool(re.fullmatch(r"[0-9a-f]{12}", sid))

# ---- original buddy.py lines 3413-3413 --------------------------------


# ---- original buddy.py lines 3414-3427 --------------------------------
def session_load(sid: str) -> "dict | None":
    if not _valid_session_id(sid):
        return None
    with STATE_LOCK:
        if sid in SESSIONS:
            return SESSIONS[sid]
    p = _session_path(sid)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("id") != sid:
        return None
    with STATE_LOCK:
        SESSIONS[sid] = data
    return data

# ---- original buddy.py lines 3428-3428 --------------------------------


# ---- original buddy.py lines 3429-3440 --------------------------------
def session_save(sess: dict) -> None:
    sess["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with STATE_LOCK:
        SESSIONS[sess["id"]] = sess
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _session_path(sess["id"]).with_suffix(".tmp")
            tmp.write_text(json.dumps(sess, ensure_ascii=False))
            tmp.replace(_session_path(sess["id"]))  # atomic-ish write
        except OSError:
            pass
        _session_prune_locked()

# ---- original buddy.py lines 3441-3441 --------------------------------


# ---- original buddy.py lines 3442-3454 --------------------------------
def _session_prune_locked() -> None:
    """Keep only the MAX_SESSIONS newest session files on disk."""
    try:
        files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    keep = max(0, int(MAX_SESSIONS))
    if len(files) > keep:
        for stale in files[:-keep] if keep else files:
            try:
                stale.unlink()
                SESSIONS.pop(stale.stem, None)
            except OSError:
                pass

# ---- original buddy.py lines 3455-3455 --------------------------------


# ---- original buddy.py lines 3456-3470 --------------------------------
def session_ensure(sid: "str | None", first_message: str = "") -> dict:
    """Return an existing session or create one titled from the first message."""
    if sid:
        sess = session_load(sid)
        if sess:
            return sess
    sess = {
        "id": uuid.uuid4().hex[:12],
        "title": first_message.strip()[:60] or "new chat",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "messages": [],
    }
    session_save(sess)
    return sess

# ---- original buddy.py lines 3471-3471 --------------------------------


# ---- original buddy.py lines 3472-3489 --------------------------------
def session_list() -> list:
    with STATE_LOCK:
        try:
            files = sorted(SESSIONS_DIR.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
    # parse OUTSIDE the lock: a multi-MB session file must not stall every
    # other webui/scheduler operation that needs STATE_LOCK
    out = []
    for p in files[:MAX_SESSIONS]:
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "id": d.get("id"), "title": d.get("title", "(untitled)"),
            "updated": d.get("updated", ""), "count": len(d.get("messages", [])),
        })
    return out

# ---- original buddy.py lines 3490-3490 --------------------------------


# ---- original buddy.py lines 3491-3494 --------------------------------
def session_delete(sid: str) -> bool:
    """Delete a chat session: drop the cached copy and unlink the file."""
    if not _valid_session_id(sid):
        return False
    with STATE_LOCK:
        SESSIONS.pop(sid, None)
        try:
            p = _session_path(sid)
        except Exception:
            return False
    try:
        p.unlink()
        return True
    except OSError:
        return False

def cancel_event(session_id: str) -> threading.Event:
    """The agent runner polls the Event registered here for session_id."""
    with STATE_LOCK:
        return CANCELS.setdefault(session_id, threading.Event())

# ---- original buddy.py lines 3495-3495 --------------------------------


# ---- original buddy.py lines 3496-3505 --------------------------------
def cancel_session(session_id: "str | None") -> bool:
    """/api/stop: flag the live turn for session_id. True if one was running."""
    if not session_id:
        return False
    with STATE_LOCK:
        ev = CANCELS.get(session_id)
    if ev:
        ev.set()
        return True
    return False

# ---- original buddy.py lines 3506-3506 --------------------------------


# ---- original buddy.py lines 3507-3510 --------------------------------
def _cancel_cleanup(session_id: str, ev: threading.Event) -> None:
    with STATE_LOCK:
        if CANCELS.get(session_id) is ev:
            CANCELS.pop(session_id, None)


def _rollback_turn(sess: dict, my_turn: dict) -> None:
    """Drop THIS turn's failed user message (and its reply) — never a
    concurrent turn's. The old `del messages[n_user:]` used an index
    captured before the append, so a second tab/retry starting on the
    same session had its messages deleted by the first turn's failure.
    Caller holds STATE_LOCK."""
    msgs = sess.get("messages")
    if not isinstance(msgs, list):
        return
    try:
        i = next(k for k, m in enumerate(msgs) if m is my_turn)
    except StopIteration:
        return  # already trimmed (e.g. by a reload) — nothing to undo
    # Only tail-trim when nothing from another turn landed after ours;
    # otherwise remove just our own message and keep the rest intact.
    if all(m.get("role") != "user" for m in msgs[i + 1:]):
        del msgs[i:]
    else:
        del msgs[i]

# ---- original buddy.py lines 3511-3511 --------------------------------


# ---- original buddy.py lines 3512-3530 --------------------------------
def _inbox_entries() -> list:
    """Parse INBOX markdown into entries: the '## <ts> — <title>' heading
    blocks that deliver() writes, newest first."""
    with STATE_LOCK:
        text = INBOX.read_text() if INBOX.exists() else ""
    entries, cur_ts, cur_title, cur_body = [], "", "", []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur_ts:
                entries.append({"ts": cur_ts, "title": cur_title,
                                "text": "\n".join(cur_body).strip()})
            head = line[3:].strip()
            cur_ts, cur_title, cur_body = head, head.split(" — ", 1)[-1], []
        elif cur_ts:
            cur_body.append(line)
    if cur_ts:
        entries.append({"ts": cur_ts, "title": cur_title,
                        "text": "\n".join(cur_body).strip()})
    return list(reversed(entries))

# ---- original buddy.py lines 3531-3531 --------------------------------


# ---- original buddy.py lines 3532-3547 --------------------------------
def _inbox_dismiss(ts: str) -> None:
    """Rewrite INBOX without the entry whose heading line matches ts."""
    with STATE_LOCK:
        if not INBOX.exists():
            return
        blocks, cur = [], None
        for line in INBOX.read_text().splitlines(keepends=True):
            if line.startswith("## "):
                if cur is not None and cur[0] != ts:
                    blocks.append("".join(cur[1]))
                cur = (line[3:].strip(), [line])
            elif cur is not None:
                cur[1].append(line)
        if cur is not None and cur[0] != ts:
            blocks.append("".join(cur[1]))
        INBOX.write_text("".join(blocks))

# ---- original buddy.py lines 3548-3550 --------------------------------

# ================================================================== web UI =

# ---- original buddy.py lines 3551-4297 --------------------------------
PAGE = r"""<!doctype html>
<!-- buddy web UI v2 - standalone draft. Becomes the PAGE string in buddy.py.
     Plain string (no .format), safe for triple-quoted embedding. -->
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#100e0c">
<title>buddy</title>
<style>
:root{
  /* buddy "ember" — warm dark terminal palette (default web scheme) */
  --bg:#100e0c; --surface:#181512; --surface2:#201c18; --surface3:#3a332b;
  --border:#2b2620; --border2:#3a332b;
  --text:#e6dfd6; --muted:#a3968a; --faint:#6b6157;
  --accent:#e8a478; --accent2:#d4764a; --ok:#7fbf7f; --warn:#e0a44a; --err:#e06c5f;
  --accent-contrast:#171310; /* text on accent fills; JS recomputes per theme */
  --radius:10px;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 28px rgba(0,0,0,.35);
  --sat:env(safe-area-inset-top); --sab:env(safe-area-inset-bottom);
}
*{box-sizing:border-box; -webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{
  margin:0; background:var(--bg); color:var(--text);
  background-image:radial-gradient(1200px 500px at 50% -12%, color-mix(in srgb,var(--accent) 7%,transparent), transparent 70%);
  background-repeat:no-repeat;
  font-family:"Berkeley Mono","IBM Plex Mono",ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace;
  -webkit-font-smoothing:antialiased;
  display:flex; flex-direction:column;
  height:100vh; height:100dvh; overflow:hidden;
}
button{font-family:inherit}
::selection{background:color-mix(in srgb,var(--accent) 35%,transparent)}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:var(--surface3);border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

/* ---------------------------------------------------------- header */
header{
  position:sticky; top:0; z-index:20; flex:none;
  display:flex; align-items:center; gap:.7rem;
  padding:calc(.65rem + var(--sat)) 1rem .65rem;
  background:color-mix(in srgb,var(--bg) 72%,transparent);
  backdrop-filter:blur(16px) saturate(1.2); -webkit-backdrop-filter:blur(16px) saturate(1.2);
  border-bottom:1px solid var(--border);
}
.ava{
  width:38px;height:38px;border-radius:12px;flex:none;
  display:flex;align-items:center;justify-content:center;font-size:18px;
  background:linear-gradient(135deg, color-mix(in srgb,var(--accent) 85%,#000), var(--accent2));
  border:1px solid color-mix(in srgb,var(--accent) 45%,transparent);
  box-shadow:0 0 14px color-mix(in srgb,var(--accent) 25%,transparent);
}
.who{display:flex;flex-direction:column;min-width:0}
.who b{font-size:.95rem;line-height:1.25;letter-spacing:.02em}
.who span{font-size:.72rem;color:var(--muted);display:flex;align-items:center;gap:.35rem}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok);flex:none}
.dot.busy{background:var(--warn);box-shadow:0 0 8px var(--warn);animation:pulse 1.1s infinite}
@keyframes pulse{50%{opacity:.4}}
.chip-model{
  margin-left:auto; font-size:.72rem; color:var(--text); white-space:nowrap;
  background:var(--surface2); border:1px solid var(--border2);
  border-radius:99px; padding:.34rem .8rem;
  box-shadow:var(--shadow);
}
/* the model chip is a real <select> (model picker) — make it look like the
   old static chip, and keep the native dropdown usable */
select.chip-model{
  font-family:inherit; cursor:pointer; max-width:15rem;
  text-overflow:ellipsis; appearance:none; -webkit-appearance:none;
  padding-right:1.4rem;
  background-image:linear-gradient(45deg,transparent 50%,currentColor 50%),
                   linear-gradient(135deg,currentColor 50%,transparent 50%);
  background-position:calc(100% - .85rem) .9rem, calc(100% - .6rem) .9rem;
  background-size:.25rem .25rem, .25rem .25rem;
  background-repeat:no-repeat;
}
select.chip-model:hover{border-color:var(--accent); color:var(--accent)}
select.chip-model:disabled{opacity:.55; cursor:progress}
select.chip-model option{background:var(--surface); color:var(--text)}
select.chip-model optgroup{background:var(--surface); color:var(--muted);
  font-style:normal; font-size:.65rem}
.workbadge{
  font-size:.7rem; color:var(--accent); white-space:nowrap;
  background:color-mix(in srgb,var(--accent) 10%,var(--surface));
  border:1px solid color-mix(in srgb,var(--accent) 35%,transparent);
  border-radius:99px; padding:.3rem .8rem; margin-bottom:.3rem;
  animation:pulse 1.6s ease-in-out infinite;
}
.workbadge.off{animation:none; opacity:.6}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}
.chip.failed .spin{display:none}
.chip.failed .tick{display:inline; color:var(--err)}
.iconbtn{
  width:40px;height:40px;border-radius:12px;border:1px solid var(--border2);flex:none;
  background:var(--surface2); color:var(--muted); cursor:pointer;
  display:flex;align-items:center;justify-content:center; position:relative;
  transition:color .15s, border-color .15s, background .15s, transform .15s;
}
.iconbtn:hover{color:var(--accent);border-color:var(--accent);transform:translateY(-1px)}
.iconbtn .badge{
  position:absolute;top:-5px;right:-5px;min-width:17px;height:17px;border-radius:9px;
  background:var(--accent);color:var(--accent-contrast);
  font-size:.62rem;font-weight:700;display:flex;align-items:center;justify-content:center;
  padding:0 4px;
}

/* ------------------------------------------------------------- log */
#log{
  flex:1; overflow-y:auto; overscroll-behavior:contain;
  padding:1.4rem 2.5rem .6rem; width:100%; max-width:78rem; margin:0 auto;
}
/* part rows: a hairline bar along each part, flat content */
.row{display:flex;gap:.625rem;margin:.7rem 0;align-items:stretch;animation:rise .22s ease}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.row::before{content:'';flex:none;width:3px;border-radius:2px;
  background:linear-gradient(to bottom, var(--accent), color-mix(in srgb,var(--accent) 25%,transparent))}
.row.you{flex-direction:row;justify-content:flex-end}
.row .ava{display:none}
.msg{
  flex:1;min-width:0;box-sizing:border-box;
  padding:.65rem 1rem;border-radius:4px 14px 14px 14px;line-height:1.65;
  font-size:.9rem;max-width:56rem;word-wrap:break-word;overflow-wrap:break-word;
}
.msg.bud{background:var(--surface);border:1px solid var(--border)}
.you{justify-content:flex-end}
.you .msg{
  flex:0 1 auto;max-width:56rem;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  color:var(--accent-contrast);border-radius:14px 4px 14px 14px;border:none;
}
.you .msg a{color:var(--accent-contrast)}
.msg .meta{font-size:.75rem;color:var(--muted);margin-top:.35rem;user-select:none}
.msg p{margin:0 0 .55rem}
.msg p:last-child{margin-bottom:0}
.msg h1,.msg h2,.msg h3{margin:.6rem 0 .35rem;line-height:1.3;font-weight:600;color:var(--accent)}
.msg h1{font-size:1.1rem}.msg h2{font-size:1rem}.msg h3{font-size:.95rem}
.msg ul,.msg ol{margin:.3rem 0 .55rem;padding-left:1.3rem}
.msg li{margin:.15rem 0}
.msg code{
  font-family:inherit;font-size:.84em;
  background:var(--surface2);
  color:var(--accent);
  border:1px solid var(--border2);
  border-radius:.35rem;padding:.1em .4em;
}
.msg pre{
  background:color-mix(in srgb,var(--bg) 60%,var(--surface2));border:1px solid var(--border2);border-radius:10px;
  padding:.6rem .8rem;overflow-x:auto;margin:.5rem 0;
}
.msg pre code{background:none;border:none;padding:0;color:var(--text);font-size:.75rem;line-height:1.6}
.msg a{color:var(--accent)}
.msg blockquote{margin:.4rem 0;padding-left:.8rem;border-left:2px solid var(--accent);color:var(--muted)}
.msg hr{border:0;border-top:1px solid var(--border2);margin:.8rem 0}
.msg table{border-collapse:collapse;margin:.5rem 0;width:100%;font-size:.8rem}
.msg th,.msg td{border:1px solid var(--border2);padding:.35rem .6rem;text-align:left}
.msg th{background:var(--surface2);font-weight:600;color:var(--accent)}
.typing{display:flex;gap:4px;padding:.4rem .2rem}
.typing i{width:7px;height:7px;border-radius:50%;background:var(--accent);animation:blink 1.2s infinite}
.typing i:nth-child(2){animation-delay:.2s}
.typing i:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}
.sysmsg{
  text-align:center;font-size:.76rem;color:var(--muted);margin:.8rem 0;
  display:flex;align-items:center;gap:.8rem;
}
.sysmsg::before,.sysmsg::after{content:'';flex:1;height:1px;background:var(--border2)}

/* tool activity: tool titles — no box, just label + arg */
.tools{display:flex;flex-direction:column;gap:.35rem;margin-bottom:.5rem;max-width:56rem}
.chip{
  display:inline-flex;flex-wrap:wrap;align-items:center;gap:.5rem;align-self:flex-start;
  font-size:.875rem;color:var(--muted);
  background:none;border:none;border-radius:0;
  padding:0;
}
.chip .spin{
  width:11px;height:11px;border-radius:50%;flex:none;
  border:2px solid var(--border2);border-top-color:var(--accent);
  animation:spin .7s linear infinite;
}
@keyframes spin{to{transform:rotate(360deg)}}
.chip .tick{color:var(--ok);font-weight:700;display:none}
.chip.done{color:var(--muted)}
.chip.done .spin{display:none}
.chip.done .tick{display:inline}
.chip .secs{color:var(--muted);font-variant-numeric:tabular-nums}
.chip .cname{color:var(--accent);font-weight:500}
.chip.failed{color:var(--err)}
.chip.expandable{cursor:pointer}
.chip .caret{display:none;color:var(--muted);font-size:.7rem}
.chip .carg{color:var(--text);font-weight:500;word-break:break-all}
/* bash part: bordered box, dimmed $ command header with hairline,
   output body clamped to 10 lines until the chip is expanded */
.chip .cout{
  flex-basis:100%;margin:.4rem 0 0;max-width:56rem;width:100%;
  border:1px solid var(--border2);border-radius:10px;overflow:hidden;
  background:color-mix(in srgb,var(--bg) 55%,var(--surface2));
}
.chip .chead{
  border-bottom:1px solid var(--border);
  padding:.32rem .8rem;font-size:.75rem;color:var(--muted);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.chip .cbody{
  margin:0;padding:.5rem .8rem;
  font-size:.75rem;line-height:1.6;color:var(--text);
  white-space:pre-wrap;word-break:break-word;
  display:-webkit-box;-webkit-box-orient:vertical;
  -webkit-line-clamp:10;line-clamp:10;overflow:hidden;
}
.chip.open .cbody{display:block;-webkit-line-clamp:none;line-clamp:none;overflow:visible}
/* todo rows: ✓ green, • amber, pending faint */
.chip .tlist .titem{display:block}
.chip .tlist .t-done{color:color-mix(in srgb,var(--ok) 75%,var(--text))}
.chip .tlist .t-run{color:var(--amber,var(--warn,#e0a44a))}
.chip .tlist .t-wait{color:var(--muted)}
/* expandable detail: args as a dash/key/value grid, then the unified diff */
.chip .cdetail{flex-basis:100%;display:flex;flex-direction:column;gap:.5rem;margin-top:.4rem}
.chip .dgrid{display:inline-grid;grid-template-columns:max-content max-content minmax(0,1fr);gap:.25rem .375rem;align-items:baseline}
.chip .ddash{width:8px;height:2px;border-radius:1px;background:var(--accent)}
.chip .dkey{font-size:.72rem;color:var(--muted)}
.chip .dval{font-size:.75rem;color:var(--muted);word-break:break-word;padding-left:.125rem}
.chip .ddiff{
  margin:0;max-width:56rem;width:100%;font-size:.75rem;line-height:1.6;
  background:color-mix(in srgb,var(--bg) 60%,var(--surface2));border:1px solid var(--border2);border-radius:10px;
  padding:.5rem .8rem;overflow-x:auto;white-space:pre;color:var(--text);
}
.ddiff .dl{display:block}
.ddiff .dadd{background:color-mix(in srgb,var(--ok) 16%,transparent);color:color-mix(in srgb,var(--ok) 70%,var(--text))}
.ddiff .ddel{background:color-mix(in srgb,var(--err) 14%,transparent);color:color-mix(in srgb,var(--err) 75%,var(--text))}
.ddiff .dhunk{color:var(--muted)}
.ddiff .dfile{color:var(--accent);font-weight:500}

/* empty state: wordmark with an ember glow, caption, intro + suggestions */
#empty{max-width:27rem;margin:7vh auto 0;text-align:center;padding:0 1rem}
#empty .wordmark{
  font-family:inherit;font-size:12px;line-height:1.15;letter-spacing:0;
  margin:0 0 .9rem;user-select:none;
  text-shadow:0 0 24px color-mix(in srgb,var(--accent) 55%,transparent);
}
#empty .wordmark .w2{color:var(--accent)}
#empty .wordmark .w1{color:var(--faint)}
#empty .cap{
  color:var(--muted);font-size:.66rem;margin:0 0 1.5rem;
  text-transform:uppercase;letter-spacing:.22em;
}
#empty p{color:var(--muted);font-size:.84rem;margin:0 0 1.6rem;line-height:1.7}
#empty p b,#empty p strong{color:var(--text);font-weight:500}
#empty .sugrule{
  display:flex;align-items:center;gap:.6rem;margin:0 0 .8rem;
  color:var(--faint);font-size:.62rem;letter-spacing:.18em;text-transform:uppercase;
}
#empty .sugrule::before,#empty .sugrule::after{
  content:"";flex:1;height:1px;background:var(--border2);
}
#empty .tipline{
  color:var(--faint);font-size:.72rem;margin:1.1rem 0 0;line-height:1.6;
  letter-spacing:.02em;
}
#empty .tipline .tlabel{
  color:var(--accent);font-weight:600;font-size:.62rem;
  letter-spacing:.16em;margin-right:.35rem;
}
#empty h1{display:none}
.sugs{display:flex;flex-direction:column;gap:.5rem;text-align:left}
.sug{
  display:flex;align-items:baseline;gap:.7rem;
  text-align:left;font-size:.8rem;color:var(--text);cursor:pointer;
  background:var(--surface);border:1px solid var(--border2);border-radius:12px;
  padding:.68rem .85rem .68rem .75rem;
  transition:background .15s, border-color .15s, transform .15s, box-shadow .15s;
  box-shadow:var(--shadow);
}
.sug:hover{
  background:var(--surface2);border-color:var(--accent);
  transform:translateX(2px);
  box-shadow:inset 2px 0 0 var(--accent), var(--shadow);
}
.sug:active{transform:translateX(2px) scale(.99)}
.sug .n{
  color:var(--accent);font-size:.68rem;font-variant-numeric:tabular-nums;
  letter-spacing:.05em;flex:none;
}
.sug .t{flex:1;min-width:0}
.sug small{display:block;color:var(--faint);margin-top:.1rem;font-size:.68rem}
@keyframes rise{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}
#empty .cap,#empty>p,#empty .sugrule,#empty .sug,#empty .tipline{
  animation:rise .45s cubic-bezier(.2,.7,.3,1) both;
}
#empty .cap{animation-delay:.05s}
#empty>p{animation-delay:.12s}
#empty .sugrule{animation-delay:.2s}
#empty .sug:nth-child(1){animation-delay:.26s}
#empty .sug:nth-child(2){animation-delay:.34s}
#empty .sug:nth-child(3){animation-delay:.42s}
#empty .tipline{animation-delay:.52s}
@media (prefers-reduced-motion:reduce){
  #empty .cap,#empty>p,#empty .sugrule,#empty .sug,#empty .tipline{animation:none}
  .sug{transition:none}
}

/* --------------------------------------------------------- composer */
footer{
  position:sticky;bottom:0;z-index:20;flex:none;
  padding:.7rem 1rem calc(.7rem + var(--sab));
  background:linear-gradient(to top,var(--bg) 70%,transparent);
}
form{display:flex;gap:.55rem;max-width:78rem;margin:0 auto;width:100%;align-items:flex-end}
#i{
  flex:1;resize:none;font-size:16px; /* 16px kills iOS focus-zoom */
  font-family:inherit;line-height:1.4;color:var(--text);
  background:var(--surface);border:1px solid var(--border2);
  border-radius:14px;
  padding:.78rem .95rem;outline:none;max-height:120px;min-height:46px;
  box-shadow:var(--shadow);
  transition:border-color .15s, box-shadow .15s;
}
#i:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent), var(--shadow)}
#i::placeholder{color:var(--faint)}
#send{
  width:46px;height:46px;border-radius:14px;border:0;cursor:pointer;flex:none;
  color:var(--accent-contrast);background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 0 16px color-mix(in srgb,var(--accent) 30%,transparent);
  transition:transform .1s, opacity .15s, box-shadow .15s;
}
#send:hover{opacity:.92}
#send:active{transform:scale(.96)}
#send.stop{
  background:var(--err);color:#fff;
  animation:pulse 1.1s infinite;box-shadow:none;
}
#send svg{pointer-events:none}
/* status bar: the TUI's dim bottom line — model left, version right */
.statusbar{
  display:flex;justify-content:space-between;max-width:78rem;margin:.5rem auto 0;
  padding:0 2.5rem;
  font-size:.68rem;color:var(--faint);user-select:none;
}

/* ---------------------------------------------------------- drawers */
.overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:40;
  opacity:0;pointer-events:none;transition:opacity .2s;backdrop-filter:blur(3px);
}
.overlay.open{opacity:1;pointer-events:auto}
.drawer{
  position:fixed;top:0;right:0;bottom:0;z-index:50;width:min(22rem,100%);
  background:var(--surface);border-left:1px solid var(--border2);
  transform:translateX(105%);transition:transform .25s cubic-bezier(.3,.9,.3,1);
  display:flex;flex-direction:column;
  padding-top:var(--sat);padding-bottom:var(--sab);
}
.drawer.open{transform:none}
.drawer header{
  position:static;background:none;border-bottom:1px solid var(--border2);
  padding:.8rem 1rem;backdrop-filter:none;
}
.drawer header b{font-size:.95rem}
.drawer .x{margin-left:auto}
.drawer .body{flex:1;overflow-y:auto;padding:.8rem}
.item{
  border:1px solid var(--border2);border-radius:12px;background:var(--surface2);
  padding:.68rem .85rem;margin-bottom:.5rem;cursor:pointer;
  transition:border-color .15s;
}
.item:hover{border-color:var(--muted)}
.item.active{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,var(--surface2))}
.item .t{font-size:.82rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item{position:relative}
.sess-del{
  position:absolute;top:.45rem;right:.45rem;width:30px;height:30px;
  border:0;border-radius:8px;background:none;color:var(--faint);cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:color .15s, background .15s;
}
.sess-del:hover{color:var(--err);background:color-mix(in srgb,var(--err) 12%,transparent)}
.sess-del svg{pointer-events:none}
.item .s{font-size:.7rem;color:var(--muted);margin-top:.2rem;display:flex;gap:.6rem}
.primary{
  width:100%;border:0;border-radius:12px;cursor:pointer;color:var(--accent-contrast);
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  padding:.75rem;font-size:.85rem;font-weight:700;margin-bottom:.8rem;
}
.primary:hover{opacity:.92}
.card-head{display:flex;align-items:center;gap:.5rem}
.card-head .t{flex:1;min-width:0}
.dismiss{
  flex:none;border:0;background:none;color:var(--faint);cursor:pointer;
  font-size:1rem;width:32px;height:32px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;
}
.dismiss:hover{color:var(--err);background:color-mix(in srgb,var(--err) 12%,transparent)}
.inbox-body{font-size:.83rem;color:var(--muted);line-height:1.5;margin-top:.4rem;white-space:pre-wrap;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
.item.open .inbox-body{display:block;-webkit-line-clamp:unset}
.caret{font-size:.65rem;color:var(--faint);transition:transform .15s}
.item.open .caret{transform:rotate(90deg)}
.empty-note{color:var(--faint);font-size:.82rem;text-align:center;padding:2rem 1rem}
.theme-item{display:flex;align-items:center;gap:.7rem;width:100%;text-align:left;
  font-size:.9rem;color:var(--text);
  border:1px solid transparent;border-radius:10px;padding:.4rem .5rem}
.theme-item.on{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,var(--surface2))}
.theme-item .swatch{width:20px;height:20px;border-radius:6px;flex:none;
  border:1px solid var(--border2)}

@media(max-width:560px){
  .msg{max-width:88%}
  .chip-model{display:none}
  #log{padding:1rem .7rem .4rem}
}
@media(min-width:561px) and (max-width:1100px){
  #log{padding:1.4rem 1.2rem .6rem}
}
</style>
</head>
<body>

<header>
  <div class="ava">🐶</div>
  <div class="who"><b>buddy</b><span><i class="dot" id="dot"></i><span id="status">online</span></span></div>
  <select class="chip-model" id="model" title="switch model" aria-label="switch model">
    <option>…</option>
  </select>
  <button class="iconbtn" id="btn-theme" title="theme" aria-label="theme">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.688-1.688h1.093c3.573 0 6.446-2.915 6.446-6.49 0-3.941-3.583-7.032-8.006-7.032z"/></svg>
  </button>
  <button class="iconbtn" id="btn-inbox" title="inbox" aria-label="inbox">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
    <span class="badge" id="inbox-badge" style="display:none">0</span>
  </button>
  <button class="iconbtn" id="btn-sessions" title="chats" aria-label="chats">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
  </button>
</header>

<main id="log">
  <div id="empty">
<pre class="wordmark"><span class="w1">██████╗  </span><span class="w2">██╗   ██╗ ██████╗  ██████╗  ██╗   ██╗</span>
<span class="w1">██╔══██╗ </span><span class="w2">██║   ██║ ██╔══██╗ ██╔══██╗ ╚██╗ ██╔╝</span>
<span class="w1">██████╔╝ </span><span class="w2">██║   ██║ ██║  ██║ ██║  ██║  ╚████╔╝</span>
<span class="w1">██╔══██╗ </span><span class="w2">██║   ██║ ██║  ██║ ██║  ██║   ╚██╔╝</span>
<span class="w1">██████╔╝ </span><span class="w2">╚██████╔╝ ██████╔╝ ██████╔╝    ██║</span>
<span class="w1">╚═════╝  </span><span class="w2"> ╚═════╝  ╚═════╝  ╚═════╝     ╚═╝</span></pre>
    <p class="cap">buddy v3 · local · yours</p>
    <p>hey, I'm <b>buddy</b> — I live on this machine.<br>ask me anything, give me a job, or just say hi.</p>
    <div class="sugrule">try</div>
    <div class="sugs" id="sugs"></div>
    <p class="tipline"><span class="tlabel">TIP</span> <span id="tip"></span></p>
  </div>
</main>

<footer><form id="f" autocomplete="off">
  <label class="iconbtn" title="attach image" aria-label="attach image" style="padding:0;display:flex;align-items:center;justify-content:center;cursor:pointer;flex:none;margin-right:0px;margin-bottom:8px;">
    <input type="file" id="f-img" accept="image/*" style="display:none">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
  </label>
  <div id="img-preview" style="display:none;position:absolute;bottom:100%;left:3rem;margin-bottom:.5rem;padding:.5rem;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);z-index:30;">
    <img id="img-preview-src" style="max-height:100px;border-radius:8px;">
    <button type="button" id="img-remove" class="iconbtn x" style="position:absolute;top:-8px;right:-8px;background:var(--bg);border:1px solid var(--border);">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <textarea id="i" rows="1" placeholder="talk to buddy…"></textarea>
  <button id="send" type="submit" title="send" aria-label="send">
    <svg id="ic-send" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
    <svg id="ic-stop" width="15" height="15" viewBox="0 0 24 24" fill="currentColor" style="display:none"><rect x="5" y="5" width="14" height="14" rx="2.5"/></svg>
  </button>
</form>
<div class="statusbar"><span id="sb-left">chat</span><span>buddy v3</span></div>
</footer>

<div class="overlay" id="overlay"></div>

<aside class="drawer" id="drawer-sessions" aria-label="chats">
  <header><b>Chats</b>
    <button class="iconbtn x" data-close aria-label="close">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>
  </header>
  <div class="body">
    <button class="primary" id="btn-new">＋ New chat</button>
    <div id="session-list"><div class="empty-note">loading…</div></div>
  </div>
</aside>

<aside class="drawer" id="drawer-theme" aria-label="theme">
  <header><b>Theme</b>
    <span id="theme-current" style="margin-left:.5rem;color:var(--muted);font-size:.75rem"></span>
    <button class="iconbtn x" data-close aria-label="close">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>
  </header>
  <div class="body" id="theme-list"></div>
</aside>

<aside class="drawer" id="drawer-inbox" aria-label="inbox">
  <header><b>Inbox</b>
    <button class="iconbtn x" data-close aria-label="close">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>
  </header>
  <div class="body" id="inbox-list"><div class="empty-note">loading…</div></div>
</aside>

<script>
'use strict';
/* ============================== themes (OpenCode-style) ============== */
var THEME_MAP = {
  bg: 'background', surface: 'backgroundPanel', surface2: 'backgroundElement',
  surface3: 'border', border: 'borderSubtle', border2: 'border',
  text: 'text', muted: 'textMuted', faint: 'border',
  accent: 'primary', accent2: 'secondary', ok: 'success',
  warn: 'warning', err: 'error'
};
var ALL_THEMES = {};
function luminance(hex){
  var m = /^#?([0-9a-fA-F]{6})$/.exec(hex || '');
  if (!m) return 0;
  var n = parseInt(m[1], 16);
  var lum = function(c){ c /= 255; return c <= .03928 ? c / 12.92 : Math.pow((c + .055) / 1.055, 2.4); };
  return .2126 * lum(n >> 16 & 255) + .7152 * lum(n >> 8 & 255) + .0722 * lum(n & 255);
}
function applyTheme(t){
  if (!t || !t.primary) return;
  var root = document.documentElement.style;
  for (var cssVar in THEME_MAP){
    var v = t[THEME_MAP[cssVar]];
    if (v) root.setProperty('--' + cssVar, v);
  }
  /* on-accent text: dark ink on light accents, near-white on dark ones */
  root.setProperty('--accent-contrast', luminance(t.primary) > .4 ? '#171310' : '#ffffff');
  var meta = document.querySelector('meta[name=theme-color]');
  if (meta && t.background) meta.setAttribute('content', t.background);
}
function setTheme(name, persist){
  var t = ALL_THEMES[name];
  if (!t) return;
  applyTheme(t);
  try { localStorage.setItem('buddy_theme', name); } catch(e){}
  var label = $('theme-current');
  if (label) label.textContent = name;
  var sb = $('sb-left');
  if (sb) sb.textContent = sb.textContent.replace(/ · [^·]*$/, ' · ' + name);
  document.querySelectorAll('#theme-list button').forEach(function(b){
    b.classList.toggle('on', b.dataset.name === name);
  });
}
function loadThemes(){
  fetch(api('/api/themes'), { headers: ah() }).then(function(r){ return r.json(); }).then(function(j){
    ALL_THEMES = j.themes || {};
    var saved = null;
    try { saved = localStorage.getItem('buddy_theme'); } catch(e){}
      setTheme(saved || 'ocweb');   /* web default: opencode.ai light */
  }).catch(function(){ /* default :root palette already matches */ });
}

function postJSON(p, body){
  return fetch(api(p), {
    method: 'POST',
    headers: ah({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body || {})
  }).then(function(r){ return r.json(); });
}

function toast(msg){
  try { addSys(msg); } catch(e){ console.log(msg); }
}

/* ============================== plumbing ============================== */
var TOKEN = location.hash.slice(1);           /* buddy auth: token in URL hash (never sent as query) */
function api(p){ return p; }
function ah(extra){ var h = { 'X-Buddy-Token': TOKEN }; if (extra) for (var k in extra) h[k] = extra[k]; return h; }
function $(id){ return document.getElementById(id); }
var log = $('log');

function now(){
  return new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
function scrollDown(){ requestAnimationFrame(function(){ log.scrollTop = log.scrollHeight; }); }

/* ======================= markdown (escape-first) ====================== */
function escapeHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function renderInline(s){
  s = s.replace(/`([^`\n]+)`/g, function(m,c){ return '<code>' + c + '</code>'; });
  s = s.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
/* input has ALREADY been escaped - we only add tags */
function mdBlock(chunk){
  var lines = chunk.split('\n'), out = [], list = null; /* 'ul' | 'ol' | null */
  function closeList(){ if(list){ out.push('</' + list + '>'); list = null; } }
  function isPipeRow(l){ var s = l.trim(); return s.indexOf('|') === 0 && s.lastIndexOf('|') === s.length - 1 && s.split('|').length >= 3; }
  function isSepRow(l){ return isPipeRow(l) && /^[\s|:\-]+$/.test(l.trim()); }
  for (var i = 0; i < lines.length; i++){
    var ln = lines[i];
    var h = ln.match(/^(#{1,3})\s+(.*)$/);
    var ul = ln.match(/^\s*[-*]\s+(.*)$/);
    var ol = ln.match(/^\s*\d+[.)]\s+(.*)$/);
    var bq = ln.match(/^>\s*(.*)$/);
    var hr = ln.match(/^\s*[-*_]{3,}\s*$/);
    if (h){ closeList(); out.push('<h' + h[1].length + '>' + renderInline(h[2]) + '</h' + h[1].length + '>'); }
    else if (hr){ closeList(); out.push('<hr>'); }
    else if (bq){ closeList(); out.push('<blockquote>' + renderInline(bq[1]) + '</blockquote>'); }
    else if (isPipeRow(ln) && !isSepRow(ln)){
      closeList();
      var tableHtml = '<table>';
      var isHeader = (i + 1 < lines.length && isSepRow(lines[i + 1]));
      var cells = ln.trim().slice(1, -1).split('|');
      var tag = isHeader ? 'th' : 'td';
      tableHtml += '<tr>' + cells.map(function(c){ return '<' + tag + '>' + renderInline(c.trim()) + '</' + tag + '>'; }).join('') + '</tr>';
      if (isHeader) i++;
      while (i + 1 < lines.length && isPipeRow(lines[i + 1]) && !isSepRow(lines[i + 1])){
        i++;
        var rcells = lines[i].trim().slice(1, -1).split('|');
        tableHtml += '<tr>' + rcells.map(function(c){ return '<td>' + renderInline(c.trim()) + '</td>'; }).join('') + '</tr>';
      }
      tableHtml += '</table>';
      out.push(tableHtml);
    }
    else if (ul){
      if (list !== 'ul'){ closeList(); out.push('<ul>'); list = 'ul'; }
      out.push('<li>' + renderInline(ul[1]) + '</li>');
    } else if (ol){
      if (list !== 'ol'){ closeList(); out.push('<ol>'); list = 'ol'; }
      out.push('<li>' + renderInline(ol[1]) + '</li>');
    } else if (ln.trim() === ''){ closeList(); }
    else { closeList(); out.push('<p>' + renderInline(ln) + '</p>'); }
  }
  closeList();
  return out.join('');
}
function markdown(text){
  var src = escapeHtml(text);
  var parts = src.split('```');
  var html = '';
  for (var i = 0; i < parts.length; i++){
    if (i % 2 === 1){ /* fenced block (odd index) */
      var body = parts[i].replace(/^[a-zA-Z0-9_+#.-]*\n/, ''); /* drop lang line */
      html += '<pre><code>' + body.replace(/^\n/, '') + '</code></pre>';
    } else {
      html += mdBlock(parts[i]);
    }
  }
  return html;
}

/* =========================== chat rendering =========================== */
function addRow(cls, emoji){
  var r = document.createElement('div'); r.className = 'row ' + cls;
  var a = document.createElement('div'); a.className = 'ava'; a.textContent = emoji;
  var wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;flex-direction:column;max-width:100%;min-width:0';
  r.appendChild(a); r.appendChild(wrap); log.appendChild(r);
  return { row: r, wrap: wrap };
}
function addMsg(text, cls){
  var v = addRow(cls, cls === 'you' ? '👤' : '🐶');
  var d = document.createElement('div'); d.className = 'msg ' + cls;
  d.innerHTML = cls === 'you' ? escapeHtml(text) : markdown(text);
  var m = document.createElement('div'); m.className = 'meta'; m.textContent = now();
  d.appendChild(m);
  v.wrap.appendChild(d); scrollDown();
  return d;
}
function addSys(text){
  var d = document.createElement('div'); d.className = 'sysmsg'; d.textContent = text;
  log.appendChild(d); scrollDown();
}
function hideEmpty(){ var e = $('empty'); if (e) e.remove(); }

/* typing dots + streaming bubble */
var live = null; /* {wrap, msg, tools, text} while a turn runs */
function beginTurn(){
  hideEmpty();
  live = addRow('bud', '🐶');
  var tools = document.createElement('div'); tools.className = 'tools'; tools.style.display = 'none';
  var msg = document.createElement('div'); msg.className = 'msg bud';
  msg.innerHTML = '<div class="typing"><i></i><i></i><i></i></div>';
  live.wrap.appendChild(tools); live.wrap.appendChild(msg);
  live.msg = msg; live.tools = tools; live.text = ''; live.steps = 0;
  var badge = document.createElement('div'); badge.className = 'workbadge';
  badge.textContent = 'Working · 0 steps';
  live.wrap.insertBefore(badge, tools);
  live.badge = badge;
  scrollDown();
}
function chipFor(name){
  var c = document.createElement('div'); c.className = 'chip';
  c.innerHTML = '<span class="spin"></span><span class="tick">✓</span>' +
    '<span class="cname">🔧 ' + escapeHtml(name) + '</span>' +
    '<span class="carg"></span><span class="caret">▾</span><span class="secs"></span>' +
    '<pre class="cout" style="display:none"></pre>' +
    '<div class="cdetail" style="display:none"><div class="dgrid"></div><pre class="ddiff" style="display:none"></pre></div>';
  c.addEventListener('click', function(){
    if (!c.classList.contains('expandable')) return;
    var open = c.classList.toggle('open');
    c.querySelector('.cdetail').style.display = open ? '' : 'none';
  });
  live.tools.appendChild(c);
  live.tools.style.display = '';
  scrollDown();
  return c;
}
function colorizeDiff(diff){
  return diff.split('\n').map(function(ln){
    var esc = escapeHtml(ln);
    var cls = 'dl';
    if (ln.indexOf('---') === 0 || ln.indexOf('+++') === 0) cls = 'dl dfile';
    else if (ln.indexOf('@@') === 0) cls = 'dl dhunk';
    else if (ln.charAt(0) === '+') cls = 'dl dadd';
    else if (ln.charAt(0) === '-') cls = 'dl ddel';
    return '<span class="' + cls + '">' + (esc || ' ') + '</span>';
  }).join('\n');
}
function argsSummary(args){
  for (var k in (args || {})){
    var v = args[k];
    if (typeof v === 'string' && v.trim()){
      return v.replace(/\s+/g, ' ').trim().slice(0, 60);
    }
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  }
  return '';
}
function onToolEvent(j){
  if (!live) return;
  var name = j.name || 'tool';
  var chip = null;
  if (j.id){
    var known = live.tools.querySelectorAll('.chip');
    for (var i = 0; i < known.length; i++){
      if (known[i].dataset.tcid === j.id){ chip = known[i]; break; }
    }
  }
  if (!chip){
    chip = chipFor(name);
    if (j.id) chip.dataset.tcid = j.id;
  }
  live.steps = (live.steps || 0) + 1;
  if (live.badge) live.badge.textContent = 'Working · ' + live.steps +
    (live.steps === 1 ? ' step' : ' steps');
  /* args arrive on the start event — that IS "what it's doing" */
  if (j.args && !chip.dataset.arged){
    chip.dataset.arged = '1';
    var sum = argsSummary(j.args);
    if (sum) chip.querySelector('.carg').textContent = sum;
    var grid = chip.querySelector('.dgrid');
    Object.keys(j.args).forEach(function(k){
      var row = document.createElement('div'); row.className = 'drow';
      var dash = document.createElement('div'); dash.className = 'ddash';
      var key = document.createElement('div'); key.className = 'dkey'; key.textContent = k;
      var val = document.createElement('div'); val.className = 'dval'; val.textContent = String(j.args[k]);
      row.appendChild(dash); row.appendChild(key); row.appendChild(val);
      grid.appendChild(row);
    });
  }
  /* opencode-style live output: progress events stream the command's tail */
  if (j.status === 'progress' && j.tail != null){
    var co = chip.querySelector('.cout');
    co.style.display = '';
    if (!co.querySelector('.chead')){
      var cmdv = (j.args && j.args.command) || '';
      var hd = document.createElement('div'); hd.className = 'chead';
      hd.textContent = cmdv ? '$ ' + cmdv : '$ …';
      var bd = document.createElement('pre'); bd.className = 'cbody';
      co.appendChild(hd); co.appendChild(bd);
      chip._coutBody = bd;
      chip.classList.add('expandable');
      chip.querySelector('.caret').style.display = '';
    }
    if (chip._coutBody) chip._coutBody.textContent = j.tail;
    scrollDown();
    return;  /* progress never settles the chip */
  }
  if (j.status === 'ok' || j.status === 'done'){
    chip.classList.add('done');
    if (j.secs != null) chip.querySelector('.secs').textContent = '· ' + Number(j.secs).toFixed(1) + 's';
  } else if (j.status === 'error'){
    chip.classList.add('failed');
    if (j.secs != null) chip.querySelector('.secs').textContent = '· ' + Number(j.secs).toFixed(1) + 's';
  }
  /* opencode todo panel: ← Todos header + checkbox rows */
  if (j.name === 'todo_write' && j.args && j.args.todos){
    var tco = chip.querySelector('.cout');
    tco.style.display = '';
    if (!tco.querySelector('.chead')){
      var thd = document.createElement('div'); thd.className = 'chead';
      thd.textContent = '← Todos';
      tco.appendChild(thd);
      var tbd = document.createElement('pre'); tbd.className = 'cbody tlist';
      tco.appendChild(tbd);
      tco._tlist = tbd;
    }
    if (tco._tlist){
      tco._tlist.innerHTML = j.args.todos.map(function(t){
        var c = String(t.content || '');
        var s = t.status === 'completed' ? 't-done' :
                (t.status === 'in_progress' ? 't-run' : 't-wait');
        return '<span class="titem ' + s + '">[' +
          (t.status === 'completed' ? '✓' : (t.status === 'in_progress' ? '•' : ' ')) +
          '] ' + escapeHtml(c) + '</span>';
      }).join('');
    }
  }
  /* opencode-style diff: attach on completion, reveal the caret */
  if (j.diff){
    chip.querySelector('.ddiff').innerHTML = colorizeDiff(j.diff);
    chip.querySelector('.ddiff').style.display = '';
    /* opencode shows the edit diff inline under a ← title — mirror it */
    var eco = chip.querySelector('.cout');
    eco.style.display = '';
    if (!eco.querySelector('.chead')){
      var pp = (j.args && j.args.path) || '';
      var ehd = document.createElement('div'); ehd.className = 'chead';
      ehd.textContent = (j.status === 'error' ? '× ' : '← ') +
        (pp ? pp.split('/').pop() : 'file');
      var ebd = document.createElement('pre'); ebd.className = 'cbody';
      eco.appendChild(ehd); eco.appendChild(ebd);
    }
    eco.querySelector('.cbody').innerHTML = colorizeDiff(j.diff);
  }
  /* opencode read part: "→ Read file" title + code preview (result text) */
  if (j.name === 'read_file' && j.result != null){
    var rco = chip.querySelector('.cout');
    rco.style.display = '';
    if (!rco.querySelector('.chead')){
      var rp = (j.args && j.args.path) || '';
      var rhd = document.createElement('div'); rhd.className = 'chead';
      rhd.textContent = (j.status === 'error' ? '× ' : '→ Read ') +
        (rp ? rp.split('/').pop() : 'file');
      var rbd = document.createElement('pre'); rbd.className = 'cbody';
      rco.appendChild(rhd); rco.appendChild(rbd);
    }
    rco.querySelector('.cbody').textContent = j.result;
  }
  /* opencode grep part: "✱ Grep "pattern" (N matches)" + match rows */
  if (j.name === 'grep' && j.result != null){
    var gco = chip.querySelector('.cout');
    gco.style.display = '';
    if (!gco.querySelector('.chead')){
      var gp = (j.args && j.args.pattern) || '';
      var ghd = document.createElement('div'); ghd.className = 'chead';
      var mm = j.result.match(/^(\d+) match/);
      ghd.textContent = (j.status === 'error' ? '× ' : '✱ Grep "') + gp + '"' +
        (mm ? ' (' + mm[1] + (mm[1] === '1' ? ' match' : ' matches') + ')' : '');
      var gbd = document.createElement('pre'); gbd.className = 'cbody';
      gco.appendChild(ghd); gco.appendChild(gbd);
    }
    var lines = j.result.split('\n');
    if (lines.length && /^(\d+) match/.test(lines[0])) lines = lines.slice(1);
    gco.querySelector('.cbody').textContent = lines.join('\n');
  }
  if (j.diff || (j.args && Object.keys(j.args).length)){
    chip.classList.add('expandable');
    chip.querySelector('.caret').style.display = '';
  } else {
    chip.querySelector('.caret').style.display = 'none';
  }
}
var paintQueued = false;
function paintStream(){
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(function(){
    paintQueued = false;
    if (!live) return;
    var m = live.msg.querySelector('.meta');
    live.msg.innerHTML = markdown(live.text || '…');
    if (m) live.msg.appendChild(m);
    scrollDown();
  });
}
function onDelta(j){
  if (!live) return;
  if (!live.text && live.msg.querySelector('.typing')) live.msg.innerHTML = '';
  live.text += (j.text || '');
  var m = live.msg.querySelector('.meta');
  if (!m){ m = document.createElement('div'); m.className = 'meta'; live.msg.appendChild(m); }
  paintStream();
}
function endTurn(finalText){
  if (!live) return;
  if (finalText != null && finalText !== '') live.text = finalText;
  var m = document.createElement('div'); m.className = 'meta'; m.textContent = now();
  live.msg.innerHTML = live.text ? markdown(live.text) : '<em style="color:var(--faint)">(no reply)</em>';
  live.msg.appendChild(m);
  live.tools.querySelectorAll('.chip').forEach(function(c){ c.classList.add('done'); });
  if (live.badge){
    live.badge.classList.add('off');
    live.badge.textContent = 'Done · ' + live.steps + ' steps';
    var doneBadge = live.badge;
    setTimeout(function(){ if (doneBadge) doneBadge.remove(); }, 1500);
  }
  live = null;
  scrollDown();
}

/* ============================ empty state ============================= */
/* random suggestions + tip on every page load */
var SUGGESTIONS = [
  ["What's new today?", "web search · current events"],
  ["Clean up my Downloads folder", "file tools · asks before deleting"],
  ["Remind me to stretch every hour", "scheduler · lands in your inbox"],
  ["What's eating my disk space?", "file tools · system report"],
  ["Summarize this week's git log", "read files · run commands"],
  ["Watch my build log and ping me when it finishes", "watchers · inbox"],
  ["Learn this machine — what have you got?", "probe system · capabilities"],
  ["Backup ~/.buddy to /backups with a timestamp", "file tools · shell"],
  ["Check the weather in Accra", "web search · current events"],
  ["Every morning at 8, brief me on my inbox", "scheduler · recurring jobs"],
  ["Find all TODO comments in buddy and list them", "file tools · grep"],
  ["Translate 'good morning' into 5 languages", "plain chat"],
  ["What processes are hogging CPU right now?", "shell · system report"],
  ["Draft a changelog from recent commits", "git · writing"],
  ["Tell me a joke so dry the desert complains", "plain chat · humor"],
];
var TIPS = [
  "jobs keep running here even while you're away — results land in your inbox",
  "I show a diff for every file I write — click a tool row to expand it",
  "subagents work in parallel — ask and I'll fan a job out",
  "ask me to remember something and it sticks",
  "the /theme command recolors the terminal; this drawer recolors the web",
  "dangerous shell needs your confirmation — I ask, you decide",
];
(function(){
  var host = document.getElementById('sugs');
  if (!host) return;
  var pool = SUGGESTIONS.slice();
  for (var i = pool.length - 1; i > 0; i--){
    var j = Math.floor(Math.random() * (i + 1));
    var t = pool[i]; pool[i] = pool[j]; pool[j] = t;
  }
  pool.slice(0, 3).forEach(function(s, i){
    var b = document.createElement('button');
    b.className = 'sug';
    b.innerHTML = '<span class="n">' + ('0' + (i + 1)).slice(-2) + '</span>' +
      '<span class="t">' + escapeHtml(s[0]) + '<small>' + escapeHtml(s[1]) + '</small></span>';
    b.onclick = function(){ ta.value = s[0]; ta.focus(); };
    host.appendChild(b);
  });
  var tip = document.getElementById('tip');
  if (tip) tip.textContent = TIPS[Math.floor(Math.random() * TIPS.length)];
})();

/* ============================ SSE streaming =========================== */
/* POST /api/chat, parse event:/data: frames from a ReadableStream (NOT EventSource) */
function streamChat(payload, handlers){
  return fetch(api('/api/chat'), {
    method:'POST',
    headers: ah({ 'Content-Type':'application/json' }),
    body: JSON.stringify(payload)
  }).then(function(res){
    if (!res.ok) throw new Error('HTTP ' + res.status);
    if (!res.body) throw new Error('no stream body');
    var reader = res.body.getReader();
    var dec = new TextDecoder();
    var buf = '';
    function pump(){
      return reader.read().then(function(chunk){
        if (chunk.done) return;
        buf += dec.decode(chunk.value, { stream:true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0){
          var frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var ev = '', data = '', lines = frame.split('\n'), i;
          for (i = 0; i < lines.length; i++){
            if (lines[i].indexOf('event:') === 0) ev = lines[i].slice(6).trim();
            else if (lines[i].indexOf('data:') === 0) data += lines[i].slice(5).trim();
          }
          if (!ev || !data) continue;
          var j; try { j = JSON.parse(data); } catch(e){ j = { raw:data }; }
          if (handlers[ev]) handlers[ev](j);
        }
        return pump();
      });
    }
    return pump();
  });
}

/* ============================== sessions ============================== */
var SESSION_KEY = 'buddy_session_id';
var sessionId = localStorage.getItem(SESSION_KEY) || '';
var running = false;
var abortCtl = null;

/* image attach wiring: the elements exist in the HTML but were never
   connected — send() referenced these variables undeclared, which under
   'use strict' threw a ReferenceError on every message send */
var currentImage = null;
var imgPreview = $('img-preview');
var imgPreviewSrc = $('img-preview-src');
var imgRemove = $('img-remove');
var fImg = $('f-img');
if (fImg) fImg.addEventListener('change', function(){
  var file = fImg.files && fImg.files[0];
  if (!file) return;
  if (file.size > 2500000) {
    addSys('image too large: 2.5MB max — shrink it first');
    fImg.value = '';
    return;
  }
  var rd = new FileReader();
  rd.onload = function(){
    currentImage = rd.result;                 /* data: URL — matches image_url.url */
    if (imgPreviewSrc) imgPreviewSrc.src = rd.result;
    if (imgPreview) imgPreview.style.display = 'block';
  };
  rd.readAsDataURL(file);
});
if (imgRemove) imgRemove.onclick = function(){
  currentImage = null;
  if (imgPreview) imgPreview.style.display = 'none';
  if (fImg) fImg.value = '';
};

function setRunning(on){
  running = on;
  var b = $('send');
  b.classList.toggle('stop', on);
  b.type = on ? 'button' : 'submit';
  b.title = on ? 'stop' : 'send';
  $('ic-send').style.display = on ? 'none' : '';
  $('ic-stop').style.display = on ? '' : 'none';
  $('dot').classList.toggle('busy', on);
  $('status').textContent = on ? 'thinking…' : 'online';
}

function chatPayload(msg){
  return { message: msg, session_id: sessionId || undefined, stream: STREAM, image: currentImage };
}

function send(v){
  addMsg(v + (currentImage ? " [Image Attached]" : ""), 'you');
  beginTurn();
  setRunning(true);
  abortCtl = new AbortController();
  var p = chatPayload(v);
  currentImage = null;
  if(imgPreview) imgPreview.style.display = 'none';
  if(fImg) fImg.value = '';
  var opts = { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(p) };
  if (abortCtl) opts.signal = abortCtl.signal;

  var finish = function(reply){
    endTurn(reply);
    setRunning(false);
    abortCtl = null;
    inboxCount(); /* a turn may have produced inbox items */
  };
  var fail = function(e){
    if (e && e.name === 'AbortError'){ endTurn(live ? live.text : ''); }
    else { endTurn(''); addSys('connection error — is the daemon up?'); }
    setRunning(false);
    abortCtl = null;
  };

  if (!STREAM){
    fetch(api('/api/chat'), (opts.headers = ah(opts.headers), opts))
      .then(function(r){ return r.json(); })
      .then(function(j){
        if (j.session_id){
          sessionId = j.session_id;
          localStorage.setItem(SESSION_KEY, sessionId);
        }
        finish(j.reply || (j.error ? '(error) ' + j.error : ''));
      })
      .catch(fail);
    return;
  }
  streamChat(p, {
    delta: onDelta,
    tool: onToolEvent,
    done: function(j){
      if (j.session_id){
        sessionId = j.session_id;
        localStorage.setItem(SESSION_KEY, sessionId);
      }
      finish(j.reply != null ? j.reply : (live ? live.text : ''));
    }
  }).catch(fail);
}

function stop(){
  if (abortCtl){ try { abortCtl.abort(); } catch(e){} }
  fetch(api('/api/stop'), { method:'POST', headers: ah({'Content-Type':'application/json'}),
    body: JSON.stringify({ session_id: sessionId || undefined }) }).catch(function(){});
  addSys('stopped');
  setRunning(false);
}

/* =============================== inbox ================================ */
function inboxCount(){
  fetch(api('/api/inbox'), { headers: ah() }).then(function(r){ return r.json(); }).then(function(j){
    var n = (j.entries || []).length;
    var b = $('inbox-badge');
    b.style.display = n ? '' : 'none';
    b.textContent = n;
    renderInbox(j.entries || []);
  }).catch(function(){});
}
function renderInbox(entries){
  var box = $('inbox-list');
  box.innerHTML = '';
  if (!entries.length){
    box.innerHTML = '<div class="empty-note">inbox empty — scheduled results and notifications land here.</div>';
    return;
  }
  entries.forEach(function(e){
    var item = document.createElement('div'); item.className = 'item';
    var head = document.createElement('div'); head.className = 'card-head';
    var caret = document.createElement('span'); caret.className = 'caret'; caret.textContent = '▶';
    var t = document.createElement('div'); t.className = 't'; t.textContent = (e.title || 'buddy') + ' · ' + (e.ts || '');
    var x = document.createElement('button'); x.className = 'dismiss'; x.innerHTML = '✕'; x.title = 'dismiss';
    head.appendChild(caret); head.appendChild(t); head.appendChild(x);
    var body = document.createElement('div'); body.className = 'inbox-body'; body.textContent = e.text || '';
    item.appendChild(head); item.appendChild(body);
    item.onclick = function(){ item.classList.toggle('open'); };
    x.onclick = function(ev){
      ev.stopPropagation();
      fetch(api('/api/inbox/dismiss'), { method:'POST',
        headers: ah({'Content-Type':'application/json'}), body: JSON.stringify({ ts: e.ts }) })
        .catch(function(){});
      item.remove();
      inboxCount();
    };
    box.appendChild(item);
  });
}
function loadInbox(){ inboxCount(); }

/* ============================ session drawer ========================== */
function openDrawer(id){
  $('overlay').classList.add('open');
  $(id).classList.add('open');
  if (id === 'drawer-sessions') loadSessions();
}
function closeDrawers(){
  $('overlay').classList.remove('open');
  ['drawer-sessions','drawer-inbox','drawer-theme'].forEach(function(d){ $(d).classList.remove('open'); });
}
function relTime(iso){
  try {
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  } catch(e){ return ''; }
}
function loadSessions(){
  var box = $('session-list');
  fetch(api('/api/sessions'), { headers: ah() }).then(function(r){ return r.json(); }).then(function(j){
    var ss = j.sessions || [];
    box.innerHTML = '';
    if (!ss.length){ box.innerHTML = '<div class="empty-note">no chats yet</div>'; return; }
    ss.forEach(function(s){
      var item = document.createElement('div');
      item.className = 'item' + (s.id === sessionId ? ' active' : '');
      item.innerHTML = '<div class="t"></div><div class="s"><span></span><span></span></div>';
      item.querySelector('.t').textContent = s.title || '(untitled)';
      var spans = item.querySelectorAll('.s span');
      spans[0].textContent = relTime(s.updated);
      spans[1].textContent = (s.count || 0) + ' msgs';
      var del = document.createElement('button');
      del.className = 'sess-del';
      del.title = 'delete chat';
      del.setAttribute('aria-label', 'delete chat');
      del.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';
      del.onclick = function(ev){
        ev.stopPropagation();
        if (!window.confirm('Delete "' + (s.title || 'this chat') + '"? This cannot be undone.')) return;
        fetch(api('/api/session/delete'), {
          method: 'POST', headers: ah({'Content-Type': 'application/json'}),
          body: JSON.stringify({id: s.id})
        }).then(function(r){ return r.json().then(function(j){ return {ok: r.ok, j: j}; }); })
          .then(function(res){
            if (!res.ok){ addSys(res.j && res.j.error || 'could not delete that chat'); }
            if (s.id === sessionId){
              sessionId = '';
              try { localStorage.setItem(SESSION_KEY, ''); } catch(e){}
              log.querySelectorAll('.row,.sysmsg').forEach(function(n){ n.remove(); });
              addSys('chat deleted');
            }
            loadSessions();
          })
          .catch(function(){ addSys('could not delete that chat'); });
      };
      item.appendChild(del);
      item.onclick = function(){
        closeDrawers();
        openSession(s.id);
      };
      box.appendChild(item);
    });
  }).catch(function(){ box.innerHTML = '<div class="empty-note">could not load chats</div>'; });
}
function openSession(id){
  fetch(api('/api/session?id=' + encodeURIComponent(id)), { headers: ah() })
    .then(function(r){ return r.json(); })
    .then(function(j){
      hideEmpty();
      log.querySelectorAll('.row,.sysmsg').forEach(function(n){ n.remove(); });
      sessionId = id;
      localStorage.setItem(SESSION_KEY, id);
      (j.messages || []).forEach(function(m){
        var c = m.content;
        if (Object.prototype.toString.call(c) === '[object Array]')
          c = c.map(function(part){ return (part && part.text) || ''; }).join(' ').trim();
        if (m.role === 'user') addMsg(c, 'you');
        else if (m.role === 'assistant' && c) addMsg(c, 'bud');
      });
      scrollDown();
    }).catch(function(){ addSys('could not load that chat'); });
}
function newChat(){
  fetch(api('/api/new'), { method:'POST', headers: ah({'Content-Type':'application/json'}), body:'{}' })
    .then(function(r){ return r.json(); })
    .then(function(j){
      sessionId = j.id || '';
      localStorage.setItem(SESSION_KEY, sessionId);
    }).catch(function(){ sessionId = ''; localStorage.removeItem(SESSION_KEY); });
  closeDrawers();
  hideEmpty();
  log.querySelectorAll('.row,.sysmsg').forEach(function(n){ n.remove(); });
  addSys('new chat');
}

/* =============================== wiring =============================== */
var STREAM = true;
(function(){ /* ?stream=0 - legacy single-JSON fallback */
  var m = location.search.match(/stream=(\d)/);
  if (m && m[1] === '0') STREAM = false;
})();

var ta = $('i');
ta.addEventListener('input', function(){
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
});
ta.addEventListener('keydown', function(e){
  if (e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    if ($('f').requestSubmit) $('f').requestSubmit(); else send(ta.value.trim());
  }
});
$('f').onsubmit = function(e){
  e.preventDefault();
  var v = ta.value.trim();
  if (!v || running) return;
  ta.value = ''; ta.style.height = 'auto';
  send(v);
};
$('send').addEventListener('click', function(e){
  if (running){ e.preventDefault(); stop(); }
});
$('btn-sessions').onclick = function(){ openDrawer('drawer-sessions'); };
$('btn-theme').onclick = function(){
  var list = $('theme-list');
  if (list && !list.childElementCount){
    Object.keys(ALL_THEMES).sort().forEach(function(name){
      var b = document.createElement('button');
      b.className = 'item theme-item';
      b.dataset.name = name;
      var t = ALL_THEMES[name];
      var safeHex = function(v, fb){ return /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(v||'') ? v : fb; };
      var c1 = safeHex(t.primary, '#888'), c2 = safeHex(t.secondary, '#888'), c3 = safeHex(t.background, '#111');
      var sw = document.createElement('span');
      sw.className = 'swatch';
      sw.style.background = 'linear-gradient(135deg,' + c1 + ',' + c2 + ',' + c3 + ')';
      var label = document.createElement('span');
      label.textContent = name;
      b.textContent = '';
      b.appendChild(sw);
      b.appendChild(label);
      b.onclick = function(){ setTheme(name); };
      list.appendChild(b);
    });
  }
  openDrawer('drawer-theme');
};
$('btn-inbox').onclick = function(){ openDrawer('drawer-inbox'); };
$('btn-new').onclick = newChat;
$('overlay').onclick = closeDrawers;
document.querySelectorAll('[data-close]').forEach(function(b){ b.onclick = closeDrawers; });
/* suggestion buttons wire themselves when the empty-state block builds them */

function loadModels(){
  return fetch(api('/api/models'), { headers: ah() }).then(function(r){
    if (!r.ok) throw new Error('models unavailable');
    return r.json();
  }).then(function(j){
    var sel = $('model');
    if (!sel || !j.models) return;
    sel.innerHTML = '';
    var groups = {};
    j.models.forEach(function(m){
      var g = m.provider || 'other';
      (groups[g] = groups[g] || []).push(m);
    });
    Object.keys(groups).forEach(function(g){
      var og = document.createElement('optgroup');
      og.label = (g === 'ADDED') ? 'ADDED BY YOU' : g.toUpperCase();
      groups[g].forEach(function(m){
        var o = document.createElement('option');
        o.value = m.model;
        o.textContent = m.model + (m.source === 'live' ? '' : '  (preset)');
        if (m.active) o.selected = true;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    sel.onchange = function(){
      var m = sel.value;
      if (!m) return;
      sel.disabled = true;
      postJSON('/api/model', { model: m }).then(function(j){
        if (j && j.error){ toast(j.error); sel.disabled = false; return; }
        if (j && j.ok){
          toast('model → ' + (j.model || m));
          var sb = $('sb-left');
          if (sb) sb.textContent = 'chat · ' + (j.model || m) + ' · ' +
            (($('theme-current') || {}).textContent || 'opencode');
          if (j.new_session) newChat();
        }
        sel.disabled = false;
      }).catch(function(){ sel.disabled = false; });
    };
  }).catch(function(){
    var sel = $('model');
    if (sel) sel.innerHTML = '<option value="">models unavailable</option>';
  });
}

fetch(api('/api/status'), { headers: ah() }).then(function(r){ return r.json(); })
  .then(function(j){
    $('model').title = 'model: ' + (j.model || '?') + ' (click to switch)';
    var sb = $('sb-left');
    if (sb) sb.textContent = 'chat · ' + (j.model || '?') + ' · ' + (j.theme || 'opencode');
  })
  .catch(function(){});
loadModels();
loadInbox();
loadThemes();
</script>
</body>
</html>
"""

# ---- original buddy.py lines 4298-4299 --------------------------------


# web has no interactive prompt: when a chat message carries an API key we
# ask in-band, remember the pending key per session, and store on "yes"
_PENDING_KEYS: dict[str, tuple[str, str]] = {}


def _pending_key_put(sid: str, provider: str, key: str) -> None:
    if len(_PENDING_KEYS) > 20:
        _PENDING_KEYS.pop(next(iter(_PENDING_KEYS)), None)
    _PENDING_KEYS[sid] = (provider, key)


def _redact_keys(text: str) -> str:
    """Strip full keys out of text kept in the session (they would otherwise
    be sent to the LLM on every later turn)."""
    try:
        from .agent import _API_KEY_PATTERNS
        for _prov, rx in _API_KEY_PATTERNS:
            text = rx.sub(lambda m: "…" + m.group(0)[-6:], text)
    except Exception:
        pass
    return text


def _web_confirm(cmd: str) -> bool:
    """Confirm callback for explicit WebUI user actions (! commands, chat
    turns). Honors config "yolo": true / mode "bypass" like the terminal
    REPL; otherwise denies (the web has no interactive prompt). Reads
    the live WebUI.cfg (reloading config.json if it changed) so a config
    edit applies without a daemon restart."""
    try:
        live = maybe_reload(WebUI.cfg) or {}
    except Exception:
        try:
            live = WebUI.cfg or {}
        except Exception:
            live = {}
    return bool(live.get("yolo") or live.get("mode") == "bypass")


# ---- original buddy.py lines 4300-4543 --------------------------------
class WebUI(BaseHTTPRequestHandler):
    cfg: dict = {}
    mcp: MCPManager = None
    # Socket timeout: reaps dead/stalled connections (a half-open client
    # holding a thread forever) without touching healthy SSE streams —
    # idle-but-open senders don't hit it, only actual timed-out reads.
    timeout = 120
    # Explicit user clicks honor "yolo": true / mode "bypass" (see
    # _web_confirm); background workers pass confirm=None to auto-deny.
    confirm = staticmethod(_web_confirm)
    auth_disabled: bool = False  # opt-in via config "web_auth": false (loopback only)

    def log_message(self, *a):  # silence
        pass

    def _auth(self) -> bool:
        if self.auth_disabled:
            return True
        # Prefer headers (no log leakage); ?t= kept for webhooks/EventSource.
        supplied = (self.headers.get("Authorization") or "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = (self.headers.get("X-Buddy-Token") or "").strip()
        if not supplied:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            supplied = (q.get("t", [""])[0] or "")
        try:
            return hmac.compare_digest(supplied.encode("utf-8", "replace"),
                                       str(web_token(self.cfg)).encode())
        except Exception:
            return False

    def _send(self, code, body: bytes, ctype="application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _maybe_key_setup(self, sid: str, message: str):
        """Detect a pasted API key in a web chat message. Returns a reply
        string if the turn is handled (ask / stored), else None."""
        from .agent import detect_api_key, _store_provider_key
        hit = detect_api_key(message)
        if hit:
            provider, key = hit
            pending = _PENDING_KEYS.pop(sid, None)
            if pending != (provider, key):  # fresh key: ask before storing
                _pending_key_put(sid, provider, key)
                return (f"that looks like a {provider} API key (…{key[-6:]}). "
                        f"Reply 'yes' and I'll store it and switch to {provider}.")
            pending = None  # re-sent key after our ask = confirmation
        # no key in this message: an explicit 'yes' confirms a pending one
        pending = _PENDING_KEYS.get(sid)
        if pending and message.strip().lower().rstrip("!.?") in ("yes", "y", "ok", "sure", "do it", "set it up"):
            _PENDING_KEYS.pop(sid, None)
            provider, key = pending
            return _store_provider_key(provider, key, self.cfg)
        if pending and message.strip().lower().rstrip("!.?") in ("no", "n", "nope", "cancel"):
            _PENDING_KEYS.pop(sid, None)
            return "ok — I dropped it. Nothing was stored."
        return None

    def _read_json(self, cap: int = 0) -> dict | None:
        value = self.headers.get("Content-Length")
        if value is None:
            return {}
        try:
            length = int(value)
        except ValueError:
            self._send(400, b'{"error":"invalid Content-Length"}')
            return None
        limit = cap or MAX_WEB_BODY
        if length < 0 or length > limit:
            self._send(413, b'{"error":"request too large"}')
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b'{"error":"invalid json"}')
            return None
        if not isinstance(body, dict):
            self._send(400, b'{"error":"JSON object required"}')
            return None
        return body

    def _sse_headers(self) -> None:
        """Begin an SSE response (POST /api/chat streaming branch)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # close the connection when the stream ends — with no Content-Length,
        # EOF is how clients detect the end (a keep-alive header here makes
        # http.server hold the socket open and readers hang forever)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def _sse(self, event: str, data: dict) -> None:
        """Write one SSE frame: 'event: <name>\\ndata: <json>\\n\\n'.
        Locked: parallel tool workers can emit events concurrently."""
        with _SSE_LOCK:
            self.wfile.write(("event: " + event + "\ndata: " +
                              json.dumps(data, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            # the shell page itself carries no secrets (the token stays in the
            # browser hash and is only appended to API calls by its JS)
            self._send(200, PAGE.encode(), "text/html")
            return
        if not self._auth():
            self._send(401, b'{"error":"bad token"}')
            return
        if path.startswith("/site/"):
            # buddy's published pages: /site/<file> from workspace/site
            name = urllib.parse.unquote(path[len("/site/"):])
            site = WORKSPACE / "site"
            target = (site / name).resolve()
            if target != site.resolve() and site.resolve() not in target.parents:
                self._send(404, b'{"error":"not found"}')
                return
            if target.is_file() and site.resolve() in target.parents:
                ctype = "text/html" if target.suffix in (".html", ".htm") else (
                    "text/css" if target.suffix == ".css" else
                    "application/javascript" if target.suffix == ".js" else "text/plain")
                # User-published HTML runs same-origin: sandbox it so a malicious
                # page cannot read the ?t= token from location.search / history.
                extra = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src * data:; media-src * data:; font-src * data:;"} if ctype == "text/html" else None
                self._send(200, target.read_bytes(), ctype, extra)
            else:
                self._send(404, b'{"error":"not found"}')
        elif path == "/api/status":
            self._send(200, json.dumps({
                "model": self.cfg.get("model", "?"),
                "theme": self.cfg.get("theme", "opencode"),
            }).encode())
        elif path == "/api/themes":
            from .theme import THEMES as _THEMES, list_themes as _list_themes
            # web picker shows a curated set only (terminal /theme keeps all)
            _WEB_THEMES = ("ocweb", "ocweb-dark", "catppuccin", "gruvbox")
            _out_themes = {}
            for n in _list_themes():
                if n not in _WEB_THEMES:
                    continue
                if n in _THEMES and _THEMES.get(n):
                    _out_themes[n] = _THEMES.get(n)
                    continue
                try:
                    _out_themes[n] = json.loads((HOME / "themes" / f"{n}.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue  # corrupt user theme must not kill handler
            self._send(200, json.dumps({
                "default": self.cfg.get("theme", "opencode"),
                "themes": _out_themes,
            }).encode())
        elif path == "/api/inbox":
            self._send(200, json.dumps({"entries": _inbox_entries()}).encode())
        elif path == "/api/sessions":
            self._send(200, json.dumps({"sessions": session_list()}).encode())
        elif path == "/api/session":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sess = session_load((q.get("id", [""])[0])[:64])
            if not sess:
                self._send(404, b'{"error":"no such session"}')
                return
            self._send(200, json.dumps({
                "id": sess["id"],
                "messages": [m for m in sess.get("messages", [])
                             if m.get("role") in ("user", "assistant")],
            }).encode())
        elif path == "/api/playbook":
            self._send(200, json.dumps({"text": read_playbook()}).encode())
        elif path == "/api/models":
            # provider-grouped catalog for the header model picker
            try:
                from .commands import _model_entries
                maybe_reload(self.cfg)
                models = [{"n": e["n"], "label": e["label"],
                           "provider": e.get("provider", ""),
                           "model": e["model"], "base": e["base"],
                           "source": e.get("source", ""),
                           "active": e.get("active", False)}
                          for e in _model_entries(self.cfg)]
                self._send(200, json.dumps(
                    {"model": self.cfg.get("model"), "models": models}).encode())
            except Exception as e:
                self._send(200, json.dumps({"error": str(e),
                                            "models": []}).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/hook/"):
            # inbound webhook: /hook/<name>?t=<token> — point external services here
            if not self._auth():
                self._send(401, b'{"error":"bad token"}')
                return
            name = path[len("/hook/"):].strip("/")[:60]
            name = re.sub(r"[^a-zA-Z0-9_-]", "", name) or "hook"
            body = self._read_json()
            if body is None:
                return
            deliver(f"webhook '{name}':\n{json.dumps(body)[:800]}",
                    title=f"webhook {name}")
            # hook_triggers: config {"<name>": "prompt ..."} makes buddy REACT
            trigger = ((self.cfg or {}).get("hook_triggers") or {}).get(name)
            if trigger:
                cfg = dict(self.cfg or {})
                def worker():
                    try:
                        run_agent(cfg, _sub_mcp(),
                                  f"{trigger}\n\nWebhook payload:\n{json.dumps(body)[:2000]}",
                                  quiet=True, confirm=None)
                    except Exception as e:
                        # run_agent already logged the traceback; make the
                        # failed reaction visible in the inbox too
                        try:
                            deliver(f"(webhook '{name}' reaction failed: {e})",
                                    title=f"webhook {name}")
                        except Exception:
                            pass
                threading.Thread(target=worker, daemon=True).start()
                self._send(200, b'{"accepted":true}')
                return
            self._send(200, b'{"ok":true}')
            return
        if not self._auth():
            self._send(401, b'{"error":"bad token"}')
            return
        body = self._read_json(cap=4_500_000)  # /api/chat carries base64 images
        if body is None:
            return
        if path == "/api/session/delete":
            sid = (body.get("id") or "")[:64]
            if not sid:
                self._send(400, b'{"error":"missing id"}')
                return
            ok = session_delete(sid)
            self._send(200 if ok else 404,
                       b'{"ok":true}' if ok else b'{"error":"no such session"}')
            return
        if path == "/api/chat":
            maybe_reload(self.cfg)  # honor `buddy.py model set ...` live
            message = (body.get("message") or "").strip()
            image = body.get("image")
            if isinstance(image, str):
                if not image.startswith("data:image/"):
                    self._send(400, b'{"error":"image must be a data:image/* URL"}')
                    return
                if len(image) > 3500000:
                    self._send(400, b'{"error":"image too large (2.5MB max)"}')
                    return
            elif image is not None:
                self._send(400, b'{"error":"bad image"}')
                return
            if not message and not image:
                self._send(400, b'{"error":"empty"}')
                return
            sid = (body.get("session_id") or "")[:64]
            # API-key auto-detect: ask in-band, store on an explicit "yes";
            # either way the raw key never reaches the LLM or the stored log
            # (detect on the raw message, but persist only redacted text)
            sess = session_ensure(sid or None, _redact_keys(message) or "(image)")
            key_reply = self._maybe_key_setup(sess["id"], message)
            message = _redact_keys(message)
            if key_reply is not None:
                with STATE_LOCK:
                    sess["messages"].append({"role": "user", "content": message,
                                             "ts": time.time()})
                    sess["messages"].append({"role": "assistant",
                                             "content": key_reply, "ts": time.time()})
                    session_save(sess)
                self._sse_headers()
                self._sse("delta", {"text": key_reply})
                self._sse("done", {"reply": key_reply, "session_id": sess["id"]})
                return
            with STATE_LOCK:  # concurrent turns on one session: atomic append
                if image:
                    _my_turn = {"role": "user", "content": [
                        {"type": "text", "text": message or "(user shared an image)"},
                        {"type": "image_url", "image_url": {"url": image}}
                    ], "ts": time.time()}
                else:
                    _my_turn = {"role": "user", "content": message,
                                "ts": time.time()}
                sess["messages"].append(_my_turn)

            # run_messages elides old content IN PLACE when over budget; hand it
            # copies of the message dicts so the stored session never gets
            # rewritten as "(elided: …)"
            turn = [dict(m) for m in sess["messages"]]
            if turn and turn[0].get("role") != "system":
                try:  # web turns run with the same identity/tool discipline
                    from .prompts import _system_prompt
                    turn = [{"role": "system", "content": _system_prompt()}] + turn
                except Exception:
                    pass

            if message.startswith("/") and not image:
                from .commands import run_slash_command
                reply = run_slash_command(self.cfg, message,
                                          confirm=lambda _m: True)  # user-typed in web UI
                if body.get("stream") is False:
                    with STATE_LOCK:
                        sess["messages"].append({"role": "assistant", "content": reply, "ts": time.time()})
                        session_save(sess)
                    self._send(200, json.dumps({"reply": reply, "session_id": sess["id"]}).encode())
                    return
                self._sse_headers()
                self._sse("delta", {"text": reply})
                self._sse("done", {"reply": reply, "session_id": sess["id"]})
                with STATE_LOCK:
                    sess["messages"].append({"role": "assistant", "content": reply, "ts": time.time()})
                    session_save(sess)
                return

            if message.startswith("!") and not image:
                from .tools import run_command
                cmd_to_run = message[1:].strip()
                reply = run_command(cmd_to_run, confirm=self.confirm) if cmd_to_run else "(no command)"
                if body.get("stream") is False:
                    with STATE_LOCK:
                        sess["messages"].append({"role": "assistant", "content": reply, "ts": time.time()})
                        session_save(sess)
                    self._send(200, json.dumps({"reply": reply, "session_id": sess["id"]}).encode())
                    return
                self._sse_headers()
                self._sse("delta", {"text": reply})
                self._sse("done", {"reply": reply, "session_id": sess["id"]})
                with STATE_LOCK:
                    sess["messages"].append({"role": "assistant", "content": reply, "ts": time.time()})
                    session_save(sess)
                return

            if not image:
                # Deterministic intents (model switch, "update this pc",
                # "what time is it", calc, git status, …) run in the TUI
                # before the LLM sees the message, but the web path went
                # straight to the model — so "change model to X" depended
                # on the model choosing to call a tool, and it would happily
                # claim success without switching. Same layer, same result.
                try:
                    from .commands import _run_intent, parse_intent
                    _intent = parse_intent(message)
                except Exception:
                    _intent = None
                if _intent is not None:
                    try:
                        summary = _run_intent(_intent, self.cfg, self.confirm)
                    except Exception as e:
                        summary = f"(that didn't work: {e})"
                    if summary is not None:
                        if body.get("stream") is False:
                            with STATE_LOCK:
                                sess["messages"].append(
                                    {"role": "assistant", "content": summary,
                                     "ts": time.time()})
                                session_save(sess)
                            self._send(200, json.dumps(
                                {"reply": summary,
                                 "session_id": sess["id"]}).encode())
                            return
                        self._sse_headers()
                        self._sse("delta", {"text": summary})
                        self._sse("done", {"reply": summary,
                                           "session_id": sess["id"]})
                        with STATE_LOCK:
                            sess["messages"].append(
                                {"role": "assistant", "content": summary,
                                 "ts": time.time()})
                            session_save(sess)
                        return

            # ---- legacy non-stream fallback (?stream=0): single JSON ----
            if body.get("stream") is False:
                cancel = cancel_event(sess["id"])
                try:
                    reply = run_messages(self.cfg, self.mcp, turn, quiet=True,
                                         confirm=self.confirm, cancel=cancel)
                except Exception as e:
                    _cancel_cleanup(sess["id"], cancel)
                    with STATE_LOCK:
                        _rollback_turn(sess, _my_turn)  # no orphan user turn
                        session_save(sess)
                    self._send(200, json.dumps(
                        {"error": str(e), "session_id": sess["id"]}).encode())
                    return
                _cancel_cleanup(sess["id"], cancel)
                with STATE_LOCK:
                    sess["messages"].append({"role": "assistant", "content": reply,
                                             "ts": time.time()})
                    session_save(sess)
                self._send(200, json.dumps(
                    {"reply": reply, "session_id": sess["id"]}).encode())
                return

            # --------------------------- SSE streaming branch -----------
            self._sse_headers()
            cancel = cancel_event(sess["id"])
            final = {"text": ""}

            def on_delta(text: str) -> None:
                final["text"] += text
                self._sse("delta", {"text": text})

            def on_tool(name: str, status: str, secs=None, args=None, diff=None,
                        call_id=None, tail=None, result=None) -> None:
                slim = {}
                for k, v in (args or {}).items():
                    if isinstance(v, str):
                        slim[k] = v[:600] + ("…" if len(v) > 600 else "")
                    elif isinstance(v, (int, float, bool)) or v is None:
                        slim[k] = v
                    elif isinstance(v, (list, dict)):
                        # structured args (e.g. todo_write's todos) must reach
                        # the UI whole — capped, not dropped
                        try:
                            slim[k] = v if len(json.dumps(v)) < 4000 else (
                                v[:8] if isinstance(v, list)
                                else {kk: v[kk] for kk in list(v)[:8]})
                        except Exception:
                            slim[k] = "(unserializable arg)"
                    else:
                        try:
                            slim[k] = str(v)[:600]
                        except Exception:
                            slim[k] = "(unserializable arg)" 
                ev = {"name": name, "status": status, "secs": secs,
                      "args": slim, "diff": diff, "id": call_id}
                if tail is not None:
                    ev["tail"] = tail
                if result is not None:
                    ev["result"] = result
                self._sse("tool", ev)

            saved = False
            try:
                reply = run_messages(self.cfg, self.mcp, turn,
                                     quiet=True, confirm=self.confirm,
                                     cancel=cancel, on_tool=on_tool,
                                     on_delta=on_delta)
                with STATE_LOCK:
                    sess["messages"].append({"role": "assistant", "content": reply,
                                             "ts": time.time()})
                    session_save(sess)
                    saved = True
                try:
                    self._sse("done", {"reply": reply, "session_id": sess["id"]})
                except Exception:
                    # client disconnected after save: turn is safe on disk.
                    _log_error("webui done-send failed (client gone, turn saved)")
            except Exception as e:
                client_gone = isinstance(e, (BrokenPipeError,
                                             ConnectionResetError,
                                             ConnectionAbortedError))
                if cancel.is_set() or client_gone:
                    # cancelled or tab closed: keep whatever streamed so a
                    # reload doesn't lose the turn
                    with STATE_LOCK:
                        sess["messages"].append(
                            {"role": "assistant", "content": final["text"],
                             "ts": time.time()})
                        session_save(sess)
                    if client_gone:
                        _log_error("webui client disconnected mid-turn (partial saved)")
                    else:
                        self._sse("done", {"cancelled": True,
                                           "session_id": sess["id"]})
                elif not saved:
                    with STATE_LOCK:
                        _rollback_turn(sess, _my_turn)  # no orphan user turn
                        session_save(sess)
                    try:
                        self._sse("done", {"error": str(e),
                                           "session_id": sess["id"]})
                    except Exception:
                        # client already disconnected — the error must not be lost
                        _log_error("webui turn failed (client gone):\n"
                                   + traceback.format_exc())
            finally:
                _cancel_cleanup(sess["id"], cancel)
        elif path == "/api/stop":
            ok = cancel_session((body.get("session_id") or "")[:64])
            self._send(200, json.dumps({"stopped": ok}).encode())
        elif path == "/api/model":
            # Header model picker: switch to a model id from /api/models.
            want = str(body.get("model") or "").strip()
            if not want:
                self._send(400, b'{"error":"model required"}')
                return
            try:
                from .commands import _model_entries
                maybe_reload(self.cfg)
                entry = next((e for e in _model_entries(self.cfg)
                              if e["model"] == want), None)
                if entry is None:
                    self._send(400, json.dumps(
                        {"error": f"unknown model: {want}"}).encode())
                    return
                if entry["model"] == self.cfg.get("model") and \
                        entry["base"] == (self.cfg.get("api_base") or "").rstrip("/"):
                    self._send(200, json.dumps(
                        {"ok": True, "model": entry["model"],
                         "new_session": False}).encode())
                    return
                from .commands import _apply_model_entry
                msg = _apply_model_entry(self.cfg, entry)  # persists config
                # The old transcript was produced by another model; tell the
                # page to start clean so the history isn't a mixed account.
                self._send(200, json.dumps(
                    {"ok": True, "model": self.cfg.get("model"),
                     "message": msg, "new_session": True}).encode())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())
        elif path == "/api/new":
            sess = session_ensure(None)
            self._send(200, json.dumps({"id": sess["id"]}).encode())
        elif path == "/api/inbox/dismiss":
            ts = str(body.get("ts") or "")
            if not ts:
                self._send(400, b'{"error":"missing ts"}')
                return
            _inbox_dismiss(ts)
            self._send(200, b'{"ok":true}')
        elif path == "/api/clear_inbox":
            with STATE_LOCK:
                INBOX.unlink(missing_ok=True)
            self._send(200, b"{}")
        else:
            self._send(404, b"{}")

# ---- original buddy.py lines 4544-4545 --------------------------------


# ---- original buddy.py lines 4546-4600 --------------------------------
def serve(cfg: dict) -> None:
    # Baseline for maybe_reload(): later turns reload when config.json changes.
    _stamp_config(cfg)
    if DAEMON_PID.exists():
        try:
            pid = int(DAEMON_PID.read_text().strip())
            os.kill(pid, 0)  # succeeds only if a live process holds this pid
            if _pid_is_buddy(pid):
                print(f"daemon already running (pid {pid})")
                sys.exit(1)
            # pid recycled by another program — stale file, start fresh
        except (OSError, ValueError):
            pass  # stale or unreadable pid file — start fresh
    HOME.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    host = cfg.get("web_host", "127.0.0.1")
    try:
        httpd = ThreadingHTTPServer((host, PORT), WebUI)
    except OSError as e:
        # Bind BEFORE claiming the pid: a port conflict fails fast with a
        # clear message instead of a pid file + a retry-spamming process.
        print(f"(web UI can't bind {host}:{PORT}: {e} — is another daemon already running?)")
        sys.exit(1)
    DAEMON_PID.write_text(str(os.getpid()))
    try:
        os.chmod(DAEMON_PID, 0o600)
    except OSError:
        pass
    import atexit as _atexit
    _me = os.getpid()
    def _drop_pid():
        try:
            if os.getpid() == _me and DAEMON_PID.exists() \
                    and DAEMON_PID.read_text().strip() == str(_me):
                DAEMON_PID.unlink()
        except OSError:
            pass
    _atexit.register(_drop_pid)
    from .theme import apply as _apply_theme
    _apply_theme(cfg.get("theme", "opencode"))  # daemon banner + web default
    mcp = MCPManager()
    mcp.start_all()
    for name, err in mcp.error.items():
        print(f"(MCP {name} failed: {err[:120]})")
    sched = Scheduler(cfg, mcp, confirm=None)  # daemon: auto-deny dangerous
    sched.start()
    Watcher(cfg).start()
    if cfg.get("telegram") and secret_get("telegram_token"):
        TelegramBot(cfg).start()
        print(f"  telegram: paired chat {cfg.get('telegram_chat_id', '(awaiting first message)')}")
    AutoEvolve(cfg, mcp).start()
    print(f"  auto-evolve: every {cfg.get('evolve_hours', 24)}h (config 'evolve_hours' to change, 0 = off)")
    AutoUpgrade(cfg, mcp).start()
    print(f"  auto-upgrade: armed (every {cfg.get('upgrade_hours', 24)}h, config 'upgrade_hours' to change, 0 = off)")
    ErrorReaper(cfg, mcp).start()
    print("  self-repair: armed (fixes his own crashes & bugs, max once/hour)")
    WebUI.cfg = cfg
    WebUI.mcp = mcp
    # NB: WebUI.confirm defaults to _web_confirm (yolo/bypass-aware);
    # Sched/Telegram workers keep confirm=None to auto-deny dangerous shell.
    no_auth = cfg.get("web_auth") is False
    if no_auth and host not in ("127.0.0.1", "localhost", "::1"):
        # Never serve unauthenticated on the network: fail fast, not open.
        print(f"(refusing: config web_auth=false with web_host={host!r} — "
              f"tokenless mode is loopback-only)")
        sys.exit(1)
    WebUI.auth_disabled = no_auth
    tok = "" if no_auth else web_token(cfg)
    url = f"http://127.0.0.1:{PORT}/" if no_auth else f"http://127.0.0.1:{PORT}/#{tok}"
    hookurl = (f"http://127.0.0.1:{PORT}/hook/<name>" if no_auth
               else f"http://127.0.0.1:{PORT}/hook/<name>?t={tok}")
    print("buddy daemon online.")
    import sys as _sys
    if no_auth:
        print(f"  web UI: {url}")
        print("  ⚠ web auth DISABLED by config (loopback-only, no token)")
    elif _sys.stdout.isatty():
        print(f"  web UI: {url}")
    else:
        # Logs/journals persist: don't leave the bearer token in them.
        print(f"  web UI: http://127.0.0.1:{PORT}/#<token> "
              f"(masked in logs — run `python3 buddy.py web` locally for the URL)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        lan_ip = ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        if lan_ip:
            lan_url = f"http://{lan_ip}:{PORT}/#{tok}"
            if not _sys.stdout.isatty():
                lan_url = f"http://{lan_ip}:{PORT}/#<token> (masked in logs)"
            print(f"  on LAN: {lan_url}")
        print("  ⚠ web UI exposed on LAN — token required")
    if _sys.stdout.isatty():
        print(f"  inbound webhooks: {hookurl}")
    else:
        # logs/journals persist — same masking rule as the web UI URL
        print(f"  inbound webhooks: http://127.0.0.1:{PORT}/hook/<name>"
              f"?t=<token> (masked — run `python3 buddy.py web` for the URL)")
    print(f"  model:  {cfg.get('model', '?')}, MCP servers: {len(mcp.servers)}")
    print('  jobs run here 24/7; dangerous shell is auto-denied (set config "yolo": true to allow)')
    while True:  # self-heal: the daemon refuses to die
        try:
            httpd.serve_forever()
        except Exception as e:
            print(f"(web server hiccup, restarting: {e})")
            time.sleep(2)

