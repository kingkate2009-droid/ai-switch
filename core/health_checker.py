import json
import random
import threading
import time
from datetime import datetime, timezone, timedelta
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

# Max models to try after primary check_model (performance cap)
_MAX_FALLBACK_MODELS = 12
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


def _adaptive_cfg() -> dict:
    """Consecutive-streak adaptive interval settings."""
    s = get_settings() or {}
    def _i(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(s.get(key, default))))
        except Exception:
            return default
    base = _i("check_interval_seconds", 300, 60, 86400 * 7)
    return {
        "base": base,
        "fail_streak": _i("health_fail_streak", 3, 1, 50),
        "ok_streak": _i("health_ok_streak", 3, 1, 50),
        "fail_interval": _i("health_fail_interval_seconds", 7200, 60, 86400 * 7),
        "ok_interval": _i("health_ok_interval_seconds", 3600, 60, 86400 * 7),
    }


def _parse_iso(ts: str):
    if not ts:
        return None
    try:
        t = str(ts).strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return datetime.fromisoformat(t)
    except Exception:
        return None


def key_is_due(cache_entry: dict, *, now: datetime = None) -> bool:
    """Whether this key should be probed on a scheduled run (adaptive due time)."""
    now = now or datetime.now(timezone.utc)
    if not cache_entry:
        return True
    due = _parse_iso(cache_entry.get("next_check_at") or "")
    if due is None:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return now >= due


