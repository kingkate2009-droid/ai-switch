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
        # Only clear slot if no other healthy key of same type remains
        ep = vendor.get("endpoint_type", "openai")
        prov = vendor.get("provider", "").lower()
        want = "anthropic" if (ep == "anthropic" or prov == "anthropic") else "openai"
        other = self.pick_syncable_key(providers={want})
        if other:
            self.on_key_added(other[0], other[1])
            return
        secrets = self._load_secrets()
        if want == "anthropic":
            secrets.pop("ANTHROPIC_API_KEY", None)
        else:
            secrets.pop("OPENAI_API_KEY", None)
        self._save_secrets(secrets)
        config = self._load_config()
        config.get("apiProvider", {}).pop(want, None)
        self._save_config(config)

    def reconcile(self) -> None:
        secrets = self._load_secrets()
        config = self._load_config()
        changed = False
        for ep_type in ("openai", "anthropic"):
            env_key = f"{ep_type.upper()}_API_KEY"
            best = self.pick_syncable_key(providers={ep_type})
            if best:
                self.on_key_added(best[0], best[1])
            elif env_key in secrets:
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
        from backends.base import detect_install, status_from_detect, process_running

        # Cline is primarily a VS Code / Cursor extension (not a standalone CLI)
        det = detect_install(
            extension_ids=(
                "saoudrizwan.claude-dev",
                "saoudrizwan.claude-dev-nightly",
                "rooveterinaryinc.roo-cline",
            ),
            # do NOT treat ~/.cline alone as installed (manager may create it)
            treat_config_as_installed=False,
        )
        secrets = self._load_secrets()
        has_key = bool(secrets)
        # Plugin form: "running" ≈ host IDE is running (extension loads with IDE)
        running = False
        if det["installed"]:
            running = process_running(
                "Visual Studio Code", "Code.exe", "Code Helper",
                "Cursor.exe", "Cursor.app", "Cursor Helper",
            )
        msg = f"{len(secrets)} key(s) configured" if has_key else "extension installed"
        return status_from_detect(
            det,
            not_installed_message="Cline extension not installed",
            message=msg,
            running=running,
        )
