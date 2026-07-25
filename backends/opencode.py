import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from backends.base import BackendAdapter, home_config_dir, home_data_dir

log = logging.getLogger(__name__)

# Built-in provider IDs that OpenCode already knows (models.dev) — only need auth.json
_BUILTIN_PROVIDERS = {
    "openai", "anthropic", "google", "gemini", "deepseek", "openrouter",
    "groq", "xai", "mistral", "cohere", "together", "fireworks", "perplexity",
    "azure", "bedrock", "amazon-bedrock", "ollama", "deepinfra", "cerebras",
    "moonshot", "minimax", "nvidia", "huggingface", "vercel", "opencode",
    "github-copilot", "gitlab", "baseten", "helicone", "nebius", "venice",
    "zenmux", "zai", "zhipu",
}


class OpenCodeAdapter(BackendAdapter):
    """Sync vendors into OpenCode (auth.json + opencode.jsonc provider defs).

    OpenCode requires:
      1. data dir auth.json  →  { providerId: {type:"api", key:"..."} }
         macOS/Linux: ~/.local/share/opencode/auth.json
         Windows: %LOCALAPPDATA%\\opencode\\auth.json
      2. config dir opencode.jsonc for custom / proxy endpoints
         macOS/Linux: ~/.config/opencode/opencode.jsonc
         Windows: %APPDATA%\\opencode\\opencode.jsonc
    """

    name = "opencode"
    display_name = "OpenCode"
    MANAGED_TAG = "ai-switch"

    @property
    def _auth_path(self) -> Path:
        env = (os.environ.get("OPENCODE_DATA_DIR") or "").strip()
        if env:
            return Path(env).expanduser() / "auth.json"
        return home_data_dir("opencode") / "auth.json"

    @property
    def _config_dir(self) -> Path:
        env = (os.environ.get("OPENCODE_CONFIG_DIR") or "").strip()
        if env:
            return Path(env).expanduser()
        return home_config_dir("opencode")

    @property
    def _config_path(self) -> Path:
        """Always write to opencode.jsonc (OpenCode primary config)."""
        return self._config_dir / "opencode.jsonc"

    @property
    def _tui_config_path(self) -> Path:
        return self._config_dir / "tui.jsonc"

    def _config_candidates(self) -> list[Path]:
        d = self._config_dir
        return [
            d / "opencode.jsonc",
            d / "opencode.json",
            d / "opencode.jsonc.bak",
            d / "opencode.json.bak",
        ]

    # ── auth.json ──────────────────────────────────────────

    def _load_auth(self) -> dict:
        if self._auth_path.exists():
            try:
                with open(self._auth_path) as f:
                    return json.load(f)
            except Exception as e:
                log.warning("Failed to load opencode auth.json: %s", e)
        return {}

    def _save_auth(self, data: dict) -> None:
        self._auth_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._auth_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    @staticmethod
    def _auth_entry(api_key: str) -> dict:
        return {"type": "api", "key": api_key}

    @staticmethod
    def _extract_key(val) -> str:
        if isinstance(val, dict):
            return val.get("key") or val.get("apiKey") or val.get("token") or ""
        if isinstance(val, str):
            return val
        return ""

    # ── opencode.jsonc ─────────────────────────────────────

    @staticmethod
    def _parse_jsonish(raw: str) -> dict:
        # First try pure JSON (our writes never use comments)
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # jsonc: strip block comments + line comments NOT inside strings
        # Only treat // as comment when it starts a line (optional whitespace)
        no_block = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.S)
        no_line = re.sub(r"(?m)^\s*//.*?$", "", no_block)
        return json.loads(no_line)

    def _load_config(self) -> dict:
        """Load best available config (prefer most providers)."""
        best = None
        best_n = -1
        for path in self._config_candidates():
            if not path.exists() or path.stat().st_size < 3:
                continue
            try:
                data = self._parse_jsonish(path.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                log.debug("Skip unreadable %s: %s", path, e)
                continue
            if not isinstance(data, dict):
                continue
            n = len(data.get("provider") or {})
            if n > best_n:
                best = data
                best_n = n
        if best is None:
            return {"$schema": "https://opencode.ai/config.json", "provider": {}}
        best.setdefault("$schema", "https://opencode.ai/config.json")
        best.setdefault("provider", {})
        return best

    def _save_config(self, data: dict) -> None:
        path = self._config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data.setdefault("$schema", "https://opencode.ai/config.json")
        data.setdefault("provider", {})
        # Never write managed options._managed into a key OpenCode might reject —
        # keep it under options for our tracking (OpenCode ignores unknown option keys)
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        # Atomic write
        tmp = path.with_suffix(".jsonc.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        # Keep plain .json in sync for tools that read it
        plain = self._config_dir / "opencode.json"
        try:
            plain.write_text(text, encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _provider_id(vendor: dict) -> str:
        pid = (vendor.get("provider") or vendor.get("name") or "custom").strip()
        pid = re.sub(r"[^a-zA-Z0-9._-]+", "-", pid).strip("-").lower()
        return pid or "custom"

    @staticmethod
    def _npm_for_endpoint(endpoint_type: str) -> str:
        ep = (endpoint_type or "openai").lower()
        if ep == "anthropic":
            return "@ai-sdk/anthropic"
        if ep in ("google", "gemini"):
            return "@ai-sdk/google"
        return "@ai-sdk/openai-compatible"

    @staticmethod
    def _normalize_base_url(url: str, endpoint_type: str = "") -> str:
        url = (url or "").strip().rstrip("/")
        if not url:
            return url
        if re.search(r"/v\d+$", url):
            return url
        if any(x in url for x in ("/v1/", "/v2/", "/v3/", "/v4/")):
            return url
        if "/api/proxy" in url:
            return url if url.endswith("/v1") else url + "/v1"
        ep = (endpoint_type or "").lower()
        # Anthropic product roots (…/anthropic) need /v1 for AI SDK
        if ep == "anthropic" or url.endswith("/anthropic") or "/anthropic/" in url + "/":
            return url + "/v1"
        return url + "/v1"

    @staticmethod
    def _models_map(key: dict) -> dict:
        from core.data import get_enabled_models
        out = {}
        enabled = set(get_enabled_models(key))
        for m in key.get("models") or []:
            mid = m["id"] if isinstance(m, dict) else m
            if not mid:
                continue
            mid = str(mid)
            if enabled and mid not in enabled:
                continue
            name = m.get("name", mid) if isinstance(m, dict) else mid
            out[mid] = {"name": str(name)}
        if not out:
            for mid in get_enabled_models(key):
                out[str(mid)] = {"name": str(mid)}
        if not out and key.get("default_model"):
            dm = str(key["default_model"])
            if not enabled or dm in enabled:
                out[dm] = {"name": dm}
        return out

    def _is_custom(self, vendor: dict) -> bool:
        pid = self._provider_id(vendor)
        if pid not in _BUILTIN_PROVIDERS:
            return True
        api_url = (vendor.get("proxy_target") or vendor.get("api_url") or "").strip()
        if not api_url:
            return False
        defaults = {
            "openai": "api.openai.com",
            "anthropic": "api.anthropic.com",
            "deepseek": "api.deepseek.com",
            "openrouter": "openrouter.ai",
            "groq": "api.groq.com",
            "xai": "api.x.ai",
            "google": "generativelanguage.googleapis.com",
            "gemini": "generativelanguage.googleapis.com",
        }
        host = defaults.get(pid, "")
        if host and host in api_url and not vendor.get("proxy_target"):
            return False
        return True

    def _build_provider_block(self, vendor: dict, key: dict, existing: Optional[dict] = None) -> dict:
        api_url = vendor.get("proxy_target") or vendor.get("api_url") or ""
        ep = vendor.get("endpoint_type") or "openai"
        # Infer anthropic from URL when endpoint_type missing
        if (not ep or ep == "openai") and ("/anthropic" in (api_url or "").lower() or "api.anthropic.com" in (api_url or "").lower()):
            ep = "anthropic"
        base_url = self._normalize_base_url(api_url, ep)
        models = self._models_map(key)
        # Preserve existing models if new key has none
        if not models and existing:
            models = dict(existing.get("models") or {})
        block = {
            "npm": self._npm_for_endpoint(ep),
            "name": vendor.get("name") or self._provider_id(vendor),
            "options": {
                "baseURL": base_url,
                "_managed": self.MANAGED_TAG,
            },
            "models": models,
        }
        if key.get("api_key"):
            block["options"]["apiKey"] = key["api_key"]
        # Prefer non-empty baseURL from existing if new empty
        if not base_url and existing:
            prev = (existing.get("options") or {}).get("baseURL")
            if prev:
                block["options"]["baseURL"] = prev
        return block

    def _pick_best_key(self, vendor: dict) -> Optional[dict]:
        """Prefer healthy, enabled key that has models list."""
        from core.data import get_enabled_models
        from core.health_checker import is_key_backend_syncable
        best = None
        best_score = -1
        for k in vendor.get("keys", []):
            if not k.get("enabled", True) or not k.get("api_key"):
                continue
            if not is_key_backend_syncable(vendor.get("id") or "", k):
                continue
            score = 0
            models = get_enabled_models(k)
            score += len(models) * 10
            if k.get("default_model") and k.get("default_model") in set(models or [k.get("default_model")]):
                score += 5
            # Prefer non-imported names
            name = (k.get("name") or "").lower()
            if name.startswith("from "):
                score -= 3
            if score > best_score:
                best_score = score
                best = k
        return best

    # ── lifecycle ──────────────────────────────────────────

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        pid = self._provider_id(vendor)

        auth = self._load_auth()
        auth[pid] = self._auth_entry(key["api_key"])
        self._save_auth(auth)

        # Always write provider block for custom endpoints; for built-ins only if models/proxy
        if self._is_custom(vendor) or key.get("models") or vendor.get("proxy_target"):
            cfg = self._load_config()
            cfg.setdefault("provider", {})
            existing = cfg["provider"].get(pid) or {}
            cfg["provider"][pid] = self._build_provider_block(vendor, key, existing)
            self._save_config(cfg)
            log.info("OpenCode: provider '%s' + auth synced", pid)
        else:
            log.info("OpenCode: auth for built-in '%s'", pid)

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled", True) and key.get("api_key"):
            self.on_key_added(vendor, key)
        else:
            self.on_key_removed(vendor, key)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        pid = self._provider_id(vendor)
        from core.data import get_vendors

        for v in get_vendors():
            if self._provider_id(v) != pid:
                continue
            other = self._pick_best_key(v)
            if other and other.get("id") != key.get("id"):
                self.on_key_added(v, other)
                return

        auth = self._load_auth()
        if pid in auth:
            del auth[pid]
            self._save_auth(auth)

        cfg = self._load_config()
        entry = (cfg.get("provider") or {}).get(pid)
        if entry and (entry.get("options") or {}).get("_managed") == self.MANAGED_TAG:
            del cfg["provider"][pid]
            self._save_config(cfg)
            log.info("OpenCode: removed managed provider '%s'", pid)

    def reconcile(self) -> None:
        """Rebuild managed providers from the best active key per provider."""
        from core.data import get_backend_config, get_enabled_models, get_vendors

        if get_backend_config(self.name).get("disabled"):
            return

        auth = self._load_auth()
        cfg = self._load_config()
        cfg.setdefault("provider", {})

        # Group vendors by provider id, pick best key
        desired: dict[str, tuple[dict, dict]] = {}
        for v in get_vendors():
            pid = self._provider_id(v)
            k = self._pick_best_key(v)
            if not k or not self.should_sync(v, k):
                continue
            # Keys with inventory but no enabled models stay system-only
            if (k.get("models") or k.get("disabled_models")) and not get_enabled_models(k):
                continue
            prev = desired.get(pid)
            if not prev:
                desired[pid] = (v, k)
                continue
            # Prefer vendor with more enabled models / real api_url
            prev_score = len(get_enabled_models(prev[1]))
            cur_score = len(get_enabled_models(k))
            if cur_score > prev_score:
                desired[pid] = (v, k)
            elif cur_score == prev_score and (v.get("api_url") or "") and not (prev[0].get("api_url") or ""):
                desired[pid] = (v, k)

        new_auth = {k: v for k, v in auth.items()
                    if isinstance(v, dict) and v.get("type") in ("oauth", "token")}

        for pid, (v, k) in desired.items():
            new_auth[pid] = self._auth_entry(k["api_key"])
            if self._is_custom(v) or k.get("models") or v.get("proxy_target") or v.get("api_url"):
                existing = cfg["provider"].get(pid) or {}
                # Only overwrite managed or missing entries
                if not existing or (existing.get("options") or {}).get("_managed") == self.MANAGED_TAG:
                    cfg["provider"][pid] = self._build_provider_block(v, k, existing)
                else:
                    # User-owned provider: only refresh apiKey in auth, leave config
                    pass

        # Drop managed providers no longer desired
        for pid, entry in list(cfg.get("provider", {}).items()):
            if (entry.get("options") or {}).get("_managed") == self.MANAGED_TAG and pid not in desired:
                del cfg["provider"][pid]

        self._save_auth(new_auth)
        self._save_config(cfg)
        log.info("OpenCode reconcile: %d provider(s), %d credential(s)",
                 len(cfg.get("provider") or {}), len(new_auth))

    def sync_from_backend(self) -> list[dict]:
        auth = self._load_auth()
        cfg = self._load_config()
        providers_cfg = cfg.get("provider") or {}
        vendors = []
        for pid, val in auth.items():
            api_key = self._extract_key(val)
            if not api_key:
                continue
            pcfg = providers_cfg.get(pid) or {}
            opts = pcfg.get("options") or {}
            base = opts.get("baseURL") or opts.get("baseUrl") or ""
            models = list((pcfg.get("models") or {}).keys())
            vendors.append({
                "name": pcfg.get("name") or pid.replace("-", " ").title(),
                "provider": pid,
                "api_url": base,
                "endpoint_type": "anthropic" if "anthropic" in (pcfg.get("npm") or "") else "openai",
                "keys": [{
                    "name": f"from {self.name}",
                    "api_key": api_key,
                    "models": models,
                }],
            })
        return vendors

    def get_status(self) -> dict:
        from backends.base import detect_install, status_from_detect

        # OpenCode is primarily a CLI (TUI); config dirs support install evidence
        det = detect_install(
            cli_commands=("opencode", "opencode.exe"),
            process_markers=("opencode", "opencode.exe"),
            data_dirs=[self._auth_path.parent],
            config_files=[self._auth_path, self._config_path],
            treat_config_as_installed=True,
        )
        auth = self._load_auth()
        n_auth = sum(1 for v in auth.values() if self._extract_key(v))
        try:
            cfg = self._load_config()
            n_prov = len(cfg.get("provider") or {})
        except Exception:
            n_prov = 0
        msg = f"{n_auth} credential(s), {n_prov} provider(s) in config"
        return status_from_detect(
            det,
            not_installed_message="opencode CLI not found",
            message=msg,
        )

    def get_version(self) -> str:
        try:
            r = subprocess.run(
                ["opencode", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return (r.stdout + r.stderr).strip().split("\n")[0][:60]
        except Exception:
            return ""

    def get_config_template(self) -> list[dict]:
        return [
            {"key": "auth_path", "label": "Auth File", "type": "text",
             "default": str(self._auth_path), "help": "Path to auth.json"},
            {"key": "config_path", "label": "Config File", "type": "text",
             "default": str(self._config_path), "help": "opencode.jsonc provider definitions"},
        ]

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(self._config_path), "label": "Global Config", "type": "jsonc"},
            {"path": str(self._tui_config_path), "label": "TUI Config", "type": "jsonc"},
            {"path": str(self._auth_path), "label": "Auth File", "type": "json"},
        ]
