import json
import logging
import shutil
from pathlib import Path
from typing import Optional

DATA_DIR = Path.home() / ".ai-switch"

log = logging.getLogger(__name__)
DATA_PATH = DATA_DIR / "data.json"
_OLD_DATA_DIR = Path.home() / ".openclaw-auto-manager"
_OLD_DATA_PATH = _OLD_DATA_DIR / "data.json"


def _migrate_old_data() -> None:
    if not DATA_PATH.exists() and _OLD_DATA_PATH.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_OLD_DATA_PATH, DATA_PATH)
        # Also migrate health cache
        old_cache = _OLD_DATA_DIR / "health_cache.json"
        if old_cache.exists():
            shutil.copy2(old_cache, DATA_DIR / "health_cache.json")
        print(f" Migrated data from {_OLD_DATA_PATH} to {DATA_PATH}")


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_data() -> dict:
    _migrate_old_data()
    _ensure_dirs()
    if DATA_PATH.exists():
        with open(DATA_PATH) as f:
            return json.load(f)
    return {"vendors": [], "settings": {"check_interval_seconds": 300}, "backends": {}}


def _save_data(data: dict) -> None:
    _ensure_dirs()
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _next_id(items: list) -> str:
    return str(max((int(i.get("id", 0)) for i in items), default=0) + 1)


def get_vendors() -> list[dict]:
    return _load_data().get("vendors", [])


def get_vendor(vendor_id: str) -> Optional[dict]:
    for v in get_vendors():
        if v["id"] == vendor_id:
            return v
    return None


def add_vendor(name: str, provider: str, api_url: str, endpoint_type: str = "openai",
               thinking_disabled: bool = False, proxy_target: str = "") -> dict:
    data = _load_data()
    vendor = {
        "id": _next_id(data.get("vendors", [])),
        "name": name,
        "provider": provider,
        "api_url": api_url.rstrip("/"),
        "endpoint_type": endpoint_type,
        "thinking_disabled": thinking_disabled,
        "proxy_target": proxy_target,
        "keys": [],
    }
    data["vendors"].append(vendor)
    _save_data(data)
    return vendor


def update_vendor(vendor_id: str, **kwargs) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            for key in ("name", "provider", "api_url", "endpoint_type", "thinking_disabled", "proxy_target"):
                if key in kwargs:
                    v[key] = kwargs[key]
            if "api_url" in kwargs:
                v["api_url"] = kwargs["api_url"].rstrip("/")
            _save_data(data)
            return v
    return None


def delete_vendor(vendor_id: str) -> Optional[dict]:
    data = _load_data()
    removed = None
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            removed = v
            break
    if not removed:
        return None
    data["vendors"] = [v for v in data["vendors"] if v["id"] != vendor_id]
    _save_data(data)
    return removed


def get_keys(vendor_id: str) -> list[dict]:
    v = get_vendor(vendor_id)
    return v.get("keys", []) if v else []


def get_key(vendor_id: str, key_id: str) -> Optional[dict]:
    for k in get_keys(vendor_id):
        if k["id"] == key_id:
            return k
    return None


def add_key(vendor_id: str, name: str, api_key: str) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            entry = {
                "id": _next_id(v.get("keys", [])),
                "name": name,
                "api_key": api_key,
                "enabled": True,
            }
            v["keys"].append(entry)
            _save_data(data)
            return entry
    return None


_KEY_FIELDS = (
    "name", "api_key", "enabled", "models", "default_model",
    "disabled_models", "model_health",
)


def model_id_of(m) -> str:
    if isinstance(m, dict):
        return str(m.get("id") or m.get("name") or "")
    return str(m or "")


def list_model_ids(key: dict) -> list[str]:
    out, seen = [], set()
    for m in key.get("models") or []:
        mid = model_id_of(m)
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    dm = key.get("default_model") or ""
    if dm and dm not in seen:
        out.append(str(dm))
    return out


def get_enabled_models(key: dict) -> list[str]:
    """Models that should be synced to backends (system keeps full models list)."""
    ids = list_model_ids(key)
    if not ids:
        return []
    disabled = set(key.get("disabled_models") or [])
    return [m for m in ids if m not in disabled]


