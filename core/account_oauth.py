"""Browser OAuth (PKCE + loopback) login for OAuth accounts.

Providers: ``codex`` (ChatGPT), ``claude`` (Anthropic), ``gemini`` (Google).
``grok`` has no public OAuth client and is import-only.

Flow:
  1. ``start_login(provider)`` -> {state, authorization_url, redirect_uri}
     and starts a tiny loopback HTTP server on the provider's callback port.
  2. The browser redirects to ``redirect_uri`` with ``?code=&state=``.
  3. The loopback server exchanges the code, stores the account and marks the
     session success. The frontend polls ``get_login_status(state)``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import requests

log = logging.getLogger(__name__)

PROVIDERS = {
    "codex": {
        "label": "Codex / ChatGPT",
        "auth_url": "https://auth.openai.com/oauth/authorize",
        "token_url": "https://auth.openai.com/oauth/token",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "client_secret": "",
        "scopes": "openid email profile offline_access",
        "port": 1455,
        "path": "/auth/callback",
        "extra_auth_params": {
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        },
        "token_style": "form",
    },
    "claude": {
        "label": "Claude",
        "auth_url": "https://claude.ai/oauth/authorize",
        "token_url": "https://api.anthropic.com/v1/oauth/token",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "client_secret": "",
        "scopes": "org:create_api_key user:profile user:inference",
        "port": 54545,
        "path": "/callback",
        "extra_auth_params": {"code": "true"},
        "token_style": "json",
    },
    "gemini": {
        "label": "Gemini",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "client_id": "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
        # Google's own Gemini CLI ships this client, but a client secret does
        # not belong in a public repository. Supplied via the environment;
        # without it Gemini stays import-only (see supports_login).
        "client_secret_env": "AI_SWITCH_GEMINI_CLIENT_SECRET",
        "client_secret": "",
        "scopes": (
            "https://www.googleapis.com/auth/cloud-platform "
            "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/userinfo.profile"
        ),
        "port": 8085,
        "path": "/oauth2callback",
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
        "token_style": "form",
    },
}

_TIMEOUT = 20
_SESSION_TTL = 10 * 60
_sessions: dict = {}
_sessions_lock = threading.RLock()
_servers: dict = {}


def _redirect_uri(provider: str) -> str:
    cfg = PROVIDERS[provider]
    return f"http://localhost:{cfg['port']}{cfg['path']}"


def supports_login(provider: str) -> bool:
    """Whether a browser OAuth login is possible for ``provider``.

    A provider that needs a client secret is only available when the secret is
    actually configured, so the UI never offers a login that cannot work.
    """
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return False
    if cfg.get("client_secret_env") and not _client_secret(cfg):
        return False
    return True


def _client_secret(cfg: dict) -> str:
    """Client secret from the environment, falling back to a literal if any."""
    env_key = str(cfg.get("client_secret_env") or "").strip()
    if env_key:
        value = str(os.environ.get(env_key) or "").strip()
        if value:
            return value
    return str(cfg.get("client_secret") or "").strip()


# ── PKCE ───────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_verifier() -> str:
    return _b64url(secrets.token_bytes(48))


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())


def _decode_jwt(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
    except Exception:
        return {}


def _jwt_exp_ms(token: str) -> Optional[int]:
    exp = _decode_jwt(token).get("exp")
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return None
    return exp * 1000 if exp > 0 else None


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = True
    return s


# ── authorization / token endpoints ────────────────────────────────

def build_authorization_url(provider: str, state: str, code_verifier: str, redirect_uri: str) -> str:
    cfg = PROVIDERS[provider]
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": cfg["scopes"],
        "state": state,
        "code_challenge": _challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    params.update(cfg.get("extra_auth_params") or {})
    return f"{cfg['auth_url']}?{urlencode(params)}"


def _parse_token_payload(provider: str, body: dict, redirect_uri: str = "") -> dict:
    access = str(body.get("access_token") or "").strip()
    refresh = str(body.get("refresh_token") or "").strip()
    id_token = str(body.get("id_token") or "").strip()
    if not access:
        raise ValueError("token 响应缺少 access_token")
    expires_at = None
    expires_in = body.get("expires_in")
    try:
        if expires_in:
            expires_at = int(time.time() * 1000) + int(float(expires_in)) * 1000
    except (TypeError, ValueError):
        expires_at = None
    if not expires_at:
        expires_at = _jwt_exp_ms(access)

    fields = {
        "provider": provider,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "expires_at": expires_at,
        "source": "oauth",
        "status": "active",
    }
    if provider == "codex":
        claims = _decode_jwt(id_token)
        auth = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
        fields["account_id"] = str(auth.get("chatgpt_account_id") or "")
        fields["plan_type"] = str(auth.get("chatgpt_plan_type") or "")
        fields["email"] = str(claims.get("email") or "")
    elif provider == "claude":
        acct = body.get("account") if isinstance(body.get("account"), dict) else {}
        org = body.get("organization") if isinstance(body.get("organization"), dict) else {}
        fields["email"] = str(acct.get("email_address") or "")
        fields["account_id"] = str(acct.get("uuid") or "")
        fields["raw_meta"] = {"organizationId": str(org.get("uuid") or ""), "organizationName": str(org.get("name") or "")}
    elif provider == "gemini":
        claims = _decode_jwt(id_token)
        fields["email"] = str(claims.get("email") or "")
        fields["project_id"] = str(body.get("project_id") or "")
    return fields


def exchange_code(provider: str, code: str, code_verifier: str, redirect_uri: str, state: str) -> dict:
    cfg = PROVIDERS[provider]
    data = {
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    secret = _client_secret(cfg)
    if secret:
        data["client_secret"] = secret
    if provider == "claude":
        data["state"] = state
    headers = {"Accept": "application/json"}
    try:
        if cfg["token_style"] == "json":
            headers["Content-Type"] = "application/json"
            r = _session().post(cfg["token_url"], json=data, headers=headers, timeout=_TIMEOUT)
        else:
            r = _session().post(cfg["token_url"], data=data, headers=headers, timeout=_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"换取令牌失败: {str(e)[:160]}")
    if r.status_code != 200:
        raise RuntimeError(f"换取令牌失败 HTTP {r.status_code}: {(r.text or '')[:200]}")
    return _parse_token_payload(provider, r.json() or {}, redirect_uri)


def refresh_tokens(provider: str, refresh_token: str) -> dict:
    cfg = PROVIDERS.get(provider)
    if not cfg:
        raise RuntimeError(f"{provider} 不支持刷新")
    data = {
        "grant_type": "refresh_token",
        "client_id": cfg["client_id"],
        "refresh_token": refresh_token,
    }
    secret = _client_secret(cfg)
    if secret:
        data["client_secret"] = secret
    headers = {"Accept": "application/json"}
    if cfg["token_style"] == "json":
        headers["Content-Type"] = "application/json"
        r = _session().post(cfg["token_url"], json=data, headers=headers, timeout=_TIMEOUT)
    else:
        r = _session().post(cfg["token_url"], data=data, headers=headers, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"刷新令牌失败 HTTP {r.status_code}: {(r.text or '')[:200]}")
    payload = _parse_token_payload(provider, r.json() or {})
    # refresh responses may omit refresh_token -> keep the old one
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh_token
    return payload


def ensure_fresh_token(account: dict, skew_ms: int = 60_000) -> Optional[dict]:
    """Refresh the stored access token if it is missing/expired. Returns the
    updated account dict, or None if nothing changed."""
    from core import accounts as acc_store

    provider = str(account.get("provider") or "").lower()
    refresh = str(account.get("refresh_token") or "").strip()
    if not refresh or provider not in PROVIDERS:
        return None
    exp = account.get("expires_at")
    try:
        exp_ms = int(exp)
    except (TypeError, ValueError):
        exp_ms = 0
    now_ms = int(time.time() * 1000)
    if exp_ms and exp_ms - skew_ms > now_ms:
        return None
    payload = refresh_tokens(provider, refresh)
    updated = acc_store.update_account(str(account.get("id")), **{
        k: payload.get(k) for k in (
            "access_token", "refresh_token", "id_token", "expires_at",
            "account_id", "plan_type", "email",
        ) if payload.get(k)
    })
    if updated:
        acc_store.update_account(str(account.get("id")), last_refresh_at=_now_iso(), last_error="")
    return updated


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ── loopback callback server ───────────────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "AISwitchOAuth/1.0"

    def log_message(self, *args):  # silence
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        error = (params.get("error") or [""])[0]
        result = self.server.oauth_result  # type: ignore[attr-defined]
        provider = result.get("provider")
        redirect_uri = result.get("redirect_uri")

        if error:
            self._finish(result, False, f"授权被拒绝: {error}")
            return
        if not code or not state:
            self._respond(400, "缺少 code / state")
            return
        try:
            fields = exchange_code(provider, code, result["code_verifier"], redirect_uri, state)
            from core import accounts as acc_store
            saved = acc_store.add_account(fields)
            self._finish(result, True, account_id=str(saved["account"].get("id")), created=saved.get("created"))
        except Exception as e:
            log.exception("oauth callback exchange failed")
            self._finish(result, False, str(e))

    def _finish(self, result, ok, message, **extra):
        with _sessions_lock:
            result["status"] = "success" if ok else "error"
            result["error"] = "" if ok else message
            result.update(extra)
            result["updated_at"] = _now_iso()
        html = _result_page(ok, message)
        self._respond(200 if ok else 400, html, html=True)

    def _respond(self, status, body, html=False):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _result_page(ok: bool, message: str) -> str:
    title = "登录成功" if ok else "登录失败"
    color = "#16846b" if ok else "#c2414b"
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title></head><body style="font-family:system-ui;text-align:center;padding:64px">
<h1 style="color:{color}">{title}</h1><p>{message}</p>
<p style="color:#7b8494">可以关闭此页面并返回 AI Switch。</p></body></html>"""


