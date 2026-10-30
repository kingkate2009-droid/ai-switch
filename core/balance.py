"""Fetch real-time balance/quota from upstream providers.

Background (mirrors mainstream tooling)
----------------------------------------
For **New API / One API / sub2api** relay sites (the most common upstream for
API-key aggregators), the balance is *not* reachable with the downstream
``sk-xxx`` API key. Those sites expose the account balance through their
dashboard API:

    GET {base}/api/status       -> data.quota_per_unit  (quota units per USD)
    GET {base}/api/user/self    -> data.quota, data.used_quota
                                   (Authorization: Bearer <access_token>)

The ``access_token`` is the system access token a user creates in the site's
"Personal Settings" page (a UUID for New API / One API). It is distinct from
the ``sk-`` relay key used for inference. A key entry may therefore carry an
optional ``access_token`` field used only for balance lookups.

Quota units are integers; the USD value is ``quota / quota_per_unit`` where
``quota_per_unit`` defaults to 500000 on New API. One API uses the same
concept (its config ``QuotaPerUnit``).

For native providers we use their documented billing endpoints where they
exist (OpenRouter ``/api/v1/credits``, DeepSeek ``/v1/user_balance``).

Every fetcher degrades gracefully and never raises to the caller.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from core.providers import _new_session, get_probe_timeout

log = logging.getLogger(__name__)

_DEFAULT_QUOTA_PER_UNIT = 500000.0  # New API / One API default: 500000 quota = $1
_BALANCE_TIMEOUT = 10

# In-memory cache: {cache_key: (timestamp, result)}
_cache: dict = {}
_CACHE_TTL = 300  # 5 minutes


# -- cache helpers ---------------------------------------------------------

def _mask_secret(value: str) -> str:
    value = str(value or "")
    if len(value) >= 8:
        return value[:4] + "****" + value[-4:]
    return value


def _ckey(provider_id: str, api_url: str, api_key: str, access_token: str = "") -> str:
    return "|".join([
        str(provider_id or ""),
        str(api_url or ""),
        _mask_secret(api_key),
        _mask_secret(access_token),
    ])


def _cache_get(cache_key: str) -> Optional[dict]:
    entry = _cache.get(cache_key)
    if entry is None:
        return None
    ts, result = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[cache_key]
        return None
    return result


def _cache_set(cache_key: str, result: dict) -> None:
    _cache[cache_key] = (time.time(), result)


def clear_balance_cache() -> None:
    _cache.clear()
    _ratios_cache.clear()


# -- ratios (model ratio + group ratio) ------------------------------------

# {base_url: (timestamp, result)}
_ratios_cache: dict = {}
_RATIOS_TTL = 600  # 10 minutes


def fetch_ratios(vendor: dict, key_entry: dict, force: bool = False) -> dict:
    """Fetch a relay site's model ratios and group ratios.

    New API exposes them via the public ``GET /api/pricing``::

        {"data": [{"model_name": ..., "model_ratio": ..., "completion_ratio": ...}],
         "group_ratio": {"default": 1, "vip": 0.9, ...}}

    Returns ``{group_ratio, user_group, model_ratio, completion_ratio, source}``.
    """
    vendor = vendor or {}
    key_entry = key_entry or {}
    base = _relay_base(str(vendor.get("api_url") or ""))
    if not base:
        return {"error": "No base URL configured", "group_ratio": {}, "model_ratio": {}}
    now = time.time()
    if not force and base in _ratios_cache:
        ts, res = _ratios_cache[base]
        if now - ts < _RATIOS_TTL:
            return res

    access_token = (str(key_entry.get("access_token") or vendor.get("access_token") or "")).strip()
    user_id = (str(vendor.get("access_token_user_id") or key_entry.get("access_token_user_id") or "")).strip()
    headers = _bearer_headers(access_token, user_id)

    result = {"group_ratio": {}, "user_group": "", "model_ratio": {}, "completion_ratio": {}, "model_price": {}, "source": ""}
    if _relay_family(base, headers) != "new-api":
        result["error"] = "Ratios are only available on New API family sites"
        _ratios_cache[base] = (now, result)
        return result

    session = _new_session()
    try:
        r = session.get(base + "/api/pricing", headers=headers, timeout=get_probe_timeout(_BALANCE_TIMEOUT))
        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code} on /api/pricing"
            _ratios_cache[base] = (now, result)
            return result
        body = r.json() or {}
        if body.get("success") is False:
            result["error"] = _relay_err_msg(body, "ratios not exposed")
            _ratios_cache[base] = (now, result)
            return result
        gr = body.get("group_ratio")
        if isinstance(gr, dict):
            result["group_ratio"] = {str(k): _to_float(v, 1.0) for k, v in gr.items()}
        for item in (body.get("data") or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("model_name") or item.get("model") or "").strip()
            if not name:
                continue
            if item.get("model_ratio") is not None:
                result["model_ratio"][name] = _to_float(item.get("model_ratio"), 0.0)
            if item.get("completion_ratio") is not None:
                result["completion_ratio"][name] = _to_float(item.get("completion_ratio"), 0.0)
            if item.get("model_price") is not None:
                result["model_price"][name] = _to_float(item.get("model_price"), 0.0)
        result["source"] = "new-api:/api/pricing"
    except Exception as exc:
        result["error"] = f"ratios exception: {str(exc)[:200]}"
        _ratios_cache[base] = (now, result)
        return result

    # The user's group (determines which group_ratio applies)
    try:
        u = session.get(base + "/api/user/self", headers=headers, timeout=get_probe_timeout(_BALANCE_TIMEOUT))
        if u.status_code == 200:
            ud = _relay_payload(u.json() or {})
            if isinstance(ud, dict):
                result["user_group"] = str(ud.get("group") or "")
    except Exception:
        pass

    _ratios_cache[base] = (now, result)
    return result


def get_cached_balance(
    provider_id: str = "",
    api_url: str = "",
    api_key: str = "",
    access_token: str = "",
) -> Optional[dict]:
    """Return a cached balance result without hitting the network, or None."""
    return _cache_get(_ckey(provider_id, api_url, api_key, access_token))


# -- helpers ---------------------------------------------------------------

def _empty_result(provider_id: str) -> dict:
    return {
        "provider": provider_id,
        "available": None,
        "total": None,
        "used": None,
        "currency": "USD",
        "reset_at": None,
        "raw": None,
        "error": None,
        "source": "",
    }


def _bearer_headers(api_key: str, user_id: str = "") -> dict:
    headers = {
        "User-Agent": "AI-Switch/2.2 (balance probe)",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Some New API forks require the logged-in user id header alongside the
    # system access token, otherwise /api/user/self returns HTTP 401
    # "New-Api-User header not provided".
    if user_id:
        headers["New-Api-User"] = str(user_id).strip()
    return headers


def _looks_like_relay(url: str) -> bool:
    """Heuristic: hosts that usually run New API / One API dashboards."""
    low = (url or "").lower()
    return any(token in low for token in (
        "newapi", "new-api", "one-api", "oneapi", "sub2api",
        "anyrouter", "cliproxy",
    ))


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# -- New API / One API / sub2api ------------------------------------------

_VERSION_SUFFIXES = ("/v1beta", "/v1", "/v2", "/v3", "/v4", "/openai", "/v1beta/", "/api")


def _relay_base(url: str) -> str:
    """Return the site root suitable for dashboard ``/api/...`` calls.

    A vendor's ``api_url`` usually points at the inference endpoint
    (``https://host/v1``), but New API's dashboard lives at the site root
    (``https://host/api/user/self``). Strip trailing ``/v1`` & friends so we
    do not request ``/v1/api/user/self`` (which returns 404).
    """
    base = (url or "").strip().rstrip("/")
    # Repair malformed schemes like "https:host" (missing //)
    if base.startswith("https:") and not base.startswith("https://"):
        base = "https://" + base[len("https:"):].lstrip("/")
    elif base.startswith("http:") and not base.startswith("http://"):
        base = "http://" + base[len("http:"):].lstrip("/")
    for suffix in _VERSION_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/")


def _relay_401(resp, user_id: str = "") -> dict:
    """Build a clear error for HTTP 401 from a relay dashboard."""
    msg = ""
    try:
        msg = str((resp.json() or {}).get("message") or "")
    except Exception:
        msg = (resp.text or "")[:200]
    low = msg.lower()
    needs_uid = (
        ("new-api-user" in low and ("not provided" in low or "\u672a\u63d0\u4f9b" in msg))
        or "user_id_not_provided" in low
        or ("user-id" in low and "not provided" in low)
    )
    if needs_uid:
        return {
            "error": (
                "This site requires the 'New-Api-User' header. Add your numeric "
                "user ID on this key (Site \u2192 Personal Settings shows it)."
            ),
            "needs_user_id": True,
        }
    if "new-api-user" in low and "does not match" in low:
        return {
            "error": (
                "The saved user ID does not match this access token. "
                "Fix the user ID on this key."
            ),
            "needs_user_id": True,
        }
    return {"error": msg or "Access token rejected (HTTP 401); regenerate it on the site"}


def _fetch_relay_balance(
    url: str,
    api_key: str,
    access_token: str,
    provider_id: str = "",
    user_id: str = "",
) -> dict:
    """Fetch quota from a New API / One API style dashboard.

    Requires an access token (system token). Without it we return a clear
    message telling the user where to get one.
    """
    base = _relay_base(url)
    if not base:
        return {"error": "No base URL configured"}

    token = (access_token or "").strip()
    if not token:
        return {
            "error": (
                "Relay balance needs a system access token. Create one in the "
                "site's Personal Settings and store it on this key."
            ),
            "needs_access_token": True,
        }

    headers = _bearer_headers(token, user_id)

    # 1) quota_per_unit from /api/status (public)
    quota_per_unit = _DEFAULT_QUOTA_PER_UNIT
    currency = "USD"
    try:
        status_resp = _new_session().get(
            base + "/api/status",
            headers={"User-Agent": "AI-Switch/2.2 (balance probe)", "Accept": "application/json"},
            timeout=get_probe_timeout(_BALANCE_TIMEOUT),
        )
        if status_resp.status_code == 200:
            status_data = status_resp.json() or {}
            data = status_data.get("data") if isinstance(status_data.get("data"), dict) else status_data
            qpu = _to_float((data or {}).get("quota_per_unit"), 0.0)
            if qpu > 0:
                quota_per_unit = qpu
            usd_rate = (data or {}).get("usd_exchange_rate")
            if usd_rate:
                currency = "USD"
    except Exception:
        pass

    # 2) account quota from /api/user/self
    resp = _new_session().get(
        base + "/api/user/self",
        headers=headers,
        timeout=get_probe_timeout(_BALANCE_TIMEOUT),
    )
    if resp.status_code == 401:
        return _relay_401(resp, user_id)
    if resp.status_code == 403:
        return {"error": "Access token lacks permission (HTTP 403)"}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code} on /api/user/self"}

    try:
        payload = resp.json() or {}
    except Exception:
        return {"error": "Invalid JSON from /api/user/self"}

    if payload.get("success") is False:
        return {"error": str(payload.get("message") or "API returned success=false")[:200]}

    data = payload.get("data")
    if not isinstance(data, dict):
        return {"error": "Unexpected balance payload shape"}

    quota = _to_float(data.get("quota"), 0.0)
    used_quota = _to_float(data.get("used_quota"), 0.0)

    available_usd = quota / quota_per_unit if quota_per_unit else 0.0
    used_usd = used_quota / quota_per_unit if quota_per_unit else 0.0
    total_usd = available_usd + used_usd

    return {
        "available": round(available_usd, 6),
        "total": round(total_usd, 6) if total_usd else None,
        "used": round(used_usd, 6) if used_usd else None,
        "currency": currency,
        "reset_at": None,
        "source": "relay:/api/user/self",
        "raw": {
            "quota": quota,
            "used_quota": used_quota,
            "quota_per_unit": quota_per_unit,
            "request_count": data.get("request_count"),
            "username": data.get("username"),
            "group": data.get("group"),
            "aff_quota": data.get("aff_quota"),
        },
    }


# -- New API / One API / sub2api check-in ----------------------------------

# sub2api family: /api/v1 prefix, {"code":0,"message":...,"data":...} envelope.
# Check-in is a per-deployment extension, so we probe candidate paths.
_SUB2API_PREFIX = "/api/v1"
_SUB2API_CHECKIN_CANDIDATES = (
    "/user/checkin",
    "/user/check-in",
    "/user/daily-checkin",
    "/checkin",
    "/user/sign-in",
    "/user/signin",
    "/user/daily",
    "/user/reward/checkin",
)
_CHECKIN_STATUS_MARKERS = (
    "checked_in_today", "can_checkin", "total_checkins", "checkin_dates",
    "total_balance_awarded", "checkin_count", "total_quota", "last_checkin",
)


def _relay_ok(body) -> bool:
    """Success flag for both envelopes: {success:bool} and {code:int}."""
    if not isinstance(body, dict):
        return False
    if "success" in body:
        return bool(body.get("success"))
    if "code" in body:
        c = body.get("code")
        return c in (0, 200, "0")
    return False


def _relay_payload(body):
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        return body.get("data")
    return body


def _relay_err_msg(body, fallback: str = "") -> str:
    if isinstance(body, dict):
        for k in ("message", "msg", "error", "detail"):
            v = body.get(k)
            if v:
                return str(v)[:220]
        txt = body.get("_text")
        if txt:
            return str(txt)[:220]
    if isinstance(body, str) and body:
        return body[:220]
    return fallback


def _needs_turnstile(msg: str) -> bool:
    """True when a check-in error is really a Cloudflare Turnstile requirement.

    Some deployments enable Turnstile on the check-in endpoint. A programmatic
    claim then fails with messages like "Turnstile token 为空" / "人机验证".
    Such check-ins require a human to solve the captcha in the browser.
    """
    low = (msg or "").lower()
    return any(
        k in low or k in (msg or "")
        for k in (
            "turnstile", "captcha", "cf-turnstile", "cloudflare",
            "人机验证", "安全验证", "验证码", "请完成验证",
            "token 为空", "token为空", "token is empty", "token missing",
        )
    )


def _relay_family(base: str, headers: dict) -> str:
    """Return 'new-api' or 'sub2api' (best effort). Defaults to 'new-api'."""
    session = _new_session()
    try:
        r = session.get(
            base + "/api/status",
            headers={"User-Agent": headers.get("User-Agent", "AI-Switch"), "Accept": "application/json"},
            timeout=get_probe_timeout(_BALANCE_TIMEOUT),
        )
        if r.status_code == 200:
            data = _relay_payload(r.json() or {})
            if isinstance(data, dict) and ("version" in data or "system_name" in data or "turnstile_check" in data):
                return "new-api"
    except Exception:
        pass
    # sub2api embedding window.__APP_CONFIG__
    for path in ("/checkin", "/"):
        try:
            r = session.get(base + path, headers={"User-Agent": headers.get("User-Agent", "AI-Switch")},
                            timeout=get_probe_timeout(_BALANCE_TIMEOUT))
            if "window.__APP_CONFIG__" in (r.text or ""):
                return "sub2api"
        except Exception:
            continue
    # Probe /api/v1/status reachability
    try:
        r = session.get(base + _SUB2API_PREFIX + "/status", timeout=get_probe_timeout(_BALANCE_TIMEOUT))
        if r.status_code not in (404, 502, 503):
            return "sub2api"
    except Exception:
        pass
    return "new-api"


def _sub2api_resolve_checkin(base: str, headers: dict) -> tuple:
    """Probe candidate sub2api check-in paths → (status_path, claim_path)."""
    session = _new_session()
    for cand in _SUB2API_CHECKIN_CANDIDATES:
        path = _SUB2API_PREFIX + cand
        try:
            r = session.get(base + path, headers=headers, timeout=get_probe_timeout(_BALANCE_TIMEOUT))
        except Exception:
            continue
        if r.status_code in (404, 405):
            continue
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        data = _relay_payload(body)
        if isinstance(data, dict) and any(m in data for m in _CHECKIN_STATUS_MARKERS):
            return path, path
        if r.status_code == 200 and isinstance(data, dict) and data:
            return path, path
    return None, None


def checkin(vendor: dict, key_entry: dict) -> dict:
    """Perform (or query) a daily sign-in on a New API / One API / sub2api relay.

    Returns a normalized dict with:
      - ``claimed``  bool: whether the reward was just claimed
      - ``quota_awarded`` float|None: quota units granted today (None when not claimed this call)
      - ``checkin_date`` str|None: today's date if already checked in
      - ``min_quota`` / ``max_quota`` int|None: site configured bounds
      - ``stats`` dict|None: month stats from GET (signed count etc.)
    """
    base = _relay_base(str((vendor or {}).get("api_url") or ""))
    access_token = (str((key_entry or {}).get("access_token") or (vendor or {}).get("access_token") or "")).strip()
    provider_id = str((vendor or {}).get("provider") or "")

    if not base:
        return {"error": "No base URL configured", "claimed": False}
    if not access_token:
        return {
            "error": (
                "Relay check-in needs a system access token. Create one in the "
                "site's Personal Settings and store it on this key."
            ),
            "needs_access_token": True,
            "claimed": False,
        }

    user_id = (str((vendor or {}).get("access_token_user_id") or (key_entry or {}).get("access_token_user_id") or "")).strip()
    headers = _bearer_headers(access_token, user_id)
    today = _today_str()
    session = _new_session()

    family = _relay_family(base, headers)
    if family == "sub2api":
        return _checkin_sub2api(base, headers, today)

    # 1) status first
    try:
        status_resp = session.get(
            base + "/api/user/checkin",
            headers=headers,
            timeout=get_probe_timeout(_BALANCE_TIMEOUT),
        )
        if status_resp.status_code == 200:
            status_payload = status_resp.json() or {}
            status_data = _relay_payload(status_payload)
            if isinstance(status_data, dict):
                stats = status_data.get("stats") if isinstance(status_data.get("stats"), dict) else status_data
                summary = _checkin_stats_summary(stats, today)
                if summary["checked_in_today"]:
                    return {
                        "claimed": False,
                        "checkin_date": today,
                        "checked_in_today": True,
                        "checkin_count": summary["count"],
                        "records": summary["records"],
                        "min_quota": status_data.get("min_quota"),
                        "max_quota": status_data.get("max_quota"),
                        "enabled": status_data.get("enabled"),
                        "source": "relay:/api/user/checkin",
                    }
    except Exception:
        pass

    # 2) attempt to claim
    try:
        resp = session.post(
            base + "/api/user/checkin",
            headers=headers,
            timeout=get_probe_timeout(_BALANCE_TIMEOUT),
        )
        if resp.status_code == 401:
            return _relay_401(resp, user_id)
        if resp.status_code == 403:
            return {"error": "Access token lacks permission (HTTP 403)", "claimed": False}
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code} on check-in", "claimed": False}
        payload = resp.json() or {}
        if not _relay_ok(payload) and _relay_err_msg(payload):
            msg = _relay_err_msg(payload, "check-in failed")
            # "already signed in today" is not a real failure
            if any(x in msg for x in ("\u5df2\u7b7e\u5230", "\u4eca\u65e5\u5df2", "already", "signed in today")):
                return {
                    "claimed": False,
                    "checkin_date": today,
                    "checked_in_today": True,
                    "source": "relay:/api/user/checkin",
                }
            if _needs_turnstile(msg):
                return {
                    "claimed": False,
                    "needs_turnstile": True,
                    "error": "This site requires a captcha (Turnstile) to check in. Open the site's check-in page to complete it manually.",
                    "source": "relay:/api/user/checkin",
                }
            return {"error": msg[:200], "claimed": False}
        data = _relay_payload(payload)
        if isinstance(data, dict):
            return {
                "claimed": True,
                "quota_awarded": _to_float(data.get("quota_awarded") or data.get("reward") or data.get("balance_awarded"), 0.0),
                "checkin_date": data.get("checkin_date") or today,
                "checked_in_today": True,
                "min_quota": data.get("min_quota"),
                "max_quota": data.get("max_quota"),
                "source": "relay:/api/user/checkin",
                "raw": data,
            }
    except Exception as exc:
        return {"error": f"check-in exception: {str(exc)[:200]}", "claimed": False}

    return {"error": "check-in not available on this site", "claimed": False}


def _checkin_sub2api(base: str, headers: dict, today: str) -> dict:
    """Check-in for the sub2api family (per-deployment paths, {code} envelope)."""
    session = _new_session()
    status_path, claim_path = _sub2api_resolve_checkin(base, headers)
    if not claim_path:
        return {
            "claimed": False,
            "unsupported": True,
            "error": "This sub2api site does not expose a check-in endpoint",
            "source": "sub2api:probe",
        }

    # 1) status first
    try:
        s_resp = session.get(status_path, headers=headers, params={"month": today[:7], "timezone": "Asia/Shanghai"},
                             timeout=get_probe_timeout(_BALANCE_TIMEOUT))
        if s_resp.status_code == 200:
            body = s_resp.json() or {}
            data = _relay_payload(body) or {}
            stats = data.get("stats") if isinstance(data, dict) and isinstance(data.get("stats"), dict) else data
            if isinstance(stats, dict):
                done = bool(stats.get("checked_in_today") or stats.get("checked_today"))
                if done:
                    return {
                        "claimed": False,
                        "checkin_date": today,
                        "checked_in_today": True,
                        "checkin_count": _to_int(stats.get("total_checkins") or stats.get("checkin_count"), None),
                        "source": "sub2api:" + status_path,
                    }
    except Exception:
        pass

    # 2) claim
    try:
        resp = session.post(claim_path, headers=headers, timeout=get_probe_timeout(_BALANCE_TIMEOUT))
        if resp.status_code == 401:
            return _relay_401(resp, "")
        if resp.status_code in (404, 405):
            return {"claimed": False, "unsupported": True, "error": "check-in endpoint not available", "source": "sub2api:" + claim_path}
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code} on check-in", "claimed": False}
        body = resp.json() or {}
        data = _relay_payload(body)
        msg = _relay_err_msg(body, "")
        if _relay_ok(body):
            reward = 0.0
            if isinstance(data, dict):
                for k in ("balance_awarded", "quota_awarded", "reward", "amount", "points_awarded", "quota"):
                    if data.get(k) is not None:
                        reward = _to_float(data.get(k), 0.0)
                        break
            return {
                "claimed": True,
                "quota_awarded": reward,
                "checkin_date": today,
                "checked_in_today": True,
                "source": "sub2api:" + claim_path,
                "raw": data if isinstance(data, dict) else {},
            }
        low = (msg or "").lower()
        if any(t in msg for t in ("\u5df2\u7b7e\u5230", "\u5df2\u7ecf\u7b7e\u5230", "\u91cd\u590d\u7b7e\u5230")) or any(
            t in low for t in ("already", "duplicate", "has checked")
        ):
            return {"claimed": False, "checkin_date": today, "checked_in_today": True, "source": "sub2api:" + claim_path}
        if _needs_turnstile(msg):
            return {
                "claimed": False,
                "needs_turnstile": True,
                "error": "This sub2api site requires a captcha (Turnstile) to check in. Open the site's check-in page to complete it manually.",
                "source": "sub2api:" + claim_path,
            }
        return {"error": msg or "check-in failed", "claimed": False}
    except Exception as exc:
        return {"error": f"check-in exception: {str(exc)[:200]}", "claimed": False}


def _to_int(value, default):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default



def checkin_status(vendor: dict, key_entry: dict) -> dict:
    """Query today's check-in status without claiming the reward.

    Mirrors :func:`checkin` but only does the GET (never POSTs), returning
    ``checkin_date`` when already signed in today and ``claimed=False``.
    """
    base = _relay_base(str((vendor or {}).get("api_url") or ""))
    access_token = (str((key_entry or {}).get("access_token") or (vendor or {}).get("access_token") or "")).strip()

    if not base:
        return {"error": "No base URL configured", "claimed": False}
    if not access_token:
        return {
            "error": (
                "Relay check-in needs a system access token. Create one in the "
                "site's Personal Settings and store it on this key."
            ),
            "needs_access_token": True,
            "claimed": False,
        }

    user_id = (str((vendor or {}).get("access_token_user_id") or (key_entry or {}).get("access_token_user_id") or "")).strip()
    headers = _bearer_headers(access_token, user_id)
    today = _today_str()
    session = _new_session()

    family = _relay_family(base, headers)
    if family == "sub2api":
        status_path, _ = _sub2api_resolve_checkin(base, headers)
        if not status_path:
            return {"claimed": False, "checked_in_today": False, "unsupported": True,
                    "error": "This sub2api site does not expose a check-in endpoint", "source": "sub2api:probe"}
        try:
            resp = session.get(status_path, headers=headers, params={"month": today[:7], "timezone": "Asia/Shanghai"},
                               timeout=get_probe_timeout(_BALANCE_TIMEOUT))
            if resp.status_code == 401:
                return _relay_401(resp, "")
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code} on check-in status", "claimed": False}
            body = resp.json() or {}
            data = _relay_payload(body) or {}
            stats = data.get("stats") if isinstance(data, dict) and isinstance(data.get("stats"), dict) else data
            stats = stats if isinstance(stats, dict) else {}
            done = bool(stats.get("checked_in_today") or stats.get("checked_today"))
            return {
                "claimed": False,
                "checked_in_today": done,
                "checkin_date": today if done else None,
                "checkin_count": _to_int(stats.get("total_checkins") or stats.get("checkin_count"), None),
                "records": [],
                "source": "sub2api:" + status_path,
            }
        except Exception as exc:
            return {"error": f"check-in status exception: {str(exc)[:200]}", "claimed": False}

    try:
        resp = session.get(
            base + "/api/user/checkin",
            headers=headers,
            timeout=get_probe_timeout(_BALANCE_TIMEOUT),
        )
        if resp.status_code == 401:
            return _relay_401(resp, user_id)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code} on check-in status", "claimed": False}
        payload = resp.json() or {}
        data = _relay_payload(payload)
        if isinstance(data, dict):
            stats = data.get("stats") if isinstance(data.get("stats"), dict) else data
            summary = _checkin_stats_summary(stats, today)
            enabled = data.get("enabled")
            if enabled is False:
                return {
                    "claimed": False,
                    "checked_in_today": False,
                    "enabled": False,
                    "unsupported": True,
                    "error": "Check-in is disabled on this site",
                    "source": "relay:/api/user/checkin",
                }
            return {
                "claimed": False,
                "checked_in_today": summary["checked_in_today"],
                "checkin_date": today if summary["checked_in_today"] else None,
                "checkin_count": summary["count"],
                "records": summary["records"],
                "enabled": enabled,
                "min_quota": data.get("min_quota"),
                "max_quota": data.get("max_quota"),
                "source": "relay:/api/user/checkin",
            }
        return {"error": _relay_err_msg(payload, "check-in status unavailable"), "claimed": False}
    except requests.exceptions.Timeout:
        return {"error": "Timeout checking check-in status", "claimed": False}
    except Exception as exc:
        return {"error": f"check-in status exception: {str(exc)[:200]}", "claimed": False}


# -- New API / One API / sub2api consumption logs -------------------------


def fetch_consumption_logs(
    vendor: dict,
    key_entry: dict,
    days: int = 7,
    max_entries: int = 500,
) -> dict:
    """Fetch recent consumption records from ``/api/log/self``.

    Each record contains ``model_name``, ``quota`` (consumed quota units),
    ``prompt_tokens``, ``completion_tokens``, ``token_name``, ``created_at``.

    Returns a dict with:
      - ``logs`` — raw records
      - ``summary`` — aggregated by model_name: cost, calls, tokens, avg_latency
      - ``by_key`` — aggregated by token_name (API key name)
      - ``quota_per_unit`` — conversion rate for USD computation
      - ``error`` — if the fetch failed
    """
    base = _relay_base(str((vendor or {}).get("api_url") or ""))
    access_token = (str((key_entry or {}).get("access_token") or (vendor or {}).get("access_token") or "")).strip()
    provider_id = str((vendor or {}).get("provider") or "")

    if not base:
        return {"error": "No base URL configured", "logs": [], "summary": {}, "by_key": {}}
    if not access_token:
        return {
            "error": "Consumption logs need a system access token",
            "needs_access_token": True,
            "logs": [], "summary": {}, "by_key": {},
        }

    user_id = (str((vendor or {}).get("access_token_user_id") or (key_entry or {}).get("access_token_user_id") or "")).strip()
    headers = _bearer_headers(access_token, user_id)
    import time as _time

    now = int(_time.time())
    start_ts = now - days * 86400
    session = _new_session()

    # 1) quota_per_unit from /api/status
    quota_per_unit = _DEFAULT_QUOTA_PER_UNIT
    try:
        sr = session.get(
            base + "/api/status",
            headers={"User-Agent": "AI-Switch/2.2 (balance probe)", "Accept": "application/json"},
            timeout=get_probe_timeout(_BALANCE_TIMEOUT),
        )
        if sr.status_code == 200:
            sd = (sr.json() or {}).get("data") or sr.json()
            qpu = _to_float((sd or {}).get("quota_per_unit"), 0)
            if qpu > 0:
                quota_per_unit = qpu
    except Exception:
        pass

    # 2) fetch logs in pages (newest first, up to max_entries)
    logs = []
    page = 0
    page_size = min(max_entries, 100)
    try:
        while len(logs) < max_entries:
            offset = page * page_size
            remaining = max_entries - len(logs)
            if remaining < page_size:
                page_size = remaining

            log_resp = session.get(
                base + "/api/log/self",
                params={
                    "type": 2,
                    "start_timestamp": str(start_ts),
                    "end_timestamp": str(now),
                    "p": str(page),
                    "page_size": str(page_size),
                },
                headers=headers,
                timeout=get_probe_timeout(15),
            )
            if log_resp.status_code == 401:
                break
            if log_resp.status_code != 200:
                break
            body = log_resp.json() or {}
            if not body.get("success"):
                break
            items = body.get("data", body.get("items", []))
            if isinstance(items, list) and items:
                logs.extend(items)
                total = body.get("total", 0) or body.get("total_count", 0)
                if offset + len(items) >= total:
                    break
                page += 1
            else:
                break
    except Exception as exc:
        return {
            "error": str(exc)[:200],
            "logs": logs,
            "summary": _summarize_logs(logs, quota_per_unit),
            "by_key": _summarize_logs_by_key(logs, quota_per_unit),
            "quota_per_unit": quota_per_unit,
        }

    return {
        "error": None,
        "logs": logs,
        "summary": _summarize_logs(logs, quota_per_unit),
        "by_key": _summarize_logs_by_key(logs, quota_per_unit),
        "quota_per_unit": quota_per_unit,
    }


def _summarize_logs(logs: list, quota_per_unit: float) -> dict:
    """Aggregate logs by model_name."""
    from collections import defaultdict

    agg = defaultdict(lambda: {"calls": 0, "quota": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "errors": 0})
    for r in logs:
        if isinstance(r, dict):
            m = r.get("model_name") or "unknown"
            agg[m]["calls"] += 1
            agg[m]["quota"] += int(r.get("quota") or 0)
            agg[m]["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
            agg[m]["completion_tokens"] += int(r.get("completion_tokens") or 0)
            if r.get("type") == 5:
                agg[m]["errors"] += 1
    for m in agg:
        a = agg[m]
        a["total_tokens"] = a["prompt_tokens"] + a["completion_tokens"]
        a["cost_usd"] = round(a["quota"] / quota_per_unit, 6) if quota_per_unit else 0
        a["success_rate"] = round((a["calls"] - a["errors"]) / a["calls"] * 100, 1) if a["calls"] else 100
    # sort by cost desc
    return dict(sorted(agg.items(), key=lambda x: x[1]["cost_usd"], reverse=True))


def _summarize_logs_by_key(logs: list, quota_per_unit: float) -> dict:
    """Aggregate logs by token_name (== key name)."""
    from collections import defaultdict

    agg = defaultdict(lambda: {"calls": 0, "quota": 0, "total_tokens": 0, "cost_usd": 0.0, "models": set()})
    for r in logs:
        if isinstance(r, dict):
            kn = r.get("token_name") or "unknown"
            agg[kn]["calls"] += 1
            agg[kn]["quota"] += int(r.get("quota") or 0)
            agg[kn]["total_tokens"] += int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
            agg[kn]["models"].add(r.get("model_name") or "unknown")
    for kn in agg:
        a = agg[kn]
        a["cost_usd"] = round(a["quota"] / quota_per_unit, 6) if quota_per_unit else 0
        a["model_count"] = len(a["models"])
        del a["models"]
    return dict(sorted(agg.items(), key=lambda x: x[1]["cost_usd"], reverse=True))


def _today_str() -> str:
    import datetime as _dt
    return _dt.date.today().isoformat()


def _checkin_today(stats: dict, today: str) -> bool:
    """Inspect checkin stats to see if already signed in today.

    Real New API shape::

        {"checked_in_today": true, "checkin_count": 22,
         "records": [{"checkin_date": "2026-09-22", "quota_awarded": 132855}, ...]}
    """
    if not isinstance(stats, dict):
        return False
    if stats.get("checked_in_today") is True:
        return True
    recs = stats.get("records")
    if isinstance(recs, list):
        for r in recs:
            if isinstance(r, dict) and str(r.get("checkin_date") or "")[:10] == today:
                return True
    if str(stats.get("last_checkin") or "")[:10] == today:
        return True
    if str(stats.get("checkin_date") or "")[:10] == today:
        return True
    dates = stats.get("checkin_dates") or stats.get("dates")
    if isinstance(dates, list):
        return today in [str(d)[:10] for d in dates]
    return False


def _checkin_stats_summary(stats: dict, today: str) -> dict:
    """Normalize checkin stats into {checked_in_today, count, records}. """
    out = {"checked_in_today": False, "count": None, "records": []}
    if not isinstance(stats, dict):
        return out
    out["checked_in_today"] = _checkin_today(stats, today)
    cnt = stats.get("checkin_count")
    if cnt is not None:
        try:
            out["count"] = int(cnt)
        except (TypeError, ValueError):
            pass
    recs = stats.get("records")
    if isinstance(recs, list):
        out["records"] = [
            {
                "date": str(r.get("checkin_date") or "")[:10],
                "reward": _to_float(r.get("quota_awarded"), 0.0),
            }
            for r in recs if isinstance(r, dict) and r.get("checkin_date")
        ][:31]
    return out


# -- OpenRouter ------------------------------------------------------------

def _fetch_openrouter_balance(url: str, api_key: str) -> dict:
    base = (url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    resp = _new_session().get(
        base + "/api/v1/credits",
        headers=_bearer_headers(api_key),
        timeout=get_probe_timeout(_BALANCE_TIMEOUT),
    )
    if resp.status_code == 401:
        return {"error": "OpenRouter key rejected (HTTP 401)"}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code} on /api/v1/credits"}
    payload = resp.json() or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    total = _to_float((data or {}).get("total_credits"), 0.0)
    used = _to_float((data or {}).get("total_usage"), 0.0)
    available = total - used if total else 0.0
    return {
        "available": round(available, 6),
        "total": round(total, 6) if total else None,
        "used": round(used, 6) if used else None,
        "currency": "USD",
        "source": "openrouter:/api/v1/credits",
        "raw": data,
    }


# -- DeepSeek --------------------------------------------------------------

def _fetch_deepseek_balance(url: str, api_key: str) -> dict:
    base = (url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    resp = _new_session().get(
        base + "/user/balance",
        headers=_bearer_headers(api_key),
        timeout=get_probe_timeout(_BALANCE_TIMEOUT),
    )
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code} on /user/balance"}
    payload = resp.json() or {}
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        info = payload.get("balance") if isinstance(payload.get("balance"), dict) else {}
        infos = [info] if info else []
    if not infos:
        return {"error": "No balance info returned", "raw": payload}
    info = infos[0]
    available = _to_float(info.get("total_balance"), 0.0)
    currency = info.get("currency") or "CNY"
    return {
        "available": round(available, 6),
        "total": None,
        "used": None,
        "currency": currency,
        "source": "deepseek:/user/balance",
        "raw": payload,
    }


# -- Z.AI / Zhipu ----------------------------------------------------------

def _fetch_zhipu_balance(url: str, api_key: str) -> dict:
    base = (url or "").rstrip("/")
    resp = _new_session().get(
        base + "/api/paas/v4/account/balance",
        headers={
            "User-Agent": "AI-Switch/2.2 (balance probe)",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        timeout=get_probe_timeout(_BALANCE_TIMEOUT),
    )
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code} on account balance"}
    payload = resp.json() or {}
    available = _to_float(
        payload.get("available_amount", payload.get("balance", 0.0)),
        0.0,
    )
    return {
        "available": round(available, 6),
        "total": None,
        "used": None,
        "currency": payload.get("currency") or "CNY",
        "source": "zhipu:/account/balance",
        "raw": payload,
    }


# -- generic fallback ------------------------------------------------------

def _fetch_generic_balance(url: str, api_key: str) -> dict:
    base = (url or "").rstrip("/")
    candidates = [
        base + "/api/user/self",
        base + "/user/balance",
        base + "/v1/dashboard/billing/credit_grants",
    ]
    for target in candidates:
        try:
            resp = _new_session().get(
                target,
                headers=_bearer_headers(api_key),
                timeout=get_probe_timeout(_BALANCE_TIMEOUT),
            )
            if resp.status_code != 200:
                continue
            payload = resp.json() or {}
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

            if isinstance(data, dict) and "quota" in data:
                qpu = _DEFAULT_QUOTA_PER_UNIT
                return {
                    "available": round(_to_float(data.get("quota")) / qpu, 6),
                    "total": None,
                    "used": round(_to_float(data.get("used_quota")) / qpu, 6),
                    "currency": "USD",
                    "source": "generic:quota",
                    "raw": data,
                }
            if isinstance(data, dict) and "total_balance" in data:
                return {
                    "available": _to_float(data.get("total_balance")),
                    "total": None,
                    "used": None,
                    "currency": data.get("currency") or "USD",
                    "source": "generic:total_balance",
                    "raw": data,
                }
        except Exception:
            continue
    return {"error": "No balance endpoint responded successfully"}


# -- public entry point ----------------------------------------------------

def fetch_balance(
    provider_id: str = "",
    api_url: str = "",
    api_key: str = "",
    endpoint_type: str = "openai",
    access_token: str = "",
    user_id: str = "",
    force: bool = False,
) -> dict:
    cache_key = _ckey(provider_id, api_url, api_key, access_token)
    if not force:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    pid = (provider_id or "").strip().lower()
    url = (api_url or "").rstrip("/")
    result = _empty_result(pid)

    try:
        if pid in ("newapi", "new-api", "oneapi", "one-api", "sub2api", "cliproxyapi", "anyrouter") or (
            url and _looks_like_relay(url)
        ):
            result.update(_fetch_relay_balance(url, api_key, access_token, pid, user_id))
        elif pid == "openrouter" or (url and "openrouter.ai" in url):
            result.update(_fetch_openrouter_balance(url, api_key))
        elif pid == "deepseek" or (url and "deepseek.com" in url):
            result.update(_fetch_deepseek_balance(url, api_key))
        elif pid in ("zai", "zhipu", "glm") or (url and "bigmodel.cn" in url):
            result.update(_fetch_zhipu_balance(url, api_key))
        else:
            # Last resort: many "custom" vendors are New API relays.
            relay = _fetch_relay_balance(url, api_key, access_token, pid, user_id)
            if relay.get("error") and not relay.get("needs_access_token"):
                result.update(_fetch_generic_balance(url, api_key))
            else:
                result.update(relay)
    except requests.exceptions.Timeout:
        result["error"] = "Timeout fetching balance"
    except requests.exceptions.RequestException as exc:
        result["error"] = f"Network error: {str(exc)[:200]}"
    except Exception as exc:
        result["error"] = f"Unexpected error: {str(exc)[:200]}"
        log.exception("Balance fetch failed for provider %s", pid)

    _cache_set(cache_key, result)
    return result


def get_balance_for_key(vendor: dict, key_entry: dict, force: bool = False) -> dict:
    """Convenience: fetch balance using vendor + key data."""
    vendor = vendor or {}
    key_entry = key_entry or {}
    return fetch_balance(
        provider_id=vendor.get("provider", ""),
        api_url=vendor.get("api_url", ""),
        api_key=key_entry.get("api_key", ""),
        endpoint_type=vendor.get("endpoint_type", "openai"),
        access_token=key_entry.get("access_token") or vendor.get("access_token", ""),
        user_id=vendor.get("access_token_user_id") or key_entry.get("access_token_user_id", ""),
        force=force,
    )
