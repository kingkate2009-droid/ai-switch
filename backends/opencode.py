import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter


class OpenCodeAdapter(BackendAdapter):
    name = "opencode"
    display_name = "OpenCode"

    @property
    def _auth_path(self) -> Path:
        return Path.home() / ".local" / "share" / "opencode" / "auth.json"

    @property
    def _config_path(self) -> Path:
        return Path.home() / ".config" / "opencode" / "opencode.jsonc"

    @property
    def _tui_config_path(self) -> Path:
        return Path.home() / ".config" / "opencode" / "tui.jsonc"

    def _load_auth(self) -> dict:
        if self._auth_path.exists():
            with open(self._auth_path) as f:
                return json.load(f)
        return {}

    def _save_auth(self, data: dict) -> None:
        self._auth_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._auth_path, "w") as f:
            json.dump(data, f, indent=2)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        auth = self._load_auth()
        # Use provider as key name, or custom vendor name
        provider_name = vendor.get("provider", "openai")
        auth[provider_name] = key["api_key"]
        self._save_auth(auth)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        auth = self._load_auth()
        provider_name = vendor.get("provider", "openai")
        auth.pop(provider_name, None)
        self._save_auth(auth)

    def get_status(self) -> dict:
        try:
            r = subprocess.run(
                ["opencode", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version = (r.stdout + r.stderr).strip()
            auth = self._load_auth()
            key_count = len(auth)
            return {
                "running": r.returncode == 0,
                "version": version,
                "message": f"{key_count} key(s) configured",
            }
        except FileNotFoundError:
            return {"running": False, "version": "", "message": "opencode CLI not found"}
        except Exception as e:
            return {"running": False, "version": "", "message": str(e)[:100]}

    def sync_from_backend(self) -> list[dict]:
        auth = self._load_auth()
        if not auth:
            return []
        vendors = []
        for provider_name, api_key in auth.items():
            if not api_key or not isinstance(api_key, str):
                continue
            vendors.append({
                "name": provider_name.replace("-", " ").title(),
                "provider": provider_name,
                "api_url": "",
                "endpoint_type": "openai",
                "keys": [{"name": f"from {self.name}", "api_key": api_key}],
            })
        return vendors

    def reconcile(self) -> None:
        from core.data import get_vendors
        auth = self._load_auth()
        if not auth:
            return
        vendors = get_vendors()
        active = set()
        for v in vendors:
            prov = v.get("provider", "")
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key"):
                    active.add((prov, k["api_key"]))
        changed = False
        for prov_name, api_key in list(auth.items()):
            if not isinstance(api_key, str) or not api_key:
                continue
            if (prov_name, api_key) not in active:
                del auth[prov_name]
                changed = True
        if changed:
            self._save_auth(auth)

    def get_config_template(self) -> list[dict]:
        return [
            {"key": "auth_path", "label": "Auth File", "type": "text",
             "default": str(self._auth_path), "help": "Path to auth.json"},
        ]

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "Global Config", "type": "jsonc"},
            {"path": str(self._tui_config_path), "label": "TUI Config", "type": "jsonc"},
            {"path": str(self._auth_path), "label": "Auth File", "type": "json"},
        ]
