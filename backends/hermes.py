import os
import subprocess
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from backends.base import BackendAdapter


class HermesAdapter(BackendAdapter):
    name = "hermes"
    display_name = "Hermes Agent"

    @property
    def _config_path(self) -> Path:
        return Path.home() / ".hermes" / "config.yaml"

    @property
    def _env_path(self) -> Path:
        return Path.home() / ".hermes" / ".env"

    def _load_env(self) -> dict:
        if not self._env_path.exists():
            return {}
        env = {}
        with open(self._env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("\"'")
        return env

    def _save_env(self, env: dict) -> None:
        self._env_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._env_path, "w") as f:
            for k, v in env.items():
                f.write(f'{k}="{v}"\n')

    def _load_config(self) -> dict:
        if self._config_path.exists() and yaml:
            with open(self._config_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        prov = vendor.get("provider", "").lower()
        env = self._load_env()

        # Map provider to hermes env var
        env_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "google": "GEMINI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "github": "GITHUB_TOKEN",
        }
        env_key = env_map.get(prov, f"{prov.upper()}_API_KEY")
        env[env_key] = key["api_key"]
        self._save_env(env)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        prov = vendor.get("provider", "").lower()
        env = self._load_env()
        env_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "google": "GEMINI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "github": "GITHUB_TOKEN",
        }
        env_key = env_map.get(prov, f"{prov.upper()}_API_KEY")
        env.pop(env_key, None)
        self._save_env(env)

    def reconcile(self) -> None:
        env = self._load_env()
        if not env:
            return
        from core.data import get_vendors
        active_bare = {k["api_key"] for v in get_vendors() for k in v.get("keys", [])
                      if k.get("enabled", True) and k.get("api_key")}
        changed = False
        for k, v in list(env.items()):
            if v not in active_bare:
                del env[k]
                changed = True
        if changed:
            self._save_env(env)

    def sync_from_backend(self) -> list[dict]:
        env = self._load_env()
        rev_map = {
            "OPENAI_API_KEY": ("openai", "openai"),
            "ANTHROPIC_API_KEY": ("anthropic", "anthropic"),
            "DEEPSEEK_API_KEY": ("deepseek", "openai"),
            "GEMINI_API_KEY": ("gemini", "openai"),
            "OPENROUTER_API_KEY": ("openrouter", "openai"),
            "GITHUB_TOKEN": ("github", "openai"),
        }
        vendors = []
        for env_key, (prov, ep) in rev_map.items():
            api_key = env.get(env_key, "")
            if api_key:
                vendors.append({
                    "name": prov.title(),
                    "provider": prov,
                    "api_url": "",
                    "endpoint_type": ep,
                    "keys": [{"name": f"from {self.name}", "api_key": api_key}],
                })
        return vendors

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "Config", "type": "yml"},
            {"path": str(self._env_path), "label": "Environment", "type": "env"},
        ]

    def get_status(self) -> dict:
        from backends.base import make_status, cli_available

        installed, version = cli_available("hermes")
        env = self._load_env()
        if not installed and (self._config_path.exists() or self._env_path.exists() or env):
            installed = True
        if not installed:
            return make_status(installed=False, message="hermes CLI not found")
        msg = "CLI available" if version else (f"{len(env)} key(s) configured" if env else "Installed")
        return make_status(installed=True, running=False, version=version, message=msg)
