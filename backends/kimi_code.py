"""Kimi Code CLI backend adapter.

Docs: https://www.kimi.com/code/docs/
Config: ~/.kimi-code/config.toml  (override with $KIMI_CODE_HOME)
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from backends.base import BackendAdapter

log = logging.getLogger(__name__)

# Managed provider/model prefix so we never overwrite OAuth managed:kimi-code
_MANAGED_PREFIX = "aiswitch@"

_KIMI_CODING_BASE = "https://api.kimi.com/coding/v1"
_MOONSHOT_BASE = "https://api.moonshot.cn/v1"

_DEFAULT_KIMI_MODELS = (
    ("k3", 1_048_576, "K3"),
    ("kimi-for-coding", 262_144, "Kimi for Coding"),
    ("kimi-for-coding-highspeed", 262_144, "Kimi for Coding Highspeed"),
)

_KIMI_PROVIDER_IDS = {
    "kimi", "kimi-code", "kimicode", "moonshot", "moonshot-ai", "moonshotai",
}


def _home_dir() -> Path:
    env = (os.environ.get("KIMI_CODE_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".kimi-code"


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("read kimi config failed: %s", e)
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
        log.warning("parse kimi config.toml failed: %s", e)
        return {}


def _escape_key(key: str) -> str:
    """TOML bare key or quoted key."""
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
        # inline table
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
            continue  # nested handled separately
        if isinstance(v, list) and v and isinstance(v[0], dict):
            continue
        lines.append(f"{_escape_key(str(k))} = {_dump_value(v)}")


def _dump_toml(data: dict) -> str:
    """Minimal TOML writer for our config shape (top scalars + tables + nested tables)."""
    lines: list[str] = [
        "# Managed in part by ai-switch (Kimi Code backend).",
        "# Docs: https://www.kimi.com/code/docs/kimi-code-cli/configuration/config-files.html",
        "",
    ]
    # top-level scalars first
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
            # array of tables [[name]]
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
        # providers / models style: map of subtables
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
            # nested deeper (e.g. providers.x.env or models.x.overrides)
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


def _provider_id(vendor_id: str, key_id: str) -> str:
    return f"{_MANAGED_PREFIX}{vendor_id}@{key_id}"


def _model_alias(provider_id: str, model: str) -> str:
    mid = (model or "").strip() or "default"
    return f"{provider_id}/{mid}"


def _is_kimi_like(vendor: dict) -> bool:
    prov = (vendor.get("provider") or "").lower().strip()
    url = (vendor.get("proxy_target") or vendor.get("api_url") or "").lower()
    if prov in _KIMI_PROVIDER_IDS:
        return True
    if "kimi.com/coding" in url or "api.kimi.com" in url:
        return True
    if "moonshot" in url or "moonshot.cn" in url or "moonshot.ai" in url:
        return True
    return False


def _provider_type(vendor: dict) -> str:
    ep = (vendor.get("endpoint_type") or "").lower().strip()
    if ep in ("anthropic", "claude"):
        return "anthropic"
    if _is_kimi_like(vendor):
        return "kimi"
    if ep in ("google", "gemini"):
        return "google-genai"
    return "openai"


def _base_url(vendor: dict) -> str:
    url = (vendor.get("proxy_target") or vendor.get("api_url") or "").rstrip("/")
    if not url and _is_kimi_like(vendor):
        return _KIMI_CODING_BASE
    # Claude Code style often wants base without path; Kimi docs want /v1 for openai/kimi
    return url


def _context_size(model: str, vendor: dict) -> int:
    m = (model or "").lower()
    if m in ("k3", "kimi-k3"):
        return 1_048_576
    if "highspeed" in m or "kimi-for-coding" in m or m.startswith("kimi"):
        return 262_144
    if _is_kimi_like(vendor):
        return 262_144
    return 128_000


def _list_models(key: dict, vendor: dict) -> list[str]:
    from core.data import get_enabled_models, list_model_ids
    ids = []
    try:
        ids = get_enabled_models(key) or list_model_ids(key) or []
    except Exception:
        ids = []
    ids = [str(m).strip() for m in ids if str(m).strip()]
    if ids:
        return ids
    if _is_kimi_like(vendor):
        return [m[0] for m in _DEFAULT_KIMI_MODELS]
    # generic fallback
    dm = (key.get("default_model") or "").strip()
    return [dm] if dm else ["default"]


class KimiCodeAdapter(BackendAdapter):
    name = "kimi-code"
    display_name = "Kimi Code"

    @property
    def _config_dir(self) -> Path:
        return _home_dir()

    @property
    def _config_path(self) -> Path:
        return self._config_dir / "config.toml"

    @property
    def _tui_path(self) -> Path:
        return self._config_dir / "tui.toml"

    def _load(self) -> dict:
        return _load_toml(self._config_path)

    def _save(self, data: dict) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        path = self._config_path
        # backup once
        try:
            if path.exists():
                bak = path.with_suffix(path.suffix + ".bak")
                if not bak.exists():
                    bak.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except Exception:
            pass
        text = _dump_toml(data)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _upsert_provider_models(self, data: dict, vendor: dict, key: dict) -> None:
        vid = str(vendor.get("id") or "")
        kid = str(key.get("id") or "")
        if not vid or not kid or not key.get("api_key"):
            return
        pid = _provider_id(vid, kid)
        ptype = _provider_type(vendor)
        base = _base_url(vendor)
        providers = data.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            data["providers"] = providers
        entry: dict[str, Any] = {
            "type": ptype,
            "api_key": key["api_key"],
        }
        if base:
            entry["base_url"] = base
        # env fallback keys per docs (stored in config, not shell)
        env_tbl: dict[str, str] = {}
        if ptype == "kimi":
            env_tbl["KIMI_API_KEY"] = key["api_key"]
            if base:
                env_tbl["KIMI_BASE_URL"] = base
        elif ptype == "anthropic":
            env_tbl["ANTHROPIC_API_KEY"] = key["api_key"]
            if base:
                env_tbl["ANTHROPIC_BASE_URL"] = base
        elif ptype == "openai":
            env_tbl["OPENAI_API_KEY"] = key["api_key"]
            if base:
                env_tbl["OPENAI_BASE_URL"] = base
        if env_tbl:
            entry["env"] = env_tbl
        providers[pid] = entry

        models = data.setdefault("models", {})
        if not isinstance(models, dict):
            models = {}
            data["models"] = models
        # remove previous models for this provider
        for mk in list(models.keys()):
            if str(mk).startswith(pid + "/") or (isinstance(models.get(mk), dict) and models[mk].get("provider") == pid):
                del models[mk]
        for mid in _list_models(key, vendor):
            alias = _model_alias(pid, mid)
            mentry: dict[str, Any] = {
                "provider": pid,
                "model": mid if mid != "default" else (key.get("default_model") or mid),
                "max_context_size": _context_size(mid, vendor),
                "display_name": f"{vendor.get('name') or vendor.get('provider') or 'AI'}: {key.get('name') or kid} / {mid}",
            }
            if ptype == "kimi":
                mentry["capabilities"] = ["thinking", "image_in", "video_in", "tool_use"]
                if mid == "k3":
                    mentry["support_efforts"] = ["low", "high", "max"]
                    mentry["default_effort"] = "high"
            models[alias] = mentry

        # set default_model if unset and this is kimi-like primary/first
        if not data.get("default_model"):
            prefer = None
            if _is_kimi_like(vendor):
                for cand in ("k3", "kimi-for-coding", "kimi-for-coding-highspeed"):
                    a = _model_alias(pid, cand)
                    if a in models:
                        prefer = a
                        break
            if not prefer:
                # first model of this key
                for mid in _list_models(key, vendor):
                    prefer = _model_alias(pid, mid)
                    break
            if prefer:
                data["default_model"] = prefer

        # mirror api_key into services for kimi coding search/fetch when coding URL
        if ptype == "kimi" and base and "kimi.com/coding" in base:
            services = data.setdefault("services", {})
            if not isinstance(services, dict):
                services = {}
                data["services"] = services
            services.setdefault("moonshot_search", {})
            services.setdefault("moonshot_fetch", {})
            if isinstance(services.get("moonshot_search"), dict):
                services["moonshot_search"]["base_url"] = "https://api.kimi.com/coding/v1/search"
                services["moonshot_search"]["api_key"] = key["api_key"]
            if isinstance(services.get("moonshot_fetch"), dict):
                services["moonshot_fetch"]["base_url"] = "https://api.kimi.com/coding/v1/fetch"
                services["moonshot_fetch"]["api_key"] = key["api_key"]

    def _remove_provider(self, data: dict, vendor_id: str, key_id: str) -> None:
        pid = _provider_id(str(vendor_id), str(key_id))
        providers = data.get("providers")
        if isinstance(providers, dict):
            providers.pop(pid, None)
        models = data.get("models")
        if isinstance(models, dict):
            for mk in list(models.keys()):
                mv = models.get(mk)
                if str(mk).startswith(pid + "/") or (isinstance(mv, dict) and mv.get("provider") == pid):
                    del models[mk]
        if data.get("default_model") and str(data.get("default_model")).startswith(pid):
            # pick another managed model if any
            new_default = ""
            if isinstance(models, dict):
                for mk in models:
                    if str(mk).startswith(_MANAGED_PREFIX):
                        new_default = str(mk)
                        break
            if new_default:
                data["default_model"] = new_default
            else:
                data.pop("default_model", None)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not self.should_sync(vendor, key):
            return
        if not key.get("api_key") or key.get("enabled") is False:
            return
        data = self._load()
        self._upsert_provider_models(data, vendor, key)
        self._save(data)

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled") is False or not key.get("api_key"):
            self.on_key_removed(vendor, key)
            return
        if not self.should_sync(vendor, key):
            self.on_key_removed(vendor, key)
            return
        self.on_key_added(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        data = self._load()
        self._remove_provider(data, vendor.get("id"), key.get("id"))
        self._save(data)

    def on_vendor_removed(self, vendor: dict) -> None:
        data = self._load()
        vid = str(vendor.get("id") or "")
        providers = data.get("providers") or {}
        if isinstance(providers, dict):
            for pid in list(providers.keys()):
                if str(pid).startswith(f"{_MANAGED_PREFIX}{vid}@"):
                    # parse key id
                    rest = str(pid)[len(_MANAGED_PREFIX):]
                    parts = rest.split("@", 1)
                    kid = parts[1] if len(parts) == 2 else ""
                    self._remove_provider(data, vid, kid)
        self._save(data)

    def reconcile(self) -> None:
        from core.data import get_vendors
        data = self._load()
        providers = data.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            data["providers"] = providers

        # desired managed set
        desired: dict[str, tuple[dict, dict]] = {}
        for v in get_vendors():
            for k in v.get("keys") or []:
                if not self.should_sync(v, k):
                    continue
                if not k.get("api_key") or k.get("enabled") is False:
                    continue
                pid = _provider_id(str(v.get("id")), str(k.get("id")))
                desired[pid] = (v, k)

        # remove stale managed providers
        for pid in list(providers.keys()):
            if not str(pid).startswith(_MANAGED_PREFIX):
                continue
            if pid not in desired:
                rest = str(pid)[len(_MANAGED_PREFIX):]
                parts = rest.split("@", 1)
                vid = parts[0] if parts else ""
                kid = parts[1] if len(parts) == 2 else ""
                self._remove_provider(data, vid, kid)

        # upsert all desired
        for pid, (v, k) in desired.items():
            self._upsert_provider_models(data, v, k)

        self._save(data)

    def sync_from_backend(self) -> list[dict]:
        data = self._load()
        providers = data.get("providers") or {}
        if not isinstance(providers, dict):
            return []
        out = []
        for name, p in providers.items():
            if not isinstance(p, dict):
                continue
            api_key = (p.get("api_key") or "").strip()
            if not api_key and isinstance(p.get("env"), dict):
                env = p["env"]
                api_key = (
                    env.get("KIMI_API_KEY")
                    or env.get("OPENAI_API_KEY")
                    or env.get("ANTHROPIC_API_KEY")
                    or ""
                ).strip()
            if not api_key:
                continue
            # skip pure oauth placeholders without key
            ptype = (p.get("type") or "openai").lower()
            base = (p.get("base_url") or "").rstrip("/")
            if ptype == "kimi":
                endpoint = "openai"
                provider = "kimi-code" if "kimi.com/coding" in base else "moonshot"
                if not base:
                    base = _KIMI_CODING_BASE
            elif ptype == "anthropic":
                endpoint = "anthropic"
                provider = "anthropic"
            else:
                endpoint = "openai"
                provider = str(name).replace("managed:", "").replace(_MANAGED_PREFIX, "aiswitch-")[:40]
            out.append({
                "name": f"Kimi Code: {name}",
                "provider": provider,
                "api_url": base,
                "endpoint_type": endpoint,
                "keys": [{"name": f"from {self.name}", "api_key": api_key}],
            })
        return out

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "config.toml", "type": "toml"},
            {"path": str(self._tui_path), "label": "tui.toml", "type": "toml"},
        ]

    def get_status(self) -> dict:
        from backends.base import detect_install, status_from_detect

        cfg_exists = self._config_path.exists()
        data = self._load() if cfg_exists else {}
        providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
        managed = sum(1 for k in providers if str(k).startswith(_MANAGED_PREFIX))
        total = len(providers or {})
        det = detect_install(
            cli_commands=("kimi", "kimi.exe", "kimi-code"),
            config_files=[self._config_path],
            data_dirs=[self._config_dir],
            treat_config_as_installed=True,
        )
        msg = f"{managed} managed / {total} provider(s)"
        if not cfg_exists:
            msg = "config.toml missing"
        elif "cli" not in (det.get("install_kinds") or []):
            msg += "; kimi CLI not found"
        return status_from_detect(
            det,
            not_installed_message="kimi CLI not found",
            message=msg,
            enabled=True,
            config_path=str(self._config_path),
        )

    def get_version(self) -> str:
        try:
            r = subprocess.run(["kimi", "--version"], capture_output=True, text=True, timeout=5)
            return (r.stdout or r.stderr or "").strip().splitlines()[0] if (r.stdout or r.stderr) else ""
        except Exception:
            return ""
