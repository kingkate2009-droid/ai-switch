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
        from core.data import get_vendors
        for v in get_vendors():
            if v.get("provider", "").lower() != "google":
                continue
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key") and k.get("id") != key.get("id"):
                    return
        settings = self._load_settings()
        settings.pop("GEMINI_API_KEY", None)
        settings.pop("GOOGLE_GEMINI_BASE_URL", None)
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    def reconcile(self) -> None:
        from core.data import get_vendors
        has_active = False
        for v in get_vendors():
            if v.get("provider", "").lower() != "google":
                continue
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key"):
                    has_active = True
                    break
            if has_active:
                break
        if not has_active:
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
        try:
            r = subprocess.run(
                ["agy", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version = (r.stdout + r.stderr).strip()
            settings = self._load_settings()
            has_key = bool(settings.get("GEMINI_API_KEY"))
            return {
                "running": r.returncode == 0,
                "version": version,
                "message": "API key configured" if has_key else "OAuth mode (no custom key)",
            }
        except FileNotFoundError:
            return {"running": False, "version": "", "message": "agy CLI not found"}
        except Exception as e:
            return {"running": False, "version": "", "message": str(e)[:100]}

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._settings_path), "label": "Settings", "type": "json"},
            {"path": str(self._mcp_config_path), "label": "MCP Config", "type": "json"},
        ]
