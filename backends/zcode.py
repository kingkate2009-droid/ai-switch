"""ZCode (智谱 ZCode) desktop app backend adapter.

Config: ~/.zcode/v2/config.json
Docs: https://zcode.z.ai/en/docs/configuration
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter, detect_install, status_from_detect, is_windows, env_path

log = logging.getLogger(__name__)


def _config_path() -> Path:
    return Path.home() / ".zcode" / "v2" / "config.json"


def _load() -> dict:
    p = _config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            log.warning("zcode config parse failed: %s", e)
    return {}


def _save(data: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _provider_id(vendor: dict) -> str:
    pid = (vendor.get("provider") or vendor.get("name") or "custom").strip()
    pid = re.sub(r"[^a-zA-Z0-9._-]+", "-", pid).strip("-").lower()
    return pid or "custom"


def _normalize_base_url(vendor: dict) -> str:
    url = (vendor.get("proxy_target") or vendor.get("api_url") or "").strip().rstrip("/")
    if not url:
        return ""
    if re.search(r"/v\d+$", url):
        return url
    ep = (vendor.get("endpoint_type") or "").lower()
    if ep == "anthropic" or "anthropic" in url.lower():
        return url + "/v1"
    return url + "/v1"


def _list_enabled_models(key: dict) -> list[str]:
    from core.data import get_enabled_models
    try:
        enabled = get_enabled_models(key)
    except Exception:
        enabled = []
    return [str(m).strip() for m in enabled if str(m).strip()]


class ZCodeAdapter(BackendAdapter):
    name = "zcode"
    display_name = "ZCode"

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not self.should_sync(vendor, key):
            return
        if not key.get("api_key") or key.get("enabled") is False:
            return
        data = _load()
        self._upsert_provider(data, vendor, key)
        _save(data)

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled") is False or not key.get("api_key"):
            self.on_key_removed(vendor, key)
            return
        if not self.should_sync(vendor, key):
            self.on_key_removed(vendor, key)
            return
        self.on_key_added(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        data = _load()
        self._remove_provider(data, vendor)
        _save(data)

    def on_vendor_removed(self, vendor: dict) -> None:
        data = _load()
        self._remove_provider(data, vendor)
        _save(data)

    def _upsert_provider(self, data: dict, vendor: dict, key: dict) -> None:
        pid = _provider_id(vendor)
        api_key = key.get("api_key", "")
        if not api_key:
            return
        base_url = _normalize_base_url(vendor)
        models = _list_enabled_models(key)
        providers = data.setdefault("provider", {})
        providers[pid] = {
            "options": {
                "apiKey": api_key,
                "baseURL": base_url,
                "apiKeyRequired": True,
            },
            "models": models,
        }

    def _remove_provider(self, data: dict, vendor: dict) -> None:
        pid = _provider_id(vendor)
        providers = data.get("provider")
        if isinstance(providers, dict):
            providers.pop(pid, None)

    def reconcile(self) -> None:
        from core.data import get_vendors
        data = _load()
        providers = data.get("provider")
        if not isinstance(providers, dict):
            providers = {}
            data["provider"] = providers
        desired: dict[str, tuple[dict, dict]] = {}
        for v in get_vendors():
            for k in v.get("keys") or []:
                if not self.should_sync(v, k):
                    continue
                if not k.get("api_key") or k.get("enabled") is False:
                    continue
                pid = _provider_id(v)
                desired[pid] = (v, k)
        for pid in list(providers.keys()):
            if pid not in desired:
                providers.pop(pid, None)
        for pid, (v, k) in desired.items():
            self._upsert_provider(data, v, k)
        _save(data)

    def sync_from_backend(self) -> list[dict]:
        data = _load()
        providers = data.get("provider") or {}
        if not isinstance(providers, dict):
            return []
        out = []
        for name, p in providers.items():
            if not isinstance(p, dict):
                continue
            opts = p.get("options") or {}
            api_key = (opts.get("apiKey") or "").strip()
            if not api_key:
                continue
            base = (opts.get("baseURL") or "").rstrip("/")
            models = p.get("models") or []
            out.append({
                "name": f"ZCode: {name}",
                "provider": name,
                "api_url": base,
                "endpoint_type": "openai",
                "keys": [{
                    "name": f"from {self.name}",
                    "api_key": api_key,
                    "models": [{"id": m, "name": m} for m in models if isinstance(m, str)],
                }],
            })
        return out

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(_config_path()), "label": "config.json", "type": "json"},
        ]

    def get_status(self) -> dict:
        cfg_exists = _config_path().exists()
        data = _load() if cfg_exists else {}
        providers = data.get("provider") if isinstance(data.get("provider"), dict) else {}
        count = len(providers)
        home = Path.home()
        app_paths = ["/Applications/ZCode.app", home / "Applications" / "ZCode.app"]
        if is_windows():
            local = env_path("LOCALAPPDATA") or (home / "AppData" / "Local")
            app_paths.append(local / "Programs" / "ZCode" / "ZCode.exe")
        det = detect_install(
            cli_commands=("zcode", "zcode.exe"),
            config_files=[_config_path()],
            app_paths=app_paths,
            process_markers=["ZCode", "ZCode.exe"],
            treat_config_as_installed=True,
        )
        if cfg_exists:
            msg = f"{count} provider(s) configured"
        else:
            msg = "config.json not found"
        return status_from_detect(
            det,
            not_installed_message="ZCode not found",
            message=msg,
        )
