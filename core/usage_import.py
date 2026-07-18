"""Import real usage records from OpenClaw session transcripts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.data import (
    _all_key_models,
    _load_data,
    _normalize_model_name,
    _save_data,
    get_vendors,
)

log = logging.getLogger(__name__)

OPENCLAW_SESSIONS_DIR = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
_FAKE_FRAC = "630931"
_FAKE_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "deepseek-chat", "deepseek-reasoner",
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3.5-sonnet",
    "gemini-1.5-pro", "gemini-1.5-flash",
}


def purge_synthetic_usage() -> int:
    """Remove clearly synthetic demo usage (identical sub-second stamps + fake model set)."""
    data = _load_data()
    records = data.get("usage") or []
    if not records:
        return 0

    fracs = {str(r.get("timestamp") or "").split(".")[-1] for r in records}
    models = {str(r.get("model") or "") for r in records}
    models.discard("")
    key_models = _all_key_models()
    # Heuristic: every record shares one synthetic microsecond stamp AND models are demo-only
    synthetic = (
        len(fracs) == 1
        and next(iter(fracs)) == _FAKE_FRAC
        and bool(models)
        and models.issubset(_FAKE_MODELS)
        and (not key_models or not (models & key_models))
    )
    # Or explicitly marked samples
    kept = []
    removed = 0
    for r in records:
        if r.get("_sample") or r.get("_synthetic"):
            removed += 1
            continue
        if synthetic:
            removed += 1
            continue
        kept.append(r)
    if removed:
        data["usage"] = kept
        _save_data(data)
        log.info("Purged %d synthetic/sample usage records", removed)
    return removed


def _ts_to_iso(ts) -> str:
    if ts is None:
        return datetime.now().isoformat()
    if isinstance(ts, (int, float)):
        # ms or s
        val = float(ts)
        if val > 1e12:
            val = val / 1000.0
        return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
    s = str(ts).strip()
    if not s:
        return datetime.now().isoformat()
    # already iso-ish
    return s.replace("Z", "+00:00") if s.endswith("Z") else s


def _build_provider_index() -> dict:
    """Map openclaw provider / ocp_key / vendor.provider -> (vendor, key)."""
    idx = {}
    for v in get_vendors():
        pname = (v.get("provider") or "").lower()
        if pname:
            idx[pname] = (v, None)
        for k in v.get("keys") or []:
            ocp = f"{v.get('provider')}@{k.get('name')}".lower()
            idx[ocp] = (v, k)
            # also bare key name under provider
            idx[f"{pname}@{str(k.get('name') or '').lower()}"] = (v, k)
            if k.get("api_key"):
                idx[f"key:{k['api_key']}"] = (v, k)
        # vendor name as weak fallback
        vname = (v.get("name") or "").lower()
        if vname and vname not in idx:
            idx[vname] = (v, None)
    return idx


def _match_vendor_key(provider: str, index: dict) -> tuple[Optional[dict], Optional[dict]]:
    if not provider:
        return None, None
    p = provider.strip()
    low = p.lower()
    if low in index:
        return index[low]
    # strip @suffix variants already full
    if "@" in low:
        base = low.split("@", 1)[0]
        if base in index:
            v, k = index[base]
            # try exact ocp again with original casing key part
            return v, k
    return None, None


def _iter_session_files(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.exists():
        return []
    files = []
    for p in sessions_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        # active + reset archives (contain real history)
        if name.endswith(".jsonl") or ".jsonl.reset." in name:
            if name.endswith(".trajectory.jsonl"):
                continue
            files.append(p)
    return files


def _extract_from_line(obj: dict) -> Optional[dict]:
    """Extract one usage record from an OpenClaw session jsonl object."""
    if not isinstance(obj, dict):
        return None
    msg = None
    if obj.get("type") == "message" and isinstance(obj.get("message"), dict):
        msg = obj["message"]
    elif isinstance(obj.get("message"), dict) and obj["message"].get("usage"):
        msg = obj["message"]
    if not msg or msg.get("role") != "assistant":
        return None
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None

    model = msg.get("model") or obj.get("model") or ""
    provider = msg.get("provider") or obj.get("provider") or ""
    if not model and not usage:
        return None

    prompt = usage.get("input") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("output") or usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total = usage.get("totalTokens") or usage.get("total_tokens") or 0
    if not total:
        total = (prompt or 0) + (completion or 0)

    cost = 0.0
    c = usage.get("cost")
    if isinstance(c, dict):
        cost = float(c.get("total") or 0)
    elif isinstance(c, (int, float)):
        cost = float(c)
    if not cost:
        try:
            from core.pricing import estimate_cost
            cost = estimate_cost(
                str(model or ""),
                int(prompt or 0),
                int(completion or 0),
                int(total or 0),
            )
        except Exception:
            pass

    msg_id = obj.get("id") or msg.get("responseId") or msg.get("id") or ""
    ts = obj.get("timestamp") or msg.get("timestamp")
    stop = msg.get("stopReason") or ""
    success = stop not in ("error", "aborted") if stop else True

    return {
        "timestamp": _ts_to_iso(ts),
        "provider": provider,
        "model": _normalize_model_name(str(model)) or str(model),
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": int(total or 0),
        "cost": float(cost or 0),
        "success": bool(success),
        "status_code": 200 if success else 500,
        "error": "" if success else (stop or "error"),
        "source": "openclaw",
        "source_id": str(msg_id or f"{provider}:{model}:{ts}:{total}"),
        "elapsed_ms": 0,
    }


def import_openclaw_usage(sessions_dir: Optional[Path] = None, max_files: int = 40) -> dict:
    """Scan OpenClaw session transcripts and append new usage records."""
    purge_synthetic_usage()

    root = sessions_dir or OPENCLAW_SESSIONS_DIR
    data = _load_data()
    records = data.setdefault("usage", [])
    seen = {str(r.get("source_id")) for r in records if r.get("source_id")}
    # also fingerprint older records without source_id
    for r in records:
        if r.get("source") == "openclaw" and r.get("source_id"):
            continue
        fp = f"{r.get('timestamp')}|{r.get('model')}|{r.get('total_tokens')}|{r.get('provider')}"
        seen.add(fp)

    index = _build_provider_index()
    files = sorted(_iter_session_files(root), key=lambda p: p.stat().st_mtime, reverse=True)
    if max_files:
        files = files[:max_files]

    added = 0
    scanned = 0
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    scanned += 1
                    rec = _extract_from_line(obj)
                    if not rec:
                        continue
                    sid = rec["source_id"]
                    if sid in seen:
                        continue
                    fp = f"{rec['timestamp']}|{rec['model']}|{rec['total_tokens']}|{rec['provider']}"
                    if fp in seen:
                        continue

                    v, k = _match_vendor_key(rec.get("provider") or "", index)
                    if v:
                        rec["vendor_id"] = v.get("id", "")
                        rec["vendor_name"] = v.get("name", "")
                        rec["provider"] = v.get("provider") or rec.get("provider") or ""
                    else:
                        rec["vendor_id"] = ""
                        rec["vendor_name"] = rec.get("provider") or "openclaw"
                    if k:
                        rec["key_id"] = k.get("id", "")
                        rec["key_name"] = k.get("name", "")
                    else:
                        # provider-level match: pick first enabled key
                        if v:
                            keys = [x for x in (v.get("keys") or []) if x.get("enabled", True)]
                            if keys:
                                rec["key_id"] = keys[0].get("id", "")
                                rec["key_name"] = keys[0].get("name", "")
                            else:
                                rec["key_id"] = ""
                                rec["key_name"] = ""
                        else:
                            rec["key_id"] = ""
                            rec["key_name"] = ""

                    rec["id"] = str(max((int(x.get("id", 0) or 0) for x in records), default=0) + 1)
                    records.append(rec)
                    seen.add(sid)
                    seen.add(fp)
                    added += 1
        except Exception as e:
            log.warning("Failed reading session %s: %s", path, e)

    if added:
        data["usage"] = records
        data.setdefault("settings", {})["usage_imported_at"] = datetime.now(timezone.utc).isoformat()
        _save_data(data)
        log.info("Imported %d OpenClaw usage records (scanned lines w/ usage: %d)", added, scanned)

    return {"added": added, "scanned": scanned, "files": len(files), "total": len(records)}
