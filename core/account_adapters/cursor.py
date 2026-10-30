"""Cursor account adapter (import + switch).

Cursor is a VS Code fork, so its login lives in the shared
``globalStorage/state.vscdb`` SQLite file — but unlike the ``secret://``
platforms, Cursor's auth rows are stored as **plain text** under well-known
keys, which is what makes switching it straightforward.

Keys (``ItemTable``, ``INSERT OR REPLACE``)::

    cursorAuth/accessToken, cursorAuth/refreshToken, cursorAuth/cachedEmail,
    cursorAuth/stripeMembershipType, cursorAuth/stripeSubscriptionStatus,
    cursorAuth/authId, cursorAuth/cachedSignUpType,
    cursor.accessToken, cursor.email

Prefer switching while Cursor is closed: a running editor can overwrite the row
from its in-memory state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.account_adapters.base import (
    AccountAdapter,
    StateVscdb,
    SwitchError,
    app_support_dir,
)

KEY_ACCESS = "cursorAuth/accessToken"
KEY_REFRESH = "cursorAuth/refreshToken"
KEY_EMAIL = "cursorAuth/cachedEmail"
KEY_AUTH_ID = "cursorAuth/authId"
KEY_MEMBERSHIP = "cursorAuth/stripeMembershipType"
KEY_SUB_STATUS = "cursorAuth/stripeSubscriptionStatus"
KEY_SIGNUP = "cursorAuth/cachedSignUpType"
KEY_ACCESS_ALT = "cursor.accessToken"
KEY_EMAIL_ALT = "cursor.email"


class CursorAdapter(AccountAdapter):
    provider = "cursor"
    label = "Cursor"
    aliases = ("cursor-cli", "cursor_cli", "cursor-ide")
    supports_import = True
    supports_switch = True
    supports_oauth = True
    supports_quota = True
    install_hint = "未检测到 Cursor 登录态（…/Cursor/User/globalStorage/state.vscdb）"

    def _db_path(self) -> Path:
        return app_support_dir("Cursor") / "User" / "globalStorage" / "state.vscdb"

    def credential_path(self) -> Optional[Path]:
        return self._db_path()

    def _db(self) -> StateVscdb:
        return StateVscdb(self._db_path())

    def read(self) -> Optional[dict]:
        db = self._db()
        if not db.exists():
            return None
        access = (db.get(KEY_ACCESS) or db.get(KEY_ACCESS_ALT) or "").strip()
        refresh = (db.get(KEY_REFRESH) or "").strip()
        if not (access or refresh):
            return None
        return {
            "provider": self.provider,
            "email": (db.get(KEY_EMAIL) or db.get(KEY_EMAIL_ALT) or "").strip(),
            "account_id": (db.get(KEY_AUTH_ID) or "").strip(),
            "access_token": access,
            "refresh_token": refresh,
            "plan_type": (db.get(KEY_MEMBERSHIP) or "").strip(),
            "source": "local",
        }

    def switch(self, account: dict) -> dict:
        access = str(account.get("access_token") or "").strip()
        refresh = str(account.get("refresh_token") or "").strip()
        if not (access or refresh):
            raise SwitchError("该账号没有可写入的 Cursor token")
        db = self._db()
        if not db.exists():
            raise SwitchError(f"未找到 Cursor 数据库: {db.path}")

        rows: list[tuple[str, str]] = []
        if access:
            rows.append((KEY_ACCESS, access))
            rows.append((KEY_ACCESS_ALT, access))
        if refresh:
            rows.append((KEY_REFRESH, refresh))
        email = str(account.get("email") or "").strip()
        if email:
            rows.append((KEY_EMAIL, email))
            rows.append((KEY_EMAIL_ALT, email))
        if account.get("account_id"):
            rows.append((KEY_AUTH_ID, str(account["account_id"])))
        if account.get("plan_type"):
            rows.append((KEY_MEMBERSHIP, str(account["plan_type"])))
        if account.get("subscription_status"):
            rows.append((KEY_SUB_STATUS, str(account["subscription_status"])))

        written = []
        # One backup for the whole switch: only the first write backs up.
        for index, (key, value) in enumerate(rows):
            db.write(key, value, backup=(index == 0))
            written.append(key)

        return {
            "provider": self.provider,
            "target": str(db.path),
            "backup": str(db.path.with_name(db.path.name + ".ai-switch.bak")),
            "written": written,
        }
