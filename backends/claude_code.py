import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter


class ClaudeCodeAdapter(BackendAdapter):
    name = "claude-code"
    display_name = "Claude Code"

    @property
    def _settings_path(self) -> Path:
        return Path.home() / ".claude" / "settings.json"

    def _load_settings(self) -> dict:
        if self._settings_path.exists():
            with open(self._settings_path) as f:
                return json.load(f)
        return {}

    def _save_settings(self, data: dict) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(data, f, indent=2)

    def _find_anthropic_keys(self) -> list[tuple[str, str, str]]:
        """Find keys with anthropic-compatible vendors. Returns [(provider, key_name, api_key)]. """
        from core.data import get_vendors
        results = []
        for v in get_vendors():
            ep = v.get("endpoint_type", "")
            if ep == "anthropic" or v.get("provider", "").lower() in ("anthropic",):
                for k in v.get("keys", []):
                    if k.get("enabled", True) and k.get("api_key"):
                        results.append((v["provider"], k["name"], k["api_key"]))
        return results

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        ep = vendor.get("endpoint_type", "")
        prov = vendor.get("provider", "").lower()
        if ep != "anthropic" and prov != "anthropic":
            return
        settings = self._load_settings()
        settings.setdefault("env", {})
        settings["env"]["ANTHROPIC_API_KEY"] = key["api_key"]
        api_url = vendor.get("api_url", "")
        if api_url:
            settings["env"]["ANTHROPIC_BASE_URL"] = api_url
        self._save_settings(settings)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        settings = self._load_settings()
        env = settings.get("env", {})
        # Only clear if no other anthropic keys exist
        others = self._find_anthropic_keys()
        others = [(p, n, k) for p, n, k in others if n != key.get("name", "")]
        if not others:
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_BASE_URL", None)
            if env:
                settings["env"] = env
            else:
                settings.pop("env", None)
            self._save_settings(settings)

    def reconcile(self) -> None:
        from core.data import get_vendors
        has_active = False
        for v in get_vendors():
            ep = v.get("endpoint_type", "")
            if ep != "anthropic" and v.get("provider", "").lower() != "anthropic":
                continue
            for k in v.get("keys", []):
                if k.get("enabled", True) and k.get("api_key"):
                    has_active = True
                    break
            if has_active:
                break
        if not has_active:
            settings = self._load_settings()
            env = settings.get("env", {})
            removed = env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_BASE_URL", None)
            if removed is not None:
                if env:
                    settings["env"] = env
                else:
                    settings.pop("env", None)
                self._save_settings(settings)

    def sync_from_backend(self) -> list[dict]:
        settings = self._load_settings()
        env = settings.get("env", {})
        api_key = env.get("ANTHROPIC_API_KEY", "")
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        if not api_key:
            return []
        return [{
            "name": "Anthropic",
            "provider": "anthropic",
            "api_url": base_url,
            "endpoint_type": "anthropic",
            "keys": [{"name": f"from {self.name}", "api_key": api_key}],
        }]

    @property
    def _claude_json_path(self) -> Path:
        return Path.home() / ".claude.json"

    @property
    def _keybindings_path(self) -> Path:
        return Path.home() / ".claude" / "keybindings.json"

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._settings_path), "label": "Settings", "type": "json"},
            {"path": str(self._claude_json_path), "label": "Global Config", "type": "json"},
            {"path": str(self._keybindings_path), "label": "Keybindings", "type": "json"},
        ]

    def get_status(self) -> dict:
        try:
            r = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            version = (r.stdout + r.stderr).strip()
            settings = self._load_settings()
            has_key = bool(settings.get("env", {}).get("ANTHROPIC_API_KEY"))
            return {
                "running": r.returncode == 0,
                "version": version,
                "message": "API key configured" if has_key else "No API key configured",
            }
        except FileNotFoundError:
            return {"running": False, "version": "", "message": "claude CLI not found"}
        except Exception as e:
            return {"running": False, "version": "", "message": str(e)[:100]}
