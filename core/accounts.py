"""OAuth / login-state account store (separate from API-key vendors).

Accounts are the *login* identities (Codex `auth.json`, Claude Code OAuth,
Gemini CLI OAuth, Grok SSO). They are NOT `sk-` API keys and live in their own
file so they can be managed, refreshed and quota-checked independently.

Storage: ``~/.ai-switch/accounts.json`` ::

    {"accounts": [ {account}, ... ]}

Secrets are never returned to the client; callers use :func:`public_view`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from core.data import load_kv_payload, save_kv_payload

log = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".ai-switch"
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
_ACCOUNTS_LOCK = threading.RLock()

_FALLBACK_PROVIDERS = ("claude", "codex", "gemini", "grok")
_FALLBACK_LABELS = {
    "claude": "Claude",
    "codex": "Codex / ChatGPT",
    "gemini": "Gemini",
    "grok": "Grok",
}


def _adapter_registry():
    """The account-adapter registry, or ``None`` if it failed to import."""
    try:
        from core import account_adapters
        return account_adapters
    except Exception:  # pragma: no cover - defensive
        log.exception("account adapter registry unavailable")
        return None


def _resolve_providers() -> tuple:
    """Provider list is driven by the adapter registry so a new tool only needs
    a new adapter module. Falls back to the built-in set if that import fails."""
    registry = _adapter_registry()
    if registry is not None:
        try:
            found = registry.providers()
            if found:
                return found
        except Exception:  # pragma: no cover - defensive
            log.exception("failed to enumerate account adapters")
    return _FALLBACK_PROVIDERS


PROVIDERS = _resolve_providers()
PROVIDER_LABELS = dict(_FALLBACK_LABELS)
_registry = _adapter_registry()
if _registry is not None:
    try:
        PROVIDER_LABELS.update(_registry.labels())
    except Exception:  # pragma: no cover - defensive
        pass

# Fields that must never leave the server.
SECRET_FIELDS = ("access_token", "refresh_token", "id_token", "sso_token", "api_key")


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load() -> dict:
    try:
        data = load_kv_payload("accounts")
        if data is None:
            # one-time migration from the legacy accounts.json
            if ACCOUNTS_PATH.exists():
                try:
                    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    save_kv_payload("accounts", data if isinstance(data, dict) else {"accounts": []})
                except Exception:
                    data = {"accounts": []}
            else:
                data = {"accounts": []}
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data
        return {"accounts": []}
    except Exception:
        log.exception("failed to load accounts store")
        return {"accounts": []}


def _save(data: dict) -> None:
    save_kv_payload("accounts", data)


def _next_id(accounts: list) -> str:
    top = 0
    for a in accounts:
        try:
            top = max(top, int(str(a.get("id") or 0)))
        except Exception:
            continue
    return str(top + 1)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _norm_secret(value: str) -> str:
    return str(value or "").strip()


def _norm_provider(provider: str) -> str:
    p = str(provider or "").strip().lower()
    aliases = {
        "chatgpt": "codex", "openai": "codex", "codex-cli": "codex",
        "anthropic": "claude", "claude-code": "claude", "claudeai": "claude",
        "google": "gemini", "gemini-cli": "gemini",
        "xai": "grok", "x": "grok", "grok-cli": "grok",
    }
    if _registry is not None:
        try:
            resolved = _registry.norm(p)
            if resolved != p:
                return resolved
        except Exception:  # pragma: no cover - defensive
            pass
    return aliases.get(p, p)


def _account_identity(account: dict) -> tuple:
    return (
        _norm_provider(account.get("provider")),
        str(account.get("email") or "").strip().lower(),
        str(account.get("account_id") or "").strip(),
        str(account.get("project_id") or "").strip(),
    )


def _secret_identity(account: dict) -> tuple:
    return (
        _norm_provider(account.get("provider")),
        _norm_secret(account.get("refresh_token")) or _norm_secret(account.get("access_token"))
        or _norm_secret(account.get("sso_token")),
    )


# ── CRUD ───────────────────────────────────────────────────────────

def list_accounts(provider: str = "") -> list:
    with _ACCOUNTS_LOCK:
        accounts = _load().get("accounts", [])
    if provider:
        want = _norm_provider(provider)
        accounts = [a for a in accounts if _norm_provider(a.get("provider")) == want]
    return accounts


def get_account(account_id: str) -> Optional[dict]:
    with _ACCOUNTS_LOCK:
        for a in _load().get("accounts", []):
            if str(a.get("id")) == str(account_id):
                return a
    return None


def find_account(provider: str, *, email: str = "", account_id: str = "",
                 project_id: str = "", secret: str = "") -> Optional[dict]:
    want_provider = _norm_provider(provider)
    email_l = str(email or "").strip().lower()
    account_id = str(account_id or "").strip()
    project_id = str(project_id or "").strip()
    secret = _norm_secret(secret)
    with _ACCOUNTS_LOCK:
        for a in _load().get("accounts", []):
            if _norm_provider(a.get("provider")) != want_provider:
                continue
            if email_l and str(a.get("email") or "").strip().lower() == email_l:
                return a
            if account_id and str(a.get("account_id") or "").strip() == account_id:
                return a
            if project_id and str(a.get("project_id") or "").strip() == project_id:
                return a
            if secret and (
                _norm_secret(a.get("refresh_token")) == secret
                or _norm_secret(a.get("access_token")) == secret
                or _norm_secret(a.get("sso_token")) == secret
            ):
                return a
    return None


_ALLOWED_FIELDS = (
    "provider", "label", "email", "plan_type", "access_token", "refresh_token",
    "id_token", "account_id", "project_id", "scopes", "expires_at", "source",
    "status", "quota", "notes", "raw_meta", "last_refresh_at", "last_error",
    "sso_token",
)


def add_account(fields: dict, *, dedupe: bool = True) -> dict:
    """Insert an account. Returns {account, created, existing}.

    When dedupe is on, an account matching provider + identity/secret is updated
    in place (tokens refreshed) instead of duplicated.
    """
    provider = _norm_provider(fields.get("provider"))
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {fields.get('provider')!r}")

    clean = {k: fields.get(k) for k in _ALLOWED_FIELDS if k in fields}
    clean["provider"] = provider

    with _ACCOUNTS_LOCK:
        data = _load()
        accounts = data.get("accounts", [])

        existing = None
        if dedupe:
            existing = find_account(
                provider,
                email=clean.get("email") or "",
                account_id=clean.get("account_id") or "",
                project_id=clean.get("project_id") or "",
                secret=clean.get("refresh_token") or clean.get("access_token") or clean.get("sso_token") or "",
            )

        now = _now_iso()
        if existing:
            for k, v in clean.items():
                if v in (None, "", [], {}):
                    continue
                existing[k] = v
            existing["updated_at"] = now
            existing.setdefault("status", "active")
            _save(data)
            return {"account": existing, "created": False, "existing": True}

        entry = {
            "id": _next_id(accounts),
            "provider": provider,
            "label": clean.get("label") or clean.get("email") or f"{provider}-{len(accounts) + 1}",
            "email": clean.get("email") or "",
            "plan_type": clean.get("plan_type") or "",
            "access_token": clean.get("access_token") or "",
            "refresh_token": clean.get("refresh_token") or "",
            "id_token": clean.get("id_token") or "",
            "account_id": clean.get("account_id") or "",
            "project_id": clean.get("project_id") or "",
            "sso_token": clean.get("sso_token") or "",
            "scopes": clean.get("scopes") or [],
            "expires_at": clean.get("expires_at") or None,
            "source": clean.get("source") or "import",
            "status": clean.get("status") or "active",
            "quota": clean.get("quota") or None,
            "notes": clean.get("notes") or "",
            "raw_meta": clean.get("raw_meta") or {},
            "created_at": now,
            "updated_at": now,
            "last_refresh_at": None,
            "last_error": "",
        }
        accounts.append(entry)
        data["accounts"] = accounts
        _save(data)
        return {"account": entry, "created": True, "existing": False}


def update_account(account_id: str, **fields) -> Optional[dict]:
    with _ACCOUNTS_LOCK:
        data = _load()
        for a in data.get("accounts", []):
            if str(a.get("id")) != str(account_id):
                continue
            for k, v in fields.items():
                if k in _ALLOWED_FIELDS:
                    a[k] = v
            a["updated_at"] = _now_iso()
            _save(data)
            return a
    return None


def delete_account(account_id: str) -> bool:
    with _ACCOUNTS_LOCK:
        data = _load()
        before = len(data.get("accounts", []))
        data["accounts"] = [a for a in data.get("accounts", []) if str(a.get("id")) != str(account_id)]
        if len(data["accounts"]) != before:
            _save(data)
            return True
    return False


# ── views / helpers ────────────────────────────────────────────────

def _mask(value: str) -> str:
    v = str(value or "")
    if not v:
        return ""
    if len(v) <= 10:
        return v[:2] + "…" + v[-2:]
    return v[:6] + "…" + v[-4:]


def _expiry_state(account: dict) -> str:
    exp = account.get("expires_at")
    try:
        exp_ms = int(exp)
    except (TypeError, ValueError):
        return ""
    if exp_ms <= 0:
        return ""
    now_ms = int(time.time() * 1000)
    if exp_ms <= now_ms:
        return "expired"
    if exp_ms - now_ms < 5 * 60 * 1000:
        return "expiring"
    return "valid"


def public_view(account: dict) -> dict:
    """Secret-free representation for the UI/API."""
    out = {k: v for k, v in account.items() if k not in SECRET_FIELDS}
    out["provider_label"] = PROVIDER_LABELS.get(_norm_provider(account.get("provider")), account.get("provider"))
    out["has_access_token"] = bool(_norm_secret(account.get("access_token")))
    out["has_refresh_token"] = bool(_norm_secret(account.get("refresh_token")))
    out["has_sso_token"] = bool(_norm_secret(account.get("sso_token")))
    out["token_preview"] = _mask(account.get("access_token") or account.get("sso_token") or "")
    out["expiry_state"] = _expiry_state(account)
    out["can_refresh"] = bool(_norm_secret(account.get("refresh_token")))
    return out


def stats(accounts: Optional[list] = None) -> dict:
    accounts = accounts if accounts is not None else list_accounts()
    counts = {p: 0 for p in PROVIDERS}
    active = 0
    for a in accounts:
        p = _norm_provider(a.get("provider"))
        counts[p] = counts.get(p, 0) + 1
        if (a.get("status") or "active") == "active":
            active += 1
    return {"total": len(accounts), "active": active, "by_provider": counts}


# ── backup integration ─────────────────────────────────────────────

def export_accounts() -> list:
    return list_accounts()


def import_accounts(items: list, *, dedupe: bool = True) -> dict:
    added = updated = 0
    errors = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            res = add_account(item, dedupe=dedupe)
            if res.get("created"):
                added += 1
            else:
                updated += 1
        except Exception as e:
            errors.append({"account": item.get("label") or item.get("provider"), "error": str(e)})
    return {"added": added, "updated": updated, "errors": errors}
