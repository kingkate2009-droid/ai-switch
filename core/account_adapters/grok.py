"""Grok CLI account adapter (import + switch).

Credential location: ``~/.grok/auth.json`` — a registry keyed by
``"<oidc_issuer>::<oidc_client_id>"`` (the default CLI entry is
``https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828``)::

    {"https://auth.x.ai::b1a00492-...": {
        "key": "<access token>", "auth_mode": "oidc",
        "email": "...", "refresh_token": "...", "user_id": "..."}}
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.account_adapters.base import (
    AccountAdapter,
    SwitchError,
    atomic_write_json,
    home,
    read_json,
)

OIDC_ISSUER = "https://auth.x.ai"
OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_REGISTRY_KEY = f"{OIDC_ISSUER}::{OIDC_CLIENT_ID}"


class GrokAdapter(AccountAdapter):
    provider = "grok"
    label = "Grok CLI"
    aliases = ("xai", "x", "grok-cli", "grok_cli")
    supports_import = True
    supports_switch = True
    supports_oauth = True
    supports_quota = True
    install_hint = "未检测到 Grok CLI 登录态（~/.grok/auth.json）"

    def credential_path(self) -> Optional[Path]:
        return home() / ".grok" / "auth.json"

    def _read_registry(self) -> Optional[dict]:
        obj = read_json(self.credential_path())
        return obj if isinstance(obj, dict) else None

    def _pick_entry(self, registry: dict) -> Optional[dict]:
        entry = registry.get(DEFAULT_REGISTRY_KEY)
        if isinstance(entry, dict):
            return entry
        for key, value in registry.items():
            if isinstance(value, dict) and str(key).endswith(f"::{OIDC_CLIENT_ID}"):
                return value
        for value in registry.values():
            if isinstance(value, dict) and value.get("key"):
                return value
        return None

    def read(self) -> Optional[dict]:
        registry = self._read_registry()
        if not registry:
            return None
        entry = self._pick_entry(registry)
        if not entry:
            return None
        access = str(entry.get("key") or entry.get("access_token") or "").strip()
        refresh = str(entry.get("refresh_token") or "").strip()
        if not (access or refresh):
            return None
        return {
            "provider": self.provider,
            "email": str(entry.get("email") or "").strip(),
            "account_id": str(entry.get("user_id") or entry.get("principal_id") or "").strip(),
            "access_token": access,
            "refresh_token": refresh,
            "source": "local",
        }

    def switch(self, account: dict) -> dict:
        token = str(account.get("access_token") or account.get("sso_token") or "").strip()
        if not token:
            raise SwitchError("该账号没有可写入的 token")
        path = self.credential_path()
        registry = self._read_registry() or {}
        entry = self._pick_entry(registry)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry["key"] = token
        entry["auth_mode"] = "oidc"
        if account.get("email"):
            entry["email"] = str(account["email"])
        if account.get("refresh_token"):
            entry["refresh_token"] = str(account["refresh_token"])
        if account.get("account_id"):
            entry["user_id"] = str(account["account_id"])
        registry[DEFAULT_REGISTRY_KEY] = entry
        atomic_write_json(path, registry)
        return {
            "provider": self.provider,
            "target": str(path),
            "backup": str(path.with_name(path.name + ".ai-switch.bak")),
            "written": [str(path)],
        }
