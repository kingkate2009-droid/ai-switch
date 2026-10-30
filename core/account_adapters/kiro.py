"""Kiro (AWS) account adapter (import + switch).

Kiro keeps its login in two plain-JSON files — no SQLite, no encryption:

- auth token : ``~/.aws/sso/cache/kiro-auth-token.json``
- profile    : ``<Kiro dir>/User/globalStorage/kiro.kiroagent/profile.json``
  where ``<Kiro dir>`` is ``~/Library/Application Support/Kiro`` (macOS),
  ``%APPDATA%\\Kiro`` (Windows), ``~/.config/Kiro`` (Linux).

The auth token carries ``accessToken`` / ``refreshToken`` plus the AWS identity
bits (``idc_region``, ``issuer_url``, ``client_id``, ``clientIdHash``,
``profileArn``) that must survive a switch, so the whole document is preserved
in ``raw_meta`` and merged back on write.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.account_adapters.base import (
    AccountAdapter,
    SwitchError,
    app_support_dir,
    atomic_write_json,
    home,
    read_json,
    to_ms,
)

KIRO_DIR_NAMES = ("Kiro",)


def _kiro_dir() -> Path:
    return app_support_dir("Kiro")


def _token_path() -> Path:
    return home() / ".aws" / "sso" / "cache" / "kiro-auth-token.json"


def _profile_path() -> Path:
    return _kiro_dir() / "User" / "globalStorage" / "kiro.kiroagent" / "profile.json"


class KiroAdapter(AccountAdapter):
    provider = "kiro"
    label = "Kiro"
    aliases = ("aws-kiro", "kiro-cli", "kiro_ide", "amazon-q")
    supports_import = True
    supports_switch = True
    supports_oauth = False
    supports_quota = False
    install_hint = "未检测到 Kiro 登录态（~/.aws/sso/cache/kiro-auth-token.json）"

    def credential_path(self) -> Optional[Path]:
        return _token_path()

    def detect(self) -> bool:
        return _token_path().is_file() or _profile_path().is_file()

    # ── read ──

    def read(self) -> Optional[dict]:
        token = read_json(_token_path()) or {}
        profile = read_json(_profile_path()) or {}
        access = str(token.get("accessToken") or token.get("access_token") or "").strip()
        refresh = str(token.get("refreshToken") or token.get("refresh_token") or "").strip()
        if not (access or refresh):
            return None
        scopes = token.get("scopes") or token.get("scope") or ""
        if isinstance(scopes, str):
            scopes = [s for s in scopes.replace(",", " ").split() if s]
        return {
            "provider": self.provider,
            "email": str(token.get("email") or profile.get("email") or "").strip(),
            "account_id": str(token.get("userId") or token.get("user_id") or "").strip(),
            "access_token": access,
            "refresh_token": refresh,
            "scopes": scopes,
            "expires_at": to_ms(token.get("expiresAt") or token.get("expires_at")),
            "plan_type": str(token.get("provider") or "").strip(),
            "source": "local",
            "raw_meta": {
                "kiro_auth_token": token,
                "kiro_profile": profile,
            },
        }

    # ── switch ──

    def switch(self, account: dict) -> dict:
        access = str(account.get("access_token") or "").strip()
        refresh = str(account.get("refresh_token") or "").strip()
        if not (access or refresh):
            raise SwitchError("该账号没有可写入的 Kiro token")

        meta = account.get("raw_meta") if isinstance(account.get("raw_meta"), dict) else {}
        raw_token = meta.get("kiro_auth_token") if isinstance(meta.get("kiro_auth_token"), dict) else {}
        raw_profile = meta.get("kiro_profile") if isinstance(meta.get("kiro_profile"), dict) else {}

        # Preserve the AWS identity fields; only the tokens are ours to change.
        token = dict(raw_token)
        if access:
            token["accessToken"] = access
        if refresh:
            token["refreshToken"] = refresh
        token.setdefault("tokenType", "Bearer")
        exp = to_ms(account.get("expires_at"))
        if exp:
            token["expiresAt"] = int(exp)
        if account.get("email"):
            token["email"] = str(account["email"])
        if account.get("account_id"):
            token["userId"] = str(account["account_id"])
        scopes = account.get("scopes")
        if scopes:
            token["scopes"] = " ".join(scopes) if isinstance(scopes, list) else str(scopes)

        token_path = _token_path()
        atomic_write_json(token_path, token)
        written = [str(token_path)]

        # Keep profile.json consistent so Kiro shows the right identity.
        profile_path = _profile_path()
        if raw_profile or account.get("email") or token.get("profileArn"):
            profile = dict(raw_profile)
            if token.get("profileArn"):
                profile["arn"] = token["profileArn"]
            if account.get("email"):
                profile["email"] = str(account["email"])
            if account.get("label"):
                profile.setdefault("name", str(account["label"]))
            atomic_write_json(profile_path, profile)
            written.append(str(profile_path))

        return {
            "provider": self.provider,
            "target": str(token_path),
            "backup": str(token_path.with_name(token_path.name + ".ai-switch.bak")),
            "written": written,
        }
