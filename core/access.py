"""Network access policy: when the web UI needs a token, and where it comes from.

Binding to loopback (the default, ``127.0.0.1``) keeps the app local, so no
token is required.  Binding to a routable address (``0.0.0.0``, a LAN or
Tailscale IP, ...) exposes the API key database to anything that can reach the
port, so a token is generated on first start and printed to the console.

Overrides:

* ``AI_SWITCH_REQUIRE_AUTH=1|0`` -- force the policy on or off.
* ``AI_SWITCH_ACCESS_TOKEN=...`` -- use this token instead of the stored one
  (handy for Docker secrets / systemd units).
"""
from __future__ import annotations

import os
import secrets
from typing import Optional

_LOOPBACK_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost"}


def is_loopback_host(host: Optional[str]) -> bool:
    """True when ``host`` only reaches this machine.

    ``0.0.0.0`` / ``::`` are *not* loopback: they accept connections on every
    interface, which is exactly the case that needs a token.
    """
    h = str(host or "").strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    if h.startswith("127."):
        return True
    if h == "::1":
        return True
    return False


def require_auth_for_host(host: Optional[str]) -> bool:
    """Whether a token should be required for this bind address."""
    override = os.environ.get("AI_SWITCH_REQUIRE_AUTH")
    if override is not None and str(override).strip() != "":
        return str(override).strip().lower() in ("1", "true", "yes", "on")
    return not is_loopback_host(host)


def access_token_from_env() -> str:
    return str(os.environ.get("AI_SWITCH_ACCESS_TOKEN") or "").strip()


def _stored_token() -> str:
    try:
        from core.data import get_settings

        return str((get_settings() or {}).get("access_token") or "").strip()
    except Exception:
        return ""


def resolve_access_token(host: Optional[str], *, persist: bool = True) -> dict:
    """Decide the effective access token for this run.

    Returns a dict with:

    ``auth_required``  whether requests must present the token
    ``token``          the effective token ("" when none is needed)
    ``created``        True when a new token was generated and persisted
    ``source``         ``env`` | ``settings`` | ``generated`` | ``none``
    """
    env_token = access_token_from_env()
    if env_token:
        return {"auth_required": True, "token": env_token, "created": False, "source": "env"}

    stored = _stored_token()
    if not require_auth_for_host(host):
        # Loopback bind: keep working exactly as before. An existing token is
        # still honoured so a user who set one is not silently unprotected.
        if stored:
            return {"auth_required": True, "token": stored, "created": False, "source": "settings"}
        return {"auth_required": False, "token": "", "created": False, "source": "none"}

    if stored:
        return {"auth_required": True, "token": stored, "created": False, "source": "settings"}

    token = secrets.token_urlsafe(24)
    if persist:
        try:
            from core.data import update_settings

            update_settings(access_token=token)
        except Exception:
            # Not fatal: the token is still enforced for this process.
            pass
    return {"auth_required": True, "token": token, "created": True, "source": "generated"}


def format_access_notice(host: Optional[str], port: int, info: dict) -> str:
    """Console notice for a non-loopback bind ("" when no token is needed)."""
    if not info.get("auth_required") or not info.get("token"):
        return ""
    line = "─" * 64
    addr = f"{host}:{port}"
    where = {
        "generated": "a new token was generated and stored in your AI Switch data",
        "settings": "using the token stored in Settings",
        "env": "using AI_SWITCH_ACCESS_TOKEN",
    }.get(info.get("source"), "")
    parts = [
        line,
        f" Bound to {addr} — reachable from other machines on your network.",
        " Access is therefore protected by a token" + (f" ({where})." if where else "."),
        "",
        f"   Access token: {info['token']}",
        "",
        f" Open http://{addr} and paste it into the login screen.",
        " To disable (not recommended): AI_SWITCH_REQUIRE_AUTH=0",
        line,
    ]
    return "\n".join(parts)
