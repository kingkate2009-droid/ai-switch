import json
import threading
import time
from datetime import datetime, timezone

from pathlib import Path

from core.data import get_vendor, get_vendors, update_key_data
from core.providers import get_provider, pick_default_model, probe_provider, scan_models
from backends import reconcile_all, on_key_added, on_key_removed
DATA_DIR = Path.home() / ".ai-switch"
HEALTH_CACHE_PATH = DATA_DIR / "health_cache.json"
_lock = threading.Lock()


def _load_cache() -> dict:
    if HEALTH_CACHE_PATH.exists():
        with open(HEALTH_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    with open(HEALTH_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def check_key_health(vendor_id: str, key_id: str, scan_models_flag: bool = True) -> dict:
    vendor = get_vendor(vendor_id)
    if not vendor:
        return {"key_id": key_id, "healthy": False, "latency_ms": 0, "error": "Vendor not found"}

    key_entry = None
    for k in vendor.get("keys", []):
        if k["id"] == key_id:
            key_entry = k
            break
    if not key_entry:
        return {"key_id": key_id, "healthy": False, "latency_ms": 0, "error": "Key not found"}

    api_url = vendor.get("proxy_target", "") or vendor["api_url"]
    api_key = key_entry["api_key"]
    provider_id = vendor.get("provider", "openai")
    
    # Use vendor's endpoint_type if set, otherwise get from provider registry
    endpoint_type = vendor.get("endpoint_type", "")
    if endpoint_type:
        check_type = f"openai_chat" if endpoint_type == "openai" else "anthropic"
    else:
        prov = get_provider(provider_id)
        check_type = prov["check_type"] if prov else "openai_chat"

    # Use key's existing models to test, if available
    key_models = key_entry.get("models", [])
    if key_models:
        models_to_try = [m["id"] if isinstance(m, dict) else m for m in key_models]
    else:
        models_to_try = None

    start = time.time()
    healthy, error_msg = probe_provider(check_type, api_url, api_key, models_to_try)
    latency_ms = int((time.time() - start) * 1000)

    models = []
    default_model = key_entry.get("default_model", "")
    if healthy and scan_models_flag:
        models = scan_models(check_type, api_url, api_key)
        if not default_model and models:
            default_model = pick_default_model(models)

    cache_key = f"{vendor_id}:{key_id}"
    result = {
        "key_id": key_id,
        "vendor_id": vendor_id,
        "healthy": healthy,
        "latency_ms": latency_ms,
        "error": None if healthy else error_msg,
        "message": error_msg if healthy else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
        "default_model": default_model,
    }

    with _lock:
        cache = _load_cache()
        cache[cache_key] = result
        _save_cache(cache)

    return result


def check_all_keys() -> list[dict]:
    results = []

    for v in get_vendors():
        for k in v.get("keys", []):
            health = check_key_health(v["id"], k["id"])
            results.append(health)
            if health.get("healthy"):
                models = health.get("models", [])
                default_model = health.get("default_model", "")
                updates = {"enabled": True}
                if models:
                    updates["models"] = models
                if default_model:
                    updates["default_model"] = default_model
                updated = update_key_data(v["id"], k["id"], **updates)
                if updated and updated.get("models"):
                    on_key_updated(v, updated)
                else:
                    on_key_added(v, k if not updated else updated)
            else:
                update_key_data(v["id"], k["id"], enabled=False)
                on_key_removed(v, k)

    reconcile_all()
    return results


def get_all_health_status() -> dict:
    results = {}
    with _lock:
        cache = _load_cache()
    for v in get_vendors():
        for k in v.get("keys", []):
            ck = f"{v['id']}:{k['id']}"
            results[ck] = cache.get(ck, {
                "key_id": k["id"],
                "vendor_id": v["id"],
                "healthy": None,
                "latency_ms": 0,
                "error": "Not checked yet",
            })
    return results
