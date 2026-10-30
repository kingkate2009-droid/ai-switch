"""Zed account adapter (import + switch).

Zed stores its session as a macOS **internet-password** Keychain item:

- server   : ``https://zed.dev``
- account  : the Zed ``user_id``
- password : the access token

Switching is therefore macOS-only, which is what Zed itself supports for this
flow. On other platforms the adapter still *reads* nothing and reports
``supports_switch`` accordingly via :meth:`ZedAdapter.can_switch`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.account_adapters.base import (
    AccountAdapter,
    KeychainError,
    SwitchError,
    is_macos,
    keychain_get_internet,
    keychain_has_internet,
    keychain_internet_account,
    keychain_set_internet,
)

ZED_SERVER = "https://zed.dev"


class ZedAdapter(AccountAdapter):
    provider = "zed"
    label = "Zed"
    aliases = ("zed-editor", "zed_editor")
    supports_import = True
    supports_switch = True
    supports_oauth = False
    supports_quota = False
    install_hint = "未检测到 Zed 登录态（macOS Keychain https://zed.dev）"

    def credential_path(self) -> Optional[Path]:
        return None  # Keychain-only; there is no file to point at.

    def can_switch(self) -> bool:
        return is_macos()

    def detect(self) -> bool:
        return is_macos() and keychain_has_internet(ZED_SERVER)

    def read(self) -> Optional[dict]:
        if not is_macos():
            return None
        token = keychain_get_internet(ZED_SERVER)
        if not token:
            return None
        return {
            "provider": self.provider,
            "account_id": keychain_internet_account(ZED_SERVER),
            "access_token": token.strip(),
            "source": "local",
        }

    def switch(self, account: dict) -> dict:
        if not is_macos():
            raise SwitchError("Zed 账号切换仅支持 macOS")
        token = str(account.get("access_token") or "").strip()
        if not token:
            raise SwitchError("该账号没有可写入的 token")
        account_id = str(account.get("account_id") or account.get("email") or "").strip()
        if not account_id:
            raise SwitchError("Zed 需要 user_id 作为 Keychain 账户名")
        try:
            keychain_set_internet(ZED_SERVER, account_id, token)
        except KeychainError as exc:
            raise SwitchError(str(exc)) from exc
        return {
            "provider": self.provider,
            "target": f"keychain:{ZED_SERVER}",
            "backup": "",
            "written": [f"keychain:internet-password:{ZED_SERVER}:{account_id}"],
        }

    def describe(self) -> dict:
        info = super().describe()
        info["supports_switch"] = bool(self.supports_switch and self.can_switch())
        return info