def _apply_streak_and_schedule(result: dict, prev=None) -> dict:
    """Update consecutive ok/fail streak and next_check_at on a health result."""
    cfg = _adaptive_cfg()
    prev = prev or {}
    healthy = result.get("healthy") is True
    if healthy:
        ok = int(prev.get("ok_streak") or 0) + 1
        fail = 0
    else:
        # unknown/None treated as fail for scheduling (still probed)
        fail = int(prev.get("fail_streak") or 0) + 1
        ok = 0
    result["ok_streak"] = ok
    result["fail_streak"] = fail

    # Switch absolute interval when streak thresholds hit (not max with base)
    if fail >= cfg["fail_streak"]:
        interval = cfg["fail_interval"]
        mode = "fail_slow"
    elif ok >= cfg["ok_streak"]:
        interval = cfg["ok_interval"]
        mode = "ok_slow"
    else:
        interval = cfg["base"]
        mode = "base"
    result["check_interval_seconds"] = interval
    result["schedule_mode"] = mode
    try:
        checked = _parse_iso(result.get("checked_at") or "") or datetime.now(timezone.utc)
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        result["next_check_at"] = (checked + timedelta(seconds=interval)).isoformat()
    except Exception:
        result["next_check_at"] = None
    return result


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
    if any(x in s for x in (
        "model", "not found", "compatible", "unsupported", "does not exist", "no such model",
        "no available channel", "under group", "channel for model",
    )):
        return {
            "code": "model",
            "label": "Model / channel issue",
            "suggestion": (
                "This key's group has no channel for the probed models. "
                "Set check_model to a model allowed for this key group, or use a key with model access."
            ),
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


def _merge_model_ids(*lists) -> list[str]:
    out, seen = [], set()
    for lst in lists:
        for m in lst or []:
            mid = m.get("id") if isinstance(m, dict) else str(m or "")
            mid = (mid or "").strip()
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
    return out


def _sibling_model_ids(vendor: dict, key_id: str) -> list[str]:
    borrowed, seen = [], set()
    for sk in vendor.get("keys") or []:
        if str(sk.get("id")) == str(key_id):
            continue
        for mid in list_model_ids(sk):
            if mid and mid not in seen:
                seen.add(mid)
                borrowed.append(mid)
        dm = str(sk.get("default_model") or "").strip()
        if dm and dm not in seen:
            seen.add(dm)
            borrowed.insert(0, dm)
    return borrowed


def _build_probe_order(
    *,
    check_model: str,
    inventory: list[str],
    default_model: str = "",
    prefer_gptish: bool = False,
) -> list[str]:
    """Primary check_model first; remaining inventory shuffled (capped)."""
    pool = list(inventory or [])
    if prefer_gptish:
        gpt = [m for m in pool if m.lower().startswith(("gpt-", "o1", "o3", "o4")) or "codex" in m.lower()]
        if gpt:
            pool = gpt
    primary = (check_model or "").strip()
    dm = (default_model or "").strip()
    ordered = []
    seen = set()
    if primary:
        ordered.append(primary)
        seen.add(primary)
    rest = [m for m in pool if m and m not in seen]
    # prefer default next (before shuffle) so common path is stable-ish
    if dm and dm in rest:
        rest.remove(dm)
        rest.insert(0, dm)
    # randomize fallbacks so we don't always hit the same dead models
    head = rest[:1]  # keep default sticky if present
    tail = rest[1:]
    random.shuffle(tail)
    rest = head + tail
    for m in rest:
        if len(ordered) >= 1 + _MAX_FALLBACK_MODELS:
            break
        if m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered


def _probe_models_in_order(
    check_type: str,
    api_url: str,
    api_key: str,
    models: list[str],
) -> tuple:
    """Try models in order. Returns (healthy, msg, used_model, tried).

    Auth/network hard failures stop early; model/channel errors continue.
    """
    if not models:
        healthy, msg = _probe_with_retry(check_type, api_url, api_key, None)
        return healthy, msg, None, []
    tried = []
    last_err = ""
    for mid in models:
        healthy, msg = _probe_model_with_retry(check_type, api_url, api_key, mid)
        tried.append(mid)
        if healthy:
            if msg and not str(msg).startswith("["):
                msg = f"[{mid}] {msg}"
            elif not msg:
                msg = f"[{mid}] ok"
            return True, msg, mid, tried
        last_err = msg or last_err
        # hard stop: auth / blocked / quota — not a model pick issue
        low = (msg or "").lower()
        if any(x in low for x in (
            "auth failed", "http 401", "unauthorized", "invalid api",
            "access blocked", "quota exhausted", "billing", "payment required",
            "http 402",
        )):
            return False, msg, mid, tried
        # continue on model/channel/timeout for next candidate
    detail = last_err or "No compatible model found"
    if len(tried) > 1:
        detail = f"{detail} (tried {len(tried)} models)"
    return False, detail, None, tried


def check_key_health(vendor_id: str, key_id: str, scan_models_flag: bool = True) -> dict:
    """Key-level health check.

    - Always refresh model inventory when scan_models_flag (every check).
    - Probe primary check_model first; on failure randomly try other models.
    - Key is unhealthy only when no candidate model works (or hard auth/network fail).
    """
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
    check_model = str(key_entry.get("check_model") or "").strip()
    default_model = str(key_entry.get("default_model") or "").strip()
    disabled = set(key_entry.get("disabled_models") or [])

    start = time.time()

    # 1) Always refresh model list (not only when healthy)
    scan_type = "openai_chat" if check_type == "openai_responses" else check_type
    scanned = []
    if scan_models_flag:
        try:
            scanned = scan_models(scan_type, api_url, api_key) or []
        except Exception:
            scanned = []

    existing = list_model_ids(key_entry)
    siblings = _sibling_model_ids(vendor, key_id)
    models = _merge_model_ids(scanned, existing, siblings)
    # drop disabled from probe pool but keep full inventory for storage
    probe_pool = [m for m in models if m not in disabled] or list(models)

    if not default_model and models:
        default_model = pick_default_model(models)

    # Persist inventory every check when we learned anything new
    if models and (scanned or models != existing):
        try:
            updates = {"models": models}
            if default_model:
                updates["default_model"] = default_model
            update_key_data(vendor_id, key_id, **updates)
            # refresh local view
            key_entry = get_key_entry_fresh(vendor_id, key_id) or key_entry
        except Exception:
            pass

    # 2) Build probe order: primary check_model → shuffled fallbacks
    layers = {}
    used_model = None
    tried_models = []

    if check_type == "openai_responses":
        order = _build_probe_order(
            check_model=check_model,
            inventory=probe_pool or _gptish_models(key_entry),
            default_model=default_model,
            prefer_gptish=True,
        )
        healthy, error_msg, used_model, tried_models = _probe_models_in_order(
            "openai_responses", api_url, api_key, order,
        )
        layers["responses"] = {"healthy": healthy, "message": error_msg, "tried": tried_models}
        check_layer = "responses"
    else:
        order = _build_probe_order(
            check_model=check_model,
            inventory=probe_pool,
            default_model=default_model,
            prefer_gptish=False,
        )
        healthy, error_msg, used_model, tried_models = _probe_models_in_order(
            check_type, api_url, api_key, order,
        )
        layers["connectivity"] = {
            "healthy": healthy,
            "message": error_msg,
            "tried": tried_models,
            "primary": check_model or None,
            "fallback_used": bool(check_model and used_model and used_model != check_model),
        }
        check_layer = "model" if check_model else "connectivity"

        # Optional Responses secondary for Codex-suitable keys
        if healthy and wants_responses:
            gpt_pool = [m for m in (probe_pool or []) if m.lower().startswith(("gpt-", "o1", "o3", "o4")) or "codex" in m.lower()]
            resp_order = _build_probe_order(
                check_model=check_model if (check_model and check_model in (gpt_pool or [check_model])) else (used_model or ""),
                inventory=gpt_pool or probe_pool,
                default_model=default_model,
                prefer_gptish=True,
            )
            rh, rm, r_used, r_tried = _probe_models_in_order(
                "openai_responses", api_url, api_key, resp_order,
            )
            layers["responses"] = {"healthy": rh, "message": rm, "tried": r_tried}
            if not rh:
                healthy = False
                error_msg = f"Chat OK; Responses failed: {rm}"
                check_layer = "responses"
            elif r_used:
                used_model = r_used

    latency_ms = int((time.time() - start) * 1000)
    cache_key = f"{vendor_id}:{key_id}"

    # If primary check_model failed but a fallback worked, note it (do not clear user check_model)
    primary_failed = bool(check_model and used_model and used_model != check_model and healthy)

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
        "used_model": used_model,
        "tried_models": tried_models,
        "used_check_model": bool(check_model and used_model == check_model),
        "primary_check_failed": primary_failed,
        "models_refreshed": bool(scanned) if scan_models_flag else False,
        "check_layer": check_layer,
        "check_layers": layers,
        "wants_responses": wants_responses,
    }
    _enrich_health_result(result)

    with _lock:
        cache = _load_cache()
        prev = cache.get(cache_key) or {}
        _apply_streak_and_schedule(result, prev)
        cache[cache_key] = result
        _save_cache(cache)

    return result


