"""Claude Code account adapter (import + switch).

Credential location:

- macOS  : login Keychain, generic password ``Claude Code-credentials``
- others : ``~/.claude/.credentials.json``

Both hold the same JSON blob::

    {"claudeAiOauth": {
        "accessToken": "...", "refreshToken": "...", "expiresAt": 1699999999999,
        "scopes": ["user:inference", ...], "subscriptionType": "max"}}

Writing it back is what makes Claude Code log in as a different account.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from core.account_adapters.base import (
    AccountAdapter,
    KeychainError,
    SwitchError,
    atomic_write_json,
    home,
    is_macos,
    keychain_get,
    keychain_has,
    keychain_set,
    to_ms,
)

KEYCHAIN_SERVICE = "Claude Code-credentials"


class ClaudeAdapter(AccountAdapter):
    provider = "claude"
    label = "Claude Code"
    aliases = ("anthropic", "claude-code", "claude_code", "claudeai")
    supports_import = True
    supports_switch = True
    supports_oauth = True
    supports_quota = True
    install_hint = "未检测到 Claude Code 登录态（macOS 读取 Keychain，其它平台读取 ~/.claude/.credentials.json）"

    # ── paths ──

    def credential_path(self) -> Optional[Path]:
        return home() / ".claude" / ".credentials.json"

    def detect(self) -> bool:
        if self._uses_keychain() and keychain_has(KEYCHAIN_SERVICE):
            return True
        return super().detect()

    def _uses_keychain(self) -> bool:
        return is_macos()

    # ── read ──

    def _read_raw(self) -> Optional[dict]:
        if self._uses_keychain():
            raw = keychain_get(KEYCHAIN_SERVICE, os.environ.get("USER", ""))
            if not raw:
                raw = keychain_get(KEYCHAIN_SERVICE)
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    pass
        path = self.credential_path()
        if path and path.is_file():
            try:
                obj = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
                if isinstance(obj, dict):
                    return obj
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def read(self) -> Optional[dict]:
        obj = self._read_raw()
        if not obj:
            return None
        inner = obj.get("claudeAiOauth")
        src = inner if isinstance(inner, dict) else obj
        access = str(src.get("accessToken") or src.get("access_token") or "").strip()
        refresh = str(src.get("refreshToken") or src.get("refresh_token") or "").strip()
        if not (access or refresh):
            return None
        scopes = src.get("scopes") or src.get("scope") or []
        if isinstance(scopes, str):
            scopes = [s for s in scopes.replace(",", " ").split() if s]
        return {
            "provider": self.provider,
            "email": str(src.get("email") or obj.get("email") or "").strip(),
            "plan_type": str(src.get("subscriptionType") or src.get("subscription_type") or "").strip(),
            "access_token": access,
            "refresh_token": refresh,
            "scopes": scopes,
            "expires_at": to_ms(src.get("expiresAt") or src.get("expires_at")),
            "source": "local",
        }

    # ── switch ──

    def _build_payload(self, account: dict, existing: Optional[dict]) -> dict:
        root = dict(existing or {})
        inner = root.get("claudeAiOauth")
        inner = dict(inner) if isinstance(inner, dict) else {}
        inner["accessToken"] = str(account.get("access_token") or "")
        if account.get("refresh_token"):
            inner["refreshToken"] = str(account["refresh_token"])
        if account.get("expires_at"):
            inner["expiresAt"] = int(to_ms(account["expires_at"]) or 0)
        scopes = account.get("scopes")
        if scopes:
            inner["scopes"] = scopes if isinstance(scopes, list) else [scopes]
        if account.get("plan_type"):
            inner["subscriptionType"] = str(account["plan_type"])
        root["claudeAiOauth"] = inner
        return root

    def switch(self, account: dict) -> dict:
        if not (account.get("access_token") or account.get("refresh_token")):
            raise SwitchError("该账号没有可写入的 token")
        existing = self._read_raw()
        payload = self._build_payload(account, existing)
        blob = json.dumps(payload, ensure_ascii=False)

        written: list[str] = []
        backup = ""
        if self._uses_keychain():
            try:
                keychain_set(KEYCHAIN_SERVICE, blob, os.environ.get("USER", ""))
                written.append(f"keychain:{KEYCHAIN_SERVICE}")
            except KeychainError as exc:
                raise SwitchError(str(exc)) from exc
            # Keep the on-disk copy in sync when it already exists.
            path = self.credential_path()
            if path and path.is_file():
                atomic_write_json(path, payload)
                written.append(str(path))
                backup = str(path.with_name(path.name + ".ai-switch.bak"))
        else:
            path = self.credential_path()
            atomic_write_json(path, payload)
            written.append(str(path))
            backup = str(path.with_name(path.name + ".ai-switch.bak"))

        return {
            "provider": self.provider,
            "target": "keychain" if self._uses_keychain() else str(self.credential_path()),
            "backup": backup,
            "written": written,
        }
