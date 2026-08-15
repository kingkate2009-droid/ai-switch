import json
import os
import subprocess
from pathlib import Path

from backends.base import BackendAdapter


class AntigravityAdapter(BackendAdapter):
    name = "antigravity"
    display_name = "Antigravity CLI"

    @property
    def _settings_path(self) -> Path:
        return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

    @property
    def _mcp_config_path(self) -> Path:
        return Path.home() / ".gemini" / "config" / "mcp_config.json"

    def _load_settings(self) -> dict:
        if self._settings_path.exists():
            with open(self._settings_path) as f:
                return json.load(f)
        return {}

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        prov = vendor.get("provider", "").lower()
        if prov != "google":
            return
        settings = self._load_settings()
        settings["GEMINI_API_KEY"] = key["api_key"]
        api_url = vendor.get("api_url", "")
        if api_url:
            base = api_url.replace("/v1beta", "").replace("/v1", "").replace("/v1beta/models", "")
            base = base.replace("/models", "").rstrip("/")
            if base and "generativelanguage.googleapis.com" not in base:
                settings["GOOGLE_GEMINI_BASE_URL"] = base
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        if vendor.get("provider", "").lower() != "google":
            return
        other = self.pick_syncable_key(
            providers={"google", "gemini"},
            exclude=(str(vendor.get("id") or ""), str(key.get("id") or "")),
        )
        if other and other[1].get("id") != key.get("id"):
            self.on_key_added(other[0], other[1])
            return
        settings = self._load_settings()
        settings.pop("GEMINI_API_KEY", None)
        settings.pop("GOOGLE_GEMINI_BASE_URL", None)
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    def reconcile(self) -> None:
        best = self.pick_syncable_key(providers={"google", "gemini"})
        if best:
            self.on_key_added(best[0], best[1])
            return
        settings = self._load_settings()
        removed = settings.pop("GEMINI_API_KEY", None)
        settings.pop("GOOGLE_GEMINI_BASE_URL", None)
        if removed is not None:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._settings_path, "w") as f:
                json.dump(settings, f, indent=2)

    def sync_from_backend(self) -> list[dict]:
        settings = self._load_settings()
        api_key = settings.get("GEMINI_API_KEY", "")
        base_url = settings.get("GOOGLE_GEMINI_BASE_URL", "")
        if not api_key:
            return []
        url = base_url + "/v1beta" if base_url else "https://generativelanguage.googleapis.com/v1beta"
        return [{
            "name": "Google Gemini",
            "provider": "google",
            "api_url": url,
            "endpoint_type": "gemini",
            "keys": [{"name": f"from {self.name}", "api_key": api_key}],
        }]

    def get_status(self) -> dict:
        from backends.base import detect_install, status_from_detect
        det = detect_install(
            cli_commands=("agy", "agy.exe", "antigravity"),
            config_files=[self._settings_path],
            treat_config_as_installed=True,
        )
        settings = self._load_settings()
        has_key = bool(settings.get("GEMINI_API_KEY"))
        return status_from_detect(
            det,
            not_installed_message="agy CLI not found",
            message="API key configured" if has_key else "OAuth mode (no custom key)",
        )

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._settings_path), "label": "Settings", "type": "json"},
            {"path": str(self._mcp_config_path), "label": "MCP Config", "type": "json"},
        ]
