"""Account adapter registry.

Each module in this package defines exactly one :class:`AccountAdapter`
subclass.  The registry discovers them automatically, so adding a new tool is
"drop in a file" — no central list to keep in sync.

Public API::

    providers()            -> tuple of canonical provider ids
    get("kiro")            -> adapter instance (or None)
    norm("claude-code")    -> canonical id
    describe_all()         -> JSON-safe capability list for the API/UI
    switch(account)        -> write an account into its tool
    read_active(provider)  -> parse the tool's current login
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Optional

from core.account_adapters.base import (  # re-exported for callers
    AccountAdapter,
    KeychainError,
    StateVscdb,
    SwitchError,
    atomic_write_json,
    read_json,
)

log = logging.getLogger(__name__)

_ADAPTERS: dict[str, AccountAdapter] = {}
_ALIASES: dict[str, str] = {}
_LOADED = False


def _discover() -> None:
    global _LOADED
    if _LOADED:
        return
    package_dir = Path(__file__).resolve().parent
    for info in pkgutil.iter_modules([str(package_dir)]):
        if info.name in ("base",) or info.name.startswith("_"):
            continue
        module_name = f"{__name__}.{info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception:  # pragma: no cover - a broken adapter must not kill the app
            log.exception("failed to import account adapter %s", module_name)
            continue
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, AccountAdapter)
                and attr is not AccountAdapter
                and getattr(attr, "provider", "")
                and attr.__module__ == module_name
            ):
                instance = attr()
                _ADAPTERS[instance.provider] = instance
                for alias in instance.aliases:
                    _ALIASES[alias.lower()] = instance.provider
    _LOADED = True


def _ensure() -> None:
    _discover()


def norm(provider: str) -> str:
    _ensure()
    p = str(provider or "").strip().lower()
    return _ALIASES.get(p, p)


def providers() -> tuple[str, ...]:
    _ensure()
    return tuple(sorted(_ADAPTERS))


def get(provider: str) -> Optional[AccountAdapter]:
    _ensure()
    return _ADAPTERS.get(norm(provider))


def all_adapters() -> list[AccountAdapter]:
    _ensure()
    return [_ADAPTERS[p] for p in sorted(_ADAPTERS)]


def describe_all() -> list[dict]:
    return [a.describe() for a in all_adapters()]


def labels() -> dict[str, str]:
    return {a.provider: a.label for a in all_adapters()}


def read_active(provider: str) -> Optional[dict]:
    """Parse the tool's *currently active* login (never raises)."""
    adapter = get(provider)
    if adapter is None:
        return None
    try:
        return adapter.read()
    except Exception:
        log.exception("adapter %s read() failed", provider)
        return None


def switch(account: dict) -> dict:
    """Write ``account`` into its tool. Raises :class:`SwitchError`."""
    provider = norm(account.get("provider"))
    adapter = get(provider)
    if adapter is None:
        raise SwitchError(f"未知账号类型: {account.get('provider')!r}")
    if not adapter.supports_switch:
        raise SwitchError(f"{adapter.label} 暂不支持写入切换")
    return adapter.switch(account)
