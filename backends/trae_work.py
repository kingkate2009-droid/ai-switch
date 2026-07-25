"""TRAE Work / TRAE IDE backend adapter (macOS + Windows + Linux).

Docs: https://docs.trae.ai/ide/models

Custom models are primarily configured in Settings > Models (UI).
This adapter:
  1) Detects whether TRAE / TRAE Work is installed and running (cross-platform)
  2) Maintains a managed custom-model list for OpenAI / Anthropic compatible endpoints
  3) Writes into TRAE app data dirs when present, plus a stable home fallback

Managed file shape (JSON):
{
  "_managed": "ai-switch",
  "version": 1,
  "models": [
    {
      "id": "aiswitch-12-1",
      "name": "OpenAI / key1",
      "apiFormat": "openai",
      "baseUrl": "https://api.openai.com/v1",
      "apiKey": "sk-...",
      "modelId": "gpt-4o",
      "enabled": true
    }
  ]
}
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter

_MANAGED = "ai-switch"
_MANAGED_PREFIX = "aiswitch-"

# Product display / folder names used by TRAE builds
_PRODUCT_NAMES = (
    "Trae",
    "Trae Work",
    "TraeWork",
    "TRAE",
    "TRAE Work",
    "Trae CN",
    "TraeCN",
    "trae",
    "trae-work",
)


def _is_windows() -> bool:
    return platform.system() == "Windows" or os.name == "nt"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _env_path(*keys: str) -> Optional[Path]:
    for k in keys:
        v = (os.environ.get(k) or "").strip()
        if v:
            return Path(v)
    return None


def _support_roots() -> list[Path]:
    """Candidate TRAE / TRAE Work application data roots (macOS / Windows / Linux)."""
    home = Path.home()
    roots: list[Path] = []

    if _is_macos():
        base = home / "Library" / "Application Support"
        roots.extend(base / n for n in _PRODUCT_NAMES)
        roots.append(base / "ByteDance" / "Trae")
        roots.append(base / "ByteDance" / "Trae Work")
    elif _is_windows():
        roaming = _env_path("APPDATA") or (home / "AppData" / "Roaming")
        local = _env_path("LOCALAPPDATA") or (home / "AppData" / "Local")
        for base in (roaming, local):
            roots.extend(base / n for n in _PRODUCT_NAMES)
            roots.append(base / "ByteDance" / "Trae")
            roots.append(base / "ByteDance" / "Trae Work")
            # VS Code-style user data dirs
            roots.append(base / "Trae" / "User")
            roots.append(base / "Trae Work" / "User")
    else:
        xdg_config = _env_path("XDG_CONFIG_HOME") or (home / ".config")
        xdg_data = _env_path("XDG_DATA_HOME") or (home / ".local" / "share")
        for base in (xdg_config, xdg_data, home):
            roots.extend(base / n for n in _PRODUCT_NAMES)

    # Stable fallback used by AI Switch (all platforms)
    roots.append(home / ".trae-work")
    if _is_windows():
        # Also keep a Windows-style roaming fallback
        roaming = _env_path("APPDATA") or (home / "AppData" / "Roaming")
        roots.append(roaming / "ai-switch" / "trae-work")

    # dedupe preserving order
    out, seen = [], set()
    for p in roots:
        try:
            key = str(p.resolve()) if p.exists() else str(p)
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _app_candidates() -> list[Path]:
    """Installed application / binary candidates per OS."""
    apps: list[Path] = []
    home = Path.home()

    if _is_macos():
        for name in (
            "Trae.app",
            "TRAE.app",
            "Trae Work.app",
            "TRAE Work.app",
            "Trae CN.app",
        ):
            apps.append(Path("/Applications") / name)
            apps.append(home / "Applications" / name)
        return apps

    if _is_windows():
        pf = _env_path("ProgramFiles") or Path(r"C:\Program Files")
        pf86 = _env_path("ProgramFiles(x86)") or Path(r"C:\Program Files (x86)")
        local = _env_path("LOCALAPPDATA") or (home / "AppData" / "Local")
        # Installer / portable layouts
        for base in (pf, pf86, local, local / "Programs"):
            for folder in (
                "Trae",
                "TRAE",
                "Trae Work",
                "TRAE Work",
                "TraeCN",
                "Trae CN",
            ):
                apps.append(base / folder / "Trae.exe")
                apps.append(base / folder / "TRAE.exe")
                apps.append(base / folder / "Trae Work.exe")
                apps.append(base / folder / "TRAE Work.exe")
                apps.append(base / folder / "trae.exe")
            apps.append(base / "ByteDance" / "Trae" / "Trae.exe")
            apps.append(base / "ByteDance" / "Trae Work" / "Trae Work.exe")
        return apps

    # Linux desktop entries / opt
    for p in (
        Path("/opt/Trae/trae"),
        Path("/opt/trae/trae"),
        Path("/usr/bin/trae"),
        Path("/usr/local/bin/trae"),
        home / ".local" / "bin" / "trae",
    ):
        apps.append(p)
    return apps


def _process_markers() -> tuple[str, ...]:
    if _is_windows():
        return (
            "trae.exe",
            "trae work.exe",
            "traework.exe",
            "\\trae\\",
            "\\trae work\\",
            "trae work",
            "trae.cn",
        )
    if _is_macos():
        return (
            "Trae Work.app",
            "Trae.app/",
            "TRAE Work.app",
            "TRAE.app/",
            "/Applications/Trae",
            "/Applications/TRAE",
        )
    return (
        "/opt/trae",
        "/opt/Trae",
        "trae-work",
        "trae --",
    )


def _normalize_base_url(url: str, api_format: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        if api_format == "anthropic":
            return "https://api.anthropic.com"
        return "https://api.openai.com/v1"
    for suffix in ("/chat/completions", "/messages", "/v1/messages"):
        if u.endswith(suffix):
            u = u[: -len(suffix)].rstrip("/")
    return u


def _api_format(vendor: dict) -> str:
    ep = (vendor.get("endpoint_type") or "").lower().strip()
    prov = (vendor.get("provider") or "").lower().strip()
    if ep in ("anthropic",) or prov in ("anthropic", "claude"):
        return "anthropic"
    return "openai"


def _model_id_for(vendor: dict, key: dict) -> str:
    for mid in (
        (key.get("default_model") or "").strip(),
        (key.get("check_model") or "").strip(),
    ):
        if mid:
            return mid
    for field in ("enabled_models", "models", "available_models"):
        raw = key.get(field) or []
        if isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, dict):
                s = (first.get("id") or first.get("name") or "").strip()
            else:
                s = str(first or "").strip()
            if s:
                return s
    fmt = _api_format(vendor)
    return "claude-sonnet-4-0" if fmt == "anthropic" else "gpt-4o"


def _is_fallback_root(root: Path) -> bool:
    name = root.name.lower()
    s = str(root).replace("\\", "/").lower()
    return name in (".trae-work", "trae-work") and (
        s.endswith("/.trae-work")
        or s.endswith("/trae-work")
        or "/ai-switch/trae-work" in s
    )


class TraeWorkAdapter(BackendAdapter):
    name = "trae-work"
    display_name = "TRAE Work"

    def _existing_roots(self) -> list[Path]:
        return [p for p in _support_roots() if p.exists()]

    def _primary_root(self) -> Path:
        existing = self._existing_roots()
        if existing:
            for p in existing:
                if not _is_fallback_root(p):
                    return p
            return existing[0]
        if _is_windows():
            roaming = _env_path("APPDATA") or (Path.home() / "AppData" / "Roaming")
            return roaming / "ai-switch" / "trae-work"
        return Path.home() / ".trae-work"

    def _models_paths(self) -> list[Path]:
        """All write targets for managed model lists (product dirs + fallback)."""
        paths: list[Path] = []
        roots = self._existing_roots() or [self._primary_root()]
        for root in roots:
            # If root itself is .../User, write beside globalStorage
            if root.name == "User":
                paths.append(root / "globalStorage" / "ai-switch.trae-work" / "models.json")
                paths.append(root.parent / "ai-switch-models.json")
            else:
                paths.append(root / "ai-switch-models.json")
                user = root / "User"
                if user.exists() or not _is_fallback_root(root):
                    paths.append(user / "globalStorage" / "ai-switch.trae-work" / "models.json")
                    paths.append(user / "settings.json")  # listed in config_files only when exists
        # stable fallbacks
        paths.append(Path.home() / ".trae-work" / "ai-switch-models.json")
        if _is_windows():
            roaming = _env_path("APPDATA") or (Path.home() / "AppData" / "Roaming")
            paths.append(roaming / "ai-switch" / "trae-work" / "ai-switch-models.json")

        out, seen = [], set()
        for p in paths:
            # do not write into settings.json as models dump
            if p.name == "settings.json":
                continue
            key = str(p)
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def _load_models(self) -> list[dict]:
        for p in self._models_paths():
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                models = data.get("models") if isinstance(data, dict) else None
                if isinstance(models, list):
                    return models
            except Exception:
                continue
        return []

    def _save_models(self, models: list[dict]) -> None:
        payload = {
            "_managed": _MANAGED,
            "version": 1,
            "models": models,
            "note": (
                "Managed by AI Switch. In TRAE Work / TRAE IDE: "
                "Settings > Models > Add custom model "
                "(apiFormat / baseUrl / modelId / apiKey)."
            ),
            "platforms": {
                "macos_support": "~/Library/Application Support/Trae*",
                "windows_support": "%APPDATA%\\Trae*  or  %LOCALAPPDATA%\\Trae*",
                "fallback": "~/.trae-work/ai-switch-models.json",
            },
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        for p in self._models_paths():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text + "\n", encoding="utf-8")
            except OSError:
                continue

    def _entry_id(self, vendor: dict, key: dict) -> str:
        return f"{_MANAGED_PREFIX}{vendor.get('id')}-{key.get('id')}"

    def _build_entry(self, vendor: dict, key: dict) -> Optional[dict]:
        if not key.get("api_key") or key.get("enabled") is False:
            return None
        if not self.should_sync(vendor, key):
            return None
        fmt = _api_format(vendor)
        base = _normalize_base_url(vendor.get("proxy_target") or vendor.get("api_url") or "", fmt)
        name = f"{vendor.get('name') or vendor.get('provider') or 'Custom'} / {key.get('name') or key.get('id')}"
        return {
            "id": self._entry_id(vendor, key),
            "name": name,
            "apiFormat": fmt,
            "baseUrl": base,
            "apiKey": key["api_key"],
            "modelId": _model_id_for(vendor, key),
            "enabled": True,
            "vendor_id": str(vendor.get("id") or ""),
            "key_id": str(key.get("id") or ""),
        }

    def on_key_added(self, vendor: dict, key: dict) -> None:
        entry = self._build_entry(vendor, key)
        if not entry:
            return
        models = self._load_models()
        models = [m for m in models if m.get("id") != entry["id"]]
        models.append(entry)
        self._save_models(models)

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled") is False or not key.get("api_key"):
            self.on_key_removed(vendor, key)
            return
        self.on_key_added(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        eid = self._entry_id(vendor, key)
        models = [m for m in self._load_models() if m.get("id") != eid]
        self._save_models(models)

    def on_vendor_removed(self, vendor: dict) -> None:
        prefix = f"{_MANAGED_PREFIX}{vendor.get('id')}-"
        models = [m for m in self._load_models() if not str(m.get("id") or "").startswith(prefix)]
        self._save_models(models)

    def reconcile(self) -> None:
        from core.data import get_vendors, get_backend_config

        if get_backend_config(self.name).get("disabled"):
            return
        if not self.is_installed():
            return

        wanted: dict[str, dict] = {}
        for v in get_vendors():
            for k in v.get("keys") or []:
                entry = self._build_entry(v, k)
                if entry:
                    wanted[entry["id"]] = entry

        existing = self._load_models()
        kept = [m for m in existing if not str(m.get("id") or "").startswith(_MANAGED_PREFIX)]
        models = kept + list(wanted.values())
        self._save_models(models)

    def sync_from_backend(self) -> list[dict]:
        vendors = []
        for m in self._load_models():
            api_key = m.get("apiKey") or m.get("api_key") or ""
            if not api_key:
                continue
            fmt = (m.get("apiFormat") or m.get("api_format") or "openai").lower()
            vendors.append({
                "name": m.get("name") or m.get("modelId") or "TRAE custom",
                "provider": "anthropic" if fmt == "anthropic" else "openai",
                "api_url": m.get("baseUrl") or m.get("base_url") or "",
                "endpoint_type": "anthropic" if fmt == "anthropic" else "openai",
                "keys": [{
                    "name": f"from {self.name}",
                    "api_key": api_key,
                    "default_model": m.get("modelId") or m.get("model_id") or "",
                }],
            })
        return vendors

    def get_status(self) -> dict:
        from backends.base import detect_install, status_from_detect

        # TRAE Work / TRAE IDE: primarily desktop client; optional CLI
        data_dirs = [p for p in _support_roots() if not _is_fallback_root(p)]
        det = detect_install(
            cli_commands=("trae", "trae-work", "trae-cli", "Trae", "TRAE"),
            app_paths=_app_candidates(),
            process_markers=_process_markers(),
            data_dirs=data_dirs,
            treat_config_as_installed=False,
        )
        models = self._load_models()
        managed = sum(1 for m in models if str(m.get("id") or "").startswith(_MANAGED_PREFIX))
        msg = f"{managed} managed model(s)"
        if not det.get("running"):
            msg = msg + "; open Settings > Models to import custom endpoints if needed"
        paths = self._models_paths()
        return status_from_detect(
            det,
            not_installed_message="TRAE Work / TRAE IDE not installed (app/CLI)",
            message=msg,
            models_path=str(paths[0]) if paths else "",
            platform=platform.system(),
        )

    @property
    def config_files(self) -> list[dict]:
        files = []
        seen = set()
        for p in self._models_paths()[:6]:
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            files.append({"path": key, "label": "AI Switch models", "type": "json"})
        for root in self._existing_roots()[:3]:
            if _is_fallback_root(root):
                continue
            settings = root / "User" / "settings.json" if root.name != "User" else root / "settings.json"
            key = str(settings)
            if key not in seen and (settings.exists() or settings.parent.exists()):
                seen.add(key)
                files.append({"path": key, "label": "User settings", "type": "json"})
        if not files:
            fb = Path.home() / ".trae-work" / "ai-switch-models.json"
            if _is_windows():
                roaming = _env_path("APPDATA") or (Path.home() / "AppData" / "Roaming")
                fb = roaming / "ai-switch" / "trae-work" / "ai-switch-models.json"
            files.append({"path": str(fb), "label": "AI Switch models", "type": "json"})
        return files
