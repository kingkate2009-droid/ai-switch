import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from core.data import (
    get_enabled_models,
    get_settings,
    get_vendor,
    get_vendors,
    list_model_ids,
    model_id_of,
    update_key_data,
    update_keys_data_bulk,
)
from core.providers import (
    get_provider,
    pick_default_model,
    probe_provider,
    probe_single_model_endpoint,
    probe_single_model,
    scan_models,
)
from core.endpoints import (
    capability_record,
    endpoint_candidates,
    effective_model_endpoints,
)

# Max models to try for key-level health (performance cap).
# Full matrix across the whole inventory is check_key_models(), not key health.
_MAX_FALLBACK_MODELS = 5
# Quick/bulk health: fewer fallbacks, shorter wall time on dead keys
_MAX_FALLBACK_MODELS_QUICK = 2
from backends import reconcile_all

DATA_DIR = Path.home() / ".ai-switch"
HEALTH_CACHE_PATH = DATA_DIR / "health_cache.json"
_lock = threading.RLock()

# Default network retries (total attempts = 1 + retries, but we treat as max attempts)
_DEFAULT_NETWORK_RETRIES = 3
_RETRY_BACKOFF_BASE = 0.6  # seconds; attempt 1 wait 0.6, then 1.2, ...
# Quick bulk checks: 1 attempt only (no multi-second backoff chain)
_QUICK_NETWORK_RETRIES = 1

# Thread-local quick mode (set by check_key_health(quick=True) / bulk)
_tls = threading.local()


def _in_quick_mode() -> bool:
    return bool(getattr(_tls, "quick", False))


def _max_fallback_models() -> int:
    return _MAX_FALLBACK_MODELS_QUICK if _in_quick_mode() else _MAX_FALLBACK_MODELS


def _network_retry_attempts() -> int:
    """Max attempts for network-ish probe failures. Default 3, clamped 1..10."""
    # Thread override from providers.probe_profile / quick health
    try:
        from core.providers import _tls as _prov_tls
        ov = getattr(_prov_tls, "probe_retries", None)
        if ov is not None:
            return max(1, min(10, int(ov)))
    except Exception:
        pass
    if _in_quick_mode():
        return _QUICK_NETWORK_RETRIES
    try:
        n = int((get_settings() or {}).get("health_network_retries", _DEFAULT_NETWORK_RETRIES))
    except Exception:
        n = _DEFAULT_NETWORK_RETRIES
    return max(1, min(10, n))


from contextlib import contextmanager


@contextmanager
def health_check_profile(mode: str = "full"):
    """Tune probe cost for bulk vs interactive single-key checks.

    quick: shorter timeout, 1 retry, fewer model fallbacks.
    """
    from core.providers import probe_profile, PROBE_TIMEOUT_QUICK
    prev = getattr(_tls, "quick", False)
    try:
        if mode == "quick":
            _tls.quick = True
            with probe_profile(timeout=PROBE_TIMEOUT_QUICK, retries=1):
                yield
        else:
            _tls.quick = False
            yield
    finally:
        _tls.quick = prev


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


def _archive_streak_days() -> int:
    """Configurable consecutive-fail days before a KEY auto-archives. 0 = off."""
    try:
        return max(0, int((get_settings() or {}).get("health_archive_streak_days", 10)))
    except Exception:
        return 10


def _consecutive_fail_days(row: dict):
    """Elapsed full days of the current fail streak, or None when not failing."""
    if not row or row.get("healthy") is not False:
        return None
    since = _parse_iso(str(row.get("fail_since") or "") if row.get("fail_since") else "")
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - since
    return elapsed.total_seconds() / 86400.0


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
    """Update consecutive ok/fail streak and next_check_at on a health result.

    Also tracks ``fail_since`` (ISO) — when the present continuous fail streak
    started — so the archive logic can decide by elapsed days.
    """
    cfg = _adaptive_cfg()
    prev = prev or {}
    healthy = result.get("healthy") is True
    now_iso = datetime.now(timezone.utc).isoformat()
    if healthy:
        ok = int(prev.get("ok_streak") or 0) + 1
        fail = 0
        fail_since = None
    else:
        # unknown/None treated as fail for scheduling (still probed)
        fail = int(prev.get("fail_streak") or 0) + 1
        ok = 0
        prev_fail_since = prev.get("fail_since")
        if fail == 1 or not _parse_iso(str(prev_fail_since or "") if prev_fail_since else ""):
            fail_since = now_iso
        else:
            fail_since = prev_fail_since
    result["ok_streak"] = ok
    result["fail_streak"] = fail
    result["fail_since"] = fail_since

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


# Priority when picking the "most actionable" failure among endpoint probes.
_ERROR_CODE_PRIORITY = (
    "auth", "quota", "rate", "ssl", "timeout", "network", "model", "endpoint", "other",
)


