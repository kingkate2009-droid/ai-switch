"""Gemini CLI account adapter (import + switch).

Credential location: ``~/.gemini/oauth_creds.json``::

    {"access_token": "...", "refresh_token": "...", "scope": "...",
     "token_type": "Bearer", "id_token": "...", "expiry_date": 1699999999999}
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
    to_ms,
)


class GeminiAdapter(AccountAdapter):
    provider = "gemini"
    label = "Gemini CLI"
    aliases = ("google", "gemini-cli", "gemini_cli")
    supports_import = True
    supports_switch = True
    supports_oauth = True
    supports_quota = False
    install_hint = "未检测到 Gemini CLI 登录态（~/.gemini/oauth_creds.json）"

    def credential_path(self) -> Optional[Path]:
        return home() / ".gemini" / "oauth_creds.json"

    def read(self) -> Optional[dict]:
        obj = read_json(self.credential_path())
        if not obj:
            return None
        access = str(obj.get("access_token") or "").strip()
        refresh = str(obj.get("refresh_token") or "").strip()
        if not (access or refresh):
            return None
        scope = obj.get("scope") or obj.get("scopes") or ""
        if isinstance(scope, str):
            scope = [s for s in scope.replace(",", " ").split() if s]
        return {
            "provider": self.provider,
            "email": str(obj.get("email") or "").strip(),
            "access_token": access,
            "refresh_token": refresh,
            "id_token": str(obj.get("id_token") or "").strip(),
            "scopes": scope,
            "expires_at": to_ms(obj.get("expiry_date") or obj.get("expires_at")),
            "source": "local",
        }

    def switch(self, account: dict) -> dict:
        if not (account.get("access_token") or account.get("refresh_token")):
            raise SwitchError("该账号没有可写入的 token")
        path = self.credential_path()
        existing = read_json(path) or {}
        existing["access_token"] = str(account.get("access_token") or "")
        if account.get("refresh_token"):
            existing["refresh_token"] = str(account["refresh_token"])
        if account.get("id_token"):
            existing["id_token"] = str(account["id_token"])
        scopes = account.get("scopes")
        if scopes:
            existing["scope"] = " ".join(scopes) if isinstance(scopes, list) else str(scopes)
        existing["token_type"] = existing.get("token_type") or "Bearer"
        exp = to_ms(account.get("expires_at"))
        if exp:
            existing["expiry_date"] = int(exp)
        atomic_write_json(path, existing)
        return {
            "provider": self.provider,
            "target": str(path),
            "backup": str(path.with_name(path.name + ".ai-switch.bak")),
            "written": [str(path)],
        }
