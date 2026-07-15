import json
import shutil
from pathlib import Path
from typing import Optional

DATA_DIR = Path.home() / ".ai-switch"
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


def update_key(vendor_id: str, key_id: str, **kwargs) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            for k in v["keys"]:
                if k["id"] == key_id:
                    for key in ("name", "api_key", "enabled", "models", "default_model"):
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
                    for key in ("name", "api_key", "enabled", "models", "default_model"):
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
    records.append(record)
    _save_data(data)
    return record


def get_usage_records(from_ts: str = "", to_ts: str = "",
                      vendor_id: str = "", key_id: str = "",
                      provider: str = "") -> list[dict]:
    data = _load_data()
    records = data.get("usage", [])
    filtered = []
    for r in records:
        if from_ts and r.get("timestamp", "") < from_ts:
            continue
        if to_ts and r.get("timestamp", "") > to_ts:
            continue
        if vendor_id and r.get("vendor_id", "") != vendor_id:
            continue
        if key_id and r.get("key_id", "") != key_id:
            continue
        if provider and r.get("provider", "") != provider:
            continue
        filtered.append(r)
    return filtered


def get_usage_summary(from_ts: str = "", to_ts: str = "",
                      group_by: str = "vendor") -> list[dict]:
    """Summarize usage grouped by vendor, key, or backend dimension."""
    records = get_usage_records(from_ts, to_ts)
    groups = {}
    for r in records:
        if group_by == "vendor":
            key = r.get("vendor_name", r.get("vendor_id", "unknown"))
        elif group_by == "key":
            key = r.get("key_name", r.get("key_id", "unknown"))
        elif group_by == "provider":
            key = r.get("provider", "unknown")
        else:
            key = r.get("vendor_name", "unknown")
        if key not in groups:
            groups[key] = {"name": key, "total_tokens": 0,
                           "prompt_tokens": 0, "completion_tokens": 0,
                           "total_cost": 0.0, "count": 0}
        groups[key]["total_tokens"] += r.get("total_tokens", 0)
        groups[key]["prompt_tokens"] += r.get("prompt_tokens", 0)
        groups[key]["completion_tokens"] += r.get("completion_tokens", 0)
        groups[key]["total_cost"] += r.get("cost", 0)
        groups[key]["count"] += 1
    return sorted(groups.values(), key=lambda x: x["total_tokens"], reverse=True)