def classify_health_error(msg: str) -> dict:
    """Map raw probe error → {code, label_en, suggestion_en} for UI/i18n.

    code values: auth | quota | model | timeout | network | rate | ssl | endpoint | other
    """
    s = (msg or "").lower().strip()
    if not s:
        return {
            "code": "other",
            "label": "Unknown error",
            "suggestion": "Re-run the check; if it keeps failing, open Diagnostics.",
        }
    # Generic matrix summary — never treat as a model-id problem by itself.
    if s in (
        "no compatible endpoint found",
        "no selected endpoint succeeded",
        "no usable model",
    ) or s.startswith("no compatible endpoint"):
        return {
            "code": "endpoint",
            "label": "No working endpoint",
            "suggestion": "Open endpoint details for the real cause (auth, quota, timeout, or model).",
        }
    # Auth before bare "api key" quota phrases that also contain api key
    if any(x in s for x in (
        "401", "unauthorized", "auth failed", "authentication", "invalid api",
        "api key expired", "api_key_expired", "key is invalid", "incorrect api key",
    )):
        return {
            "code": "auth",
            "label": "Auth failed (401/403)",
            "suggestion": "Check API key, endpoint URL, and whether the key is revoked.",
        }
    if "403" in s or "forbidden" in s:
        # 403 often means group/model blocked rather than bad secret
        if any(x in s for x in ("model", "channel", "group", "not allow", "not_allowed", "无可用")):
            return {
                "code": "model",
                "label": "Model / channel issue",
                "suggestion": (
                    "This key's group has no channel for the probed models. "
                    "Set check_model to a model allowed for this key group, or use a key with model access."
                ),
            }
        return {
            "code": "auth",
            "label": "Auth failed (401/403)",
            "suggestion": "Check API key, endpoint URL, and whether the key is revoked.",
        }
    if any(x in s for x in ("429", "rate limit", "too many requests", "rate_limit", "rpm exhausted")):
        return {
            "code": "rate",
            "label": "Rate limited (429)",
            "suggestion": "Slow down requests or switch to another key; retry later.",
        }
    if any(x in s for x in (
        "quota", "billing", "insufficient", "payment", "balance", "credit",
        "exceeded your current quota", "usagelimit", "usage limit", "usage_limit",
        "gousagelimit", "monthly limit", "no credits", "out of credits", "额度",
        "api_key_quota", "weekly quota",
    )):
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
        "503", "502", "bad gateway", "service unavailable", "当前不可用",
    )):
        return {
            "code": "network",
            "label": "Network error",
            "suggestion": "Check internet, DNS, firewall, and proxy settings.",
        }
    # Endpoint family missing (404 on /responses etc.) — not a model-id issue
    if any(x in s for x in (
        "responses api not found", "endpoint may only support", "not found (http 404)",
        "unknown endpoint",
    )):
        return {
            "code": "endpoint",
            "label": "Endpoint unavailable",
            "suggestion": "This API path is not offered by the gateway; use another endpoint type.",
        }
    # Real model/channel failures only (avoid matching generic "compatible endpoint")
    if any(x in s for x in (
        "no compatible model", "model_not_found", "model is not found", "model_not_allowed",
        "does not exist", "no such model", "unsupported model", "model not support",
        "no available channel", "under group", "channel for model", "无可用渠道",
        "分组", "model '",
    )) or (("model" in s or "channel" in s) and any(
        x in s for x in ("not found", "not allow", "unsupported", "no available", "no channel")
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


def _iter_endpoint_check_errors(checks: dict) -> list[str]:
    """Collect non-empty error strings from a model×endpoint checks map."""
    out = []
    if not isinstance(checks, dict):
        return out
    for ep, ck in checks.items():
        if not isinstance(ck, dict):
            continue
        if ck.get("healthy") is True:
            continue
        msg = ck.get("error") or ck.get("message") or ""
        msg = str(msg or "").strip()
        if msg:
            out.append(msg)
    return out


def summarize_endpoint_failures(checks: dict, *, mode: str = "auto") -> str:
    """Pick the most actionable failure message from endpoint probe results.

    Avoids collapsing everything into the unhelpful
    ``No compatible endpoint found`` string that the UI mis-labels as a model issue.
    """
    errors = _iter_endpoint_check_errors(checks)
    if not errors:
        return "No selected endpoint succeeded" if str(mode).lower() == "manual" else "No compatible endpoint found"
    # Rank by classified code priority, then keep the richest message
    best = None
    best_rank = 999
    for msg in errors:
        code = classify_health_error(msg).get("code") or "other"
        try:
            rank = _ERROR_CODE_PRIORITY.index(code)
        except ValueError:
            rank = len(_ERROR_CODE_PRIORITY)
        if rank < best_rank or (rank == best_rank and best is not None and len(msg) > len(best)):
            best_rank = rank
            best = msg
        elif best is None:
            best = msg
    return best or errors[0]


def _enrich_health_result(result: dict) -> dict:
    """Attach error_code / error_label / suggestion when unhealthy."""
    if not isinstance(result, dict):
        return result
    if result.get("healthy") is True:
        result.setdefault("error_code", None)
        result.setdefault("suggestion", None)
        return result
    raw = result.get("error") or result.get("message") or ""
    # Prefer concrete endpoint probe errors when the top-level message is generic
    checks = result.get("endpoint_checks")
    if not checks:
        layers = result.get("check_layers") or {}
        me = layers.get("model_endpoints") or {}
        for row in me.get("results") or []:
            if isinstance(row, dict) and row.get("endpoint_checks"):
                checks = row.get("endpoint_checks")
                break
    if checks:
        concrete = summarize_endpoint_failures(checks, mode=str(result.get("endpoint_mode") or "auto"))
        if concrete and concrete.lower() not in (
            "no compatible endpoint found",
            "no selected endpoint succeeded",
        ):
            raw = concrete
            # Surface the real error on the result so UI toasts stay accurate
            if not result.get("error") or str(result.get("error") or "").lower() in (
                "no compatible endpoint found",
                "no selected endpoint succeeded",
                "no usable model",
            ):
                result["error"] = concrete
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


def _probe_endpoint_with_retry(endpoint: str, api_url: str, api_key: str, model: str) -> tuple:
    attempts = _network_retry_attempts()
    last_err = ""
    for i in range(attempts):
        healthy, msg = probe_single_model_endpoint(endpoint, api_url, api_key, model)
        if healthy:
            return healthy, msg
        last_err = msg or ""
        if not _is_network_error(last_err):
            return False, last_err
        if i + 1 >= attempts:
            break
        time.sleep(_RETRY_BACKOFF_BASE * (i + 1))
    return False, f"{last_err} (after {attempts} attempts)" if last_err else f"Network error (after {attempts} attempts)"


def _is_hard_key_failure(msg: str) -> bool:
    """Auth/quota failures that will not improve by trying another model/endpoint."""
    code = classify_health_error(msg or "").get("code")
    return code in ("auth", "quota")


def _compact_check_rec(ck: dict) -> dict:
    if not isinstance(ck, dict):
        return {}
    err = str(ck.get("error") or "")[:160]
    msg = str(ck.get("message") or "")[:120]
    return {
        "healthy": bool(ck.get("healthy")),
        "latency_ms": int(ck.get("latency_ms") or 0),
        "error": err or None,
        "message": msg or None,
        "checked_at": ck.get("checked_at"),
    }


def _compact_capability_state(state: dict) -> dict:
    """Shrink endpoint capability records before bulk SQLite writes."""
    if not isinstance(state, dict):
        return {}
    checks_in = state.get("checks") if isinstance(state.get("checks"), dict) else {}
    checks = {str(ep): _compact_check_rec(ck) for ep, ck in checks_in.items()}
    out = {
        "mode": "manual" if str(state.get("mode") or "").lower() == "manual" else "auto",
        "detected": list(state.get("detected") or []),
        "selected": list(state.get("selected") or []),
        "checks": checks,
        "checked_at": state.get("checked_at") or "",
    }
    if state.get("classified"):
        out["classified"] = list(state.get("classified") or [])
    if state.get("modality"):
        out["modality"] = state.get("modality")
    return out


def _compact_model_health_rec(rec: dict) -> dict:
    if not isinstance(rec, dict):
        return {}
    checks_in = rec.get("endpoint_checks") if isinstance(rec.get("endpoint_checks"), dict) else {}
    return {
        "healthy": bool(rec.get("healthy")),
        "latency_ms": int(rec.get("latency_ms") or 0),
        "error": (str(rec.get("error") or "")[:200] or None),
        "message": (str(rec.get("message") or "")[:160] or None),
        "checked_at": rec.get("checked_at"),
        "endpoints": list(rec.get("endpoints") or []),
        "detected_endpoints": list(rec.get("detected_endpoints") or []),
        "endpoint_mode": rec.get("endpoint_mode") or "auto",
        "selected_endpoints": list(rec.get("selected_endpoints") or []),
        "endpoint_checks": {str(ep): _compact_check_rec(ck) for ep, ck in checks_in.items()},
        "error_code": rec.get("error_code"),
        "error_label": rec.get("error_label"),
        "suggestion": (str(rec.get("suggestion") or "")[:200] or None),
    }


def _slim_key_updates_for_bulk(updates: dict) -> dict:
    """Only persist fields needed after a key-health pass; compact large maps."""
    if not isinstance(updates, dict):
        return {}
    out = {}
    for k in ("enabled", "models", "default_model", "disabled_models", "check_model"):
        if k in updates:
            out[k] = updates[k]
    if "endpoint_capabilities" in updates:
        caps = updates.get("endpoint_capabilities") or {}
        if not caps:
            out["endpoint_capabilities"] = {}
        elif isinstance(caps, dict):
            out["endpoint_capabilities"] = {
                str(mid): _compact_capability_state(state)
                for mid, state in caps.items()
                if isinstance(state, dict)
            }
    if "model_health" in updates:
        mh = updates.get("model_health")
        if mh is None:
            pass
        elif not mh:
            out["model_health"] = {}
        elif isinstance(mh, dict):
            out["model_health"] = {
                str(mid): _compact_model_health_rec(rec)
                for mid, rec in mh.items()
                if isinstance(rec, dict)
            }
    return out


def check_model_endpoints(
    vendor_id: str,
    key_id: str,
    model: str,
    *,
    persist: bool = True,
    fail_fast: bool = False,
) -> dict:
    """Probe candidate endpoints for one model and persist its matrix.

    ``fail_fast=True`` (key-level health): stop after first success, and stop
    early on hard auth/quota errors so a dead key does not burn ~1 minute
    across chat+responses+messages timeouts.

    ``fail_fast=False`` (default): full model×endpoint matrix for the UI.
    """
    vendor = get_vendor(vendor_id)
    key = next((k for k in (vendor or {}).get("keys") or [] if str(k.get("id")) == str(key_id)), None)
    model = str(model or "").strip()
    if not vendor or not key:
        return {"error": "Vendor or key not found", "model": model, "checks": {}}
    if vendor.get("archived") or key.get("archived"):
        return {"error": "Archived — excluded from health checks", "model": model, "checks": {}, "archived": True}
    if not model:
        return {"error": "Model id required", "model": model, "checks": {}}

    checks = {}
    detected = []
    started = time.time()
    hard_fail = None
    fast = bool(fail_fast or _in_quick_mode())
    candidates = endpoint_candidates(vendor, model)
    # Key health only needs one working path; try at most two endpoint families.
    if fast and len(candidates) > 2:
        candidates = list(candidates[:2])
    for endpoint in candidates:
        t0 = time.time()
        healthy, message = _probe_endpoint_with_retry(endpoint, str(vendor.get("proxy_target") or vendor.get("api_url") or ""), key.get("api_key") or "", model)
        rec = {
            "healthy": bool(healthy),
            "latency_ms": int((time.time() - t0) * 1000),
            "message": message if healthy else None,
            "error": None if healthy else message,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        checks[endpoint] = rec
        if healthy:
            detected.append(endpoint)
            if fast:
                break
        elif _is_hard_key_failure(message or ""):
            hard_fail = message
            if fast:
                break

    state = capability_record(
        detected=detected,
        checks=checks,
        mode=str((key.get("endpoint_capabilities") or {}).get(model, {}).get("mode") or "auto"),
        selected=(key.get("endpoint_capabilities") or {}).get(model, {}).get("selected") or [],
    )
    if persist:
        mode = state["mode"]
        usable = [
            endpoint for endpoint in (state["selected"] if mode == "manual" else detected)
            if checks.get(endpoint, {}).get("healthy") is True
        ]
        healthy = bool(usable)
        model_health = dict(key.get("model_health") or {})
        fail_msg = None if healthy else summarize_endpoint_failures(checks, mode=mode)
        model_health[model] = {
            "healthy": healthy,
            "latency_ms": int((time.time() - started) * 1000),
            "error": fail_msg,
            "message": "; ".join(
                str(checks[endpoint].get("message"))
                for endpoint in usable
                if checks.get(endpoint, {}).get("message")
            ) or None,
            "checked_at": state["checked_at"],
            "endpoints": usable,
            "detected_endpoints": detected,
            "endpoint_checks": checks,
            "endpoint_mode": mode,
            "selected_endpoints": state["selected"],
        }
        if not healthy:
            info = classify_health_error(fail_msg or "")
            model_health[model]["error_code"] = info.get("code")
            model_health[model]["error_label"] = info.get("label")
            model_health[model]["suggestion"] = info.get("suggestion")
        disabled = set(key.get("disabled_models") or [])
        if healthy:
            disabled.discard(model)
        else:
            disabled.add(model)
        updated_key = update_key_data(
            vendor_id,
            key_id,
            endpoint_capabilities={**(key.get("endpoint_capabilities") or {}), model: state},
            model_health=model_health,
            disabled_models=sorted(disabled),
        ) or key
        from core.endpoints import model_is_verified_usable
        key_healthy = any(
            model_is_verified_usable(updated_key, mid)
            for mid in list_model_ids(updated_key)
        )
        cache_key = f"{vendor_id}:{key_id}"
        with _lock:
            cache = _load_cache()
            previous_cache = cache.get(cache_key) or {}
            cache[cache_key] = {
                **previous_cache,
                "vendor_id": vendor_id,
                "key_id": key_id,
                "healthy": key_healthy,
                "checked_at": state["checked_at"],
                "error": None if key_healthy else "No usable model",
            }
            _save_cache(cache)
    out = {
        "vendor_id": vendor_id,
        "key_id": key_id,
        "model": model,
        "detected": detected,
        "checks": checks,
        "mode": state["mode"],
        "selected": state["selected"],
        "latency_ms": int((time.time() - started) * 1000),
    }
    if hard_fail:
        out["hard_fail"] = hard_fail
        out["hard_fail_code"] = classify_health_error(hard_fail).get("code")
    return out


def _load_cache() -> dict:
    if HEALTH_CACHE_PATH.exists():
        with open(HEALTH_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    with open(HEALTH_CACHE_PATH, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    invalidate_health_cache_snapshot()


def _resolve_check_type(vendor: dict) -> str:
    # Legacy compatibility for inventory scans only.  Per-model endpoint
    # health uses endpoint_candidates()/probe_single_model_endpoint() and does
    # not consult this vendor-level type.
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
            mid = model_id_of(m)
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
    cap = 1 + _max_fallback_models()
    for m in rest:
        if len(ordered) >= cap:
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


def check_key_health(
    vendor_id: str,
    key_id: str,
    scan_models_flag: bool = True,
    *,
    quick: bool = False,
    persist: bool = True,
    previous_health: dict = None,
) -> dict:
    """Key-level health check.

    - Refresh model inventory when scan_models_flag and (not quick or inventory empty).
    - Probe primary check_model first; on failure randomly try other models.
    - Key is unhealthy only when no candidate model works (or hard auth/network fail).
    - quick=True: fewer fallbacks, 1 network retry, shorter timeout, skip scan if inventory exists.
    """
    ctx = health_check_profile("quick") if quick else health_check_profile("full")
    with ctx:
        return _check_key_health_inner(
            vendor_id,
            key_id,
            scan_models_flag=scan_models_flag,
            quick=quick,
            persist=persist,
            previous_health=previous_health,
        )


def _check_key_health_inner(
    vendor_id: str,
    key_id: str,
    scan_models_flag: bool = True,
    *,
    quick: bool = False,
    persist: bool = True,
    previous_health: dict = None,
) -> dict:
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
    if vendor.get("archived") or key_entry.get("archived"):
        return {
            "key_id": key_id,
            "vendor_id": vendor_id,
            "healthy": False,
            "latency_ms": 0,
            "error": "Archived — excluded from health checks",
            "archived": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    api_url = vendor.get("proxy_target", "") or vendor["api_url"]
    api_key = key_entry["api_key"]
    check_type = _resolve_check_type(vendor)
    wants_responses = check_type == "openai_responses" or _vendor_wants_responses(vendor, key_entry)
    check_model = str(key_entry.get("check_model") or "").strip()
    default_model = str(key_entry.get("default_model") or "").strip()
    disabled = set(key_entry.get("disabled_models") or [])

    start = time.time()

    # 1) Model inventory: skip network scan in quick mode when we already have models
    scan_type = "openai_chat" if check_type == "openai_responses" else check_type
    scanned = []
    existing_ids = list_model_ids(key_entry)
    # Quick checks may skip inventory refresh only when the key already has a
    # model list. New imports must scan first, otherwise the endpoint phase has
    # no model to probe and incorrectly returns "No usable model".
    do_scan = bool(scan_models_flag) or (quick and not existing_ids)
    if do_scan:
        try:
            scanned = scan_models(scan_type, api_url, api_key) or []
        except Exception:
            scanned = []

    existing = existing_ids
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
            if persist:
                update_key_data(vendor_id, key_id, **updates)
                key_entry = get_key_entry_fresh(vendor_id, key_id) or key_entry
            else:
                key_entry = {**key_entry, **updates}
        except Exception:
            pass

    # 2) Model-level endpoint matrix.  This is now the authoritative key
    # health path; the legacy check_type probe below remains only as dead-code
    # compatibility for older callers that may still inspect its layer shape.
    endpoint_caps = dict(key_entry.get("endpoint_capabilities") or {})
    model_health = dict(key_entry.get("model_health") or {})
    endpoint_results = []
    cache_key = f"{vendor_id}:{key_id}"
    ordered_models = _build_probe_order(
        check_model=check_model,
        inventory=[m for m in probe_pool if m not in disabled] or probe_pool,
        default_model=default_model,
        prefer_gptish=False,
    )
    # Key-level health always caps models; full inventory matrix is check_key_models().
    cap = _MAX_FALLBACK_MODELS_QUICK if quick else _MAX_FALLBACK_MODELS
    ordered_models = ordered_models[:cap]
    for mid in ordered_models:
        matrix = check_model_endpoints(vendor_id, key_id, mid, persist=False, fail_fast=True)
        previous = endpoint_caps.get(mid) if isinstance(endpoint_caps.get(mid), dict) else {}
        mode = str(previous.get("mode") or "auto").lower()
        selected = list(previous.get("selected") or [])
        detected = list(matrix.get("detected") or [])
        usable = [ep for ep in (selected if mode == "manual" else detected)
                  if (matrix.get("checks") or {}).get(ep, {}).get("healthy") is True]
        state = capability_record(detected=detected, checks=matrix.get("checks") or {}, mode=mode, selected=selected)
        # Preserve classified/modality tags from prior classify-all runs
        if previous.get("classified"):
            state["classified"] = list(previous.get("classified") or [])
        if previous.get("modality"):
            state["modality"] = previous.get("modality")
        endpoint_caps[mid] = state
        checks_map = matrix.get("checks") or {}
        fail_msg = None if usable else (matrix.get("hard_fail") or summarize_endpoint_failures(checks_map, mode=mode))
        mh = {
            "healthy": bool(usable),
            "latency_ms": matrix.get("latency_ms") or 0,
            "error": fail_msg,
            "message": None,
            "checked_at": state.get("checked_at"),
            "endpoints": usable,
            "detected_endpoints": detected,
            "endpoint_checks": checks_map,
            "endpoint_mode": mode,
            "selected_endpoints": selected,
        }
        if not usable and fail_msg:
            info = classify_health_error(fail_msg)
            mh["error_code"] = info.get("code")
            mh["error_label"] = info.get("label")
            mh["suggestion"] = info.get("suggestion")
        model_health[mid] = mh
        endpoint_results.append({"model": mid, **mh})
        # Key health only needs one working model×endpoint.
        if usable:
            break
        # Auth/quota will not improve on the next model — stop the key early.
        if matrix.get("hard_fail") or (fail_msg and _is_hard_key_failure(fail_msg)):
            break

    key_healthy = any(bool(r.get("healthy")) for r in endpoint_results)
    failed_now = {r["model"] for r in endpoint_results if not r.get("healthy")}
    healthy_now = {r["model"] for r in endpoint_results if r.get("healthy")}
    # Key-level health only probes 1–N models.  Never disable the rest of the
    # inventory just because they were not tried — backends should still receive
    # unscanned models when the key itself is healthy.
    if key_healthy:
        disabled = set(failed_now)  # only models that failed this probe
        disabled.difference_update(healthy_now)
    else:
        disabled.update(failed_now)
        disabled.difference_update(healthy_now)
    updates = {
        "endpoint_capabilities": endpoint_caps,
        "model_health": model_health,
        "disabled_models": sorted(disabled),
    }
    if key_healthy:
        updates["enabled"] = True
        if not key_entry.get("default_model"):
            updates["default_model"] = next((r["model"] for r in endpoint_results if r.get("healthy")), default_model)
    updated_key = update_key_data(vendor_id, key_id, **updates) or key_entry if persist else {**key_entry, **updates}
    latency_ms = int((time.time() - start) * 1000)
    used = next((r["model"] for r in endpoint_results if r.get("healthy")), None)
    # Prefer highest-priority concrete failure across tried models
    fail_msgs = [str(r.get("error") or "") for r in endpoint_results if r.get("error")]
    first_error = "No usable model"
    if fail_msgs:
        best, best_rank = fail_msgs[0], 999
        for msg in fail_msgs:
            code = classify_health_error(msg).get("code") or "other"
            try:
                rank = _ERROR_CODE_PRIORITY.index(code)
            except ValueError:
                rank = len(_ERROR_CODE_PRIORITY)
            if rank < best_rank:
                best_rank = rank
                best = msg
        first_error = best
    result = {
        "key_id": key_id,
        "vendor_id": vendor_id,
        "healthy": key_healthy,
        "latency_ms": latency_ms,
        "error": None if key_healthy else first_error,
        "message": f"[{used}] endpoint ok" if key_healthy and used else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
        "default_model": updated_key.get("default_model") or default_model,
        "check_model": check_model,
        "used_model": used,
        "tried_models": [r["model"] for r in endpoint_results],
        "used_check_model": bool(check_model and used == check_model),
        "primary_check_failed": bool(check_model and used and used != check_model),
        "models_refreshed": bool(scanned) if scan_models_flag else False,
        "check_layer": "model-endpoints",
        "check_layers": {"model_endpoints": {"healthy": key_healthy, "results": endpoint_results}},
        "endpoint_capabilities": endpoint_caps,
        "wants_responses": False,
    }
    _enrich_health_result(result)
    if persist:
        with _lock:
            cache = _load_cache()
            prev = cache.get(cache_key) or {}
            _apply_streak_and_schedule(result, prev)
            cache[cache_key] = result
            _save_cache(cache)
    else:
        _apply_streak_and_schedule(result, previous_health or {})
        result["_key_updates"] = updates
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
    """Probe each model×endpoint on a key.

    A model is usable when at least one endpoint succeeds.  In manual mode the
    selected endpoints are authoritative, so a failure on an unselected
    endpoint does not disable the model.  The complete matrix is retained in
    ``endpoint_capabilities`` for backend reconciliation and the UI.
    """
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
    if vendor.get("archived") or key_entry.get("archived"):
        return {"error": "Archived — excluded from health checks", "results": [], "archived": True}

    api_url = vendor.get("proxy_target", "") or vendor.get("api_url", "")
    api_key = key_entry.get("api_key", "")
    models = list_model_ids(key_entry)

    # If inventory empty, try scanning once (system retains scan results)
    if not models:
        scan_type = _resolve_check_type(vendor)
        scanned = scan_models(scan_type, api_url, api_key)
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
    endpoint_capabilities = dict(key_entry.get("endpoint_capabilities") or {})

    for mid in models:
        matrix = check_model_endpoints(vendor_id, key_id, mid, persist=False)
        checks = matrix.get("checks") or {}
        previous = endpoint_capabilities.get(mid) if isinstance(endpoint_capabilities.get(mid), dict) else {}
        mode = str(previous.get("mode") or "auto").lower()
        selected = list(previous.get("selected") or [])
        detected = list(matrix.get("detected") or [])
        if mode == "manual":
            usable = [ep for ep in selected if checks.get(ep, {}).get("healthy") is True]
        else:
            usable = detected
        healthy = bool(usable)
        ok_messages = [checks[ep].get("message") for ep in usable if checks.get(ep, {}).get("message")]
        fail_msg = summarize_endpoint_failures(checks, mode=mode)
        msg = "; ".join(str(x) for x in ok_messages[:2]) if ok_messages else fail_msg
        latency_ms = int(matrix.get("latency_ms") or 0)
        state = capability_record(
            detected=detected,
            checks=checks,
            mode=mode,
            selected=selected,
        )
        endpoint_capabilities[mid] = state
        entry = {
            "model": mid,
            "healthy": healthy,
            "latency_ms": latency_ms,
            "message": msg if healthy else None,
            "error": None if healthy else fail_msg,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "check_layer": "model",
            "endpoints": usable,
            "detected_endpoints": detected,
            "endpoint_checks": checks,
            "endpoint_mode": mode,
            "selected_endpoints": selected,
        }
        _enrich_health_result(entry)
        results.append(entry)
        model_health[mid] = {
            "healthy": healthy,
            "latency_ms": latency_ms,
            "error": None if healthy else fail_msg,
            "message": msg if healthy else None,
            "checked_at": entry["checked_at"],
            "endpoints": usable,
            "detected_endpoints": detected,
            "endpoint_checks": checks,
            "endpoint_mode": mode,
            "selected_endpoints": selected,
            "error_code": entry.get("error_code"),
            "error_label": entry.get("error_label"),
            "suggestion": entry.get("suggestion"),
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
        "endpoint_capabilities": endpoint_capabilities,
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
            "endpoint_capabilities": updated.get("endpoint_capabilities") or {},
        }
        _save_cache(cache)

    # Rebuild backend configs from the current system state. This is important
    # for single-slot adapters: model-level failures must select the same
    # primary/backup key as a normal full push.
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
    """True when the key currently has something backends may use.

    Fresh probe result wins over stale per-model history:
    - health.healthy is True  → usable
    - health.healthy is False → not usable (do NOT resurrect via old model_health)
    - no fresh verdict        → fall back to non-disabled model_health ok entries
    """
    if not key or not key.get("api_key"):
        return False
    if health is None:
        health = {}
    if health.get("healthy") is True:
        return True
    # Explicit key/model probe failure must strip backends; stale model_health
    # from an older per-model pass must not keep dead/quota keys alive.
    if health.get("healthy") is False:
        return False
    disabled = set(key.get("disabled_models") or [])
    mh = key.get("model_health") or {}
    for mid, rec in mh.items():
        if mid in disabled:
            continue
        if isinstance(rec, dict) and rec.get("healthy") is True:
            return True
    return False


# Short-lived health cache snapshot so reconcile does not re-read a multi-MB
# JSON file for every key × every backend.
_health_snap = None
_health_snap_at = 0.0
_HEALTH_SNAP_TTL = 3.0


def get_health_cache_snapshot(*, max_age: float = None) -> dict:
    """Return a process-local snapshot of the health cache (refreshed every few seconds)."""
    global _health_snap, _health_snap_at
    ttl = _HEALTH_SNAP_TTL if max_age is None else float(max_age)
    now = time.monotonic()
    with _lock:
        if _health_snap is None or (now - _health_snap_at) > ttl:
            _health_snap = _load_cache() or {}
            _health_snap_at = now
        return _health_snap


def invalidate_health_cache_snapshot() -> None:
    global _health_snap, _health_snap_at
    with _lock:
        _health_snap = None
        _health_snap_at = 0.0


def is_key_backend_syncable(vendor_id: str, key: dict, *, cache: dict = None) -> bool:
    """Whether a key may be written into backend engine configs.

    - disabled keys: never
    - known unhealthy (latest key health False): never
    - only a recent successful key and model-endpoint probe: yes

    Pass ``cache`` (from :func:`get_health_cache_snapshot`) when checking many
    keys so reconcile does not reload the cache file hundreds of times.
    """
    if not key or not key.get("api_key"):
        return False
    if key.get("enabled") is False:
        return False
    kid = str(key.get("id") or "")
    if not kid:
        return False
    if cache is None:
        cache = get_health_cache_snapshot()
    h = (cache or {}).get(f"{vendor_id}:{kid}") or {}
    # Latest key-level verdict is authoritative — never keep quota/auth failures
    # because an older model_health entry still says ok.
    if h.get("healthy") is not True:
        return False
    checked = _parse_iso(h.get("checked_at") or "")
    if checked is None:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - checked > timedelta(hours=24):
        return False
    from core.endpoints import model_is_verified_usable
    # Cap model scan — key health only needs one verified model
    mids = list_model_ids(key)
    if not mids:
        return False
    return any(model_is_verified_usable(key, mid) for mid in mids[:30])


def apply_health_to_backends(vendor: dict, key: dict, health: dict, *, reconcile: bool = True) -> None:
    """Push healthy keys (auto-enable + sync); strip failed keys from backends.

    Policy: only the fresh probe result decides. Stale model_health must not
    keep no-quota / auth-failed keys on engines, and must not flip UI to green.
    """
    if not vendor or not key:
        return
    usable = bool(health.get("healthy"))
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
        try:
            health["enabled"] = True
            if updates.get("models") is not None:
                health["models"] = updates["models"]
            if updates.get("default_model"):
                health["default_model"] = updates["default_model"]
        except Exception:
            pass
    else:
        try:
            from core.data import get_settings
            updates = {}
            # Drop stale per-model ok flags so a later reconcile cannot resurrect
            # this key via old model_health after a key-level probe failure.
            if key.get("model_health"):
                updates["model_health"] = {}
            if bool((get_settings() or {}).get("health_auto_disable")):
                updates["enabled"] = False
                health["enabled"] = False
            if updates:
                update_key_data(vendor["id"], key["id"], **updates)
        except Exception:
            pass
    # Rebuild once after persistence. Failed keys are now excluded by the
    # health cache and enabled state, while healthy backups can take over.
    if reconcile:
        reconcile_all()


def check_all_keys(
    include_disabled: bool = True,
    *,
    only_due: bool = False,
    force: bool = False,
    quick: bool = False,
    concurrency: int = 1,
    max_round_seconds: int = 0,
    progress_callback=None,
) -> list[dict]:
    """Probe vendor keys. Healthy keys are auto-enabled and synced to backends.

    only_due: when True (scheduled runs), skip keys whose next_check_at is in the future
              (adaptive interval after consecutive ok/fail streaks).
    force: ignore due schedule (manual full check).
    """
    results = []
    skipped = 0
    probed = 0
    now = datetime.now(timezone.utc)
    with _lock:
        cache_snap = dict(_load_cache())

    targets = []
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
            targets.append((v, k))

    def _check_target(target):
        vendor, key = target
        cache_key = f"{vendor['id']}:{key['id']}"
        health = check_key_health(
            vendor["id"],
            key["id"],
            scan_models_flag=not quick,
            quick=quick,
            persist=False,
            previous_health=cache_snap.get(cache_key) or {},
        )
        return health

    def _report_progress(phase: str = "probing"):
        if progress_callback:
            try:
                # Support both (done, total) and (done, total, phase)
                try:
                    progress_callback(probed, len(targets), phase)
                except TypeError:
                    progress_callback(probed, len(targets))
            except Exception:
                pass

    if targets:
        _report_progress()
        workers = max(1, min(16, int(concurrency or 1), len(targets)))
        if workers == 1:
            for target in targets:
                results.append(_check_target(target))
                probed += 1
                _report_progress()
        else:
            dispatch_lock = threading.Lock()
            result_lock = threading.Lock()
            next_target = 0
            deadline = time.monotonic() + max_round_seconds if max_round_seconds > 0 else None

            def _worker():
                nonlocal next_target, probed
                while True:
                    with dispatch_lock:
                        if next_target >= len(targets) or (deadline and time.monotonic() >= deadline):
                            return
                        target = targets[next_target]
                        next_target += 1
                    vendor, key = target
                    try:
                        row = _check_target(target)
                    except Exception as exc:
                        row = {
                            "vendor_id": vendor.get("id"),
                            "key_id": key.get("id"),
                            "healthy": False,
                            "latency_ms": 0,
                            "error": str(exc)[:300],
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                    with result_lock:
                        results.append(row)
                        probed += 1
                        _report_progress()

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="health-probe") as pool:
                futures = [pool.submit(_worker) for _ in range(workers)]
                for future in as_completed(futures):
                    future.result()

            if next_target < len(targets):
                for vendor, key in targets[next_target:]:
                    ck = f"{vendor['id']}:{key['id']}"
                    row = dict(cache_snap.get(ck) or {
                        "vendor_id": vendor["id"],
                        "key_id": key["id"],
                        "healthy": None,
                        "latency_ms": 0,
                        "error": "Deferred to next scheduled round",
                    })
                    row["skipped"] = True
                    row["skip_reason"] = "round_deadline"
                    results.append(row)
                    skipped += 1

    if probed:
        auto_disable = bool((get_settings() or {}).get("health_auto_disable"))
        bulk_updates = []
        cache_updates = []
        for row in results:
            if row.get("skipped"):
                continue
            updates = dict(row.pop("_key_updates", {}) or {})
            # Drop huge nested blobs from the in-memory health cache row
            row.pop("endpoint_capabilities", None)
            row.pop("check_layers", None)
            if row.get("healthy") is True:
                updates["enabled"] = True
                # Only patch models that were probed this pass (merge in _apply_key_fields)
                tried = list(row.get("tried_models") or [])
                caps = updates.get("endpoint_capabilities")
                if isinstance(caps, dict) and tried:
                    updates["endpoint_capabilities"] = {
                        mid: caps[mid] for mid in tried if mid in caps
                    }
                mh = updates.get("model_health")
                if isinstance(mh, dict) and tried:
                    updates["model_health"] = {
                        mid: mh[mid] for mid in tried if mid in mh
                    }
            else:
                # Key failed: clear model_health so stale ok flags cannot resurrect sync
                updates["model_health"] = {}
                if auto_disable:
                    updates["enabled"] = False
                # Auto-archive keys that have been continuously failing for
                # health_archive_streak_days (default 10).
                days = _archive_streak_days()
                if days and _consecutive_fail_days(row) is not None and _consecutive_fail_days(row) >= days:
                    updates["archived"] = True
            bulk_updates.append({
                "vendor_id": row.get("vendor_id"),
                "key_id": row.get("key_id"),
                "updates": _slim_key_updates_for_bulk(updates),
            })
            # Compact cache entry for disk
            cache_updates.append({
                "vendor_id": row.get("vendor_id"),
                "key_id": row.get("key_id"),
                "healthy": row.get("healthy"),
                "latency_ms": row.get("latency_ms"),
                "error": (str(row.get("error") or "")[:200] or None) if row.get("healthy") is False else None,
                "error_code": row.get("error_code"),
                "error_label": row.get("error_label"),
                "suggestion": row.get("suggestion"),
                "checked_at": row.get("checked_at"),
                "used_model": row.get("used_model"),
                "tried_models": list(row.get("tried_models") or [])[:8],
                "ok_streak": row.get("ok_streak"),
                "fail_streak": row.get("fail_streak"),
                "fail_since": row.get("fail_since"),
                "schedule_mode": row.get("schedule_mode"),
                "next_check_at": row.get("next_check_at"),
                "message": row.get("message"),
            })
        try:
            update_keys_data_bulk(bulk_updates)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("bulk health persist failed")
        with _lock:
            cache = _load_cache()
            for row in cache_updates:
                cache[f"{row.get('vendor_id')}:{row.get('key_id')}"] = row
            _save_cache(cache)
        # Never block the health run on slow backend file rewrites — push async.
        try:
            from backends import reconcile_all_async
            reconcile_all_async()
        except Exception:
            try:
                reconcile_all()
            except Exception:
                pass
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


def get_all_health_status(*, blocking: bool = False) -> dict:
    results = {}
    global _health_snap, _health_snap_at
    acquired = _lock.acquire(blocking=blocking)
    if acquired:
        try:
            cache = _load_cache()
            _health_snap = cache
            _health_snap_at = time.monotonic()
        finally:
            _lock.release()
    else:
        cache = _health_snap if isinstance(_health_snap, dict) else {}
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
