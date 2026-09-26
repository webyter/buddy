"""buddy_core.memory — flat + vector memory, recall/search, session digest, compaction, memory context.

Fragment of the buddy v3 program. NOT imported as a regular module:
buddy_core.load_into() executes it into the buddy module's shared
namespace (see buddy_core/__init__.py) so every global resolves
exactly as it did in the original single-file buddy.py.
"""

from __future__ import annotations
from .brain import _completion
from .config import MEMORY, STATE_LOCK, VECTORS, api_key, load_config, CONFIG
from .util import _log_error

import hashlib
import json
import math
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
import traceback
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

# ---- original buddy.py lines 829-832 ----------------------------------


# ================================================================== memory =

# ---- original buddy.py lines 833-838 ----------------------------------
def remember(fact: str) -> str:
    fact = " ".join(str(fact).split())  # a newline inside a fact would be
    # parsed back as a digest header by memory_compact and silently dropped
    with MEMORY.open("a") as f:
        stamp = datetime.now().strftime("%Y-%m-%d")
        f.write(f"- [{stamp}] {fact}\n")
    _remember_vector(fact)
    return f"Remembered: {fact}"

# ---- original buddy.py lines 839-840 ----------------------------------


# ---- original buddy.py lines 841-877 ----------------------------------
def _remember_vector(fact: str) -> None:
    """Vector-store a fact, deduplicating near-identical memories: if an
    existing entry is >0.90 cosine-similar, replace it instead of appending.
    The flat memory.md line is always kept — dedup applies to vectors only.
    If semantic embeddings are unavailable (no embed_model / API down), this
    quietly does nothing, same as _vector_add with no embed model."""
    vec = _embed(fact)  # network call stays OUTSIDE the lock
    if not vec:
        return  # semantic recall disabled or unavailable — skip silently
    try:
        # Whole read-modify-write under the lock: reading (and deduping)
        # outside it meant two concurrent remember() calls both wrote their
        # own snapshot, and the loser's entry vanished (the store is
        # rewritten wholesale by _write_vectors).
        with STATE_LOCK:
            data = (json.loads(VECTORS.read_text())
                    if VECTORS.exists() else {"items": []})
            items = data.get("items", []) if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
            best_i, best_s = -1, 0.0
            for i, it in enumerate(items):
                v = it.get("vector") if isinstance(it, dict) else None
                if not v:
                    continue
                try:
                    s = _cosine(vec, v)
                except Exception:
                    continue
                if s > best_s:
                    best_i, best_s = i, s
            if best_s > 0.90 and 0 <= best_i < len(items):
                items[best_i]["text"] = fact[:500]
                items[best_i]["vector"] = vec
                items[best_i]["ts"] = time.time()
            else:
                items.append({"text": fact[:500], "vector": vec,
                              "ts": time.time()})
            _write_vectors({"items": items[-500:]})
    except Exception:
        pass

# ---- original buddy.py lines 878-879 ----------------------------------


# ---- original buddy.py lines 880-883 ----------------------------------
def recall_memory() -> str:
    if not MEMORY.exists():
        return "(nothing remembered yet)"
    return MEMORY.read_text()

# ---- original buddy.py lines 948-949 ----------------------------------


# ---- original buddy.py lines 950-985 ----------------------------------
def session_digest(cfg: dict, transcript: list[dict]) -> None:
    convo = "\n".join(
        f"{m['role']}: {str(m.get('content'))[:500]}"
        for m in transcript
        if m["role"] in ("user", "assistant") and m.get("content")
    )[-8000:]
    if not convo.strip():
        return
    try:
        resp = _completion(
            cfg,
            [
                {
                    "role": "system",
                    "content": "Extract durable facts about the user worth "
                    "remembering long-term (preferences, names, plans, "
                    "corrections). One per line, prefix '- '. If none, output "
                    "NOTHING. No commentary.",
                },
                {"role": "user", "content": convo},
            ],
            tools=None,
            stream=False,
        )
        text = (resp["choices"][0]["message"].get("content") or "").strip()
        if not text or text.upper().startswith("NOTHING"):
            return
        with MEMORY.open("a") as f:
            f.write(f"### session digest {datetime.now():%Y-%m-%d %H:%M}\n")
            f.write(text + "\n")
        for line in text.splitlines():
            if line.strip().startswith("-"):
                _vector_add(line.strip()[2:])
        print("(memory updated with session digest)")
    except Exception:
        pass

