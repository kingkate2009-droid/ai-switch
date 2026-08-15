"""Devin CLI backend adapter.

Docs: https://docs.devin.ai/cli
Config:  ~/.config/devin/config.json  (Windows: %APPDATA%\\devin\\config.json)
Auth:    ~/.local/share/devin/credentials.toml  (or $XDG_DATA_HOME/devin/...)
         Also accepts WINDSURF_API_KEY for ACP mode.
"""

import json
import logging
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter

log = logging.getLogger(__name__)

# Vendors/providers that map to Devin CLI auth token
_DEVIN_PROVIDERS = {
    "devin", "cognition", "windsurf", "cascade", "devin-cli",
}


class DevinAdapter(BackendAdapter):
    name = "devin"
    display_name = "Devin CLI"

    @property
    def _config_dir(self) -> Path:
        from backends.base import home_config_dir
        return home_config_dir("devin")

    @property
    def _data_dir(self) -> Path:
        from backends.base import home_data_dir, is_windows
        # Windows historically used Roaming for both; keep Local data dir for new writes
        if is_windows():
            return home_data_dir("devin")
        return home_data_dir("devin")

    @property
    def _config_path(self) -> Path:
        return self._config_dir / "config.json"

    @property
    def _credentials_path(self) -> Path:
        return self._data_dir / "credentials.toml"

    # ── config.json ────────────────────────────────────────

    def _load_config(self) -> dict:
        path = self._config_path
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        raw = re.sub(r"(?m)^\s*//.*?$", "", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("Failed to parse Devin config.json: %s", e)
            return {}

    def _save_config(self, data: dict) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        path = self._config_path
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    # ── credentials.toml ───────────────────────────────────

    def _load_credentials(self) -> dict:
        """Parse simple TOML key=value / [section] into a flat dict + sections."""
        path = self._credentials_path
        if not path.exists():
            return {}
        result = {"_raw_lines": [], "_values": {}}
        section = ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return result
        for line in text.splitlines():
            result["_raw_lines"].append(line)
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = re.match(r"^\[([^\]]+)\]$", s)
            if m:
                section = m.group(1).strip()
                continue
            if "=" in s:
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip("\"'")
                key = f"{section}.{k}" if section else k
                result["_values"][key] = v
                result["_values"][k] = v  # also bare key
        return result

    def _save_token(self, token: str) -> None:
        """Write Devin auth token to credentials.toml.

        Format is intentionally simple and matches manual-token-flow storage
        (single token field). We preserve unknown keys when possible.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        existing = self._load_credentials()
        values = dict(existing.get("_values") or {})
        # Primary fields used by CLI / ACP
        values["api_token"] = token
        values["token"] = token
        values["api_key"] = token

        lines = [
            "# Managed by ai-switch — Devin CLI credentials",
            "# Docs: https://docs.devin.ai/cli/enterprise/devin-auth",
            f'api_token = "{token}"',
            f'token = "{token}"',
            "",
        ]
        # Keep any other non-token keys from previous file
        skip = {"api_token", "token", "api_key"}
        for k, v in values.items():
            if k in skip or "." in k:
                continue
            if not re.match(r"^[A-Za-z0-9_]+$", k):
                continue
            if v == token:
                continue
            lines.append(f'{k} = "{v}"')
        self._credentials_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        # ACP path also accepts env var
        try:
            os.environ["WINDSURF_API_KEY"] = token
        except Exception:
            pass

    def _clear_credentials(self) -> None:
        path = self._credentials_path
        if path.exists():
            try:
                path.unlink()
            except Exception as e:
                log.warning("Failed to remove Devin credentials: %s", e)
        os.environ.pop("WINDSURF_API_KEY", None)

    def _get_stored_token(self) -> str:
        creds = self._load_credentials()
        vals = creds.get("_values") or {}
        for k in ("api_token", "token", "api_key", "access_token"):
            if vals.get(k):
                return vals[k]
        return ""

    # ── vendor matching ────────────────────────────────────

    @staticmethod
    def _is_devin_vendor(vendor: dict) -> bool:
        prov = (vendor.get("provider") or "").lower().strip()
        name = (vendor.get("name") or "").lower()
        if prov in _DEVIN_PROVIDERS:
            return True
        if any(x in name for x in ("devin", "windsurf", "cognition")):
            return True
        return False

    def _find_active_key(self, *, exclude: tuple[str, str] = None) -> Optional[tuple]:
        """Return (vendor, key) using the common primary/backup policy."""
        from core.data import get_vendors
        candidates = []
        for vendor_index, v in enumerate(get_vendors()):
            if not self._is_devin_vendor(v):
                continue
            for key_index, k in enumerate(v.get("keys") or []):
                if not k.get("enabled", True) or not k.get("api_key"):
                    continue
                if exclude and str(v.get("id") or "") == str(exclude[0]) and str(k.get("id") or "") == str(exclude[1]):
                    continue
                if not self.should_sync(v, k):
                    continue
                role = str(k.get("role") or "").strip().lower()
                rank = {"primary": 0, "backup": 1}.get(role, 2)
                candidates.append((rank, vendor_index, key_index, v, k))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[:3])
        return candidates[0][3], candidates[0][4]

    def _apply_model(self, key: dict) -> None:
        model = key.get("default_model") or ""
        if not model and key.get("models"):
            m0 = key["models"][0]
            model = m0["id"] if isinstance(m0, dict) else m0
        if not model:
            return
        cfg = self._load_config()
        cfg.setdefault("agent", {})
        cfg["agent"]["model"] = model
        self._save_config(cfg)

    # ── lifecycle ──────────────────────────────────────────

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        if not self._is_devin_vendor(vendor):
            return
        self._save_token(key["api_key"])
        self._apply_model(key)
        log.info("Devin CLI: credentials + model synced from %s/%s",
                 vendor.get("provider"), key.get("name"))

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled", True) and key.get("api_key") and self._is_devin_vendor(vendor):
            self.on_key_added(vendor, key)
        else:
            self.on_key_removed(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        if not self._is_devin_vendor(vendor):
            return
        other = self._find_active_key(
            exclude=(str(vendor.get("id") or ""), str(key.get("id") or "")),
        )
        if other:
            self.on_key_added(other[0], other[1])
            return
        self._clear_credentials()
        log.info("Devin CLI: credentials cleared")

    def reconcile(self) -> None:
        pair = self._find_active_key()
        if pair:
            self.on_key_added(pair[0], pair[1])
        else:
            # Do not wipe credentials if user logged in via `devin auth login`
            # unless we previously wrote managed tokens that no longer exist.
            # Only clear if file has our managed header and no matching vendor.
            path = self._credentials_path
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if "Managed by ai-switch" in text:
                        self._clear_credentials()
                except Exception:
                    pass

    def sync_from_backend(self) -> list[dict]:
        token = self._get_stored_token()
        if not token:
            token = os.environ.get("WINDSURF_API_KEY", "")
        if not token:
            return []
        cfg = self._load_config()
        model = (cfg.get("agent") or {}).get("model", "")
        return [{
            "name": "Devin",
            "provider": "devin",
            "api_url": "https://api.devin.ai",
            "endpoint_type": "openai",
            "keys": [{
                "name": f"from {self.name}",
                "api_key": token,
                "default_model": model,
                "models": [model] if model else [],
            }],
        }]

    def get_status(self) -> dict:
        version = self.get_version()
        token = self._get_stored_token() or os.environ.get("WINDSURF_API_KEY", "")
        has_token = bool(token)
        cfg = self._load_config()
        model = (cfg.get("agent") or {}).get("model", "")
        # Try CLI auth status if available
        auth_msg = ""
        try:
            r = subprocess.run(
                ["devin", "auth", "status"],
                capture_output=True, text=True, timeout=8,
            )
            auth_msg = (r.stdout + r.stderr).strip().split("\n")[0][:120]
        except FileNotFoundError:
            auth_msg = "devin CLI not found"
        except Exception:
            pass

        parts = []
        if has_token:
            parts.append("token configured")
        else:
            parts.append("not authenticated")
        if model:
            parts.append(f"model={model}")
        if auth_msg and "not found" not in auth_msg.lower():
            parts.append(auth_msg)
        elif "not found" in auth_msg.lower():
            parts.append(auth_msg)

        from backends.base import detect_install, status_from_detect

        det = detect_install(
            cli_commands=("devin", "devin.exe"),
            config_files=[self._config_path, self._credentials_path],
            data_dirs=[self._config_dir, self._data_dir],
            treat_config_as_installed=True,
        )
        if version and not det.get("version"):
            det["version"] = version
        return status_from_detect(
            det,
            not_installed_message="; ".join(parts) or "devin CLI not found",
            message="; ".join(parts) if parts else "Installed",
        )

    def get_version(self) -> str:
        for cmd in (["devin", "version"], ["devin", "--version"]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                out = (r.stdout + r.stderr).strip()
                if out:
                    return out.split("\n")[0][:80]
            except FileNotFoundError:
                return ""
            except Exception:
                continue
        return ""

    def get_config_template(self) -> list[dict]:
        return [
            {
                "key": "config_path",
                "label": "Config File",
                "type": "text",
                "default": str(self._config_path),
                "help": "Devin CLI user config (~/.config/devin/config.json)",
            },
            {
                "key": "credentials_path",
                "label": "Credentials",
                "type": "text",
                "default": str(self._credentials_path),
                "help": "Auth token file (from devin auth login)",
            },
        ]

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "User Config", "type": "json"},
            {"path": str(self._credentials_path), "label": "Credentials", "type": "toml"},
        ]
