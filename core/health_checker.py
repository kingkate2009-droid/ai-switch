import json
import threading
import time
from datetime import datetime, timezone

from pathlib import Path

from core.data import (
    get_enabled_models,
    get_vendor,
    get_vendors,
    list_model_ids,
    update_key_data,
)
from core.providers import (
    get_provider,
    pick_default_model,
    probe_provider,
    probe_single_model,
    scan_models,
)
from backends import reconcile_all, on_key_updated, on_key_removed

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


def _resolve_check_type(vendor: dict) -> str:
    endpoint_type = (vendor.get("endpoint_type") or "").lower().strip()
    if endpoint_type in ("openai", "openai_chat"):
        return "openai_chat"
    if endpoint_type in ("anthropic", "claude"):
        return "anthropic"
    if endpoint_type in ("google", "gemini"):
        return "gemini"
    if endpoint_type:
        # Unknown explicit type: still try openai-compatible chat
        return "openai_chat"

    # Prefer URL heuristics for custom/proxy endpoints (before defaulting provider)
    url = (vendor.get("proxy_target") or vendor.get("api_url") or "").lower()
    if "/anthropic" in url or "api.anthropic.com" in url or "/claude" in url:
        return "anthropic"
    if "generativelanguage.googleapis.com" in url or "/gemini" in url:
        return "gemini"

    provider_id = (vendor.get("provider") or "").strip()
    if provider_id:
        prov = get_provider(provider_id)
        if prov:
            return prov["check_type"]
    return "openai_chat"


def check_key_health(vendor_id: str, key_id: str, scan_models_flag: bool = True) -> dict:
    """Key-level health check (list detection). Does not disable individual models."""
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
    check_type = _resolve_check_type(vendor)

    # Key-level check: try any known model (full inventory), not only enabled ones
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
        # Merge scan results with existing inventory so we never wipe known models
        if models:
            existing = list_model_ids(key_entry)
            merged, seen = [], set()
            for mid in list(models) + existing:
                if mid and mid not in seen:
                    seen.add(mid)
                    merged.append(mid)
            models = merged
        if not default_model and models:
            default_model = pick_default_model(models)
        elif default_model and models and default_model not in models:
            # keep previous default if still in inventory after merge
            pass

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