# ---- original buddy.py lines 986-989 ----------------------------------


# ------------------------------------------------------------- vector mem --

# ---- original buddy.py lines 990-1008 ---------------------------------
def _write_vectors(data: dict) -> None:
    """Atomic vector-store write (tmp + os.replace) so a crash mid-write
    can never leave a truncated file that wipes all vectors."""
    tmp = VECTORS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, VECTORS)


def _vector_add(text: str) -> None:
    vec = _embed(text)
    if not vec:
        return
    with STATE_LOCK:
        data = {"items": []}
        if VECTORS.exists():
            try:
                data = json.loads(VECTORS.read_text())
            except Exception:
                pass
        # json.loads is guarded, its RESULT is not: a vectors.json holding
        # valid non-object JSON (null/[...]/\"x\") made data.get raise, and
        # that escaped _exit_pass — silently disabling playbook learning.
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = {"items": []} if not isinstance(data, dict) else data
            data["items"] = []
        now = time.time()
        for it in data["items"]:  # migrate pre-ts items on read
            if isinstance(it, dict) and not it.get("ts"):
                it["ts"] = now
        data["items"].append({"text": text[:500], "vector": vec, "ts": now})
        data["items"] = data["items"][-500:]  # cap
        _write_vectors(data)

# ---- original buddy.py lines 1009-1010 --------------------------------


# ---- original buddy.py lines 1011-1028 --------------------------------
_EMBED_CACHE: dict[str, list[float]] = {}


def _embed(text: str) -> list[float] | None:
    key = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]
    try:
        cfg = load_config() if CONFIG.exists() else {}  # set in chat/serve entry points
    except SystemExit:
        return None  # corrupt config: load_config() exits; embedding degrades to None
    if not cfg or not cfg.get("embed_model") or not cfg.get("api_base") or not api_key(cfg):
        return None
    body = json.dumps({"model": cfg["embed_model"], "input": text}).encode()
    req = urllib.request.Request(
        cfg["api_base"].rstrip("/") + "/embeddings",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key(cfg)}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            vec = json.loads(r.read())["data"][0]["embedding"]
        if len(_EMBED_CACHE) >= 200:  # bounded: drop oldest batch
            for _k in list(_EMBED_CACHE)[:100]:
                _EMBED_CACHE.pop(_k, None)
        _EMBED_CACHE[key] = vec
        return vec
    except Exception:
        return None

# ---- original buddy.py lines 1029-1030 --------------------------------


# ---- original buddy.py lines 1031-1035 --------------------------------
def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:  # mismatched/empty vectors must not crash search
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))  # zip truncates to the shorter
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)

# ---- original buddy.py lines 1036-1037 --------------------------------


# ---- original buddy.py lines 1038-1061 --------------------------------
def search_memory(query: str, k: int = 5) -> str:
    """Semantic recall over remembered facts; falls back to substring grep."""
    qvec = _embed(query)
    if qvec and VECTORS.exists():
        try:
            items = json.loads(VECTORS.read_text())["items"]
            ranked = sorted(
                ((_cosine(qvec, it["vector"]), it["text"]) for it in items),
                reverse=True,
            )[:k]
            hits = [t for s, t in ranked if s > 0.3]
            if hits:
                return "\n".join(hits)
        except Exception:
            pass
    # fallback: scored substring search (most query words matched first)
    if MEMORY.exists():
        words = [w.lower() for w in query.split() if len(w) > 2]
        scored = []
        for ln in MEMORY.read_text().splitlines():
            low = ln.lower()
            hits = sum(1 for w in words if w in low)
            if hits:
                scored.append((hits, ln))
        scored.sort(key=lambda p: p[0], reverse=True)
        return "\n".join(ln for _, ln in scored[:k]) or "(no memory matches)"
    return "(nothing remembered yet)" 

# ---- original buddy.py lines 1187-1188 --------------------------------