def set_model_enabled(vendor_id: str, key_id: str, model: str, enabled: bool) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] != vendor_id:
            continue
        for k in v["keys"]:
            if k["id"] != key_id:
                continue
            disabled = list(k.get("disabled_models") or [])
            if enabled:
                disabled = [m for m in disabled if m != model]
            elif model not in disabled:
                disabled.append(model)
            k["disabled_models"] = disabled
            _save_data(data)
            return k
    return None


def update_key(vendor_id: str, key_id: str, **kwargs) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            for k in v["keys"]:
                if k["id"] == key_id:
                    for key in _KEY_FIELDS:
                        if key in kwargs:
                            k[key] = kwargs[key]
                    _save_data(data)
                    return k
    return None


def update_key_data(vendor_id: str, key_id: str, **kwargs) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            for k in v["keys"]:
                if k["id"] == key_id:
                    for key in _KEY_FIELDS:
                        if key in kwargs:
                            k[key] = kwargs[key]
                    _save_data(data)
                    return k
    return None


def delete_key(vendor_id: str, key_id: str) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            removed = None
            for k in v["keys"]:
                if k["id"] == key_id:
                    removed = k
                    break
            if removed:
                v["keys"] = [k for k in v["keys"] if k["id"] != key_id]
                _save_data(data)
                return removed
    return None


def get_settings() -> dict:
    return _load_data().get("settings", {})


def update_settings(**kwargs) -> dict:
    data = _load_data()
    data.setdefault("settings", {})
    for key in ("check_interval_seconds",):
        if key in kwargs:
            data["settings"][key] = kwargs[key]
    _save_data(data)
    return data["settings"]


def get_backend_config(backend_name: str) -> dict:
    return _load_data().get("backends", {}).get(backend_name, {})


def get_backend_configs() -> dict:
    return _load_data().get("backends", {})


def save_backend_config(backend_name: str, config: dict) -> None:
    data = _load_data()
    data.setdefault("backends", {})
    data["backends"][backend_name] = config
    _save_data(data)


# ── Usage Statistics ────────────────────────


def add_usage_record(record: dict) -> dict:
    data = _load_data()
    records = data.setdefault("usage", [])
    record["id"] = _next_id(records)
    if "success" not in record:
        record["success"] = True
    if "status_code" not in record:
        record["status_code"] = 200 if record.get("success", True) else 500
    records.append(record)
    _save_data(data)
    return record


_FAKE_SAMPLE_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "deepseek-chat", "deepseek-reasoner",
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3.5-sonnet",
    "gemini-1.5-pro", "gemini-1.5-flash",
}


def _all_key_models() -> set:
    models = set()
    for v in get_vendors():
        for k in v.get("keys", []):
            models.update(list_model_ids(k))
    return models


def _purge_stale_sample_usage() -> None:
    """Drop demo/sample usage. Detects old synthetic seed (same µs stamp + fake models)."""
    data = _load_data()
    records = data.get("usage") or []
    if not records:
        return

    kept = [r for r in records if not r.get("_sample") and not r.get("_synthetic")]
    if not kept:
        if len(kept) != len(records):
            data["usage"] = []
            _save_data(data)
            log.info("Purged %d sample usage records", len(records))
        return

    fracs = {str(r.get("timestamp") or "").split(".")[-1] for r in kept}
    usage_models = {str(r.get("model") or "") for r in kept}
    usage_models.discard("")
    key_models = _all_key_models()
    all_fake_models = bool(usage_models) and usage_models.issubset(_FAKE_SAMPLE_MODELS)
    # Seed generator used identical fractional seconds ".630931" on every row
    synthetic_stamp = len(fracs) == 1 and next(iter(fracs)) == "630931"
    no_real_model = not key_models or not (usage_models & key_models)
    # Drop entire batch when it is clearly the old demo seed
    if synthetic_stamp and all_fake_models and no_real_model:
        removed = len(kept)
        data["usage"] = [r for r in records if r.get("source") == "openclaw" or r.get("source") == "proxy"]
        # if nothing real left, empty
        if not data["usage"]:
            data["usage"] = []
        _save_data(data)
        log.info("Purged %d synthetic demo usage records", removed)
        return

    if len(kept) == len(records):
        return
    removed = len(records) - len(kept)
    data["usage"] = kept
    _save_data(data)
    log.info("Purged %d sample/stale usage records", removed)


