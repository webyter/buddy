"""buddy_core.quota — what's left on each connected API's rate limit.

OpenAI-compatible endpoints have no "how much quota is left" API. What they
DO send: rate-limit headers on every response (x-ratelimit-remaining-*,
x-ratelimit-reset-*) and, on a 429, an error body that says exactly which
quota bucket died (Google: RESOURCE_EXHAUSTED + QuotaFailure details with
the quota id and a RetryInfo cooldown). Record both as they happen and
/quota renders the latest picture per endpoint+model.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import HOME

# headers worth keeping (prefix match, header names arrive lowercase)
_TRACKED_PREFIXES = ("x-ratelimit-", "anthropic-ratelimit-", "x-quota-",
                     "retry-after")

_QUOTA_LOCK = threading.RLock()  # quota.json read-modify-write
QUOTA_FILE = HOME / "quota.json"


def _load() -> dict:
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    """Atomic + private: quota data is boring, but state files here follow
    the same discipline as the rest of ~/.buddy."""
    tmp = QUOTA_FILE.with_suffix(".tmp")
    fd = os_open_private(tmp)
    with fd:
        fd.write(json.dumps(data, indent=1).encode())
    os_replace(tmp, QUOTA_FILE)


def os_open_private(tmp: Path):
    import os
    return os.fdopen(os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600), "wb")


def os_replace(tmp: Path, dst: Path) -> None:
    import os
    os.replace(str(tmp), str(dst))


def _key(base: str, model: str) -> str:
    return f"{(base or '?').rstrip('/')}|{model or '?'}"


def record_headers(base: str, model: str, headers) -> None:
    """Harvest rate-limit headers from a live API response. `headers` is any
    mapping (or email.Message — dict() it before calling if cheap)."""
    if headers is None:
        return
    try:
        items = dict(headers).items()
    except Exception:
        return
    picked = {}
    for k, v in items:
        lk = str(k).lower()
        if any(lk.startswith(p) for p in _TRACKED_PREFIXES):
            picked[lk] = str(v).strip()
    if not picked:
        return
    with _QUOTA_LOCK:
        data = _load()
        data[_key(base, model)] = {
            "ts": time.time(),
            "headers": picked,
        }
        _save(data)


def record_429(base: str, model: str, body: str) -> None:
    """A 429 just told us which quota bucket died. Remember it so /quota can
    say 'exhausted, back at <reset>' instead of a guess."""
    info: dict = {"ts": time.time()}
    try:
        parsed = json.loads(body)
        if isinstance(parsed, list) and parsed:
            # Google's OpenAI-compat endpoint wraps the error in an array
            parsed = parsed[0]
        err = parsed.get("error", {}) if isinstance(parsed, dict) else {}
        if str(err.get("status")) == "RESOURCE_EXHAUSTED" or "quota" in \
                str(err.get("message", "")).lower():
            for d in err.get("details") or []:
                if not isinstance(d, dict):
                    continue
                if "QuotaFailure" in str(d.get("@type", "")):
                    v = (d.get("violations") or [{}])[0]
                    if isinstance(v, dict):
                        qid = str(v.get("quotaId") or v.get("quotaMetric") or "")
                        if v.get("quotaValue"):
                            info["bucket_size"] = str(v["quotaValue"])
                    else:
                        qid = str(v or "")
                    if "PerDay" in qid:
                        info["daily_exhausted"] = qid
                    elif qid:
                        info.setdefault("exhausted", []).append(qid)
                if "RetryInfo" in str(d.get("@type", "")):
                    info["retry_after"] = str(d.get("retryDelay", ""))
    except Exception:
        pass  # non-JSON 429 body: still record the event itself
    with _QUOTA_LOCK:
        data = _load()
        entry = data.setdefault(_key(base, model), {"ts": info["ts"],
                                                    "headers": {}})
        entry.update(info)
        _save(data)


def report(cfg: dict) -> str:
    """The /quota view: latest known rate-limit state per endpoint+model."""
    with _QUOTA_LOCK:
        data = _load()
    if not data:
        return ("(no quota data yet — providers expose no quota API; buddy "
                "learns each one's rate limits from response headers and 429 "
                "bodies as you use it)")
    rows = []
    now = time.time()
    for key in sorted(data):
        entry = data.get(key) or {}
        base, model = key.split("|", 1)
        age_s = int(now - entry.get("ts", now))
        age = f"{age_s // 60}m ago" if age_s >= 60 else f"{age_s}s ago"
        rows.append(f"· {model} @ {base}  (seen {age})")
        h = entry.get("headers") or {}
        for name in sorted(h):
            rows.append(f"    {name}: {h[name]}")
        if entry.get("daily_exhausted"):
            rows.append(f"    ⚠ DAILY QUOTA EXHAUSTED ({entry['daily_exhausted']}"
                        + (f", bucket {entry['bucket_size']}"
                           if entry.get("bucket_size") else "")
                        + ") — resets when the provider's day rolls over")
        for qid in entry.get("exhausted") or []:
            rows.append(f"    ⚠ exhausted: {qid}")
        if entry.get("retry_after"):
            rows.append(f"    retry after: {entry['retry_after']}")
    return "\n".join(rows)
