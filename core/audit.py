"""Lightweight local audit log (ring buffer in settings-adjacent file)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.data import DATA_DIR

AUDIT_PATH = DATA_DIR / "audit.jsonl"
_MAX_LINES = 500
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(action: str, **meta: Any) -> None:
    """Append one audit event. Never raises to callers."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        row = {"at": _now(), "action": action}
        for k, v in meta.items():
            if v is None:
                continue
            # avoid huge payloads / secrets
            if k in ("api_key", "token", "access_token", "password"):
                continue
            if isinstance(v, str) and len(v) > 500:
                v = v[:500] + "…"
            row[k] = v
        line = json.dumps(row, ensure_ascii=False)
        with _lock:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _trim_locked()
    except Exception:
        pass


def _trim_locked() -> None:
    try:
        if not AUDIT_PATH.exists():
            return
        # cheap size guard
        if AUDIT_PATH.stat().st_size < 256_000:
            return
        with open(AUDIT_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= _MAX_LINES:
            return
        keep = lines[-_MAX_LINES:]
        tmp = AUDIT_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        tmp.replace(AUDIT_PATH)
    except Exception:
        pass


def list_events(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    if not AUDIT_PATH.exists():
        return []
    try:
        with _lock:
            with open(AUDIT_PATH, encoding="utf-8") as f:
                lines = f.readlines()
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        out.reverse()  # newest first
        return out
    except Exception:
        return []
