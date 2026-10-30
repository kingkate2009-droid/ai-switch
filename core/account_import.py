"""Parse OAuth / login credentials into account records.

Accepts the real-world shapes emitted by the CLIs:

- Codex CLI   ``~/.codex/auth.json``  ->  ``{"tokens": {id_token, access_token,
  refresh_token, account_id}, ...}``
- Claude Code ``~/.claude/.credentials.json`` -> ``{"claudeAiOauth": {accessToken,
  refreshToken, expiresAt, scopes, subscriptionType}}`` or a bare
  ``sk-ant-oat01-...`` token / ``claude_auth_token=...`` line
- Gemini CLI  ``~/.gemini/oauth_creds.json`` -> ``{"access_token", "refresh_token",
  "scope", "expiry_date", "id_token"}``
- Grok        ``sso=...`` cookie value or a raw SSO token

Multiple entries can be pasted at once (JSON array, newline-separated JSON, or
one token per line). A ``provider`` hint forces classification.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

_PROVIDER_ALIASES = {
    "chatgpt": "codex", "openai": "codex", "codex-cli": "codex", "codex_cli": "codex",
    "anthropic": "claude", "claude-code": "claude", "claude_code": "claude", "claudeai": "claude",
    "google": "gemini", "gemini-cli": "gemini", "gemini_cli": "gemini",
    "antigravity-ide": "antigravity", "antigravity_ide": "antigravity", "jetski": "antigravity",
    "xai": "grok", "x": "grok", "grok-cli": "grok",
}


def _norm_provider(value: str) -> str:
    p = str(value or "").strip().lower()
    return _PROVIDER_ALIASES.get(p, p)


def _as_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _decode_jwt(token: str) -> dict:
    token = _as_str(token)
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _jwt_exp_ms(token: str) -> Optional[int]:
    claims = _decode_jwt(token)
    exp = claims.get("exp")
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return None
    return exp * 1000 if exp > 0 else None


def _to_ms(value) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, str) and not value.strip().isdigit():
        # ISO string
        import datetime as _dt
        try:
            return int(_dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    # seconds vs ms heuristic
    if n < 10_000_000_000:
        n *= 1000
    return n


def _result(provider: str, fields: dict, *, source_name: str = "") -> dict:
    return {
        "provider": provider,
        "fields": fields,
        "source_name": source_name,
        "label": fields.get("label") or fields.get("email") or provider,
        "error": None,
    }


def _error(msg: str, *, source_name: str = "") -> dict:
    return {"provider": "", "fields": {}, "source_name": source_name, "label": "", "error": msg}


# ── object classifiers ─────────────────────────────────────────────

def _classify_codex(obj: dict) -> Optional[dict]:
    tokens = obj.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = _as_str(tokens.get("access_token"))
    refresh = _as_str(tokens.get("refresh_token"))
    id_token = _as_str(tokens.get("id_token"))
    account_id = _as_str(tokens.get("account_id"))
    if not (access or refresh):
        return None
    claims = _decode_jwt(id_token)
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    email = _as_str(claims.get("email"))
    fields = {
        "provider": "codex",
        "email": email,
        "account_id": account_id or _as_str(auth.get("chatgpt_account_id")),
        "plan_type": _as_str(auth.get("chatgpt_plan_type")),
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "expires_at": _jwt_exp_ms(access),
        "source": "import",
    }
    return _result("codex", fields, source_name="codex auth.json")


def _classify_claude(obj: dict) -> Optional[dict]:
    inner = obj.get("claudeAiOauth") if isinstance(obj.get("claudeAiOauth"), dict) else None
    src = inner or obj
    access = _as_str(src.get("accessToken") or src.get("access_token"))
    refresh = _as_str(src.get("refreshToken") or src.get("refresh_token"))
    if not (access or refresh):
        return None
    if access and not (access.startswith("sk-ant-") or access.startswith("oat")):
        # avoid misclassifying unrelated tokens
        if not inner:
            return None
    scopes = src.get("scopes") or src.get("scope") or []
    if isinstance(scopes, str):
        scopes = [s for s in re.split(r"[\s,]+", scopes) if s]
    fields = {
        "provider": "claude",
        "email": _as_str(obj.get("email") or src.get("email") or src.get("email_address")),
        "access_token": access,
        "refresh_token": refresh,
        "scopes": scopes if isinstance(scopes, list) else [],
        "expires_at": _to_ms(src.get("expiresAt") or src.get("expires_at")) or _jwt_exp_ms(access),
        "plan_type": _as_str(src.get("subscriptionType") or src.get("subscription_type")),
        "source": "import",
    }
    return _result("claude", fields, source_name="claude credentials")


def _classify_gemini(obj: dict) -> Optional[dict]:
    access = _as_str(obj.get("access_token") or obj.get("accessToken"))
    refresh = _as_str(obj.get("refresh_token") or obj.get("refreshToken"))
    scope = _as_str(obj.get("scope"))
    id_token = _as_str(obj.get("id_token") or obj.get("idToken"))
    looks_google = (
        "googleapis.com" in scope
        or "cloud-platform" in scope
        or access.startswith("ya29.")
        or "expiry_date" in obj
        or "expiry" in obj and _decode_jwt(id_token).get("email")
    )
    if not (access or refresh) or not looks_google:
        return None
    claims = _decode_jwt(id_token)
    scopes = [s for s in re.split(r"[\s,]+", scope) if s]
    fields = {
        "provider": "gemini",
        "email": _as_str(claims.get("email")),
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "scopes": scopes,
        "expires_at": _to_ms(obj.get("expiry_date") or obj.get("expiry")) or _jwt_exp_ms(access),
        "project_id": _as_str(obj.get("project_id") or obj.get("quota_project_id")),
        "source": "import",
    }
    return _result("gemini", fields, source_name="gemini oauth_creds")


def _classify_grok(obj: dict) -> Optional[dict]:
    sso = _as_str(obj.get("sso") or obj.get("sso_token") or obj.get("ssoToken"))
    if not sso:
        return None
    fields = {
        "provider": "grok",
        "sso_token": sso,
        "email": _as_str(obj.get("email")),
        "source": "import",
    }
    return _result("grok", fields, source_name="grok sso")


def _classify_object(obj: dict, hint: str = "") -> Optional[dict]:
    hint = _norm_provider(hint)
    order = [hint] if hint else []
    order += ["codex", "claude", "gemini", "grok"]
    seen = set()
    for prov in order:
        if not prov or prov in seen:
            continue
        seen.add(prov)
        if prov == "codex":
            r = _classify_codex(obj)
        elif prov == "claude":
            r = _classify_claude(obj)
        elif prov == "gemini":
            r = _classify_gemini(obj)
        elif prov == "grok":
            r = _classify_grok(obj)
        else:
            r = None
        if r:
            return r
    return None


# ── raw token classifier ───────────────────────────────────────────

def _classify_token(token: str, hint: str = "") -> Optional[dict]:
    t = token.strip().strip("\"'")
    if not t:
        return None
    # strip common "name=" prefixes
    m = re.match(r"^(?:claude_auth_token|sso|sso_token|token|access_token)\s*[:=]\s*(.+)$", t, re.I)
    if m:
        t = m.group(1).strip().strip("\"'")
    hint = _norm_provider(hint)

    if hint == "claude" or t.startswith("sk-ant-oat"):
        return _result("claude", {"provider": "claude", "access_token": t, "source": "import"},
                       source_name="claude token")
    if hint == "gemini" or t.startswith("ya29."):
        return _result("gemini", {"provider": "gemini", "access_token": t, "source": "import"},
                       source_name="gemini token")
    if hint == "grok":
        return _result("grok", {"provider": "grok", "sso_token": t, "source": "import"},
                       source_name="grok sso")
    if hint == "codex":
        return _result("codex", {"provider": "codex", "access_token": t, "source": "import"},
                       source_name="codex token")
    # JWT with openai auth claim -> codex
    claims = _decode_jwt(t)
    if isinstance(claims.get("https://api.openai.com/auth"), dict):
        return _result("codex", {"provider": "codex", "access_token": t,
                                 "expires_at": _jwt_exp_ms(t), "source": "import"},
                       source_name="codex token")
    return None


# ── public API ─────────────────────────────────────────────────────

def parse_accounts(text: str, provider_hint: str = "") -> dict:
    """Parse raw text into {items, errors, count}."""
    raw = (text or "").strip()
    items: list = []
    errors: list = []
    if not raw:
        return {"items": items, "errors": errors, "count": 0}

    # 1) whole text is JSON
    try:
        obj = json.loads(raw)
        parsed = _parse_json_value(obj, provider_hint)
        if parsed:
            return parsed
    except Exception:
        pass

    # 2) newline-separated JSON objects / raw tokens
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        if line.startswith("{") or line.startswith("["):
            try:
                obj = json.loads(line)
                parsed = _parse_json_value(obj, provider_hint)
                if parsed:
                    items.extend(parsed["items"])
                    errors.extend(parsed["errors"])
                    continue
            except Exception:
                pass
        tok = _classify_token(line, provider_hint)
        if tok:
            items.append(tok)
        else:
            errors.append({"source_name": f"line {idx + 1}", "error": "无法识别的凭据", "raw": line[:40]})

    return {"items": items, "errors": errors, "count": len(items)}


def _parse_json_value(obj, provider_hint: str) -> Optional[dict]:
    items: list = []
    errors: list = []
    if isinstance(obj, list):
        for i, entry in enumerate(obj):
            if not isinstance(entry, dict):
                errors.append({"source_name": f"item {i + 1}", "error": "不是对象"})
                continue
            r = _classify_object(entry, provider_hint)
            if r:
                items.append(r)
            else:
                errors.append({"source_name": f"item {i + 1}", "error": "无法识别的账号结构"})
        return {"items": items, "errors": errors, "count": len(items)}
    if isinstance(obj, dict):
        r = _classify_object(obj, provider_hint)
        if r:
            return {"items": [r], "errors": [], "count": 1}
        return None
    return None
