import os
import subprocess
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from backends.base import BackendAdapter


class AiderAdapter(BackendAdapter):
    name = "aider"
    display_name = "Aider"

    @property
    def _config_path(self) -> Path:
        return Path.home() / ".aider.conf.yml"

    def _load_config(self) -> dict:
        if not self._config_path.exists():
            return {}
        if yaml:
            with open(self._config_path) as f:
                return yaml.safe_load(f) or {}
        # Fallback: read as text
        with open(self._config_path) as f:
            return {"_raw": f.read()}

    def _save_config(self, data: dict) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        if yaml:
            with open(self._config_path, "w") as f:
                yaml.dump(data, f, default_flow_style=False)
        else:
            # Write env vars format if yaml not available
            with open(self._config_path, "w") as f:
                for k, v in data.items():
                    if isinstance(v, str):
                        f.write(f"{k}: {v}\n")

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        prov = vendor.get("provider", "").lower()
        config = self._load_config()

        # Map vendor provider to aider provider IDs
        provider_map = {
            "openai": "openai",
            "anthropic": "anthropic",
            "deepseek": "deepseek",
            "google": "gemini",
            "gemini": "gemini",
            "openrouter": "openrouter",
        }
        aider_prov = provider_map.get(prov, "openai")

        # Set via api-key config (aider supports multiple)
        api_keys = config.get("api-key", [])
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        new_entry = f"{aider_prov}={key['api_key']}"
        if new_entry not in api_keys:
            api_keys.append(new_entry)
        config["api-key"] = api_keys

        self._save_config(config)

    def reconcile(self) -> None:
        config = self._load_config()
        if isinstance(config, dict) and "_raw" in config:
            return
        api_keys = config.get("api-key", [])
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        from core.data import get_vendors
        active_bare = {k["api_key"] for v in get_vendors() for k in v.get("keys", [])
                      if k.get("enabled", True) and k.get("api_key")}
        kept = [entry for entry in api_keys if "=" in entry and entry.split("=", 1)[1] in active_bare]
        if len(kept) != len(api_keys):
            config["api-key"] = kept
            self._save_config(config)

    def sync_from_backend(self) -> list[dict]:
        config = self._load_config()
        if isinstance(config, dict) and "_raw" in config:
            return []
        api_keys = config.get("api-key", [])
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        vendors = []
        for entry in api_keys:
            if "=" in entry:
                prov, _, key_val = entry.partition("=")
                vendors.append({
                    "name": prov.title(),
                    "provider": prov.lower(),
                    "api_url": "",
                    "endpoint_type": "openai",
                    "keys": [{"name": f"from {self.name}", "api_key": key_val}],
                })
        return vendors

    @property
    def _model_settings_path(self) -> Path:
        return Path.home() / ".aider.model.settings.yml"

    @property
    def _model_metadata_path(self) -> Path:
        return Path.home() / ".aider.model.metadata.json"

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "Main Config", "type": "yml"},
            {"path": str(self._model_settings_path), "label": "Model Settings", "type": "yml"},
            {"path": str(self._model_metadata_path), "label": "Model Metadata", "type": "json"},
        ]

    def get_status(self) -> dict:
        from backends.base import make_status, cli_available

        installed, version = cli_available("aider")
        if not installed:
            return make_status(installed=False, message="aider CLI not found")
        config = self._load_config()
        keys = config.get("api-key", [])
        return make_status(
            installed=True,
            running=False,
            version=version,
            message=f"{len(keys) if isinstance(keys, list) else 1} key(s) configured",
        )
