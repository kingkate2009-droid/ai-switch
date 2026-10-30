"""Official quota lookups for OAuth accounts.

- Claude : ``GET https://api.anthropic.com/api/oauth/usage`` (5h / 7d windows)
- Codex  : probe the responses endpoint and read ``x-codex-*`` rate-limit headers
- Gemini : no official window endpoint -> reported as unsupported
- Grok   : no official window endpoint -> reported as unsupported

Every fetcher degrades gracefully and never raises to the caller.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Optional

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 15
CODEX_PROBE_MODEL = "gpt-5.1-codex"
CODEX_PROBE_VERSION = "0.101.0"
CODEX_PROBE_BETA = "responses-2025-03-11"
CODEX_PROBE_UA = "codex_cli_rs/0.101.0 (Mac OS 26.0.1; arm64) Apple_Terminal/464"


def _iso(ts: Optional[float] = None) -> str:
    import datetime as _dt
    if ts is None:
        return _dt.datetime.now(_dt.timezone.utc).isoformat()
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()


def _unsupported_window(message: str) -> dict:
    return {"supported": False, "message": message}


def _unsupported_snapshot(provider: str) -> dict:
    return {
        "status": "unsupported",
        "source": "official",
        "lastSyncAt": _iso(),
        "providerMessage": f"official quota windows are not exposed for {provider}",
        "windows": {
            "fiveHour": _unsupported_window("官方 5 小时额度窗口不可用"),
            "sevenDay": _unsupported_window("官方 7 天额度窗口不可用"),
        },
    }


def _error_snapshot(provider: str, message: str) -> dict:
    snap = _unsupported_snapshot(provider)
    snap["status"] = "error"
    snap["lastError"] = message
    snap["providerMessage"] = message
    return snap


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = True
    return s


def _parse_iso_or_epoch(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return _iso(ts)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_iso_or_epoch(int(s))
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except Exception:
        return None


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── Claude ─────────────────────────────────────────────────────────

def _claude_window(node) -> dict:
    if not isinstance(node, dict):
        return _unsupported_window("无该窗口数据")
    util = _num(node.get("utilization"))
    if util is None:
        util = _num(node.get("used_percent"))
    reset = _parse_iso_or_epoch(node.get("resets_at") or node.get("resetsAt"))
    if util is None and reset is None:
        return _unsupported_window("无该窗口数据")
    out = {"supported": True, "limit": 100}
    if util is not None:
        out["used"] = round(util, 2)
        out["remaining"] = round(max(0.0, 100.0 - util), 2)
    if reset:
        out["resetAt"] = reset
    out["message"] = "claude oauth usage window"
    return out


def fetch_claude_quota(account: dict, access_token: str) -> dict:
    token = (access_token or "").strip()
    if not token:
        return _error_snapshot("claude", "缺少 access token")
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "claude-code/1.0.0",
    }
    try:
        r = _session().get("https://api.anthropic.com/api/oauth/usage", headers=headers, timeout=_TIMEOUT)
    except Exception as e:
        return _error_snapshot("claude", f"请求失败: {str(e)[:160]}")
    if r.status_code != 200:
        return _error_snapshot("claude", f"HTTP {r.status_code}: {(r.text or '')[:160]}")
    try:
        body = r.json() or {}
    except Exception:
        return _error_snapshot("claude", "响应不是 JSON")
    five = _claude_window(body.get("five_hour"))
    seven = _claude_window(body.get("seven_day"))
    if not five.get("supported") and not seven.get("supported"):
        return _error_snapshot("claude", "响应未包含额度窗口")
    sub = {}
    if isinstance(body.get("extra_usage"), dict):
        sub["planType"] = str(body["extra_usage"].get("plan") or "")
    return {
        "status": "supported",
        "source": "official",
        "lastSyncAt": _iso(),
        "providerMessage": "claude oauth usage (5h / 7d)",
        "subscription": {k: v for k, v in sub.items() if v} or None,
        "windows": {"fiveHour": five, "sevenDay": seven},
    }


# ── Codex ──────────────────────────────────────────────────────────

def _header(headers, name: str) -> Optional[str]:
    for k in headers:
        if k.lower() == name.lower():
            v = headers[k]
            return str(v).strip() if v is not None else None
    return None


def _codex_window(used_pct, reset_after, window_min) -> Optional[dict]:
    used = _num(used_pct)
    reset = _num(reset_after)
    if used is None and reset is None:
        return None
    out = {"supported": True}
    if used is not None:
        out["limit"] = 100
        out["used"] = round(used, 2)
        out["remaining"] = round(max(0.0, 100.0 - used), 2)
    if reset is not None:
        out["resetAt"] = _iso(time.time() + reset)
    wm = _num(window_min)
    if wm is not None:
        out["message"] = f"codex {int(wm)}m window"
    else:
        out["message"] = "codex rate-limit window"
    return out


def fetch_codex_quota(account: dict, access_token: str) -> dict:
    token = (access_token or "").strip()
    if not token:
        return _error_snapshot("codex", "缺少 access token")
    account_id = str(account.get("account_id") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Originator": "codex_cli_rs",
        "Version": CODEX_PROBE_VERSION,
        "User-Agent": CODEX_PROBE_UA,
        "OpenAI-Beta": CODEX_PROBE_BETA,
        "Session_id": str(uuid.uuid4()),
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    payload = {
        "model": CODEX_PROBE_MODEL,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": True,
        "store": False,
        "instructions": "You are a helpful assistant.",
    }
    try:
        r = _session().post(
            "https://chatgpt.com/backend-api/codex/responses",
            headers=headers, json=payload, timeout=_TIMEOUT, stream=True,
        )
    except Exception as e:
        return _error_snapshot("codex", f"请求失败: {str(e)[:160]}")
    h = r.headers
    try:
        r.close()
    except Exception:
        pass
    primary = _codex_window(
        _header(h, "x-codex-primary-used-percent"),
        _header(h, "x-codex-primary-reset-after-seconds"),
        _header(h, "x-codex-primary-window-minutes"),
    )
    secondary = _codex_window(
        _header(h, "x-codex-secondary-used-percent"),
        _header(h, "x-codex-secondary-reset-after-seconds"),
        _header(h, "x-codex-secondary-window-minutes"),
    )
    if primary or secondary:
        # primary is usually the shorter (5h) window
        five = primary or secondary
        seven = secondary or primary
        return {
            "status": "supported",
            "source": "reverse_engineered",
            "lastSyncAt": _iso(),
            "providerMessage": "codex 用量窗口来自速率限制响应头",
            "windows": {
                "fiveHour": five or _unsupported_window("无 5h 数据"),
                "sevenDay": seven or _unsupported_window("无 7d 数据"),
            },
        }
    if r.status_code >= 400:
        return _error_snapshot("codex", f"HTTP {r.status_code}: {(r.text or '')[:160]}")
    return _error_snapshot("codex", "响应未包含 x-codex 速率限制头")


# ── dispatcher ─────────────────────────────────────────────────────

def fetch_quota(account: dict, access_token: str = "") -> dict:
    provider = str(account.get("provider") or "").lower()
    token = (access_token or account.get("access_token") or "").strip()
    if provider == "claude":
        return fetch_claude_quota(account, token)
    if provider == "codex":
        return fetch_codex_quota(account, token)
    if provider in ("gemini", "grok"):
        return _unsupported_snapshot(provider)
    return _error_snapshot(provider or "unknown", "未知账号类型")


def refresh_and_store(account_id: str) -> dict:
    """Refresh quota for one stored account, persisting the snapshot."""
    from core import accounts as acc_store
    from core import account_oauth

    account = acc_store.get_account(account_id)
    if not account:
        return {"error": "账号不存在"}
    provider = str(account.get("provider") or "").lower()

    # Best-effort token refresh before probing providers that need a live token.
    if provider in ("claude", "codex", "gemini"):
        try:
            refreshed = account_oauth.ensure_fresh_token(account)
            if refreshed:
                account = refreshed
        except Exception:
            log.exception("token refresh failed for account %s", account_id)

    snapshot = fetch_quota(account, account.get("access_token") or "")
    acc_store.update_account(account_id, quota=snapshot)
    return snapshot
