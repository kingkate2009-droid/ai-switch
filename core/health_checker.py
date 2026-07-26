import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from core.data import (
    get_enabled_models,
    get_settings,
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

# Default network retries (total attempts = 1 + retries, but we treat as max attempts)
_DEFAULT_NETWORK_RETRIES = 3
_RETRY_BACKOFF_BASE = 0.6  # seconds; attempt 1 wait 0.6, then 1.2, ...


def _network_retry_attempts() -> int:
    """Max attempts for network-ish probe failures. Default 3, clamped 1..10."""
    try:
        n = int((get_settings() or {}).get("health_network_retries", _DEFAULT_NETWORK_RETRIES))
    except Exception:
        n = _DEFAULT_NETWORK_RETRIES
    return max(1, min(10, n))


def _is_network_error(msg: str) -> bool:
    """Whether a probe error looks transient/network (safe to retry)."""
    s = (msg or "").lower()
    if not s:
        return False
    # explicit auth / quota / model should not retry
    if any(x in s for x in (
        "auth failed", "http 401", "http 403", "unauthorized", "forbidden",
        "invalid api", "quota", "billing", "insufficient", "payment",
        "no compatible model", "model '", "not found",
    )):
        # 403 access blocked is not network; 403 sometimes transient but usually not
        if "blocked" in s or "auth" in s or "401" in s or "403" in s:
            return False
    needles = (
        "timeout", "timed out", "connection", "connect", "network", "dns",
        "refused", "reset", "broken pipe", "temporarily unavailable",
        "ssl", "tls", "certificate", "ssleof", "eof occurred",
        "wrong version number", "unexpected_eof", "max retries",
        "httpsconnectionpool", "connectionpool", "name resolution",
        "nodename nor servname", "temporary failure", "proxy",
        "remote end closed", "connection aborted", "read timed out",
    )
    return any(n in s for n in needles)


def classify_health_error(msg: str) -> dict:
    """Map raw probe error → {code, label_en, suggestion_en} for UI/i18n.

    code values: auth | quota | model | timeout | network | rate | ssl | other
    """
    s = (msg or "").lower()
    if not s:
        return {
            "code": "other",
            "label": "Unknown error",
            "suggestion": "Re-run the check; if it keeps failing, open Diagnostics.",
        }
    if any(x in s for x in ("401", "403", "unauthorized", "forbidden", "invalid api", "auth failed", "authentication", "api key")):
        return {
            "code": "auth",
            "label": "Auth failed (401/403)",
            "suggestion": "Check API key, endpoint URL, and whether the key is revoked.",
        }
    if any(x in s for x in ("429", "rate limit", "too many requests", "rate_limit")):
        return {
            "code": "rate",
            "label": "Rate limited (429)",
            "suggestion": "Slow down requests or switch to another key; retry later.",
        }
    if any(x in s for x in ("quota", "billing", "insufficient", "payment", "balance", "credit", "exceeded your current quota")):
        return {
            "code": "quota",
            "label": "Quota / billing",
            "suggestion": "Top up balance or wait for quota reset; disable this key if spent.",
        }
    if any(x in s for x in ("ssl", "tls", "certificate", "ssleof", "wrong version number", "unexpected_eof")):
        return {
            "code": "ssl",
            "label": "SSL / TLS error",
            "suggestion": "Check proxy/MITM, system time, and HTTPS base URL.",
        }
    if any(x in s for x in ("timeout", "timed out", "read timed out")):
        return {
            "code": "timeout",
            "label": "Timeout",
            "suggestion": "Network is slow or the endpoint is unreachable; retry or check proxy.",
        }
    if any(x in s for x in (
        "connection", "connect", "network", "dns", "refused", "reset",
        "name resolution", "nodename", "proxy", "max retries", "httpsconnectionpool",
    )):
        return {
            "code": "network",
            "label": "Network error",
            "suggestion": "Check internet, DNS, firewall, and proxy settings.",
        }
    if any(x in s for x in ("model", "not found", "compatible", "unsupported", "does not exist", "no such model")):
        return {
            "code": "model",
            "label": "Model / channel issue",
            "suggestion": "Pick another check model or rescan models for this key.",
        }
    return {
        "code": "other",
        "label": "Check failed",
        "suggestion": "See raw error detail; re-check after fixing endpoint or key.",
    }


def _enrich_health_result(result: dict) -> dict:
    """Attach error_code / error_label / suggestion when unhealthy."""
    if not isinstance(result, dict):
        return result
    if result.get("healthy") is True:
        result.setdefault("error_code", None)
        result.setdefault("suggestion", None)
        return result
    raw = result.get("error") or result.get("message") or ""
    info = classify_health_error(str(raw))
    result["error_code"] = info.get("code")
    result["error_label"] = info.get("label")
    result["suggestion"] = info.get("suggestion")
    return result


def _probe_with_retry(check_type: str, api_url: str, api_key: str, models_to_try=None) -> tuple:
    """Call probe_provider; retry only on network-ish failures."""
    attempts = _network_retry_attempts()
    last_err = ""
    healthy, msg = False, ""
    for i in range(attempts):
        healthy, msg = probe_provider(check_type, api_url, api_key, models_to_try)
        if healthy:
            if i > 0:
                # annotate success after retry for UI/debug
                msg = (msg or "") + f" (retry {i + 1}/{attempts})"
            return healthy, msg
        last_err = msg or ""
        if not _is_network_error(last_err):
            return False, last_err
        if i + 1 >= attempts:
            break
        time.sleep(_RETRY_BACKOFF_BASE * (i + 1))
    return False, f"{last_err} (after {attempts} attempts)" if last_err else f"Network error (after {attempts} attempts)"


def _probe_model_with_retry(check_type: str, api_url: str, api_key: str, model: str) -> tuple:
    attempts = _network_retry_attempts()
    last_err = ""
    for i in range(attempts):
        healthy, msg = probe_single_model(check_type, api_url, api_key, model)
        if healthy:
            return healthy, msg
        last_err = msg or ""
        if not _is_network_error(last_err):
            return False, last_err
        if i + 1 >= attempts:
            break
        time.sleep(_RETRY_BACKOFF_BASE * (i + 1))
    return False, f"{last_err} (after {attempts} attempts)" if last_err else f"Network error (after {attempts} attempts)"


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
    if endpoint_type in ("openai_responses", "responses", "codex"):
        return "openai_responses"
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
    if "/responses" in url or "chatgpt.com/backend-api/codex" in url:
        return "openai_responses"

    provider_id = (vendor.get("provider") or "").strip()
    if provider_id:
        prov = get_provider(provider_id)
        if prov:
            return prov["check_type"]
    return "openai_chat"


def _vendor_wants_responses(vendor: dict, key_entry: dict) -> bool:
    """Whether this key should also (or primarily) verify Responses API for Codex."""
    tags = [str(t).lower() for t in (vendor.get("tags") or [])]
    if any(t in ("codex", "responses", "suitable-codex", "适合codex") for t in tags):
        return True
    notes = str(key_entry.get("notes") or "").lower()
    if "codex" in notes or "responses" in notes:
        return True
    url = (vendor.get("proxy_target") or vendor.get("api_url") or "").lower()
    if "/responses" in url or "backend-api/codex" in url:
        return True
    # GPT-ish inventory strongly suggests Codex-switchable OpenAI-compat
    models = []
    for m in key_entry.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else str(m or "")
        if mid:
            models.append(mid.lower())
    dm = str(key_entry.get("default_model") or key_entry.get("check_model") or "").lower()
    if dm:
        models.append(dm)
    for mid in models:
        if mid.startswith("gpt-") or mid.startswith("o1") or mid.startswith("o3") or mid.startswith("o4") or "codex" in mid:
            return True
    return False


def _gptish_models(key_entry: dict) -> list[str]:
    out = []
    for m in key_entry.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else str(m or "")
        if not mid:
            continue
        low = mid.lower()
        if low.startswith(("gpt-", "o1", "o3", "o4")) or "codex" in low:
            out.append(mid)
    cm = str(key_entry.get("check_model") or "").strip()
    dm = str(key_entry.get("default_model") or "").strip()
    for extra in (cm, dm):
        if extra and extra not in out:
            low = extra.lower()
            if low.startswith(("gpt-", "o1", "o3", "o4")) or "codex" in low:
                out.insert(0, extra)
    return out


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
    wants_responses = check_type == "openai_responses" or _vendor_wants_responses(vendor, key_entry)

    # Primary check model (user-selected). If set, key-level / scheduled checks
    # probe this model only. If empty, keep legacy multi-model / scan behavior.
    check_model = str(key_entry.get("check_model") or "").strip()
    key_models = key_entry.get("models", [])
    if check_model:
        models_to_try = [check_model]
    elif key_models:
        # Legacy: try any known model (full inventory), not only enabled ones
        models_to_try = [m["id"] if isinstance(m, dict) else m for m in key_models]
        # Prefer default_model first when present in inventory
        dm = str(key_entry.get("default_model") or "").strip()
        if dm and dm in models_to_try:
            models_to_try = [dm] + [m for m in models_to_try if m != dm]
    else:
        models_to_try = None

    start = time.time()
    layers = {}
    if check_type == "openai_responses":
        # Primary path is Responses (Codex)
        gpt_models = _gptish_models(key_entry)
        resp_models = [check_model] if check_model else (gpt_models or models_to_try)
        healthy, error_msg = _probe_with_retry("openai_responses", api_url, api_key, resp_models)
        layers["responses"] = {"healthy": healthy, "message": error_msg}
        check_layer = "responses"
    elif check_model:
        healthy, error_msg = _probe_model_with_retry(check_type, api_url, api_key, check_model)
        if healthy and error_msg and not str(error_msg).startswith("["):
            error_msg = f"[{check_model}] {error_msg}"
        elif healthy and not error_msg:
            error_msg = f"[{check_model}] ok"
        layers["connectivity"] = {"healthy": healthy, "message": error_msg}
        check_layer = "model"
        # Optional Responses secondary for Codex-suitable keys
        if healthy and wants_responses:
            gpt_models = _gptish_models(key_entry) or ([check_model] if check_model else None)
            rh, rm = _probe_with_retry("openai_responses", api_url, api_key, gpt_models)
            layers["responses"] = {"healthy": rh, "message": rm}
            if not rh:
                # keep connectivity success but surface responses failure as overall fail for Codex path
                healthy = False
                error_msg = f"Chat OK; Responses failed: {rm}"
                check_layer = "responses"
    else:
        healthy, error_msg = _probe_with_retry(check_type, api_url, api_key, models_to_try)
        layers["connectivity"] = {"healthy": healthy, "message": error_msg}
        check_layer = "connectivity"
        if healthy and wants_responses:
            gpt_models = _gptish_models(key_entry) or models_to_try
            rh, rm = _probe_with_retry("openai_responses", api_url, api_key, gpt_models)
            layers["responses"] = {"healthy": rh, "message": rm}
            if not rh:
                healthy = False
                error_msg = f"Chat OK; Responses failed: {rm}"
                check_layer = "responses"
    latency_ms = int((time.time() - start) * 1000)

    models = []
    default_model = key_entry.get("default_model", "")
    # Still allow inventory scan when healthy (does not change check_model)
    # Model list endpoints are chat-compatible; use openai_chat for scan when responses.
    scan_type = "openai_chat" if check_type == "openai_responses" else check_type
    if healthy and scan_models_flag:
        models = scan_models(scan_type, api_url, api_key)
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
    # When healthy and we scanned models, persist inventory immediately so
    # models modal / vendor UI don't need a second round-trip to see them.
    if healthy and models:
        try:
            updates = {"models": models}
            if default_model:
                updates["default_model"] = default_model
            update_key_data(vendor_id, key_id, **updates)
        except Exception:
            pass

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
        "check_model": check_model,
        "used_check_model": bool(check_model),
        "check_layer": check_layer,
        "check_layers": layers,
        "wants_responses": wants_responses,
    }
    _enrich_health_result(result)

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
        healthy, msg = _probe_model_with_retry(check_type, api_url, api_key, mid)
        latency_ms = int((time.time() - start) * 1000)
        entry = {
            "model": mid,
            "healthy": healthy,
            "latency_ms": latency_ms,
            "message": msg if healthy else None,
            "error": None if healthy else msg,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "check_layer": "model",
        }
        _enrich_health_result(entry)
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
    # Prefer a working enabled model as default — never overwrite user check_model
    # and only auto-fill default_model when user has not set one.
    enabled_ok = [m for m in ok_models if m not in disabled]
    user_default = str(key_entry.get("default_model") or "").strip()
    if not user_default:
        if enabled_ok:
            updates["default_model"] = enabled_ok[0]
        elif ok_models:
            updates["default_model"] = ok_models[0]
    # Any usable model ⇒ key stays/becomes enabled (vendor considered normal)
    if enabled_ok:
        updates["enabled"] = True
    elif ok_models:
        updates["enabled"] = True

    updated = update_key_data(vendor_id, key_id, **updates) or key_entry

    # Reflect aggregate key health in cache: usable model ⇒ healthy for vendor status
    cache_key = f"{vendor_id}:{key_id}"
    key_healthy = bool(enabled_ok or ok_models)
    with _lock:
        cache = _load_cache()
        prev = cache.get(cache_key) or {}
        cache[cache_key] = {
            **prev,
            "key_id": key_id,
            "vendor_id": vendor_id,
            "healthy": key_healthy,
            "latency_ms": (results[0].get("latency_ms") if results else prev.get("latency_ms") or 0),
            "error": None if key_healthy else (fail_models and f"{len(fail_models)} model(s) failed" or prev.get("error")),
            "message": (
                f"{len(ok_models)} model(s) ok"
                if key_healthy
                else (prev.get("message") or "no usable model")
            ),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "models": list_model_ids(updated) or models,
            "default_model": updated.get("default_model") or "",
            "check_model": updated.get("check_model") or "",
        }
        _save_cache(cache)

    # Backend engines: sync enabled models only (failed auto-removed via disabled_models)
    if updated.get("enabled", True) and get_enabled_models(updated):
        on_key_updated(vendor, updated)
    else:
        # No healthy/enabled models left → drop key models from backends, keep in system
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
        "healthy": key_healthy,
        "enabled": updated.get("enabled") is not False,
    }