def _normalize_model_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    # Drop common provider prefixes used by some gateways: "openai/gpt-4o" -> "gpt-4o"
    if "/" in s and not s.startswith("http"):
        left, right = s.split("/", 1)
        if right and left and not left.startswith("models"):
            if _is_provider_prefix(left):
                s = right
    return s


def _is_provider_prefix(left: str) -> bool:
    low = left.lower()
    if low in (
        "openai", "anthropic", "google", "gemini", "deepseek", "openrouter",
        "xai", "groq", "mistral", "cohere", "moonshot", "qwen", "alibaba",
        "volcengine", "doubao", "claude", "meta", "meta-llama", "google-vertex",
        "azure", "bedrock", "vertex", "z-ai", "deepseek-ai", "qwen", "meta-llama",
    ):
        return True
    # org/model style: short slug without spaces
    if low.endswith("-ai") or low.endswith("-org"):
        return True
    return False


def _norm_ts_for_compare(ts: str) -> str:
    """Normalize timestamps so Z / +00:00 / local naive compare consistently (lexicographic ISO)."""
    s = (ts or "").strip()
    if not s:
        return s
    s = s.replace("Z", "+00:00")
    # Drop timezone for string compare after converting offset-aware to comparable local-naive form
    try:
        from datetime import datetime
        if "T" in s:
            # Try parse common forms
            for fmt in (None,):
                try:
                    if s.endswith("+00:00") or (len(s) > 19 and s[19] in "+-"):
                        dt = datetime.fromisoformat(s)
                        # store as UTC naive ISO for ordering
                        if dt.tzinfo is not None:
                            from datetime import timezone
                            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                        return dt.isoformat(timespec="seconds")
                    return s[:19]
                except Exception:
                    pass
    except Exception:
        pass
    return s[:19] if len(s) >= 19 else s


def get_usage_records(from_ts: str = "", to_ts: str = "",
                      vendor_id: str = "", key_id: str = "",
                      provider: str = "", model: str = "",
                      source: str = "", auto_import: bool = True) -> list[dict]:
    _purge_stale_sample_usage()
    if auto_import:
        try:
            from core.usage_import import import_openclaw_usage
            # Throttle: import at most once per minute unless empty
            data0 = _load_data()
            last = (data0.get("settings") or {}).get("usage_imported_at", "")
            need = not (data0.get("usage") or [])
            if last:
                try:
                    from datetime import datetime, timezone
                    prev = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if prev.tzinfo is None:
                        prev = prev.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - prev).total_seconds()
                    need = need or age > 60
                except Exception:
                    need = True
            else:
                need = True
            if need:
                import_openclaw_usage()
        except Exception as e:
            log.warning("OpenClaw usage import skipped: %s", e)

    data = _load_data()
    records = data.get("usage", [])
    from_n = _norm_ts_for_compare(from_ts) if from_ts else ""
    to_n = _norm_ts_for_compare(to_ts) if to_ts else ""
    filtered = []
    for r in records:
        if r.get("_sample") or r.get("_synthetic"):
            continue
        rts = _norm_ts_for_compare(str(r.get("timestamp", "") or ""))
        if from_n and rts and rts < from_n:
            continue
        if to_n and rts and rts > to_n:
            continue
        if vendor_id and str(r.get("vendor_id", "")) != str(vendor_id):
            continue
        if key_id and str(r.get("key_id", "")) != str(key_id):
            continue
        if provider and r.get("provider", "") != provider:
            continue
        if source:
            rs = str(r.get("source") or "unknown")
            if rs != str(source):
                continue
        if model:
            want = _normalize_model_name(str(model)) or str(model)
            raw = str(r.get("model", "") or "")
            got = _normalize_model_name(raw) or raw
            if got != want and raw != str(model):
                if not (raw.endswith("/" + want) or want.endswith("/" + raw) or raw == want):
                    continue
        filtered.append(r)
    filtered.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    # Fill missing/zero cost with token-based estimate (display + aggregation)
    try:
        from core.pricing import resolve_record_cost
        for _r in filtered:
            if not (_r.get("cost") or 0):
                _r["cost"] = resolve_record_cost(_r)
                _r["_cost_estimated"] = True
    except Exception:
        pass
    return filtered


