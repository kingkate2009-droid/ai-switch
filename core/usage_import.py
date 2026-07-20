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


def _slug_id(value: str) -> str:
    """OpenCode-style provider id: lowercase, non-alnum -> hyphen."""
    import re
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-").lower()
    return s


def _build_provider_index() -> dict:
    """Map engine provider ids / ocp_key / vendor.provider / name -> (vendor, key)."""
    idx = {}
    for v in get_vendors():
        pname = (v.get("provider") or "").lower()
        if pname:
            idx[pname] = (v, None)
            slug = _slug_id(pname)
            if slug:
                idx[slug] = (v, None)
        vname = (v.get("name") or "").lower()
        if vname and vname not in idx:
            idx[vname] = (v, None)
        vslug = _slug_id(v.get("name") or "")
        if vslug and vslug not in idx:
            idx[vslug] = (v, None)
        for k in v.get("keys") or []:
            ocp = f"{v.get('provider')}@{k.get('name')}".lower()
            idx[ocp] = (v, k)
            # also bare key name under provider
            idx[f"{pname}@{str(k.get('name') or '').lower()}"] = (v, k)
            if k.get("api_key"):
                idx[f"key:{k['api_key']}"] = (v, k)
    return idx


def _match_vendor_key(provider: str, index: dict) -> tuple[Optional[dict], Optional[dict]]:
    if not provider:
        return None, None
    p = provider.strip()
    low = p.lower()
    if low in index:
        return index[low]
    slug = _slug_id(p)
    if slug and slug in index:
        return index[slug]
    # strip @suffix variants already full
    if "@" in low:
        base = low.split("@", 1)[0]
        if base in index:
            v, k = index[base]
            return v, k
        bslug = _slug_id(base)
        if bslug and bslug in index:
            return index[bslug]
    # hyphen/underscore normalize
    alt = low.replace("_", "-")
    if alt in index:
        return index[alt]
    alt2 = low.replace("-", "_")
    if alt2 in index:
        return index[alt2]
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


# ── OpenCode (SQLite) ──────────────────────────────────────

OPENCODE_DB_PATHS = (
    Path.home() / ".local" / "share" / "opencode" / "opencode.db",
    Path.home() / "Library" / "Application Support" / "opencode" / "opencode.db",
)


def _find_opencode_db() -> Optional[Path]:
    for p in OPENCODE_DB_PATHS:
        if p.exists() and p.is_file():
            return p
    return None


def _attach_vendor_fields(rec: dict, index: dict) -> None:
    v, k = _match_vendor_key(rec.get("provider") or "", index)
    if v:
        rec["vendor_id"] = v.get("id", "")
        rec["vendor_name"] = v.get("name", "")
        rec["provider"] = v.get("provider") or rec.get("provider") or ""
    else:
        rec["vendor_id"] = ""
        rec["vendor_name"] = rec.get("provider") or rec.get("source") or "unknown"
        # also try name-like provider id against vendor names in index keys
    if k:
        rec["key_id"] = k.get("id", "")
        rec["key_name"] = k.get("name", "")
    elif v:
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


