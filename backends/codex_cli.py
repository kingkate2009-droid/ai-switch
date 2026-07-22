"""Codex CLI backend adapter.

Docs: https://developers.openai.com/codex/config-file/
Config: ~/.codex/config.toml  (override with $CODEX_HOME)
Auth:   ~/.codex/auth.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from backends.base import BackendAdapter

log = logging.getLogger(__name__)

# Managed provider prefix so we never overwrite reserved IDs (openai/ollama/lmstudio)
_MANAGED_PREFIX = "aiswitch-"

# Reserved provider IDs that cannot be overridden
_RESERVED_IDS = {"openai", "ollama", "lmstudio"}

# Map endpoint_type to env_key names
_ENV_KEY_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
    "cohere": "COHERE_API_KEY",
}


def _home_dir() -> Path:
    env = (os.environ.get("CODEX_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


# ── TOML helpers ──────────────────────────────────────────────


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("read codex config failed: %s", e)
        return {}
    if not text.strip():
        return {}
    try:
        import tomli
        return tomli.loads(text)
    except Exception:
        pass
    try:
        import tomllib  # py311+
        return tomllib.loads(text)
    except Exception as e:
        log.warning("parse codex config.toml failed: %s", e)
        return {}


def _escape_key(key: str) -> str:
    if re.match(r"^[A-Za-z0-9_\-]+$", key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_str(val: str) -> str:
    return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _dump_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, str):
        return _escape_str(val)
    if isinstance(val, list):
        return "[ " + ", ".join(_dump_value(x) for x in val) + " ]"
    if isinstance(val, dict):
        parts = []
        for k, v in val.items():
            parts.append(f"{_escape_key(str(k))} = {_dump_value(v)}")
        return "{ " + ", ".join(parts) + " }"
    return _escape_str(str(val))


def _dump_table(header: str, data: dict, lines: list[str]) -> None:
    if header:
        lines.append(f"[{header}]")
    for k, v in data.items():
        if isinstance(v, dict):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            continue
        lines.append(f"{_escape_key(str(k))} = {_dump_value(v)}")


def _dump_toml(data: dict) -> str:
    """Minimal TOML writer for Codex config shape."""
    lines: list[str] = [
        "# Managed in part by ai-switch (Codex CLI backend).",
        "# Docs: https://developers.openai.com/codex/config-file/",
        "",
    ]
    nested = {}
    for k, v in data.items():
        if isinstance(v, dict):
            nested[k] = v
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            nested[k] = v
            continue
        lines.append(f"{_escape_key(str(k))} = {_dump_value(v)}")
    if nested and lines and lines[-1] != "":
        lines.append("")

    for name, table in nested.items():
        if isinstance(table, list):
            for item in table:
                if not isinstance(item, dict):
                    continue
                lines.append(f"[[{_escape_key(str(name))}]]")
                for k, v in item.items():
                    lines.append(f"{_escape_key(str(k))} = {_dump_value(v)}")
                lines.append("")
            continue
        if not isinstance(table, dict):
            continue
        sub_maps = {k: v for k, v in table.items() if isinstance(v, dict)}
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        if scalars and not sub_maps:
            _dump_table(_escape_key(str(name)), scalars, lines)
            lines.append("")
            continue
        if scalars:
            _dump_table(_escape_key(str(name)), scalars, lines)
            lines.append("")
        for sub_key, sub_val in sub_maps.items():
            deeper = {k: v for k, v in sub_val.items() if isinstance(v, dict)}
            leaf = {k: v for k, v in sub_val.items() if not isinstance(v, dict)}
            header = f"{_escape_key(str(name))}.{_escape_key(str(sub_key))}"
            if leaf or not deeper:
                _dump_table(header, leaf, lines)
                lines.append("")
            for dk, dv in deeper.items():
                if not isinstance(dv, dict):
                    continue
                h2 = f"{header}.{_escape_key(str(dk))}"
                _dump_table(h2, dv, lines)
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── Provider helpers ──────────────────────────────────────────


def _provider_id(vendor_id: str) -> str:
    return f"{_MANAGED_PREFIX}{vendor_id}"


def _env_key_for_vendor(vendor: dict) -> str:
    ep = (vendor.get("endpoint_type") or "openai").lower().strip()
    return _ENV_KEY_MAP.get(ep, "OPENAI_API_KEY")


def _wire_api_for_vendor(vendor: dict) -> str:
    ep = (vendor.get("endpoint_type") or "openai").lower().strip()
    if ep in ("openai",):
        return "responses"
    return "chat"


def _base_url(vendor: dict) -> str:
    return (vendor.get("proxy_target") or vendor.get("api_url") or "").rstrip("/")


# ── Adapter ───────────────────────────────────────────────────


class CodexCliAdapter(BackendAdapter):
    name = "codex-cli"
    display_name = "Codex CLI"

    def __init__(self):
        super().__init__()
        self._codex_dir = _home_dir()
        self._auth_path = self._codex_dir / "auth.json"
        self._config_path = self._codex_dir / "config.toml"

    def _load_auth(self) -> dict:
        if self._auth_path.exists():
            try:
                with open(self._auth_path) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_auth(self, data: dict) -> None:
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        with open(self._auth_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load_config(self) -> dict:
        return _load_toml(self._config_path)

    def _save_config(self, data: dict) -> None:
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(_dump_toml(data), encoding="utf-8")

    def _sync_vendor_to_config(self, vendor: dict, key: dict) -> None:
        """Write or update a vendor entry in config.toml model_providers."""
        if not key.get("api_key") or not key.get("enabled", True):
            return

        provider_id = _provider_id(vendor["id"])
        base_url = _base_url(vendor)
        if not base_url:
            return

        cfg = self._load_config()
        providers = cfg.setdefault("model_providers", {})

        providers[provider_id] = {
            "name": vendor.get("name") or vendor.get("provider") or "Custom",
            "base_url": base_url,
            "env_key": _env_key_for_vendor(vendor),
            "wire_api": _wire_api_for_vendor(vendor),
        }

        self._save_config(cfg)

    def _remove_vendor_from_config(self, vendor_id: str) -> None:
        """Remove a vendor entry from config.toml model_providers."""
        provider_id = _provider_id(vendor_id)
        cfg = self._load_config()
        providers = cfg.get("model_providers") or {}
        if provider_id in providers:
            del providers[provider_id]
            if not providers:
                del cfg["model_providers"]
            # If this was the active provider, clear it
            if cfg.get("model_provider") == provider_id:
                del cfg["model_provider"]
            self._save_config(cfg)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return

        # Write to auth.json
        auth = self._load_auth()
        auth["OPENAI_API_KEY"] = key["api_key"]
        self._save_auth(auth)
        os.environ["OPENAI_API_KEY"] = key["api_key"]

        # Write to config.toml
        self._sync_vendor_to_config(vendor, key)

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled", True):
            self.on_key_added(vendor, key)
        else:
            self.on_key_removed(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        # Remove from config.toml
        self._remove_vendor_from_config(vendor["id"])

        # If this was the active key in auth.json, clear it
        auth = self._load_auth()
        if auth.get("OPENAI_API_KEY") == key.get("api_key"):
            auth.pop("OPENAI_API_KEY", None)
            self._save_auth(auth)

    def on_vendor_removed(self, vendor: dict) -> None:
        self._remove_vendor_from_config(vendor["id"])

    def reconcile(self) -> None:
        from core.data import get_vendors, get_backend_config

        if get_backend_config(self.name).get("disabled"):
            return

        vendors = get_vendors()
        cfg = self._load_config()
        providers = cfg.get("model_providers") or {}

        # Build set of valid managed provider IDs
        valid_ids = set()
        for v in vendors:
            for k in v.get("keys", []):
                if not k.get("enabled", True) or not k.get("api_key"):
                    continue
                if not self.should_sync(v, k):
                    continue
                pid = _provider_id(v["id"])
                valid_ids.add(pid)
                # Ensure provider exists in config
                base_url = _base_url(v)
                if base_url:
                    providers[pid] = {
                        "name": v.get("name") or v.get("provider") or "Custom",
                        "base_url": base_url,
                        "env_key": _env_key_for_vendor(v),
                        "wire_api": _wire_api_for_vendor(v),
                    }

        # Remove stale managed providers
        stale = [pid for pid in providers if pid.startswith(_MANAGED_PREFIX) and pid not in valid_ids]
        for pid in stale:
            del providers[pid]

        if providers:
            cfg["model_providers"] = providers
        elif "model_providers" in cfg:
            del cfg["model_providers"]

        # Check if active provider still valid
        active = cfg.get("model_provider") or ""
        if active.startswith(_MANAGED_PREFIX) and active not in valid_ids:
            del cfg["model_provider"]

        self._save_config(cfg)

        # Ensure auth.json has a valid key
        has_active = any(
            k.get("enabled", True) and k.get("api_key")
            for v in vendors
            for k in v.get("keys", [])
        )
        if not has_active:
            auth = self._load_auth()
            if auth.pop("OPENAI_API_KEY", None):
                self._save_auth(auth)

    def switch_provider(self, provider_id: str = "", vendor_id: str = "", key_id: str = "") -> dict:
        """Switch the active provider for Codex CLI.

        Args:
            provider_id: Direct provider ID (e.g. "aiswitch-2")
            vendor_id: System vendor ID (will find best key)
            key_id: Specific key ID (optional, with vendor_id)

        Returns:
            dict with success, active_provider, message
        """
        from core.data import get_vendor, get_key

        # Resolve provider_id from vendor_id if needed
        if not provider_id and vendor_id:
            vendor = get_vendor(vendor_id)
            if not vendor:
                return {"success": False, "message": f"Vendor {vendor_id} not found"}
            provider_id = _provider_id(vendor_id)

            # Find the key to use
            if key_id:
                key = get_key(vendor_id, key_id)
                if not key:
                    return {"success": False, "message": f"Key {key_id} not found"}
            else:
                # Use first enabled key with api_key
                keys = vendor.get("keys") or []
                key = next((k for k in keys if k.get("enabled", True) and k.get("api_key")), None)
                if not key:
                    return {"success": False, "message": "No enabled key found for vendor"}

            # Ensure vendor is synced to config
            self._sync_vendor_to_config(vendor, key)

            # Update auth.json
            auth = self._load_auth()
            auth["OPENAI_API_KEY"] = key["api_key"]
            self._save_auth(auth)
            os.environ["OPENAI_API_KEY"] = key["api_key"]

        elif provider_id:
            # Direct provider_id - verify it exists in config
            cfg = self._load_config()
            providers = cfg.get("model_providers") or {}
            if provider_id not in providers:
                return {"success": False, "message": f"Provider {provider_id} not found in config"}

            # Find matching vendor and update auth
            if provider_id.startswith(_MANAGED_PREFIX):
                vid = provider_id[len(_MANAGED_PREFIX):]
                vendor = get_vendor(vid)
                if vendor:
                    keys = vendor.get("keys") or []
                    key = next((k for k in keys if k.get("enabled", True) and k.get("api_key")), None)
                    if key:
                        auth = self._load_auth()
                        auth["OPENAI_API_KEY"] = key["api_key"]
                        self._save_auth(auth)
                        os.environ["OPENAI_API_KEY"] = key["api_key"]
        else:
            return {"success": False, "message": "provider_id or vendor_id required"}

        # Update model_provider in config
        cfg = self._load_config()
        cfg["model_provider"] = provider_id
        self._save_config(cfg)

        return {
            "success": True,
            "active_provider": provider_id,
            "message": f"Switched to {provider_id}",
        }

    def list_providers(self) -> list[dict]:
        """List all configured providers in config.toml."""
        from core.data import get_vendors

        cfg = self._load_config()
        providers = cfg.get("model_providers") or {}
        active_id = cfg.get("model_provider") or ""

        # Build vendor lookup
        vendors_by_id = {v["id"]: v for v in get_vendors()}

        result = []
        for pid, pdata in providers.items():
            is_managed = pid.startswith(_MANAGED_PREFIX)
            vendor_id = ""
            vendor_name = ""
            if is_managed:
                vid = pid[len(_MANAGED_PREFIX):]
                vendor = vendors_by_id.get(vid)
                if vendor:
                    vendor_id = vid
                    vendor_name = vendor.get("name") or ""

            result.append({
                "id": pid,
                "name": pdata.get("name") or pid,
                "base_url": pdata.get("base_url") or "",
                "env_key": pdata.get("env_key") or "",
                "wire_api": pdata.get("wire_api") or "chat",
                "vendor_id": vendor_id,
                "vendor_name": vendor_name,
                "active": pid == active_id,
                "managed": is_managed,
            })

        # Sort: active first, then by name
        result.sort(key=lambda x: (0 if x["active"] else 1, x["name"].lower()))
        return result

    def get_active_provider(self) -> dict:
        """Get the currently active provider."""
        cfg = self._load_config()
        active_id = cfg.get("model_provider") or ""
        if not active_id:
            return {"active_provider": "", "name": "", "base_url": ""}

        providers = cfg.get("model_providers") or {}
        pdata = providers.get(active_id) or {}
        return {
            "active_provider": active_id,
            "name": pdata.get("name") or active_id,
            "base_url": pdata.get("base_url") or "",
        }

    def sync_from_backend(self) -> list[dict]:
        auth = self._load_auth()
        api_key = auth.get("OPENAI_API_KEY", "")
        if not api_key:
            return []
        return [{
            "name": "OpenAI",
            "provider": "openai",
            "api_url": "",
            "endpoint_type": "openai",
            "keys": [{"name": f"from {self.name}", "api_key": api_key}],
        }]

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "Main Config", "type": "toml"},
            {"path": str(self._auth_path), "label": "Auth File", "type": "json"},
        ]

    def get_status(self) -> dict:
        auth = self._load_auth()
        has_key = bool(auth.get("OPENAI_API_KEY"))
        config_exists = self._config_path.exists()
        cfg = self._load_config() if config_exists else {}
        active = cfg.get("model_provider") or ""
        providers = cfg.get("model_providers") or {}

        msg_parts = []
        if has_key:
            msg_parts.append("API key configured")
        if active:
            msg_parts.append(f"active: {active}")
        if providers:
            msg_parts.append(f"{len(providers)} provider(s)")

        return {
            "running": config_exists or has_key,
            "version": "",
            "message": "; ".join(msg_parts) if msg_parts else "Not configured",
            "active_provider": active,
            "provider_count": len(providers),
        }

    def restart(self) -> dict:
        return {"success": False, "message": "Codex CLI does not require restart"}
