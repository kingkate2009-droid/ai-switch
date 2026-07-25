import json
import os
import subprocess
from pathlib import Path

import yaml

from backends.base import BackendAdapter, home_config_dir


class GooseAdapter(BackendAdapter):
    name = "goose"
    display_name = "Goose"

    @property
    def _config_dir(self) -> Path:
        # macOS/Linux: ~/.config/goose  Windows: %APPDATA%\goose
        return home_config_dir("goose")

    @property
    def _config_path(self) -> Path:
        return self._config_dir / "config.yaml"

    @property
    def _secrets_path(self) -> Path:
        return self._config_dir / "secrets.yaml"

    @property
    def _custom_providers_dir(self) -> Path:
        return self._config_dir / "custom_providers"

    def _load_config(self) -> dict:
        if self._config_path.exists():
            with open(self._config_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _save_config(self, data: dict) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    def _load_secrets(self) -> dict:
        if self._secrets_path.exists():
            with open(self._secrets_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _save_secrets(self, data: dict) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with open(self._secrets_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)

    @property
    def _provider_env_map(self) -> dict:
        return {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "groq": "GROQ_API_KEY",
        }

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        secrets = self._load_secrets()
        env_var = self._provider_env_map.get(vendor.get("provider", ""))
        if env_var:
            secrets[env_var] = key["api_key"]
            self._save_secrets(secrets)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        env_var = self._provider_env_map.get(vendor.get("provider", ""))
        if not env_var:
            return
        from core.data import get_vendors
        others = False
        for v in get_vendors():
            if v.get("provider") != vendor.get("provider"):
                continue
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key") and k.get("id") != key.get("id"):
                    others = True
                    break
            if others:
                break
        if not others:
            secrets = self._load_secrets()
            secrets.pop(env_var, None)
            self._save_secrets(secrets)

    def reconcile(self) -> None:
        from core.data import get_vendors
        active_envs = set()
        for v in get_vendors():
            env_var = self._provider_env_map.get(v.get("provider", ""))
            if not env_var:
                continue
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key"):
                    active_envs.add(env_var)
        secrets = self._load_secrets()
        changed = False
        for env_var in self._provider_env_map.values():
            if env_var not in active_envs and env_var in secrets:
                del secrets[env_var]
                changed = True
        if changed:
            self._save_secrets(secrets)

    def sync_from_backend(self) -> list[dict]:
        secrets = self._load_secrets()
        if not secrets:
            return []
        env_to_provider = {v: k for k, v in self._provider_env_map.items()}
        vendors = []
        for env_var, api_key in secrets.items():
            if not api_key or not isinstance(api_key, str):
                continue
            provider_id = env_to_provider.get(env_var, "")
            if not provider_id:
                continue
            vendors.append({
                "name": provider_id.replace("-", " ").title(),
                "provider": provider_id,
                "api_url": "",
                "endpoint_type": "openai",
                "keys": [{"name": f"from {self.name}", "api_key": api_key}],
            })
        return vendors

    def get_status(self) -> dict:
        from backends.base import detect_install, status_from_detect
        det = detect_install(
            cli_commands=("goose", "goose.exe"),
            config_files=[self._config_path, self._secrets_path],
            data_dirs=[self._config_dir],
            treat_config_as_installed=True,
        )
        secrets = self._load_secrets()
        return status_from_detect(
            det,
            not_installed_message="goose CLI not found",
            message=f"{len(secrets)} key(s) in secrets.yaml",
        )

    @property
    def config_files(self) -> list[dict]:
        files = [
            {"path": str(self._config_path), "label": "Config", "type": "yaml"},
            {"path": str(self._secrets_path), "label": "Secrets", "type": "yaml"},
        ]
        cpd = self._custom_providers_dir
        if cpd.exists():
            for f in sorted(cpd.iterdir()):
                if f.suffix in (".json", ".yaml", ".yml"):
                    files.append({"path": str(f), "label": f"Provider: {f.stem}", "type": f.suffix[1:]})
        return files
