import json
import os
import subprocess
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from backends.base import BackendAdapter


class ContinueAdapter(BackendAdapter):
    name = "continue"
    display_name = "Continue.dev"

    @property
    def _config_path(self) -> Path:
        return Path.home() / ".continue" / "config.json"

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
        config = self._load_config()
        config.setdefault("models", [])

        prov = vendor.get("provider", "openai")
        api_url = vendor.get("api_url", "")
        ep = vendor.get("endpoint_type", "openai")

        # Check if this provider already has an entry
        existing = None
        for m in config["models"]:
            if m.get("title", "").lower() == prov.lower():
                existing = m
                break

        if existing:
            existing["apiKey"] = key["api_key"]
            if api_url:
                existing["apiBase"] = api_url
        else:
            entry = {
                "title": prov.title(),
                "provider": ep if ep == "anthropic" else "openai",
                "model": "AUTODETECT",
                "apiKey": key["api_key"],
            }
            if api_url:
                entry["apiBase"] = api_url
            config["models"].append(entry)

        self._save_config(config)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        config = self._load_config()
        prov = vendor.get("provider", "openai").lower()
        config["models"] = [m for m in config.get("models", [])
                           if m.get("title", "").lower() != prov]
        self._save_config(config)

    def reconcile(self) -> None:
        config = self._load_config()
        models = config.get("models", [])
        if not models:
            return
        from core.data import get_vendors
        active_bare = {k["api_key"] for v in get_vendors() for k in v.get("keys", [])
                      if k.get("enabled", True) and k.get("api_key")}
        kept = [m for m in models if m.get("apiKey", "") in active_bare or m.get("api_key", "") in active_bare]
        if len(kept) != len(models):
            config["models"] = kept
            self._save_config(config)

    def sync_from_backend(self) -> list[dict]:
        config = self._load_config()
        models = config.get("models", [])
        vendors = []
        for m in models:
            api_key = m.get("apiKey", "") or m.get("api_key", "")
            if not api_key:
                continue
            title = m.get("title", m.get("provider", "openai"))
            api_base = m.get("apiBase", m.get("api_base", ""))
            ep = m.get("provider", "openai")
            vendors.append({
                "name": title,
                "provider": title.lower(),
                "api_url": api_base,
                "endpoint_type": ep,
                "keys": [{"name": f"from {self.name}", "api_key": api_key}],
            })
        return vendors

    @property
    def _config_yaml_path(self) -> Path:
        return Path.home() / ".continue" / "config.yaml"

    @property
    def _continuerc_path(self) -> Path:
        return Path.home() / ".continue" / ".continuerc.json"

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "Config (JSON)", "type": "json"},
            {"path": str(self._config_yaml_path), "label": "Config (YAML)", "type": "yml"},
            {"path": str(self._continuerc_path), "label": "Workspace RC", "type": "json"},
        ]

    def get_status(self) -> dict:
        config = self._load_config()
        models = config.get("models", [])
        return {
            "running": config != {},
            "version": "",
            "message": f"{len(models)} model(s) configured" if models else "Not configured",
        }