def _extract_opencode_message(msg_id: str, data_obj: dict) -> Optional[dict]:
    """Build a usage record from an OpenCode assistant message JSON blob."""
    if not isinstance(data_obj, dict):
        return None
    if data_obj.get("role") and data_obj.get("role") != "assistant":
        return None

    tokens = data_obj.get("tokens") if isinstance(data_obj.get("tokens"), dict) else {}
    prompt = int(tokens.get("input") or tokens.get("prompt") or 0)
    completion = int(tokens.get("output") or tokens.get("completion") or 0)
    reasoning = int(tokens.get("reasoning") or 0)
    total = int(tokens.get("total") or 0)
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    cache_read = int((cache or {}).get("read") or 0)
    cache_write = int((cache or {}).get("write") or 0)
    if not total:
        total = prompt + completion + reasoning + cache_read + cache_write

    # skip empty assistant shells (no tokens and no cost)
    cost = data_obj.get("cost")
    try:
        cost_f = float(cost or 0)
    except Exception:
        cost_f = 0.0
    if total <= 0 and cost_f <= 0:
        # still keep errors as zero-token failures if present
        if not data_obj.get("error"):
            return None

    model = (
        data_obj.get("modelID")
        or data_obj.get("modelId")
        or (data_obj.get("model") or {}).get("modelID")
        or data_obj.get("model")
        or ""
    )
    if isinstance(model, dict):
        model = model.get("id") or model.get("modelID") or model.get("name") or ""
    provider = (
        data_obj.get("providerID")
        or data_obj.get("providerId")
        or (data_obj.get("model") or {}).get("providerID")
        or data_obj.get("provider")
        or ""
    )
    if isinstance(provider, dict):
        provider = provider.get("id") or provider.get("providerID") or ""

    tinfo = data_obj.get("time") if isinstance(data_obj.get("time"), dict) else {}
    created = tinfo.get("created") or tinfo.get("completed")
    completed = tinfo.get("completed")
    elapsed_ms = 0
    try:
        if created and completed:
            elapsed_ms = max(0, int(completed) - int(created))
    except Exception:
        elapsed_ms = 0

    err = data_obj.get("error")
    success = not bool(err)
    err_msg = ""
    if isinstance(err, dict):
        err_msg = str((err.get("data") or {}).get("message") or err.get("name") or err)[:300]
    elif err:
        err_msg = str(err)[:300]

    if not cost_f and total > 0:
        try:
            from core.pricing import estimate_cost
            cost_f = estimate_cost(str(model or ""), prompt, completion, total)
        except Exception:
            cost_f = 0.0

    return {
        "timestamp": _ts_to_iso(created or completed),
        "provider": str(provider or ""),
        "model": _normalize_model_name(str(model)) or str(model),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost": float(cost_f or 0),
        "success": success,
        "status_code": 200 if success else 500,
        "error": err_msg,
        "source": "opencode",
        "source_id": f"opencode:{msg_id}",
        "elapsed_ms": elapsed_ms,
    }


def import_opencode_usage(db_path: Optional[Path] = None, max_messages: int = 20000) -> dict:
    """Import usage from OpenCode local SQLite (message.data JSON)."""
    import sqlite3

    path = db_path or _find_opencode_db()
    if not path:
        return {"added": 0, "scanned": 0, "db": None, "total": 0, "skipped": "db_not_found"}

    purge_synthetic_usage()
    data = _load_data()
    records = data.setdefault("usage", [])
    seen = {str(r.get("source_id")) for r in records if r.get("source_id")}
    for r in records:
        if r.get("source") == "opencode" and r.get("source_id"):
            continue
        fp = f"opencode|{r.get('timestamp')}|{r.get('model')}|{r.get('total_tokens')}|{r.get('provider')}"
        seen.add(fp)

    index = _build_provider_index()
    added = 0
    scanned = 0
    try:
        uri = f"file:{path}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        # newest first; cap to avoid huge first import hangs
        rows = con.execute(
            "SELECT id, data FROM message ORDER BY time_created DESC LIMIT ?",
            (int(max_messages),),
        )
        for row in rows:
            scanned += 1
            mid = str(row["id"] or "")
            try:
                blob = json.loads(row["data"] or "{}")
            except Exception:
                continue
            rec = _extract_opencode_message(mid, blob)
            if not rec:
                continue
            sid = rec["source_id"]
            if sid in seen:
                continue
            fp = f"opencode|{rec['timestamp']}|{rec['model']}|{rec['total_tokens']}|{rec['provider']}"
            if fp in seen:
                continue
            _attach_vendor_fields(rec, index)
            rec["id"] = str(max((int(x.get("id", 0) or 0) for x in records), default=0) + 1)
            records.append(rec)
            seen.add(sid)
            seen.add(fp)
            added += 1
        con.close()
    except Exception as e:
        log.warning("OpenCode usage import failed: %s", e)
        return {"added": 0, "scanned": scanned, "db": str(path), "total": len(records), "error": str(e)}

    if added:
        data["usage"] = records
        data.setdefault("settings", {})["usage_imported_at"] = datetime.now(timezone.utc).isoformat()
        _save_data(data)
        log.info("Imported %d OpenCode usage records (scanned %d messages from %s)", added, scanned, path)

    return {
        "added": added,
        "scanned": scanned,
        "db": str(path),
        "total": len(records),
    }


def import_all_usage() -> dict:
    """Import usage from all known backend engines."""
    purged = purge_synthetic_usage()
    oc = import_openclaw_usage()
    opc = import_opencode_usage()
    return {
        "purged": purged,
        "added": int(oc.get("added") or 0) + int(opc.get("added") or 0),
        "openclaw": oc,
        "opencode": opc,
    }