def _key_has_usable_model(key: dict, health: dict = None) -> bool:
    """True if key-level healthy OR any non-disabled model is healthy in model_health."""
    if not key or not key.get("api_key"):
        return False
    if health is None:
        health = {}
    if health.get("healthy") is True:
        return True
    disabled = set(key.get("disabled_models") or [])
    mh = key.get("model_health") or {}
    for mid, rec in mh.items():
        if mid in disabled:
            continue
        if isinstance(rec, dict) and rec.get("healthy") is True:
            return True
    # enabled models list after successful inventory (key healthy path)
    try:
        if health.get("healthy") is True and (get_enabled_models(key) or list_model_ids(key)):
            return True
    except Exception:
        pass
    return False


def is_key_backend_syncable(vendor_id: str, key: dict) -> bool:
    """Whether a key may be written into backend engine configs.

    - disabled keys: never
    - known unhealthy AND no usable model: never
    - healthy / has usable model / not-yet-checked: yes
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
    if h.get("healthy") is False and not _key_has_usable_model(key, h):
        return False
    return True


def apply_health_to_backends(vendor: dict, key: dict, health: dict) -> None:
    """Push usable keys (auto-enable + sync); strip fully-unusable from backends (keep in system).

    Policy: if the key is healthy OR has at least one usable model, treat as enabled
    and sync to backends. Only strip when nothing is usable.
    """
    if not vendor or not key:
        return
    usable = bool(health.get("healthy")) or _key_has_usable_model(key, health)
    if usable:
        models = health.get("models", [])
        default_model = health.get("default_model", "") or key.get("default_model", "")
        updates = {"enabled": True}
        if models:
            updates["models"] = models
        elif not key.get("models") and default_model:
            updates["models"] = [default_model]
        if default_model:
            updates["default_model"] = default_model
        updated = update_key_data(vendor["id"], key["id"], **updates) or key
        # mark response for UI — usable ⇒ enabled + healthy for vendor aggregation
        try:
            health["enabled"] = True
            if health.get("healthy") is not True:
                health["healthy"] = True
                health.setdefault("message", "usable model available")
            if updates.get("models") is not None:
                health["models"] = updates["models"]
            if updates.get("default_model"):
                health["default_model"] = updates["default_model"]
        except Exception:
            pass
        on_key_updated(vendor, updated)
    else:
        # Always remove failed keys from backends so engines match health
        on_key_removed(vendor, key)
        try:
            from core.data import get_settings
            if bool((get_settings() or {}).get("health_auto_disable")):
                update_key_data(vendor["id"], key["id"], enabled=False)
                health["enabled"] = False
        except Exception:
            pass


def check_all_keys(include_disabled: bool = True) -> list[dict]:
    """Probe every vendor key. Healthy keys are auto-enabled and synced to backends."""
    results = []

    for v in get_vendors():
        for k in v.get("keys", []):
            if not include_disabled and k.get("enabled") is False:
                continue
            health = check_key_health(v["id"], k["id"])
            results.append(health)
            # Always apply: healthy → enable + push; unhealthy → strip backends
            apply_health_to_backends(v, k, health)

    reconcile_all()
    try:
        from core.downstream import rebuild_all_downstream_routes
        rebuild_all_downstream_routes()
    except Exception:
        pass
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