# ---- original buddy.py lines 1189-1252 --------------------------------
def memory_compact(cfg: dict) -> None:
    """Housekeeping: session digests older than 30 days pile up forever. Once
    there are more than 20 of them, make ONE LLM call distilling those old
    blocks into a compact '## distilled' section and rewrite memory.md with
    only newer content + the distillation. Never raises."""
    try:
        if not MEMORY.exists():
            return
        lines = MEMORY.read_text().splitlines()
        cutoff = datetime.now() - timedelta(days=30)
        old_idx = []  # header indexes of stale digest blocks
        for i, ln in enumerate(lines):
            m = re.match(r"### session digest (\d{4}-\d{2}-\d{2})", ln)
            if not m:
                continue
            try:
                if datetime.strptime(m.group(1), "%Y-%m-%d") < cutoff:
                    old_idx.append(i)
            except ValueError:
                continue
        if len(old_idx) <= 20:
            return
        # each digest block runs from its header to the next heading line
        spans = []
        for start in old_idx:
            def _is_block_end(k):
                ln = lines[k]
                # stop at any heading AND at remember()-style fact lines so a
                # plain fact between two old digests is never silently dropped
                return (ln.startswith("#")
                        or re.match(r"- \[\d{4}-\d{2}-\d{2}\] ", ln))
            end = next(
                (k for k in range(start + 1, len(lines)) if _is_block_end(k)),
                len(lines),
            )
            spans.append((start, end))
        old_text = "\n".join("\n".join(lines[s:e]) for s, e in spans)
        resp = _completion(
            cfg,
            [
                {
                    "role": "system",
                    "content": "These are old memory digest blocks from a "
                    "personal assistant's memory file. Distill them into ONE "
                    "compact '## distilled' section (max 1500 chars): preserve "
                    "durable facts about the user (preferences, names, plans, "
                    "corrections, environment), drop transient detail and "
                    "duplication. Output only the distilled content, no "
                    "commentary.",
                },
                {"role": "user", "content": old_text[-30000:]},
            ],
            tools=None,
            stream=False,
        )
        distilled = (resp["choices"][0]["message"].get("content") or "").strip()
        if not distilled:
            # LLM returned empty (API down / filtered) — NEVER wipe old blocks
            print("(memory compact skipped: distillation empty, keeping old digests)")
            return
        drop = set()
        for s, e in spans:
            drop.update(range(s, e))
        kept = [ln for i, ln in enumerate(lines) if i not in drop]
        while kept and not kept[-1].strip():
            kept.pop()
        new_text = "\n".join(kept)
        if distilled:
            # an old '## distilled' section is a heading, so it survives in
            # kept — merge instead of stacking duplicate sections
            marker = "\n## distilled\n"
            if marker in new_text:
                head, _, _tail = new_text.partition(marker)
                new_text = head + marker + distilled[:1500] + "\n"
            else:
                new_text += f"\n\n## distilled\n{distilled[:1500]}\n"
        # Atomic + locked: a plain write_text truncates first, so a crash
        # mid-write lost the WHOLE memory file, and it raced the appends in
        # remember()/_exit_pass (dropping the fact written meanwhile).
        with STATE_LOCK:
            tmp = MEMORY.with_suffix(".md.tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, MEMORY)
        print(f"(memory compacted: {len(old_idx)} old digests distilled)")
    except Exception:
        _log_error(traceback.format_exc())

# ---- original buddy.py lines 3245-3246 --------------------------------


# ---- original buddy.py lines 3247-3261 --------------------------------
def _memory_context() -> str:
    """Capped memory for the system prompt: the LAST 200 lines of memory.md,
    plus a pointer to how much older history exists (dig via search_memory)."""
    if not MEMORY.exists():
        return "(nothing remembered yet)"
    try:
        lines = MEMORY.read_text().splitlines()
    except Exception:
        return "(memory unreadable)"
    # Cap by chars (not just lines) so old memory cannot crowd out fresh
    # tool results against context_budget.
    tail = lines[-200:]
    older = len(lines) - len(tail)
    text = "\n".join(tail)
    if len(text) > 6000:
        text = text[-6000:]
        older = len(lines)  # truncated: point at search instead
    if older > 0:
        text += f"\n…and {older} older facts (search_memory to dig deeper)"
    return text

