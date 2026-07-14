import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter


class ClineAdapter(BackendAdapter):
    name = "cline"
    display_name = "Cline"

    @property
    def _secrets_path(self) -> Path:
        return Path.home() / ".cline" / "data" / "secrets.json"

    @property
    def _config_path(self) -> Path:
        return Path.home() / ".cline" / "config.json"

    def _load_secrets(self) -> dict:
        if self._secrets_path.exists():
            with open(self._secrets_path) as f:
                return json.load(f)
        return {}

    def _save_secrets(self, data: dict) -> None:
        self._secrets_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._secrets_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load_config(self) -> dict:
        if self._config_path.exists():
            with open(self._config_path) as f:
                return json.load(f)
        return {}

    def _save_config(self, data: dict) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w") as f:
            json.dump(data, f, indent=2)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        secrets = self._load_secrets()
        api_url = vendor.get("api_url", "")
        prov = vendor.get("provider", "").lower()
        ep = vendor.get("endpoint_type", "openai")

        # Cline stores per-provider secrets
        if ep == "anthropic" or prov == "anthropic":
            secrets["ANTHROPIC_API_KEY"] = key["api_key"]
        else:
            secrets["OPENAI_API_KEY"] = key["api_key"]

        self._save_secrets(secrets)

        # Update config with API base URL
        if api_url:
            config = self._load_config()
            config.setdefault("apiProvider", {})
            if ep == "anthropic" or prov == "anthropic":
                config["apiProvider"]["anthropic"] = {
                    "apiKey": key["api_key"],
                    "baseUrl": api_url,
                }
            else:
                config["apiProvider"]["openai"] = {
                    "apiKey": key["api_key"],
                    "baseUrl": api_url,
                }
            self._save_config(config)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        secrets = self._load_secrets()
        prov = vendor.get("provider", "").lower()
        ep = vendor.get("endpoint_type", "openai")
        if ep == "anthropic" or prov == "anthropic":
            secrets.pop("ANTHROPIC_API_KEY", None)
        else:
            secrets.pop("OPENAI_API_KEY", None)
        self._save_secrets(secrets)

    def reconcile(self) -> None:
        from core.data import get_vendors
        secrets = self._load_secrets()
        config = self._load_config()
        ep_types = {"openai", "anthropic"}
        changed = False
        for ep_type in ep_types:
            env_key = f"{ep_type.upper()}_API_KEY"
            active = any(
                k.get("enabled", True) and k.get("api_key")
                for v in get_vendors()
                if v.get("endpoint_type", "") == ep_type or v.get("provider", "").lower() == ep_type
                for k in v.get("keys", [])
            )
            if not active and env_key in secrets:
                del secrets[env_key]
                changed = True
                config.get("apiProvider", {}).pop(ep_type, None)
        if changed:
            self._save_secrets(secrets)
            self._save_config(config)

    def sync_from_backend(self) -> list[dict]:
        secrets = self._load_secrets()
        config = self._load_config()
        vendors = []
        api_providers = config.get("apiProvider", {})
        for ep_type in ("openai", "anthropic"):
            api_key = secrets.get(f"{ep_type.upper()}_API_KEY", "")
            if api_key:
                prov_info = api_providers.get(ep_type, {})
                vendor = {
                    "name": ep_type.title(),
                    "provider": ep_type,
                    "api_url": prov_info.get("baseUrl", ""),
                    "endpoint_type": ep_type,
                    "keys": [{"name": f"from {self.name}", "api_key": api_key}],
                }
                vendors.append(vendor)
        return vendors

    @property
    def _global_state_path(self) -> Path:
        return Path.home() / ".cline" / "data" / "globalState.json"

    @property
    def _mcp_settings_path(self) -> Path:
        return Path.home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json"

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._secrets_path), "label": "Secrets", "type": "json"},
            {"path": str(self._config_path), "label": "Config", "type": "json"},
            {"path": str(self._global_state_path), "label": "Global State", "type": "json"},
            {"path": str(self._mcp_settings_path), "label": "MCP Settings", "type": "json"},
        ]

    def get_status(self) -> dict:
        secrets = self._load_secrets()
        has_key = bool(secrets)
        config = self._load_config()
        return {
            "running": config != {},
            "version": "",
            "message": f"{len(secrets)} key(s) configured" if has_key else "Not configured",
        }
