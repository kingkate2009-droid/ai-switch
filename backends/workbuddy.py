"""WorkBuddy (Tencent) desktop app backend adapter.

WorkBuddy stores custom models in ~/.codebuddy/models.json (legacy path
that still works with current versions). Each model gets its own entry
with provider, baseUrl, apiKey, and capabilities.

Docs: https://www.workbuddy.ai/docs/zh/workbuddy/
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


def _models_path() -> Path:
    return Path.home() / ".codebuddy" / "models.json"


def _load() -> dict:
    p = _models_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            log.warning("workbuddy config parse failed: %s", e)
    return {}


def _save(data: dict) -> None:
    p = _models_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _normalize_base_url(vendor: dict) -> str:
    url = (vendor.get("proxy_target") or vendor.get("api_url") or "").strip().rstrip("/")
    if not url:
        return ""
    if re.search(r"/v\d+$", url):
        return url
    return url + "/v1"


def _list_enabled_models(key: dict) -> list[str]:
    from core.data import get_enabled_models, list_model_ids
    try:
        enabled = get_enabled_models(key) or list_model_ids(key) or []
    except Exception:
        enabled = []
    return [str(m).strip() for m in enabled if str(m).strip()]


def _detect_capabilities(model_id: str) -> list[str]:
    caps = ["tool_calling"]
    m = model_id.lower()
    if any(x in m for x in ("vision", "image", "omni", "multimodal")):
        caps.append("image_input")
    if any(x in m for x in ("reason", "deep", "think")):
        caps.append("reasoning")
    return caps


class WorkBuddyAdapter(BackendAdapter):
    name = "workbuddy"
    display_name = "WorkBuddy"

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not self.should_sync(vendor, key):
            return
        if not key.get("api_key") or key.get("enabled") is False:
            return
        data = _load()
        self._upsert_models(data, vendor, key)
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
        self._remove_models(data, vendor, key)
        _save(data)

    def on_vendor_removed(self, vendor: dict) -> None:
        data = _load()
        if not isinstance(data.get("models"), list):
            return
        data["models"] = [m for m in data["models"] if m.get("providerId") != str(vendor.get("id"))]
        _save(data)

    def _upsert_models(self, data: dict, vendor: dict, key: dict) -> None:
        api_key = key.get("api_key", "")
        if not api_key:
            return
        base_url = _normalize_base_url(vendor)
        provider_name = (vendor.get("provider") or "custom").lower()
        vid = str(vendor.get("id") or "")
        models = data.setdefault("models", [])
        if not isinstance(models, list):
            models = []
            data["models"] = models
        # Remove existing entries for this vendor+key
        self._remove_models(data, vendor, key)
        # Add one entry per enabled model
        enabled = _list_enabled_models(key)
        if not enabled:
            enabled = ["default"]
        for mid in enabled:
            models.append({
               "id": f"{provider_name}/{mid}",
                "provider": provider_name,
                "providerId": vid,
                "keyId": str(key.get("id") or ""),
                "name": f"{vendor.get('name') or provider_name}: {mid}",
                "baseUrl": base_url,
                "apiKey": api_key,
                "capabilities": _detect_capabilities(mid),
                "type": "openai",
            })

    def _remove_models(self, data: dict, vendor: dict, key: dict) -> None:
        models = data.get("models")
        if not isinstance(models, list):
            return
        vid = str(vendor.get("id") or "")
        kid = str(key.get("id") or "")
        data["models"] = [m for m in models if str(m.get("providerId") or "") != vid or str(m.get("keyId") or "") != kid]

    def reconcile(self) -> None:
        from core.data import get_vendors
        data = _load()
        models = data.get("models")
        if not isinstance(models, list):
            models = []
            data["models"] = models
        # Track desired set by (vid, kid)
        desired: set[tuple[str, str]] = set()
        for v in get_vendors():
            for k in v.get("keys") or []:
                if not self.should_sync(v, k):
                    continue
                if not k.get("api_key") or k.get("enabled") is False:
                    continue
                desired.add((str(v.get("id") or ""), str(k.get("id") or "")))
        # Remove stale entries not in desired set
        data["models"] = [m for m in models if (m.get("providerId"), m.get("keyId")) in desired]
        # Upsert all desired
        for v in get_vendors():
            for k in v.get("keys") or []:
                if not self.should_sync(v, k):
                    continue
                if not k.get("api_key") or k.get("enabled") is False:
                    continue
                self._upsert_models(data, v, k)
        _save(data)

    def sync_from_backend(self) -> list[dict]:
        data = _load()
        models = data.get("models")
        if not isinstance(models, list):
            return []
        by_provider: dict[str, list] = {}
        for m in models:
            if not isinstance(m, dict):
                continue
            api_key = (m.get("apiKey") or "").strip()
            if not api_key:
                continue
            prov = (m.get("provider") or "custom").strip()
            by_provider.setdefault(prov, []).append(m)
        out = []
        for prov, entries in by_provider.items():
            api_url = ""
            keys = []
            for e in entries:
                base = (e.get("baseUrl") or "").rstrip("/")
                if base and not api_url:
                    api_url = base
                mid = e.get("id", "")
                keys.append({
                    "name": e.get("name") or mid,
                    "api_key": e.get("apiKey", ""),
                    "models": [{"id": mid, "name": mid}] if mid else [],
                })
            out.append({
                "name": f"WorkBuddy: {prov}",
                "provider": prov,
                "api_url": api_url,
                "endpoint_type": "openai",
                "keys": keys,
            })
        return out

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(_models_path()), "label": "models.json", "type": "json"},
        ]

    def get_status(self) -> dict:
        cfg_exists = _models_path().exists()
        data = _load() if cfg_exists else {}
        models = data.get("models") if isinstance(data.get("models"), list) else []
        count = len(models)
        home = Path.home()
        app_paths = ["/Applications/WorkBuddy.app", home / "Applications" / "WorkBuddy.app"]
        if is_windows():
            local = env_path("LOCALAPPDATA") or (home / "AppData" / "Local")
            app_paths.append(local / "Programs" / "WorkBuddy" / "WorkBuddy.exe")
        det = detect_install(
            cli_commands=("workbuddy", "workbuddy.exe"),
            config_files=[_models_path()],
            app_paths=app_paths,
            process_markers=["WorkBuddy", "WorkBuddy.exe"],
            treat_config_as_installed=True,
        )
        if cfg_exists:
            msg = f"{count} model(s) configured"
        else:
            msg = "models.json not found"
        return status_from_detect(
            det,
            not_installed_message="WorkBuddy not found",
            message=msg,
        )