def _ensure_server(provider: str, result: dict) -> None:
    port = PROVIDERS[provider]["port"]
    srv = _servers.get(port)
    if srv is not None:
        srv.oauth_result = result  # type: ignore[attr-defined]
        return
    server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.oauth_result = result  # type: ignore[attr-defined]
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True, name=f"oauth-{provider}")
    t.start()
    _servers[port] = server


# ── login session API ──────────────────────────────────────────────

def start_login(provider: str) -> dict:
    provider = str(provider or "").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"{provider} 不支持浏览器登录，请改用导入")
    state = secrets.token_urlsafe(24)
    verifier = _make_verifier()
    redirect_uri = _redirect_uri(provider)
    result = {
        "provider": provider,
        "state": state,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "status": "pending",
        "error": "",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    with _sessions_lock:
        _prune_sessions()
        _sessions[state] = result
    try:
        _ensure_server(provider, result)
    except OSError as e:
        with _sessions_lock:
            result["status"] = "error"
            result["error"] = f"无法监听本地回调端口 {PROVIDERS[provider]['port']}: {e}"
        raise RuntimeError(result["error"])
    return {
        "provider": provider,
        "state": state,
        "authorization_url": build_authorization_url(provider, state, verifier, redirect_uri),
        "redirect_uri": redirect_uri,
        "instructions": f"在浏览器打开授权链接，完成后会自动回调 {redirect_uri}。",
    }


def get_login_status(state: str) -> dict:
    with _sessions_lock:
        _prune_sessions()
        s = _sessions.get(state)
    if not s:
        return {"status": "unknown", "error": "会话不存在或已过期"}
    out = {
        "status": s.get("status"),
        "provider": s.get("provider"),
        "error": s.get("error") or "",
        "account_id": s.get("account_id"),
        "created": s.get("created"),
    }
    return out


def _prune_sessions() -> None:
    now = time.time()
    for key in list(_sessions.keys()):
        try:
            created = _sessions[key].get("created_at")
            import datetime as _dt
            ts = _dt.datetime.fromisoformat(created).timestamp()
            if now - ts > _SESSION_TTL:
                _sessions.pop(key, None)
        except Exception:
            continue
