"""Persistent health-check run history (JSONL under ~/.ai-switch/)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path.home() / ".ai-switch"
HISTORY_PATH = DATA_DIR / "health_history.jsonl"
MAX_RUNS = 200
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def append_run(record: dict) -> dict:
    """Append one run summary. Returns the stored record (with id)."""
    _ensure()
    rec = dict(record or {})
    rec.setdefault("id", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    rec.setdefault("finished_at", _now_iso())
    line = json.dumps(rec, ensure_ascii=False)
    with _lock:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # trim if too large
        _trim_unlocked()
    return rec


def _trim_unlocked() -> None:
    if not HISTORY_PATH.exists():
        return
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_RUNS:
            return
        keep = lines[-MAX_RUNS:]
        tmp = HISTORY_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        tmp.replace(HISTORY_PATH)
    except Exception:
        pass


def list_runs(*, limit: int = 50, offset: int = 0) -> dict:
    """Return newest-first run summaries (without full per-key detail by default)."""
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    items = _read_all()
    items.reverse()  # newest first
    total = len(items)
    slice_ = items[offset: offset + limit]
    # strip heavy details for list view unless small
    out = []
    for it in slice_:
        row = {k: v for k, v in it.items() if k != "results"}
        row["has_details"] = bool(it.get("results"))
        out.append(row)
    return {"total": total, "offset": offset, "limit": limit, "runs": out}


def get_run(run_id: str) -> Optional[dict]:
    rid = str(run_id or "").strip()
    if not rid:
        return None
    for it in _read_all():
        if str(it.get("id")) == rid:
            return it
    return None


def clear_history() -> int:
    with _lock:
        n = 0
        if HISTORY_PATH.exists():
            try:
                with open(HISTORY_PATH, encoding="utf-8") as f:
                    n = sum(1 for _ in f)
            except Exception:
                n = 0
            try:
                HISTORY_PATH.unlink()
            except Exception:
                pass
        return n


def _read_all() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    items: list[dict] = []
    with _lock:
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            items.append(obj)
                    except Exception:
                        continue
        except Exception:
            return []
    return items


def summarize_results(results: list[dict]) -> dict[str, Any]:
    ok = fail = unknown = skipped = 0
    failures = []
    for r in results or []:
        if r.get("skipped"):
            skipped += 1
            continue
        h = r.get("healthy")
        if h is True:
            ok += 1
        elif h is False:
            fail += 1
            if len(failures) < 20:
                failures.append({
                    "vendor_id": r.get("vendor_id"),
                    "key_id": r.get("key_id"),
                    "error": (r.get("error") or "")[:200],
                    "latency_ms": r.get("latency_ms"),
                })
        else:
            unknown += 1
    probed = ok + fail + unknown
    return {
        "total": probed + skipped,
        "probed": probed,
        "ok": ok,
        "fail": fail,
        "unknown": unknown,
        "skipped": skipped,
        "failures": failures,
    }
