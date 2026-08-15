import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter


class QwenCodeAdapter(BackendAdapter):
    name = "qwencode"
    display_name = "QwenCode"

    @property
    def _settings_path(self) -> Path:
        return Path.home() / ".qwen" / "settings.json"

    def _load_settings(self) -> dict:
        if self._settings_path.exists():
            with open(self._settings_path) as f:
                return json.load(f)
        return {}

    def _save_settings(self, data: dict) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._settings_path, "w") as f:
            json.dump(data, f, indent=2)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        prov = vendor.get("provider", "").lower()
        settings = self._load_settings()
        settings.setdefault("env", {})

        if "dashscope" in prov or "qwen" in prov or "bailian" in prov:
            settings["env"]["DASHSCOPE_API_KEY"] = key["api_key"]
        elif "deepseek" in prov:
            settings["env"]["DEEPSEEK_API_KEY"] = key["api_key"]
        else:
            # Generic: store by vendor name
            settings["env"].setdefault("OPENAI_API_KEY", key["api_key"])

        # Also add as a model provider if base_url available
        if vendor.get("api_url"):
            settings.setdefault("modelProviders", {}).setdefault("openai", [])
            settings["modelProviders"]["openai"].append({
                "id": prov,
                "baseUrl": vendor["api_url"],
                "envKey": list(settings["env"].keys())[-1],
                "model": "qwen-coder-plus",
            })

        self._save_settings(settings)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        prov = vendor.get("provider", "").lower()
        settings = self._load_settings()
        # Remove model provider entry
        providers = settings.get("modelProviders", {}).get("openai", [])
        settings["modelProviders"]["openai"] = [p for p in providers if p.get("id") != prov]
        self._save_settings(settings)

    def reconcile(self) -> None:
        settings = self._load_settings()
        env = settings.get("env", {})
        from core.data import get_vendors

        def slot_for(provider: str) -> str:
            prov = provider.lower()
            if "dashscope" in prov or "qwen" in prov or "bailian" in prov:
                return "DASHSCOPE_API_KEY"
            if "deepseek" in prov:
                return "DEEPSEEK_API_KEY"
            return "OPENAI_API_KEY"

        slot_providers = {}
        for v in get_vendors():
            prov = str(v.get("provider") or "")
            slot_providers.setdefault(slot_for(prov), set()).add(prov)
        managed = {"DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"}
        for slot, providers in slot_providers.items():
            selected = self.pick_syncable_key(providers=providers, match_endpoint=False)
            if selected:
                env[slot] = selected[1]["api_key"]
            else:
                env.pop(slot, None)
        for slot in managed - set(slot_providers):
            env.pop(slot, None)
        settings["env"] = env
        model_providers = settings.get("modelProviders") or {}
        desired_models = []
        seen = set()
        for v in get_vendors():
            prov = str(v.get("provider") or "").lower()
            selected = self.pick_syncable_key(vendor=v)
            if not selected or not v.get("api_url"):
                continue
            if prov in seen:
                continue
            _, selected_key = selected
            from core.data import get_enabled_models
            model_ids = self.filter_model_ids(v, selected_key, get_enabled_models(selected_key))
            if selected_key.get("models") and not model_ids:
                continue
            seen.add(prov)
            slot = slot_for(prov)
            desired_models.append({
                "id": prov,
                "baseUrl": v["api_url"],
                "envKey": slot,
                "model": "qwen-coder-plus",
            })
        # The manager owns this provider list; rebuilding it removes entries
        # for deleted/disabled keys instead of accumulating duplicates.
        model_providers["openai"] = desired_models
        settings["modelProviders"] = model_providers
        self._save_settings(settings)

    def sync_from_backend(self) -> list[dict]:
        settings = self._load_settings()
        env = settings.get("env", {})
        vendors = []
        for env_key, api_key in env.items():
            if not api_key:
                continue
            prov = env_key.lower().replace("_api_key", "").replace("_token", "")
            vendors.append({
                "name": prov.title(),
                "provider": prov,
                "api_url": "",
                "endpoint_type": "openai",
                "keys": [{"name": f"from {self.name}", "api_key": api_key}],
            })
        return vendors

    @property
    def _env_path(self) -> Path:
        return Path.home() / ".qwen" / ".env"

    @property
    def _system_settings_path(self) -> Path:
        import platform
        system = platform.system()
        if system == "Darwin":
            return Path("/Library/Application Support/QwenCode/settings.json")
        if system == "Windows":
            prog = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
            return Path(prog) / "QwenCode" / "settings.json"
        return Path("/etc/qwen-code/settings.json")

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._settings_path), "label": "User Settings", "type": "json"},
            {"path": str(self._env_path), "label": "Environment", "type": "env"},
            {"path": str(self._system_settings_path), "label": "System Settings", "type": "json"},
        ]

    def get_status(self) -> dict:
        from backends.base import detect_install, status_from_detect
        settings = self._load_settings()
        has_key = bool(settings.get("env", {}))
        det = detect_install(
            cli_commands=("qwen", "qwen.exe", "qwen-code"),
            config_files=[self._settings_path, self._env_path],
            data_dirs=[Path.home() / ".qwen"],
            treat_config_as_installed=True,
        )
        msg = "CLI available" if det.get("version") else ("Key configured" if has_key else "Installed")
        return status_from_detect(
            det,
            not_installed_message="Not installed",
            message=msg,
        )