def get_key_entry_fresh(vendor_id: str, key_id: str):
    v = get_vendor(vendor_id)
    if not v:
        return None
    for k in v.get("keys") or []:
        if str(k.get("id")) == str(key_id):
            return k
    return None


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


def check_all_keys(
    include_disabled: bool = True,
    *,
    only_due: bool = False,
    force: bool = False,
) -> list[dict]:
    """Probe vendor keys. Healthy keys are auto-enabled and synced to backends.

    only_due: when True (scheduled runs), skip keys whose next_check_at is in the future
              (adaptive interval after consecutive ok/fail streaks).
    force: ignore due schedule (manual full check).
    """
    results = []
    skipped = 0
    now = datetime.now(timezone.utc)
    with _lock:
        cache_snap = dict(_load_cache())

    for v in get_vendors():
        for k in v.get("keys", []):
            if not include_disabled and k.get("enabled") is False:
                continue
            ck = f"{v['id']}:{k['id']}"
            if only_due and not force:
                prev = cache_snap.get(ck) or {}
                if not key_is_due(prev, now=now):
                    skipped += 1
                    # surface last known status so UI/history still see the key
                    if prev:
                        row = dict(prev)
                        row["skipped"] = True
                        row["skip_reason"] = "not_due"
                        results.append(row)
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
    if skipped:
        try:
            import logging
            logging.getLogger(__name__).info(
                "Health check skipped %s key(s) not yet due (adaptive interval)", skipped,
            )
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
