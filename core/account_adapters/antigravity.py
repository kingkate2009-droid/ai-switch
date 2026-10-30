"""Antigravity IDE account adapter (import + switch).

Antigravity (a Gemini-based IDE) stores its login in two places, both holding
the *same* base64 payload — no SQLite and no encryption:

- macOS   : Keychain generic-password ``service=gemini``, ``account=antigravity``
- fallback: ``~/.gemini/jetski-standalone-oauth-token``

The value is ``go-keyring-base64:<base64(JSON)>`` where the JSON is::

    {"token": {"access_token": "...", "token_type": "Bearer",
               "refresh_token": "...", "expiry": "<RFC3339 micros>"},
     "auth_method": "consumer",
     "id_token": "<jwt>"}

Older (<2.0.0) installs used protobuf rows in ``state.vscdb`` instead; those
are read-only here (we surface the error rather than guessing at the encoding).
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
from pathlib import Path
from typing import Optional

from core.account_adapters.base import (
    AccountAdapter,
    KeychainError,
    SwitchError,
    atomic_write_text,
    home,
    is_macos,
    keychain_get,
    keychain_has,
    keychain_set,
    to_ms,
)

KEYCHAIN_SERVICE = "gemini"
KEYCHAIN_ACCOUNT = "antigravity"
PREFIX = "go-keyring-base64:"


def _fallback_path() -> Path:
    return home() / ".gemini" / "jetski-standalone-oauth-token"


def _decode_payload(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("{"):
        # Some installs store the whole JSON envelope rather than the blob.
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict):
            return envelope
        if isinstance(envelope, str):
            text = envelope
    if text.startswith('"') and text.endswith('"'):
        try:
            text = json.loads(text)
        except json.JSONDecodeError:
            pass
    text = text.strip()
    if text.startswith(PREFIX):
        text = text[len(PREFIX):]
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4))
        obj = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _encode_payload(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return PREFIX + base64.b64encode(blob.encode("utf-8")).decode("ascii")


def _email_from_jwt(token: str) -> str:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        pad = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
    except Exception:
        return ""
    return str(claims.get("email") or "")


def _rfc3339_from_ms(value) -> str:
    ms = to_ms(value)
    if not ms:
        return ""
    stamp = _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class AntigravityAdapter(AccountAdapter):
    provider = "antigravity"
    label = "Antigravity IDE"
    aliases = ("antigravity-ide", "antigravity_ide", "jetski")
    supports_import = True
    supports_switch = True
    supports_oauth = False
    supports_quota = False
    install_hint = "未检测到 Antigravity 登录态（Keychain service=gemini / ~/.gemini/jetski-standalone-oauth-token）"

    def credential_path(self) -> Optional[Path]:
        return _fallback_path()

    def detect(self) -> bool:
        if _fallback_path().is_file():
            return True
        return is_macos() and keychain_has(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)

    # ── read ──

    def _read_payload(self) -> Optional[dict]:
        if is_macos():
            raw = keychain_get(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            payload = _decode_payload(raw or "")
            if payload:
                return payload
        path = _fallback_path()
        if path.is_file():
            try:
                payload = _decode_payload(path.read_text(encoding="utf-8"))
            except OSError:
                payload = None
            if payload:
                return payload
        return None

    def read(self) -> Optional[dict]:
        payload = self._read_payload()
        if not payload:
            return None
        token = payload.get("token") if isinstance(payload.get("token"), dict) else {}
        access = str(token.get("access_token") or "").strip()
        refresh = str(token.get("refresh_token") or "").strip()
        if not (access or refresh):
            return None
        id_token = str(payload.get("id_token") or "").strip()
        return {
            "provider": self.provider,
            "email": _email_from_jwt(id_token),
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "plan_type": str(payload.get("auth_method") or "").strip(),
            "expires_at": to_ms(token.get("expiry")),
            "source": "local",
            "raw_meta": {"antigravity_payload": payload},
        }

    # ── switch ──

    def switch(self, account: dict) -> dict:
        access = str(account.get("access_token") or "").strip()
        refresh = str(account.get("refresh_token") or "").strip()
        if not (access or refresh):
            raise SwitchError("该账号没有可写入的 Antigravity token")

        meta = account.get("raw_meta") if isinstance(account.get("raw_meta"), dict) else {}
        raw_payload = meta.get("antigravity_payload")
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        token = dict(payload.get("token")) if isinstance(payload.get("token"), dict) else {}

        if access:
            token["access_token"] = access
        if refresh:
            token["refresh_token"] = refresh
        token.setdefault("token_type", "Bearer")
        expiry = _rfc3339_from_ms(account.get("expires_at"))
        if expiry:
            token["expiry"] = expiry
        payload["token"] = token
        payload.setdefault("auth_method", "consumer")
        if account.get("id_token"):
            payload["id_token"] = str(account["id_token"])

        blob = _encode_payload(payload)
        written: list[str] = []

        # The fallback file is what a fresh install reads; write it first so the
        # Keychain write is the only step that can prompt.
        path = _fallback_path()
        atomic_write_text(path, blob)
        written.append(str(path))

        if is_macos():
            try:
                keychain_set(KEYCHAIN_SERVICE, blob, KEYCHAIN_ACCOUNT)
                written.append(f"keychain:{KEYCHAIN_SERVICE}:{KEYCHAIN_ACCOUNT}")
            except KeychainError as exc:
                raise SwitchError(str(exc)) from exc

        return {
            "provider": self.provider,
            "target": str(path),
            "backup": str(path.with_name(path.name + ".ai-switch.bak")),
            "written": written,
        }