def get_usage_summary(from_ts: str = "", to_ts: str = "",
                      group_by: str = "vendor",
                      vendor_id: str = "", key_id: str = "",
                      provider: str = "", model: str = "",
                      source: str = "") -> list[dict]:
    """Summarize usage grouped by vendor, key, provider, model, or source (backend engine)."""
    records = get_usage_records(
        from_ts, to_ts, vendor_id=vendor_id, key_id=key_id,
        provider=provider, model=model, source=source, auto_import=False,
    )
    key_models = _all_key_models() if group_by == "model" else set()
    groups = {}
    for r in records:
        if group_by == "vendor":
            gkey = str(r.get("vendor_id", r.get("vendor_name", "unknown")))
            name = r.get("vendor_name", gkey) or gkey
        elif group_by == "key":
            gkey = str(r.get("key_id", r.get("key_name", "unknown")))
            name = r.get("key_name", gkey) or gkey
        elif group_by == "provider":
            gkey = r.get("provider", "unknown") or "unknown"
            name = gkey
        elif group_by == "model":
            raw = r.get("model", "") or "unknown"
            gkey = _normalize_model_name(raw) or raw or "unknown"
            name = gkey
        elif group_by == "source":
            gkey = str(r.get("source") or "unknown")
            name = {
                "openclaw": "OpenClaw",
                "proxy": "Proxy",
                "unknown": "Unknown",
            }.get(gkey, gkey)
        else:
            gkey = str(r.get("vendor_id", "unknown"))
            name = r.get("vendor_name", gkey)
        if gkey not in groups:
            groups[gkey] = {
                "id": gkey,
                "name": name,
                "vendor_id": r.get("vendor_id", ""),
                "vendor_name": r.get("vendor_name", ""),
                "key_id": r.get("key_id", "") if group_by == "key" else "",
                "key_name": r.get("key_name", "") if group_by == "key" else "",
                "model": gkey if group_by == "model" else "",
                "provider": r.get("provider", ""),
                "source": gkey if group_by == "source" else (r.get("source") or ""),
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_cost": 0.0,
                "count": 0,
                "success_count": 0,
                "fail_count": 0,
            }
        g = groups[gkey]
        g["total_tokens"] += r.get("total_tokens", 0) or 0
        g["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        g["completion_tokens"] += r.get("completion_tokens", 0) or 0
        g["total_cost"] += r.get("cost", 0) or 0
        g["count"] += 1
        ok = r.get("success", True)
        if ok:
            g["success_count"] += 1
        else:
            g["fail_count"] += 1
    result = []
    for g in groups.values():
        g["total_cost"] = round(g["total_cost"], 6)
        g["success_rate"] = round(g["success_count"] / g["count"] * 100, 1) if g["count"] else 0
        result.append(g)

    # When grouping by model, only show models that appear in usage (already true).
    # Prefer matching inventory names so used models are recognizable.
    if group_by == "model" and key_models:
        inv_lower = {m.lower(): m for m in key_models}
        for g in result:
            mid = str(g.get("name") or g.get("id") or "")
            if mid in key_models:
                continue
            # exact case-insensitive
            if mid.lower() in inv_lower:
                canon = inv_lower[mid.lower()]
                g["id"] = canon
                g["name"] = canon
                g["model"] = canon
                continue
            # suffix match: usage "xxx/gpt-4o" vs inventory "gpt-4o"
            for inv in key_models:
                if mid.endswith("/" + inv) or inv.endswith("/" + mid) or mid.endswith(inv) and len(inv) > 4:
                    g["id"] = inv
                    g["name"] = inv
                    g["model"] = inv
                    break
        # merge buckets that collapsed to same model id after canonicalization
        merged = {}
        for g in result:
            mid = g["id"]
            if mid not in merged:
                merged[mid] = g
            else:
                m = merged[mid]
                m["total_tokens"] += g["total_tokens"]
                m["prompt_tokens"] += g["prompt_tokens"]
                m["completion_tokens"] += g["completion_tokens"]
                m["total_cost"] = round(m["total_cost"] + g["total_cost"], 6)
                m["count"] += g["count"]
                m["success_count"] += g["success_count"]
                m["fail_count"] += g["fail_count"]
                m["success_rate"] = round(m["success_count"] / m["count"] * 100, 1) if m["count"] else 0
        result = list(merged.values())

    return sorted(result, key=lambda x: x["count"], reverse=True)
