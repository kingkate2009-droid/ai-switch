"""Codex CLI backend adapter.

Docs: https://developers.openai.com/codex/config-file/
Config: ~/.codex/config.toml  (override with $CODEX_HOME)
Auth:   ~/.codex/auth.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from backends.base import BackendAdapter

log = logging.getLogger(__name__)

# Managed provider prefix so we never overwrite reserved IDs (openai/ollama/lmstudio)
_MANAGED_PREFIX = "aiswitch-"

# Reserved provider IDs that cannot be overridden
_RESERVED_IDS = {"openai", "ollama", "lmstudio"}

# Map endpoint_type to env_key names
_ENV_KEY_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
    "cohere": "COHERE_API_KEY",
}


def _home_dir() -> Path:
    env = (os.environ.get("CODEX_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    # Official default is ~/.codex on all platforms (Windows: %USERPROFILE%\.codex)
    return Path.home() / ".codex"


# ── TOML helpers ──────────────────────────────────────────────


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("read codex config failed: %s", e)
        return {}
    if not text.strip():
        return {}
    try:
        import tomli
        return tomli.loads(text)
    except Exception:
        pass
    try:
        import tomllib  # py311+
        return tomllib.loads(text)
    except Exception as e:
        log.warning("parse codex config.toml failed: %s", e)
        return {}


def _escape_key(key: str) -> str:
    if re.match(r"^[A-Za-z0-9_\-]+$", key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_str(val: str) -> str:
    return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _dump_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, str):
        return _escape_str(val)
    if isinstance(val, list):
        return "[ " + ", ".join(_dump_value(x) for x in val) + " ]"
    if isinstance(val, dict):
        parts = []
        for k, v in val.items():
            parts.append(f"{_escape_key(str(k))} = {_dump_value(v)}")
        return "{ " + ", ".join(parts) + " }"
    return _escape_str(str(val))


def _dump_table(header: str, data: dict, lines: list[str]) -> None:
    if header:
        lines.append(f"[{header}]")
    for k, v in data.items():
        if isinstance(v, dict):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            continue
        lines.append(f"{_escape_key(str(k))} = {_dump_value(v)}")


def _dump_toml(data: dict) -> str:
    """Minimal TOML writer for Codex config shape."""
    lines: list[str] = [
        "# Managed in part by ai-switch (Codex CLI backend).",
        "# Docs: https://developers.openai.com/codex/config-file/",
        "",
    ]
    nested = {}
    for k, v in data.items():
        if isinstance(v, dict):
            nested[k] = v
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            nested[k] = v
            continue
        lines.append(f"{_escape_key(str(k))} = {_dump_value(v)}")
    if nested and lines and lines[-1] != "":
        lines.append("")

    for name, table in nested.items():
        if isinstance(table, list):
            for item in table:
                if not isinstance(item, dict):
                    continue
                lines.append(f"[[{_escape_key(str(name))}]]")
                for k, v in item.items():
                    lines.append(f"{_escape_key(str(k))} = {_dump_value(v)}")
                lines.append("")
            continue
        if not isinstance(table, dict):
            continue
        sub_maps = {k: v for k, v in table.items() if isinstance(v, dict)}
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        if scalars and not sub_maps:
            _dump_table(_escape_key(str(name)), scalars, lines)
            lines.append("")
            continue
        if scalars:
            _dump_table(_escape_key(str(name)), scalars, lines)
            lines.append("")
        for sub_key, sub_val in sub_maps.items():
            deeper = {k: v for k, v in sub_val.items() if isinstance(v, dict)}
            leaf = {k: v for k, v in sub_val.items() if not isinstance(v, dict)}
            header = f"{_escape_key(str(name))}.{_escape_key(str(sub_key))}"
            if leaf or not deeper:
                _dump_table(header, leaf, lines)
                lines.append("")
            for dk, dv in deeper.items():
                if not isinstance(dv, dict):
                    continue
                h2 = f"{header}.{_escape_key(str(dk))}"
                _dump_table(h2, dv, lines)
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── Provider helpers ──────────────────────────────────────────


def _provider_id(vendor_id: str) -> str:
    return f"{_MANAGED_PREFIX}{vendor_id}"


def _env_key_for_vendor(vendor: dict) -> str:
    ep = (vendor.get("endpoint_type") or "openai").lower().strip()
    return _ENV_KEY_MAP.get(ep, "OPENAI_API_KEY")


def _wire_api_for_vendor(vendor: dict) -> str:
    # Codex CLI (0.144+) only supports wire_api = "responses".
    # "chat" was removed and now fails config load.
    return "responses"


def _base_url(vendor: dict) -> str:
    raw = (vendor.get("proxy_target") or vendor.get("api_url") or "").strip().rstrip("/")
    if not raw:
        return ""
    # Codex expects an OpenAI-compatible base that includes /v1 for most proxies.
    if raw.endswith("/v1") or raw.endswith("/openai/v1"):
        return raw
    # Keep absolute custom paths (e.g. Azure-style) untouched if they already have a path.
    # For bare hosts / roots, append /v1.
    try:
        from urllib.parse import urlparse
        path = (urlparse(raw).path or "").strip("/")
        if path and path not in ("v1",):
            return raw
    except Exception:
        pass
    return raw + "/v1"


def _provider_entry(vendor: dict, api_key: str = "") -> dict:
    """Build a Codex model_providers.* entry for a vendor."""
    entry = {
        "name": vendor.get("name") or vendor.get("provider") or "Custom",
        "base_url": _base_url(vendor),
        "wire_api": _wire_api_for_vendor(vendor),
    }
    # Prefer embedding the token so IDE/extension sessions don't depend on process env.
    # Do not combine env_key with experimental_bearer_token (Codex treats env_key as required).
    if api_key:
        entry["experimental_bearer_token"] = api_key
    else:
        entry["env_key"] = _env_key_for_vendor(vendor)
    return entry


def _is_gpt_related_model(model_id: str) -> bool:
    """Codex CLI works with OpenAI-style GPT / o-series model ids."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    # strip provider/ prefix: openai/gpt-4o -> gpt-4o
    if "/" in mid and not mid.startswith("http"):
        mid = mid.rsplit("/", 1)[-1]
    if mid.startswith("gpt") or "gpt-" in mid or "chatgpt" in mid:
        return True
    # OpenAI reasoning models used by Codex
    if mid in ("o1", "o3", "o4") or mid.startswith(("o1-", "o3-", "o4-", "o1_", "o3_", "o4_")):
        return True
    if mid.startswith("codex") or "codex-" in mid:
        return True
    # Open-weight OpenAI-compatible models often work via Responses API
    if "gpt-oss" in mid or mid.startswith("oss-"):
        return True
    return False