def check_key_models(vendor_id: str, key_id: str) -> dict:
    """Probe each model on a key. Failures are disabled for backends only (kept in system)."""
    vendor = get_vendor(vendor_id)
    if not vendor:
        return {"error": "Vendor not found", "results": []}

    key_entry = None
    for k in vendor.get("keys", []):
        if k["id"] == key_id:
            key_entry = k
            break
    if not key_entry:
        return {"error": "Key not found", "results": []}

    api_url = vendor.get("proxy_target", "") or vendor.get("api_url", "")
    api_key = key_entry.get("api_key", "")
    check_type = _resolve_check_type(vendor)
    models = list_model_ids(key_entry)

    # If inventory empty, try scanning once (system retains scan results)
    if not models:
        scanned = scan_models(check_type, api_url, api_key)
        if scanned:
            default_model = pick_default_model(scanned)
            update_key_data(vendor_id, key_id, models=scanned, default_model=default_model)
            key_entry = get_vendor(vendor_id)
            key_entry = next((k for k in (key_entry or {}).get("keys", []) if k["id"] == key_id), key_entry)
            models = list_model_ids(key_entry or {})

    results = []
    model_health = dict(key_entry.get("model_health") or {})
    disabled = set(key_entry.get("disabled_models") or [])
    ok_models = []
    fail_models = []

    for mid in models:
        start = time.time()
        healthy, msg = probe_single_model(check_type, api_url, api_key, mid)
        latency_ms = int((time.time() - start) * 1000)
        entry = {
            "model": mid,
            "healthy": healthy,
            "latency_ms": latency_ms,
            "message": msg if healthy else None,
            "error": None if healthy else msg,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        results.append(entry)
        model_health[mid] = {
            "healthy": healthy,
            "latency_ms": latency_ms,
            "error": None if healthy else msg,
            "message": msg if healthy else None,
            "checked_at": entry["checked_at"],
        }
        if healthy:
            ok_models.append(mid)
            disabled.discard(mid)
        else:
            fail_models.append(mid)
            disabled.add(mid)

    # Persist: keep full models list in system; failed models only go into disabled_models
    updates = {
        "disabled_models": sorted(disabled),
        "model_health": model_health,
    }
    # Prefer a working enabled model as default
    enabled_ok = [m for m in ok_models if m not in disabled]
    if enabled_ok:
        updates["default_model"] = enabled_ok[0]
        if key_entry.get("enabled", True) is False and ok_models:
            updates["enabled"] = True
    elif ok_models:
        updates["default_model"] = ok_models[0]

    updated = update_key_data(vendor_id, key_id, **updates) or key_entry

    # Backend engines: sync enabled models only (failed auto-removed via disabled_models)
    if updated.get("enabled", True):
        if get_enabled_models(updated):
            on_key_updated(vendor, updated)
        else:
            # No healthy/enabled models left → drop key models from backends, keep in system
            on_key_removed(vendor, updated)
    else:
        on_key_removed(vendor, updated)

    reconcile_all()

    return {
        "vendor_id": vendor_id,
        "key_id": key_id,
        "ok": len(ok_models),
        "fail": len(fail_models),
        "ok_models": ok_models,
        "fail_models": fail_models,
        "disabled_models": sorted(disabled),
        "enabled_models": get_enabled_models(updated),
        "results": results,
        "key": updated,
    }


def is_key_backend_syncable(vendor_id: str, key: dict) -> bool:
    """Whether a key may be written into backend engine configs.

    - disabled keys: never
    - known unhealthy (health cache): never (system may still keep the key)
    - healthy / not-yet-checked: yes (unchecked kept for first-push UX;
      after full check, failures are removed via on_key_removed + reconcile)
    """
    if not key or not key.get("api_key"):
        return False
    if key.get("enabled") is False:
        return False
    kid = str(key.get("id") or "")
    if not kid:
        return False
    with _lock:
        cache = _load_cache()
    h = cache.get(f"{vendor_id}:{kid}") or {}
    if h.get("healthy") is False:
        return False
    return True


def apply_health_to_backends(vendor: dict, key: dict, health: dict) -> None:
    """Push healthy keys; strip unhealthy keys from backend engines (keep in system)."""
    if not vendor or not key:
        return
    if health.get("healthy"):
        models = health.get("models", [])
        default_model = health.get("default_model", "") or key.get("default_model", "")
        updates = {}
        # Only re-enable when auto-managed success path wants it
        if key.get("enabled") is False:
            # leave disabled keys disabled in system, still don't push
            on_key_removed(vendor, key)
            return
        updates["enabled"] = True
        if models:
            updates["models"] = models
        elif not key.get("models") and default_model:
            updates["models"] = [default_model]
        if default_model:
            updates["default_model"] = default_model
        updated = update_key_data(vendor["id"], key["id"], **updates) or key
        on_key_updated(vendor, updated)
    else:
        # Always remove failed keys from backends so engines match health
        on_key_removed(vendor, key)
        try:
            from core.data import get_settings
            if bool((get_settings() or {}).get("health_auto_disable")):
                update_key_data(vendor["id"], key["id"], enabled=False)
        except Exception:
            pass


def check_all_keys(include_disabled: bool = True) -> list[dict]:
    """Probe every vendor key. By default includes disabled keys so UI won't stay '未检测'."""
    results = []

    for v in get_vendors():
        for k in v.get("keys", []):
            if not include_disabled and k.get("enabled") is False:
                continue
            health = check_key_health(v["id"], k["id"])
            results.append(health)
            # Disabled keys: record health only; never push to backends
            if k.get("enabled") is False:
                on_key_removed(v, k)
                continue
            apply_health_to_backends(v, k, health)

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
