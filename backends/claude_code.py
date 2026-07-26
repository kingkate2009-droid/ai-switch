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

    def _is_anthropic_vendor(self, vendor: dict) -> bool:
        ep = (vendor.get("endpoint_type") or "").lower()
        prov = (vendor.get("provider") or "").lower()
        return ep == "anthropic" or prov in ("anthropic", "claude")

    def _pick_key(self, vendor: dict, key_id: str = ""):
        keys = vendor.get("keys") or []
        if key_id:
            for k in keys:
                if str(k.get("id")) == str(key_id) and k.get("api_key"):
                    return k
            return None
        for k in keys:
            if k.get("enabled") is False:
                continue
            if k.get("api_key") and self.should_sync(vendor, k):
                return k
        for k in keys:
            if k.get("enabled") is not False and k.get("api_key"):
                return k
        return None

    @property
    def supports_active_switch(self) -> bool:
        return True

    def list_providers(self) -> list[dict]:
        from core.data import get_vendors
        settings = self._load_settings()
        env = settings.get("env") or {}
        active_key = (env.get("ANTHROPIC_API_KEY") or "").strip()
        active_url = (env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
        out = []
        for v in get_vendors():
            if not self._is_anthropic_vendor(v):
                continue
            k = self._pick_key(v)
            if not k:
                continue
            pid = f"aiswitch-{v.get('id')}"
            url = (v.get("proxy_target") or v.get("api_url") or "").rstrip("/")
            models = []
            for m in (k.get("models") or []):
                mid = m.get("id") if isinstance(m, dict) else str(m or "")
                if mid:
                    models.append(mid)
            is_active = bool(active_key) and (
                (k.get("api_key") or "").strip() == active_key
                or (active_url and url and active_url == url)
            )
            out.append({
                "id": pid,
                "name": v.get("name") or v.get("provider") or pid,
                "base_url": url,
                "vendor_id": str(v.get("id") or ""),
                "vendor_name": v.get("name") or "",
                "key_id": str(k.get("id") or ""),
                "key_name": k.get("name") or "",
                "models": models[:20],
                "model_preview": ", ".join(models[:6]) + ("…" if len(models) > 6 else ""),
                "active": is_active,
                "managed": True,
            })
        out.sort(key=lambda x: (0 if x.get("active") else 1, (x.get("name") or "").lower()))
        return out

    def get_active_provider(self) -> dict:
        settings = self._load_settings()
        env = settings.get("env") or {}
        api_key = (env.get("ANTHROPIC_API_KEY") or "").strip()
        base = (env.get("ANTHROPIC_BASE_URL") or "").strip()
        if not api_key:
            return {"active_provider": "", "name": "", "base_url": ""}
        for p in self.list_providers():
            if p.get("active"):
                return {
                    "active_provider": p.get("id") or "",
                    "name": p.get("name") or "",
                    "base_url": p.get("base_url") or base,
                }
        return {"active_provider": "env", "name": "Claude Code env", "base_url": base}

    def switch_provider(self, provider_id: str = "", vendor_id: str = "", key_id: str = "") -> dict:
        from core.data import get_vendor, get_vendors
        vendor = None
        if vendor_id:
            vendor = get_vendor(vendor_id)
        elif provider_id and str(provider_id).startswith("aiswitch-"):
            vendor = get_vendor(str(provider_id)[len("aiswitch-"):])
        if not vendor:
            # match by name/id in list
            for p in self.list_providers():
                if p.get("id") == provider_id:
                    vendor = get_vendor(str(p.get("vendor_id") or ""))
                    if not key_id:
                        key_id = str(p.get("key_id") or "")
                    break
        if not vendor:
            return {"success": False, "message": "Vendor not found"}
        if not self._is_anthropic_vendor(vendor):
            return {"success": False, "message": "Only Anthropic-compatible vendors can be active for Claude Code"}
        key = self._pick_key(vendor, key_id)
        if not key or not key.get("api_key"):
            return {"success": False, "message": "No enabled key on vendor"}
        if not self.is_installed():
            return {"success": False, "message": "Claude Code not installed"}
        # write env slot
        settings = self._load_settings()
        settings.setdefault("env", {})
        settings["env"]["ANTHROPIC_API_KEY"] = key["api_key"]
        api_url = (vendor.get("proxy_target") or vendor.get("api_url") or "").strip()
        if api_url:
            settings["env"]["ANTHROPIC_BASE_URL"] = api_url
        else:
            settings["env"].pop("ANTHROPIC_BASE_URL", None)
        self._save_settings(settings)
        pid = f"aiswitch-{vendor.get('id')}"
        return {
            "success": True,
            "active_provider": pid,
            "message": f"Claude Code active → {vendor.get('name') or pid}",
            "vendor_id": str(vendor.get("id") or ""),
            "key_id": str(key.get("id") or ""),
        }

    def reconcile(self) -> None:
        from core.data import get_vendors
        has_active = False
        for v in get_vendors():
            if not self._is_anthropic_vendor(v):
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
        from backends.base import detect_install, status_from_detect

        # Claude Code is primarily a CLI
        det = detect_install(
            cli_commands=("claude", "claude.exe"),
            process_markers=("claude.exe", " claude ", "Claude.app"),
            config_files=[self._settings_path, self._claude_json_path],
            data_dirs=[Path.home() / ".claude"],
            treat_config_as_installed=True,
        )
        settings = self._load_settings()
        has_key = bool(settings.get("env", {}).get("ANTHROPIC_API_KEY"))
        msg = "API key configured" if has_key else "No API key configured"
        return status_from_detect(
            det,
            not_installed_message="claude CLI not found",
            message=msg,
        )
