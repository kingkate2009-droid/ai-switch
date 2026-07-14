import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter


class CodexCliAdapter(BackendAdapter):
    name = "codex-cli"
    display_name = "Codex CLI"

    @property
    def _codex_dir(self) -> Path:
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))

    @property
    def _auth_path(self) -> Path:
        return self._codex_dir / "auth.json"

    @property
    def _config_path(self) -> Path:
        return self._codex_dir / "config.toml"

    def _load_auth(self) -> dict:
        if self._auth_path.exists():
            with open(self._auth_path) as f:
                return json.load(f)
        return {}

    def _save_auth(self, data: dict) -> None:
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        with open(self._auth_path, "w") as f:
            json.dump(data, f, indent=2)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        auth = self._load_auth()
        auth["OPENAI_API_KEY"] = key["api_key"]
        self._save_auth(auth)
        # Also set as env var for current process
        os.environ["OPENAI_API_KEY"] = key["api_key"]

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        auth = self._load_auth()
        auth.pop("OPENAI_API_KEY", None)
        self._save_auth(auth)

    def reconcile(self) -> None:
        from core.data import get_vendors
        # Check if any active key exists at all
        has_active = any(
            k.get("enabled", True) and k.get("api_key")
            for v in get_vendors()
            for k in v.get("keys", [])
        )
        if not has_active:
            auth = self._load_auth()
            if auth.pop("OPENAI_API_KEY", None):
                self._save_auth(auth)

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
        msg_parts = []
        if has_key:
            msg_parts.append("API key configured")
        if config_exists:
            msg_parts.append("config present")
        return {
            "running": config_exists,
            "version": "",
            "message": "; ".join(msg_parts) if msg_parts else "Not configured",
        }
