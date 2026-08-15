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
        other = self.pick_syncable_key(
            exclude=(str(vendor.get("id") or ""), str(key.get("id") or "")),
        )
        if other and other[1].get("id") != key.get("id"):
            self.on_key_added(other[0], other[1])
            return
        settings = self._load_settings()
        settings.pop("byok", None)
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(settings, f, indent=2)

    def reconcile(self) -> None:
        best = self.pick_syncable_key()
        if best:
            self.on_key_added(best[0], best[1])
            return
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
        from backends.base import (
            make_status, enriched_env, INSTALL_CLI, INSTALL_EXTENSION, INSTALL_CONFIG,
        )
        import shutil

        settings = self._load_settings()
        has_byok = "byok" in settings
        env = enriched_env()
        kinds = []
        version = ""
        if shutil.which("gh", path=env.get("PATH")) or shutil.which("gh"):
            kinds.append(INSTALL_CLI)
        try:
            r = subprocess.run(
                ["gh", "copilot", "--version"],
                capture_output=True, text=True, timeout=5, env=env,
            )
            out = (r.stdout or "") + (r.stderr or "")
            low = out.lower()
            if r.returncode == 0 and "unknown command" not in low and "available commands" not in low:
                kinds.append(INSTALL_EXTENSION)
                version = out.strip().splitlines()[0][:80] if out.strip() else "gh copilot"
                return make_status(
                    installed=True,
                    running=False,
                    version=version,
                    message=f"[CLI+extension] {'BYOK configured' if has_byok else 'GitHub auth mode'}",
                    install_kinds=list(dict.fromkeys(kinds + [INSTALL_EXTENSION])),
                )
            if has_byok or self._settings_path.exists():
                if INSTALL_CONFIG not in kinds:
                    kinds.append(INSTALL_CONFIG)
                return make_status(
                    installed=True,
                    running=False,
                    version="",
                    message="[CLI] BYOK configured (gh copilot extension missing)",
                    install_kinds=kinds or [INSTALL_CONFIG],
                )
            if kinds:
                return make_status(
                    installed=True,
                    running=False,
                    message="[CLI] gh installed; copilot extension missing",
                    install_kinds=kinds,
                )
            return make_status(installed=False, message="gh CLI / copilot extension not installed")
        except FileNotFoundError:
            if has_byok or self._settings_path.exists():
                return make_status(
                    installed=True,
                    running=False,
                    message="[config] BYOK file present; gh CLI not found",
                    install_kinds=[INSTALL_CONFIG],
                )
            return make_status(installed=False, message="gh CLI not found")
        except Exception as e:
            return make_status(installed=False, message=str(e)[:100])

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._settings_path), "label": "Settings", "type": "jsonc"},
            {"path": str(self._config_path), "label": "App State", "type": "json"},
        ]
