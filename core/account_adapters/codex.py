"""Codex CLI account adapter (import + switch).

Credential location: ``~/.codex/auth.json`` (or ``$CODEX_HOME/auth.json``)::

    {"OPENAI_API_KEY": null,
     "tokens": {"id_token": "...", "access_token": "...",
                "refresh_token": "...", "account_id": "..."},
     "last_refresh": "2024-01-01T00:00:00Z"}

ChatGPT (subscription) logins carry ``tokens``; plain API-key installs only
carry ``OPENAI_API_KEY``.  Switching writes ``tokens`` and clears the API key,
which is exactly what ``codex login`` does.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from core.account_adapters.base import (
    AccountAdapter,
    SwitchError,
    atomic_write_json,
    now_iso,
    read_json,
    to_ms,
)


def _codex_dir() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def _jwt_email(id_token: str) -> str:
    import base64
    import json

    parts = str(id_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        pad = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
    except Exception:
        return ""
    return str(claims.get("email") or "")


class CodexAdapter(AccountAdapter):
    provider = "codex"
    label = "Codex / ChatGPT"
    aliases = ("chatgpt", "openai", "codex-cli", "codex_cli")
    supports_import = True
    supports_switch = True
    supports_oauth = True
    supports_quota = True
    install_hint = "未检测到 Codex CLI 登录态（~/.codex/auth.json）"

    def credential_path(self) -> Optional[Path]:
        return _codex_dir() / "auth.json"

    def read(self) -> Optional[dict]:
        obj = read_json(self.credential_path())
        if not obj:
            return None
        tokens = obj.get("tokens") if isinstance(obj.get("tokens"), dict) else {}
        access = str(tokens.get("access_token") or "").strip()
        refresh = str(tokens.get("refresh_token") or "").strip()
        id_token = str(tokens.get("id_token") or "").strip()
        if not (access or refresh):
            return None  # API-key-only install: no account to model
        return {
            "provider": self.provider,
            "email": _jwt_email(id_token),
            "account_id": str(tokens.get("account_id") or "").strip(),
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "expires_at": to_ms(_jwt_exp(access)),
            "source": "local",
        }

    def switch(self, account: dict) -> dict:
        if not (account.get("access_token") or account.get("refresh_token")):
            raise SwitchError("该账号没有可写入的 token")
        path = self.credential_path()
        existing = read_json(path) or {}
        tokens = dict(existing.get("tokens") or {})
        for src, dst in (
            ("access_token", "access_token"),
            ("refresh_token", "refresh_token"),
            ("id_token", "id_token"),
            ("account_id", "account_id"),
        ):
            value = str(account.get(src) or "").strip()
            if value:
                tokens[dst] = value
        existing["tokens"] = tokens
        existing["last_refresh"] = now_iso()
        # ChatGPT-auth mode: the CLI keeps this null so the token is used.
        existing["OPENAI_API_KEY"] = None
        atomic_write_json(path, existing)
        return {
            "provider": self.provider,
            "target": str(path),
            "backup": str(path.with_name(path.name + ".ai-switch.bak")),
            "written": [str(path)],
        }


def _jwt_exp(token: str) -> Optional[int]:
    import base64
    import json

    parts = str(token or "").split(".")
    if len(parts) < 2:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
    except Exception:
        return None
    try:
        exp = int(claims.get("exp"))
    except (TypeError, ValueError):
        return None
    return exp * 1000 if exp > 0 else None
