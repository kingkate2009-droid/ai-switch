"""Shared plumbing for account adapters (the "switch credentials into a tool" side).

An *account adapter* knows three things about one AI tool:

1. **Where** its login credential lives on disk (per OS).
2. **How** to parse that credential into the same account fields the
   ``core.accounts`` store uses (import / detect current login).
3. **How** to write an account back into that store (the actual 换号 / switch).

Everything OS-specific and every format quirk lives in :mod:`base` so the
individual adapters stay small and declarative.

Design rules:

- Never destroy user data: every write goes through :func:`atomic_write_json`
  or :meth:`StateVscdb.write`, both of which take a timestamped backup first.
- Never raise from a *probe* (``detect``/``read``); return ``None``/``False``.
  ``switch`` may raise :class:`SwitchError` so the API can report why.
- Stdlib only (``sqlite3``, ``json``, ``shutil``, ...). No required dependencies.
  Platforms whose credentials are encrypted go through the optional
  :mod:`core.account_adapters.crypto` layer instead.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

_BACKUP_SUFFIX = ".ai-switch.bak"


class SwitchError(RuntimeError):
    """Raised when an adapter cannot write an account into its tool."""


# ── platform paths ─────────────────────────────────────────────────

def home() -> Path:
    return Path.home()


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return os.name == "nt"


def app_support_dir(name: str) -> Path:
    """Return the per-user data directory for ``name`` on the current OS.

    - macOS:  ``~/Library/Application Support/<name>``
    - Windows: ``%APPDATA%\\<name>``
    - Linux:  ``~/.config/<name>``
    """
    if is_macos():
        return home() / "Library" / "Application Support" / name
    if is_windows():
        base = os.environ.get("APPDATA") or str(home() / "AppData" / "Roaming")
        return Path(base) / name
    return home() / ".config" / name


def first_existing(paths: Iterable[Path]) -> Optional[Path]:
    """Return the first path that exists, else ``None``."""
    for p in paths:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


# ── time helpers ───────────────────────────────────────────────────

def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def to_ms(value: Any) -> Optional[int]:
    """Best-effort epoch-millis normaliser (accepts s, ms, or ISO strings)."""
    if value in (None, ""):
        return None
    if isinstance(value, str) and not value.strip().isdigit():
        try:
            return int(
                _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
            )
        except Exception:
            return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n < 10_000_000_000:  # seconds, not millis
        n *= 1000
    return n


def iso_from_ms(ms: Any) -> str:
    ms = to_ms(ms)
    if not ms:
        return ""
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc).isoformat()


# ── safe file writes ───────────────────────────────────────────────

def backup_file(path: Path) -> Optional[Path]:
    """Copy ``path`` to ``<path>.ai-switch.bak`` before we touch it."""
    try:
        if path.exists():
            bak = path.with_name(path.name + _BACKUP_SUFFIX)
            shutil.copy2(path, bak)
            return bak
    except OSError as exc:  # pragma: no cover - filesystem dependent
        log.warning("could not back up %s: %s", path, exc)
    return None


def atomic_write_json(path: Path, data: Any, *, backup: bool = True) -> Path:
    """Write JSON atomically, backing the previous file up first."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup:
        backup_file(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def atomic_write_text(path: Path, text: str, *, backup: bool = True) -> Path:
    """Write a text file atomically, backing the previous file up first."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup:
        backup_file(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def read_json(path: Path) -> Optional[dict]:
    """Read a JSON object, tolerating a UTF-8 BOM and trailing junk."""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ── VS Code globalStorage (state.vscdb) ────────────────────────────

class StateVscdb:
    """Thin accessor for a VS Code / Cursor-family ``state.vscdb`` SQLite file.

    These editors keep extension state in ``globalStorage/state.vscdb`` inside
    an ``ItemTable(key TEXT PRIMARY KEY, value BLOB)`` table.  Auth blobs are
    stored as JSON (sometimes wrapped) under well-known keys, which is what the
    IDE-specific adapters read and write.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        try:
            return self.path.is_file()
        except OSError:
            return False

    def _connect(self) -> sqlite3.Connection:
        # read/write; short timeout so a running IDE does not hang us forever.
        return sqlite3.connect(str(self.path), timeout=5.0)

    def get(self, key: str) -> Optional[str]:
        try:
            con = self._connect()
        except sqlite3.Error as exc:
            log.debug("state.vscdb open failed (%s): %s", self.path, exc)
            return None
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            con.close()
        if not row:
            return None
        value = row[0]
        if isinstance(value, (bytes, bytearray)):
            try:
                return value.decode("utf-8", "replace")
            except Exception:
                return None
        return str(value)

    def get_json(self, key: str) -> Optional[dict]:
        raw = self.get(key)
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def keys(self) -> list[str]:
        try:
            con = self._connect()
        except sqlite3.Error:
            return []
        try:
            rows = con.execute("SELECT key FROM ItemTable").fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        return [str(r[0]) for r in rows]

    def find_keys(self, *needles: str) -> list[str]:
        """Keys whose name contains any of ``needles`` (case-insensitive)."""
        lowered = [n.lower() for n in needles]
        return [k for k in self.keys() if any(n in k.lower() for n in lowered)]

    def write(self, key: str, value: str, *, backup: bool = True) -> Path:
        if backup:
            backup_file(self.path)
        try:
            con = self._connect()
        except sqlite3.Error as exc:
            raise SwitchError(f"无法打开 {self.path}: {exc}") from exc
        try:
            con.execute(
                "INSERT INTO ItemTable (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            con.commit()
        except sqlite3.Error as exc:
            raise SwitchError(f"写入 {self.path} 失败: {exc}") from exc
        finally:
            con.close()
        return self.path

    def write_json(self, key: str, obj: Any, *, backup: bool = True) -> Path:
        return self.write(key, json.dumps(obj, ensure_ascii=False), backup=backup)

    def delete(self, key: str, *, backup: bool = True) -> Path:
        if backup:
            backup_file(self.path)
        con = self._connect()
        try:
            con.execute("DELETE FROM ItemTable WHERE key = ?", (key,))
            con.commit()
        finally:
            con.close()
        return self.path


# ── macOS Keychain ─────────────────────────────────────────────────

class KeychainError(RuntimeError):
    pass


def keychain_available() -> bool:
    return is_macos() and shutil.which("security") is not None


def keychain_has(service: str, account: str = "") -> bool:
    """True when a generic-password item exists.

    Queries metadata only (no ``-w``), which does not require the user to
    approve access, so it is safe to call from an install probe.
    """
    if not keychain_available():
        return False
    import subprocess

    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd += ["-a", account]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def keychain_get(service: str, account: str = "") -> Optional[str]:
    """Read a generic-password secret from the login Keychain.

    macOS may show an "allow access" prompt the first time; a denied prompt
    surfaces as ``None`` rather than an exception.
    """
    if not keychain_available():
        return None
    import subprocess

    cmd = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd += ["-a", account]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("keychain read failed for %s: %s", service, exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def keychain_set(service: str, secret: str, account: str = "") -> None:
    """Create or update a generic-password item in the login Keychain."""
    if not keychain_available():
        raise KeychainError("当前系统不支持 Keychain 写入")
    import subprocess

    account = account or os.environ.get("USER") or "ai-switch"
    cmd = [
        "security", "add-generic-password", "-U",
        "-s", service, "-a", account, "-w", secret,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeychainError(f"Keychain 写入失败: {exc}") from exc
    if out.returncode != 0:
        raise KeychainError(
            f"Keychain 写入失败: {out.stderr.strip() or out.returncode}"
        )


def keychain_has_internet(server: str) -> bool:
    """True when an internet-password item exists for ``server``."""
    if not keychain_available():
        return False
    import subprocess

    try:
        out = subprocess.run(
            ["security", "find-internet-password", "-s", server],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def keychain_get_internet(server: str, account: str = "") -> Optional[str]:
    """Read the password of an internet-password item (e.g. Zed's token)."""
    if not keychain_available():
        return None
    import subprocess

    cmd = ["security", "find-internet-password", "-s", server, "-w"]
    if account:
        cmd += ["-a", account]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def keychain_internet_account(server: str) -> str:
    """Return the account name of an internet-password item (metadata only)."""
    if not keychain_available():
        return ""
    import re
    import subprocess

    try:
        out = subprocess.run(
            ["security", "find-internet-password", "-s", server],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    match = re.search(r'"acct"<blob>="([^"]*)"', out.stdout)
    return match.group(1) if match else ""


def keychain_set_internet(server: str, account: str, password: str) -> None:
    """Create or update an internet-password item."""
    if not keychain_available():
        raise KeychainError("当前系统不支持 Keychain 写入")
    import subprocess

    cmd = [
        "security", "add-internet-password", "-U",
        "-s", server, "-a", account or "ai-switch", "-w", password,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeychainError(f"Keychain 写入失败: {exc}") from exc
    if out.returncode != 0:
        raise KeychainError(
            f"Keychain 写入失败: {out.stderr.strip() or out.returncode}"
        )


# ── adapter base ───────────────────────────────────────────────────

class AccountAdapter:
    """Base class: subclasses override the class attrs and the probe/switch hooks.

    Only :meth:`read` and :meth:`switch` are interesting; everything else is
    capability metadata used by the API/UI to decide what to show.
    """

    provider: str = ""
    label: str = ""
    aliases: tuple[str, ...] = ()

    supports_import: bool = True
    supports_switch: bool = False
    supports_oauth: bool = False
    supports_quota: bool = False

    #: Shown in the UI when a switch cannot work (e.g. tool not installed).
    install_hint: str = ""

    # ── capability probes ──

    def credential_path(self) -> Optional[Path]:
        """Primary on-disk credential location, or ``None`` if unknown."""
        return None

    def detect(self) -> bool:
        """True when the tool appears to be installed/used on this machine."""
        path = self.credential_path()
        if path is None:
            return False
        try:
            return path.exists()
        except OSError:
            return False

    # ── read / switch ──

    def read(self) -> Optional[dict]:
        """Parse the *currently active* login into account ``fields``.

        Returns ``None`` when nothing is logged in / the tool is absent.
        Must never raise.
        """
        return None

    def switch(self, account: dict) -> dict:
        """Write ``account`` into the tool so it becomes the active login.

        Returns ``{"provider", "target", "backup", "written"}``.
        Raises :class:`SwitchError` with a human-readable reason on failure.
        """
        raise SwitchError(f"{self.label or self.provider} 暂不支持写入切换")

    # ── optional ──

    def quota(self, account: dict) -> dict:  # pragma: no cover - overridden
        raise SwitchError(f"{self.label or self.provider} 不支持额度查询")

    def describe(self) -> dict:
        """JSON-safe capability record for the API."""
        path = self.credential_path()
        return {
            "provider": self.provider,
            "label": self.label,
            "aliases": list(self.aliases),
            "supports_import": self.supports_import,
            "supports_switch": self.supports_switch,
            "supports_oauth": self.supports_oauth,
            "supports_quota": self.supports_quota,
            "installed": self.detect(),
            "credential_path": str(path) if path else "",
            "install_hint": self.install_hint,
        }
