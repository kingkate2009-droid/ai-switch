import json
import os
import subprocess
from pathlib import Path

from backends.base import BackendAdapter


class CopilotCliAdapter(BackendAdapter):
    name = "copilot-cli"
    display_name = "GitHub Copilot CLI"

    @property
    def _settings_path(self) -> Path:
        return Path.home() / ".copilot" / "settings.json"

    @property
    def _config_path(self) -> Path:
        return Path.home() / ".copilot" / "config.json"

    def _load_settings(self) -> dict:
        if self._settings_path.exists():
            with open(self._settings_path) as f:
                return json.load(f)
        return {}

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        prov = vendor.get("provider", "").lower()
        provider_type = None
        if prov in ("openai", "deepseek", "groq", "openrouter"):
            provider_type = "openai"
        elif prov in ("anthropic",):
            provider_type = "anthropic"
        elif prov in ("google",):
            provider_type = "google"
        elif prov in ("azure", "azure-openai"):
            provider_type = "azure"
        else:
            return
        settings = self._load_settings()
        section = settings.get("byok", {})
        section["provider_type"] = provider_type
        section["base_url"] = vendor.get("api_url", "")
        section["api_key"] = key["api_key"]
        section["model"] = key.get("default_model", "")
        settings["byok"] = section
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        from core.data import get_vendors
        for v in get_vendors():
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key"):
                    return
        settings = self._load_settings()
        settings.pop("byok", None)
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    def reconcile(self) -> None:
        from core.data import get_vendors
        has_active = False
        for v in get_vendors():
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key"):
                    has_active = True
                    break
            if has_active:
                break
        if not has_active:
            settings = self._load_settings()
            if "byok" in settings:
                del settings["byok"]
                self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._settings_path, "w") as f:
                    json.dump(settings, f, indent=2)

    def sync_from_backend(self) -> list[dict]:
        settings = self._load_settings()
        byok = settings.get("byok", {})
        api_key = byok.get("api_key", "")
        if not api_key:
            return []
        return [{
            "name": byok.get("provider_type", "openai").title(),
            "provider": byok.get("provider_type", "openai"),
            "api_url": byok.get("base_url", ""),
            "endpoint_type": "openai" if byok.get("provider_type") != "anthropic" else "anthropic",
            "keys": [{"name": f"from {self.name}", "api_key": api_key}],
        }]

    def get_status(self) -> dict:
        try:
            r = subprocess.run(
                ["gh", "copilot", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version = (r.stdout + r.stderr).strip() or "installed"
            settings = self._load_settings()
            has_byok = "byok" in settings
            return {
                "running": True,
                "version": version,
                "message": "BYOK configured" if has_byok else "GitHub auth mode",
            }
        except FileNotFoundError:
            import shutil
            if shutil.which("gh"):
                return {"running": True, "version": "gh extension", "message": "gh copilot extension installed"}
            return {"running": False, "version": "", "message": "gh CLI not found"}
        except Exception as e:
            return {"running": False, "version": "", "message": str(e)[:100]}

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._settings_path), "label": "Settings", "type": "jsonc"},
            {"path": str(self._config_path), "label": "App State", "type": "json"},
        ]