def _vendor_all_models(vendor: dict) -> list[str]:
    """All known model ids from enabled keys (default model first)."""
    from core.data import get_enabled_models, list_model_ids

    out, seen = [], set()
    for k in vendor.get("keys") or []:
        if k.get("enabled") is False or not k.get("api_key"):
            continue
        ids = list(get_enabled_models(k) or list_model_ids(k) or [])
        dm = (k.get("default_model") or "").strip()
        if dm:
            ids = [dm] + [x for x in ids if x != dm]
        for mid in ids:
            s = str(mid or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def _vendor_gpt_models(vendor: dict) -> list[str]:
    """Collect GPT-related model ids from enabled keys on a vendor."""
    return [m for m in _vendor_all_models(vendor) if _is_gpt_related_model(m)]


def _vendor_codex_models(vendor: dict) -> list[str]:
    """Models suitable for Codex: prefer GPT-family, else any OpenAI-compatible inventory."""
    gpt = _vendor_gpt_models(vendor)
    if gpt:
        return gpt
    return _vendor_all_models(vendor)


def _vendor_is_codex_switchable(vendor: dict) -> bool:
    """Vendors with OpenAI-compatible endpoint + enabled key (or GPT model inventory)."""
    if not _base_url(vendor):
        return False
    has_key = any(
        k.get("enabled") is not False and k.get("api_key")
        for k in (vendor.get("keys") or [])
    )
    if not has_key:
        return False
    # Prefer inventory with GPT-related models
    if _vendor_gpt_models(vendor):
        return True
    # OpenAI-compatible endpoints (including third-party Responses proxies)
    ep = (vendor.get("endpoint_type") or "").lower()
    prov = (vendor.get("provider") or "").lower()
    url = _base_url(vendor).lower()
    if ep in ("openai", "") or prov in ("openai",) or "api.openai.com" in url or "/v1" in url:
        return True
    # Any vendor that already has a scanned model inventory can be tried
    if _vendor_all_models(vendor):
        return True
    return False


def _apply_third_party_safe_defaults(cfg: dict, vendor: Optional[dict] = None) -> None:
    """Disable features third-party OpenAI proxies often reject (web_search tools, etc.)."""
    url = ""
    if vendor:
        url = _base_url(vendor).lower()
    # Always safe for non-official OpenAI hosts
    if not url or "api.openai.com" not in url:
        cfg["web_search"] = "disabled"
        tools = cfg.get("tools")
        if not isinstance(tools, dict):
            tools = {}
        tools["web_search"] = False
        cfg["tools"] = tools


# ── Adapter ───────────────────────────────────────────────────


class CodexCliAdapter(BackendAdapter):
    name = "codex-cli"
    display_name = "Codex CLI"

    def __init__(self):
        super().__init__()
        self._codex_dir = _home_dir()
        self._auth_path = self._codex_dir / "auth.json"
        self._config_path = self._codex_dir / "config.toml"

    def _load_auth(self) -> dict:
        if self._auth_path.exists():
            try:
                with open(self._auth_path) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_auth(self, data: dict) -> None:
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        with open(self._auth_path, "w") as f:
            json.dump(data, f, indent=2)

    def _load_config(self) -> dict:
        return _load_toml(self._config_path)

    def _save_config(self, data: dict) -> None:
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(_dump_toml(data), encoding="utf-8")

    def _sync_vendor_to_config(self, vendor: dict, key: dict) -> None:
        """Write or update a vendor entry in config.toml model_providers."""
        if not key.get("api_key") or not key.get("enabled", True):
            return
        if not _vendor_is_codex_switchable(vendor):
            return

        provider_id = _provider_id(vendor["id"])
        base_url = _base_url(vendor)
        if not base_url:
            return

        cfg = self._load_config()
        providers = cfg.setdefault("model_providers", {})
        providers[provider_id] = _provider_entry(vendor, key.get("api_key") or "")
        self._save_config(cfg)

    def _set_active_auth(self, api_key: str) -> None:
        """Write auth.json + process env for the active API key."""
        if not api_key:
            return
        auth = self._load_auth()
        auth["OPENAI_API_KEY"] = api_key
        self._save_auth(auth)
        os.environ["OPENAI_API_KEY"] = api_key

    def _remove_vendor_from_config(self, vendor_id: str) -> None:
        """Remove a vendor entry from config.toml model_providers."""
        provider_id = _provider_id(vendor_id)
        cfg = self._load_config()
        providers = cfg.get("model_providers") or {}
        if provider_id in providers:
            del providers[provider_id]
            if not providers:
                del cfg["model_providers"]
            # If this was the active provider, clear it
            if cfg.get("model_provider") == provider_id:
                del cfg["model_provider"]
            self._save_config(cfg)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        # Only write provider catalog entry. Do NOT hijack active auth/provider
        # unless this vendor is already the active model_provider (or none set).
        self._sync_vendor_to_config(vendor, key)
        cfg = self._load_config()
        pid = _provider_id(vendor["id"])
        active = cfg.get("model_provider") or ""
        if not active or active == pid:
            cfg["model_provider"] = pid
            models = _vendor_codex_models(vendor)
            if models:
                cfg["model"] = models[0]
            _apply_third_party_safe_defaults(cfg, vendor)
            # keep active provider entry tokenized
            providers = cfg.setdefault("model_providers", {})
            providers[pid] = _provider_entry(vendor, key.get("api_key") or "")
            self._save_config(cfg)
            self._set_active_auth(key.get("api_key") or "")

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled", True):
            self.on_key_added(vendor, key)
        else:
            self.on_key_removed(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        # Remove from config.toml
        self._remove_vendor_from_config(vendor["id"])

        # If this was the active key in auth.json, clear it
        auth = self._load_auth()
        if auth.get("OPENAI_API_KEY") == key.get("api_key"):
            auth.pop("OPENAI_API_KEY", None)
            self._save_auth(auth)

    def on_vendor_removed(self, vendor: dict) -> None:
        self._remove_vendor_from_config(vendor["id"])

    def reconcile(self) -> None:
        from core.data import get_vendors, get_backend_config

        if get_backend_config(self.name).get("disabled"):
            return

        vendors = get_vendors()
        cfg = self._load_config()
        providers = cfg.get("model_providers") or {}

        # Build set of valid managed provider IDs (GPT-capable vendors only)
        valid_ids = set()
        for v in vendors:
            if not _vendor_is_codex_switchable(v):
                continue
            for k in v.get("keys", []):
                if not k.get("enabled", True) or not k.get("api_key"):
                    continue
                if not self.should_sync(v, k):
                    continue
                pid = _provider_id(v["id"])
                valid_ids.add(pid)
                # Ensure provider exists in config (with token so IDE does not need env)
                base_url = _base_url(v)
                if base_url:
                    providers[pid] = _provider_entry(v, k.get("api_key") or "")

        # Remove stale managed providers
        stale = [pid for pid in providers if pid.startswith(_MANAGED_PREFIX) and pid not in valid_ids]
        for pid in stale:
            del providers[pid]

        if providers:
            cfg["model_providers"] = providers
        elif "model_providers" in cfg:
            del cfg["model_providers"]

        # Check if active provider still valid; if not, pick first valid managed provider.
        active = cfg.get("model_provider") or ""
        if active.startswith(_MANAGED_PREFIX) and active not in valid_ids:
            if valid_ids:
                # Prefer keeping a deterministic order by vendor id numeric
                def _pid_sort(pid: str):
                    tail = pid[len(_MANAGED_PREFIX):]
                    try:
                        return (0, int(tail))
                    except Exception:
                        return (1, tail)
                active = sorted(valid_ids, key=_pid_sort)[0]
                cfg["model_provider"] = active
            else:
                cfg.pop("model_provider", None)
                active = ""

        # Keep active provider's auth.json + bearer token aligned
        active_key = ""
        if active.startswith(_MANAGED_PREFIX):
            vid = active[len(_MANAGED_PREFIX):]
            for v in vendors:
                if str(v.get("id")) != str(vid):
                    continue
                for k in v.get("keys") or []:
                    if k.get("enabled", True) and k.get("api_key") and self.should_sync(v, k):
                        active_key = k["api_key"]
                        # ensure active provider entry has token
                        providers[active] = _provider_entry(v, active_key)
                        models = _vendor_codex_models(v)
                        if models and not (cfg.get("model") or "").strip():
                            cfg["model"] = models[0]
                        _apply_third_party_safe_defaults(cfg, v)
                        break
                break

        if providers:
            cfg["model_providers"] = providers
        self._save_config(cfg)

        if active_key:
            self._set_active_auth(active_key)
        else:
            # Ensure auth.json has a valid key only if nothing active
            has_active = any(
                k.get("enabled", True) and k.get("api_key")
                for v in vendors
                for k in v.get("keys", [])
            )
            if not has_active:
                auth = self._load_auth()
                if auth.pop("OPENAI_API_KEY", None):
                    self._save_auth(auth)

    def switch_provider(self, provider_id: str = "", vendor_id: str = "", key_id: str = "") -> dict:
        """Switch the active provider for Codex CLI.

        Args:
            provider_id: Direct provider ID (e.g. "aiswitch-2")
            vendor_id: System vendor ID (will find best key)
            key_id: Specific key ID (optional, with vendor_id)

        Returns:
            dict with success, active_provider, message
        """
        from core.data import get_vendor, get_key

        # Resolve provider_id from vendor_id if needed
        if not provider_id and vendor_id:
            vendor = get_vendor(vendor_id)
            if not vendor:
                return {"success": False, "message": f"Vendor {vendor_id} not found"}
            if not _vendor_is_codex_switchable(vendor):
                return {"success": False, "message": "Vendor is not OpenAI-compatible for Codex (need API URL + enabled key)"}
            provider_id = _provider_id(vendor_id)

            # Find the key to use
            if key_id:
                key = get_key(vendor_id, key_id)
                if not key:
                    return {"success": False, "message": f"Key {key_id} not found"}
            else:
                # Use first enabled key with api_key
                keys = vendor.get("keys") or []
                key = next((k for k in keys if k.get("enabled", True) and k.get("api_key")), None)
                if not key:
                    return {"success": False, "message": "No enabled key found for vendor"}

            # Ensure vendor is synced to config
            self._sync_vendor_to_config(vendor, key)
            self._set_active_auth(key.get("api_key") or "")

        elif provider_id:
            # Direct provider_id - verify it exists in config
            cfg = self._load_config()
            providers = cfg.get("model_providers") or {}
            if provider_id not in providers and not provider_id.startswith(_MANAGED_PREFIX):
                return {"success": False, "message": f"Provider {provider_id} not found in config"}

            # Find matching vendor and update auth
            if provider_id.startswith(_MANAGED_PREFIX):
                vid = provider_id[len(_MANAGED_PREFIX):]
                vendor = get_vendor(vid)
                if vendor:
                    if not _vendor_is_codex_switchable(vendor):
                        return {"success": False, "message": "Vendor is not OpenAI-compatible for Codex"}
                    keys = vendor.get("keys") or []
                    key = next((k for k in keys if k.get("enabled", True) and k.get("api_key")), None)
                    if key:
                        self._sync_vendor_to_config(vendor, key)
                        self._set_active_auth(key.get("api_key") or "")
                        models = _vendor_codex_models(vendor)
                        if models:
                            cfg0 = self._load_config()
                            cfg0["model"] = models[0]
                            self._save_config(cfg0)
        else:
            return {"success": False, "message": "provider_id or vendor_id required"}

        # Update model_provider in config
        cfg = self._load_config()
        cfg["model_provider"] = provider_id
        # Always set model from inventory when available (avoids stale broken model ids)
        resolved_vid = vendor_id
        if not resolved_vid and provider_id.startswith(_MANAGED_PREFIX):
            resolved_vid = provider_id[len(_MANAGED_PREFIX):]
        if resolved_vid:
            vendor = get_vendor(resolved_vid)
            if vendor:
                models = _vendor_codex_models(vendor)
                if models:
                    cfg["model"] = models[0]
                # ensure active provider entry includes bearer token
                key = next((k for k in (vendor.get("keys") or []) if k.get("enabled", True) and k.get("api_key")), None)
                if key:
                    providers = cfg.setdefault("model_providers", {})
                    providers[provider_id] = _provider_entry(vendor, key.get("api_key") or "")
                    self._set_active_auth(key.get("api_key") or "")
                _apply_third_party_safe_defaults(cfg, vendor)
        self._save_config(cfg)

        return {
            "success": True,
            "active_provider": provider_id,
            "message": f"Switched to {provider_id}",
            "model": cfg.get("model") or "",
        }

    def list_providers(self) -> list[dict]:
        """List switchable Codex providers (vendors with GPT-related models).

        Includes managed entries from system vendors (even if not yet written
        to config.toml) plus any already-configured aiswitch-* entries that still
        qualify.
        """
        from core.data import get_vendors

        cfg = self._load_config()
        providers = cfg.get("model_providers") or {}
        active_id = cfg.get("model_provider") or ""
        vendors = get_vendors()
        vendors_by_id = {str(v["id"]): v for v in vendors}

        result = []
        seen_ids = set()

        # 1) System vendors that have GPT-related models
        for v in vendors:
            if not _vendor_is_codex_switchable(v):
                continue
            # need at least one syncable key
            keys = [
                k for k in (v.get("keys") or [])
                if k.get("enabled") is not False and k.get("api_key") and self.should_sync(v, k)
            ]
            if not keys:
                continue
            pid = _provider_id(v["id"])
            seen_ids.add(pid)
            pdata = providers.get(pid) or {}
            gpt_models = _vendor_gpt_models(v)
            result.append({
                "id": pid,
                "name": v.get("name") or pdata.get("name") or v.get("provider") or pid,
                "base_url": _base_url(v) or pdata.get("base_url") or "",
                "env_key": pdata.get("env_key") or _env_key_for_vendor(v),
                "wire_api": pdata.get("wire_api") or _wire_api_for_vendor(v),
                "vendor_id": str(v.get("id") or ""),
                "vendor_name": v.get("name") or "",
                "models": gpt_models,
                "model_preview": ", ".join(gpt_models[:6]) + ("…" if len(gpt_models) > 6 else ""),
                "has_gpt_models": bool(gpt_models),
                "active": pid == active_id,
                "managed": True,
            })

        # 2) Extra config.toml managed providers still mapped to GPT vendors
        for pid, pdata in providers.items():
            if pid in seen_ids:
                continue
            if not pid.startswith(_MANAGED_PREFIX):
                continue
            vid = pid[len(_MANAGED_PREFIX):]
            vendor = vendors_by_id.get(str(vid))
            if not vendor or not _vendor_is_codex_switchable(vendor):
                continue
            gpt_models = _vendor_gpt_models(vendor)
            result.append({
                "id": pid,
                "name": pdata.get("name") or (vendor.get("name") if vendor else pid),
                "base_url": pdata.get("base_url") or "",
                "env_key": pdata.get("env_key") or "",
                "wire_api": pdata.get("wire_api") or "chat",
                "vendor_id": str(vid),
                "vendor_name": (vendor.get("name") if vendor else "") or "",
                "models": gpt_models,
                "model_preview": ", ".join(gpt_models[:6]) + ("…" if len(gpt_models) > 6 else ""),
                "has_gpt_models": bool(gpt_models),
                "active": pid == active_id,
                "managed": True,
            })

        # Sort: active first, then vendors with more GPT models, then name
        result.sort(key=lambda x: (
            0 if x["active"] else 1,
            -len(x.get("models") or []),
            (x.get("name") or "").lower(),
        ))
        return result

    def get_active_provider(self) -> dict:
        """Get the currently active provider."""
        cfg = self._load_config()
        active_id = cfg.get("model_provider") or ""
        if not active_id:
            return {"active_provider": "", "name": "", "base_url": ""}

        providers = cfg.get("model_providers") or {}
        pdata = providers.get(active_id) or {}
        return {
            "active_provider": active_id,
            "name": pdata.get("name") or active_id,
            "base_url": pdata.get("base_url") or "",
        }

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
        from backends.base import detect_install, status_from_detect, process_running

        # Codex: CLI and/or VS Code / Cursor ChatGPT extension (plugin)
        det = detect_install(
            cli_commands=("codex", "codex.exe"),
            extension_ids=("openai.chatgpt",),
            process_markers=(
                "codex app-server",
                "codex -c features",
                "openai.chatgpt-",
            ),
            data_dirs=[self._codex_dir],
            config_files=[self._config_path, self._auth_path],
            treat_config_as_installed=True,
        )
        # Windows: codex.exe + app-server
        if not det.get("running"):
            if process_running("codex.exe") and process_running("app-server"):
                det["running"] = True
                if "cli" not in det.get("install_kinds", []):
                    det.setdefault("install_kinds", []).append("cli")
                det["installed"] = True

        auth = self._load_auth()
        has_key = bool(auth.get("OPENAI_API_KEY"))
        cfg = self._load_config() if self._config_path.exists() else {}
        active = cfg.get("model_provider") or ""
        providers = cfg.get("model_providers") or {}

        msg_parts = []
        if has_key:
            msg_parts.append("API key configured")
        if active:
            msg_parts.append(f"active: {active}")
        if providers:
            msg_parts.append(f"{len(providers)} provider(s)")
        msg = "; ".join(msg_parts) if msg_parts else "Installed"
        if det.get("running"):
            msg = "app-server running; " + msg

        return status_from_detect(
            det,
            not_installed_message="codex CLI / ChatGPT extension not installed",
            message=msg,
            active_provider=active,
            provider_count=len(providers),
        )

    def restart(self) -> dict:
        return {"success": False, "message": "Codex CLI does not require restart"}
