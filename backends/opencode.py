import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

import requests

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

    def __init__(self):
        self._last_runtime_apply = {
            "runtime_applied": False,
            "runtime_message": "New OpenCode sessions will use the updated config",
        }

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
        fd, tmp_name = tempfile.mkstemp(prefix="auth.", suffix=".tmp", dir=str(self._auth_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._auth_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

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
            plain_tmp = plain.with_suffix(".json.tmp")
            plain_tmp.write_text(text, encoding="utf-8")
            plain_tmp.replace(plain)
        except Exception:
            pass

    def _apply_running_server(self, auth: dict, config: dict) -> dict:
        """Apply config to a known OpenCode HTTP server and verify it."""
        configured = str(os.environ.get("OPENCODE_SERVER_URL") or "").strip().rstrip("/")
        urls = [configured] if configured else ["http://127.0.0.1:4096"]
        username = str(os.environ.get("OPENCODE_SERVER_USERNAME") or "opencode")
        password = str(os.environ.get("OPENCODE_SERVER_PASSWORD") or "")
        basic_auth = (username, password) if password else None
        last_error = ""
        for base_url in urls:
            try:
                health = requests.get(f"{base_url}/global/health", auth=basic_auth, timeout=1.5)
                if health.status_code != 200 or not (health.json() or {}).get("healthy"):
                    continue
                for provider_id, credentials in auth.items():
                    response = requests.put(
                        f"{base_url}/auth/{quote(str(provider_id), safe='')}",
                        json=credentials,
                        auth=basic_auth,
                        timeout=3,
                    )
                    response.raise_for_status()
                response = requests.patch(
                    f"{base_url}/config",
                    json=config,
                    auth=basic_auth,
                    timeout=5,
                )
                response.raise_for_status()
                verify = requests.get(f"{base_url}/config", auth=basic_auth, timeout=3)
                verify.raise_for_status()
                actual = verify.json() or {}
                expected_ids = set((config.get("provider") or {}).keys())
                actual_ids = set((actual.get("provider") or {}).keys())
                if not expected_ids.issubset(actual_ids):
                    raise RuntimeError("OpenCode runtime config verification failed")
                return {
                    "runtime_applied": True,
                    "runtime_message": f"Applied to running OpenCode server at {base_url}",
                }
            except Exception as exc:
                last_error = str(exc)[:200]
        message = "New OpenCode sessions will use the updated config"
        if configured and last_error:
            message += f"; runtime apply failed: {last_error}"
        return {"runtime_applied": False, "runtime_message": message}

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

    @staticmethod
    def _is_opencode_zen(vendor: dict) -> bool:
        """OpenCode Zen / built-in opencode provider — models come from models.dev.

        Writing a managed provider.opencode.models map overrides the catalog and
        hides free models (big-pickle, *-free, etc.). Auth-only is correct.
        """
        raw = (vendor.get("provider") or vendor.get("name") or "").strip().lower()
        pid = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-")
        api_url = (vendor.get("proxy_target") or vendor.get("api_url") or "").strip().lower()
        # Exact / common aliases
        if pid in ("opencode", "zen", "opencode-zen", "opencode-zen-free", "zen-free"):
            if not api_url or "opencode.ai" in api_url:
                return True
        # Name contains both tokens (e.g. "OpenCode Zen Free", "opencode-2")
        if ("opencode" in pid or "opencode" in raw) and (
            "zen" in pid or "zen" in raw or not api_url or "opencode.ai" in api_url
        ):
            if not api_url or "opencode.ai" in api_url:
                return True
        if "opencode.ai" in api_url and (
            "/zen" in api_url or "zen." in api_url or "/go" in api_url
        ):
            return True
        return False

    def _is_custom(self, vendor: dict) -> bool:
        # Zen must stay on built-in catalog (auth.json only)
        if self._is_opencode_zen(vendor):
            return False
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
            "opencode": "opencode.ai",
            "zai": "api.z.ai",
            "zhipu": "open.bigmodel.cn",
        }
        host = defaults.get(pid, "")
        if host and host in api_url and not vendor.get("proxy_target"):
            return False
        return True

    def _build_provider_block(self, vendor: dict, key: dict, existing: Optional[dict] = None) -> dict:
        """Build one OpenCode provider block.

        OpenCode selects one SDK implementation per provider block.  The
        public helper below splits a mixed key into several blocks; this
        method deliberately receives the already-filtered model map so a
        block can never contain models for another SDK.
        """
        api_url = vendor.get("proxy_target") or vendor.get("api_url") or ""
        ep = vendor.get("endpoint_type") or "openai"
        # Infer anthropic from URL when endpoint_type missing
        if (not ep or ep == "openai") and ("/anthropic" in (api_url or "").lower() or "api.anthropic.com" in (api_url or "").lower()):
            ep = "anthropic"
        base_url = self._normalize_base_url(api_url, ep)
        models = dict(existing.get("models") or {}) if existing and existing.get("models") else self._models_map(key)
        selected_endpoint = next(iter(models), "")
        if selected_endpoint:
            selected_endpoint = self.selected_model_endpoint(vendor, key, selected_endpoint)
        if selected_endpoint == "anthropic_messages":
            ep = "anthropic"
        elif selected_endpoint == "gemini_generate":
            ep = "google"
        else:
            ep = "openai"
        base_url = self._normalize_base_url(api_url, ep)
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

    @staticmethod
    def _endpoint_family(endpoint: str) -> str:
        if endpoint == "anthropic_messages":
            return "anthropic"
        if endpoint == "gemini_generate":
            return "google"
        if endpoint == "openai_chat":
            return "openai"
        return ""

    def _build_provider_blocks(self, vendor: dict, key: dict, base_pid: str,
                               existing: Optional[dict] = None) -> dict[str, dict]:
        """Return provider blocks grouped by the SDK endpoint family.

        A single OpenCode provider cannot mix ``@ai-sdk/openai-compatible``
        with ``@ai-sdk/anthropic`` or ``@ai-sdk/google``.  Grouping here keeps
        model-level endpoint selection intact while preserving one API key.
        Responses-only models are excluded because OpenCode's compatible
        provider does not expose the Responses API.
        """
        raw = self._models_map(key)
        usable = self.filter_model_ids(vendor, key, raw.keys())
        groups: dict[str, dict] = {}
        for mid in usable:
            family = self._endpoint_family(self.selected_model_endpoint(vendor, key, mid))
            if family:
                groups.setdefault(family, {})[mid] = raw[mid]
        if not groups and existing:
            # Preserve a legacy block only when it still has no inventory
            # metadata to evaluate. A detected/manual empty endpoint set must
            # not be resurrected here.
            if not key.get("models") and not key.get("endpoint_capabilities"):
                groups["openai"] = dict(existing.get("models") or {})
        if not groups:
            return {}
        multiple = len(groups) > 1
        out = {}
        for family, models in groups.items():
            pid = base_pid if not multiple else f"{base_pid}-{family}"
            previous = existing if pid == base_pid else None
            out[pid] = self._build_provider_block_for_models(vendor, key, models, family, previous)
        return out

    def _build_provider_block_for_models(self, vendor: dict, key: dict, models: dict,
                                         family: str, existing: Optional[dict] = None) -> dict:
        api_url = vendor.get("proxy_target") or vendor.get("api_url") or ""
        ep = {"openai": "openai", "anthropic": "anthropic", "google": "google"}.get(family, "openai")
        block = {
            "npm": self._npm_for_endpoint(ep),
            "name": vendor.get("name") or self._provider_id(vendor),
            "options": {"baseURL": self._normalize_base_url(api_url, ep), "_managed": self.MANAGED_TAG},
            "models": models,
        }
        if key.get("api_key"):
            block["options"]["apiKey"] = key["api_key"]
        if not block["options"]["baseURL"] and existing:
            prev = (existing.get("options") or {}).get("baseURL")
            if prev:
                block["options"]["baseURL"] = prev
        return block

    def _pick_best_key(self, vendor: dict, *, exclude: tuple[str, str] = None) -> Optional[dict]:
        """Return the unified primary → backup → first-healthy key."""
        selected = self.pick_syncable_key(vendor=vendor, exclude=exclude)
        return selected[1] if selected else None

    # ── lifecycle ──────────────────────────────────────────

    def on_key_added(self, vendor: dict, key: dict) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return
        pid = self._provider_id(vendor)
        # Force official Zen onto built-in id so /models shows free catalog
        if self._is_opencode_zen(vendor):
            pid = "opencode"

        auth = self._load_auth()
        auth[pid] = self._auth_entry(key["api_key"])
        self._save_auth(auth)

        # Zen / pure built-ins: auth only — never write models map (hides free models)
        if self._is_opencode_zen(vendor) or (
            not self._is_custom(vendor) and not vendor.get("proxy_target")
        ):
            cfg = self._load_config()
            entry = (cfg.get("provider") or {}).get(pid)
            if entry and (entry.get("options") or {}).get("_managed") == self.MANAGED_TAG:
                del cfg["provider"][pid]
                self._save_config(cfg)
                log.info("OpenCode: cleared managed override for built-in '%s'", pid)
            else:
                log.info("OpenCode: auth for built-in '%s'", pid)
            return

        if self._is_custom(vendor) or key.get("models") or vendor.get("proxy_target"):
            cfg = self._load_config()
            cfg.setdefault("provider", {})
            existing = cfg["provider"].get(pid) or {}
            blocks = self._build_provider_blocks(vendor, key, pid, existing)
            desired_pids = set(blocks)
            for old_pid, old_entry in list(cfg["provider"].items()):
                if (old_pid == pid or old_pid.startswith(pid + "-")) and \
                        (old_entry.get("options") or {}).get("_managed") == self.MANAGED_TAG \
                        and old_pid not in desired_pids:
                    del cfg["provider"][old_pid]
            cfg["provider"].update(blocks)
            for auth_pid in list(auth):
                if (auth_pid == pid or auth_pid.startswith(pid + "-")) and auth_pid not in desired_pids:
                    del auth[auth_pid]
            for block_pid in desired_pids:
                auth[block_pid] = self._auth_entry(key["api_key"])
            self._save_auth(auth)
            self._save_config(cfg)
            log.info("OpenCode: provider '%s' + auth synced", pid)
        else:
            log.info("OpenCode: auth for built-in '%s'", pid)

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled", True) and key.get("api_key"):
            self.on_key_added(vendor, key)
        else:
            self.on_key_removed(vendor, key)

    def _effective_provider_id(self, vendor: dict) -> str:
        if self._is_opencode_zen(vendor):
            return "opencode"
        return self._provider_id(vendor)

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        pid = self._effective_provider_id(vendor)
        from core.data import get_vendors

        # Prefer another healthy key for the same effective provider id
        for v in get_vendors():
            if self._effective_provider_id(v) != pid:
                continue
            other = self._pick_best_key(
                v,
                exclude=(str(vendor.get("id") or ""), str(key.get("id") or "")),
            )
            if other and other.get("id") != key.get("id"):
                self.on_key_added(v, other)
                return

        auth = self._load_auth()
        # Also drop endpoint-split ids (for example provider-anthropic) and
        # the legacy id if Zen was stored under a non-opencode id.
        legacy = self._provider_id(vendor)
        drop_ids = {pid, legacy}
        auth_changed = False
        for auth_pid in list(auth):
            if any(auth_pid == drop or auth_pid.startswith(drop + "-") for drop in drop_ids):
                del auth[auth_pid]
                auth_changed = True
        if auth_changed:
            self._save_auth(auth)

        cfg = self._load_config()
        changed = False
        for drop_pid in {pid, legacy}:
            for config_pid, entry in list((cfg.get("provider") or {}).items()):
                if config_pid != drop_pid and not config_pid.startswith(drop_pid + "-"):
                    continue
                if entry and (entry.get("options") or {}).get("_managed") == self.MANAGED_TAG:
                    del cfg["provider"][config_pid]
                    changed = True
        if changed:
            self._save_config(cfg)
            log.info("OpenCode: removed managed provider '%s'", pid)

    @property
    def supports_active_switch(self) -> bool:
        return True

    def list_providers(self) -> list[dict]:
        """List OpenCode providers (system vendors + managed config entries)."""
        from core.data import get_enabled_models, get_vendors
        cfg = self._load_config()
        auth = self._load_auth()
        providers_cfg = cfg.get("provider") or {}
        # preferred default provider if present in config
        preferred = ""
        try:
            preferred = str((cfg.get("model") or "").split("/")[0] or "")
        except Exception:
            preferred = ""
        if not preferred:
            preferred = str((cfg.get("provider_id") or cfg.get("default_provider") or "") or "")

        out = []
        seen = set()
        for v in get_vendors():
            pid = self._effective_provider_id(v)
            k = self._pick_best_key(v)
            if not k or not self.should_sync(v, k):
                continue
            # Zen free catalog does not require key model inventory
            if not self._is_opencode_zen(v):
                if (k.get("models") or k.get("disabled_models")) and not get_enabled_models(k):
                    continue
            if pid in seen:
                continue
            seen.add(pid)
            models = get_enabled_models(k) or []
            pcfg = providers_cfg.get(pid) or {}
            if not pcfg:
                split = [
                    (split_pid, split_cfg) for split_pid, split_cfg in providers_cfg.items()
                    if split_pid.startswith(pid + "-") and isinstance(split_cfg, dict)
                ]
                if split:
                    pcfg = split[0][1]
            opts = pcfg.get("options") or {}
            base = opts.get("baseURL") or opts.get("baseUrl") or (v.get("proxy_target") or v.get("api_url") or "")
            if self._is_opencode_zen(v):
                base = base or "https://opencode.ai/zen"
                preview = "Zen free catalog (models.dev)"
                model_list = list(models)[:20]
            else:
                preview = ", ".join(list(models)[:6]) + ("…" if len(models) > 6 else "")
                model_list = list(models)[:20]
            out.append({
                "id": pid,
                "name": v.get("name") or pcfg.get("name") or pid,
                "base_url": base,
                "vendor_id": str(v.get("id") or ""),
                "vendor_name": v.get("name") or "",
                "key_id": str(k.get("id") or ""),
                "key_name": k.get("name") or "",
                "models": model_list,
                "model_preview": preview,
                "active": bool(
                    preferred and (preferred == pid or preferred.startswith(pid + "-"))
                ) or (not preferred and (pid in auth or any(x.startswith(pid + "-") for x in auth))),
                "managed": True,
                "has_auth": pid in auth or any(x.startswith(pid + "-") for x in auth),
                "auth_only": self._is_opencode_zen(v),
            })

        # config-only managed providers not in system
        for pid, pcfg in providers_cfg.items():
            if pid in seen:
                continue
            opts = pcfg.get("options") or {}
            if opts.get("_managed") != self.MANAGED_TAG and pid not in auth:
                continue
            models = list((pcfg.get("models") or {}).keys())
            out.append({
                "id": pid,
                "name": pcfg.get("name") or pid,
                "base_url": opts.get("baseURL") or opts.get("baseUrl") or "",
                "vendor_id": "",
                "vendor_name": "",
                "models": models[:20],
                "model_preview": ", ".join(models[:6]) + ("…" if len(models) > 6 else ""),
                "active": bool(preferred and preferred == pid),
                "managed": opts.get("_managed") == self.MANAGED_TAG,
                "has_auth": pid in auth,
            })

        # if nothing marked active, mark first with auth
        if out and not any(x.get("active") for x in out):
            for x in out:
                if x.get("has_auth"):
                    x["active"] = True
                    break
        out.sort(key=lambda x: (0 if x.get("active") else 1, (x.get("name") or "").lower()))
        return out

    def get_active_provider(self) -> dict:
        for p in self.list_providers():
            if p.get("active"):
                return {
                    "active_provider": p.get("id") or "",
                    "name": p.get("name") or "",
                    "base_url": p.get("base_url") or "",
                }
        return {"active_provider": "", "name": "", "base_url": ""}

    def switch_provider(self, provider_id: str = "", vendor_id: str = "", key_id: str = "") -> dict:
        """Make a vendor/provider the preferred OpenCode slot (auth + managed provider + default model)."""
        from core.data import get_enabled_models, get_vendor, get_vendors

        vendor = None
        if vendor_id:
            vendor = get_vendor(vendor_id)
        if not vendor and provider_id:
            # match by provider id (including Zen → opencode)
            for v in get_vendors():
                if self._effective_provider_id(v) == provider_id or self._provider_id(v) == provider_id:
                    vendor = v
                    break
        if not vendor:
            return {"success": False, "message": "Vendor not found for OpenCode switch"}

        pid = self._effective_provider_id(vendor)
        key = None
        if key_id:
            for k in vendor.get("keys") or []:
                if (
                    str(k.get("id")) == str(key_id)
                    and k.get("api_key")
                    and self.should_sync(vendor, k)
                ):
                    key = k
                    break
        if not key:
            key = self._pick_best_key(vendor)
        if not key or not key.get("api_key"):
            return {"success": False, "message": "No enabled key on vendor"}
        if not self.is_installed():
            return {"success": False, "message": "OpenCode not installed"}

        # Auth always
        auth = self._load_auth()
        auth[pid] = self._auth_entry(key["api_key"])
        self._save_auth(auth)

        cfg = self._load_config()
        cfg.setdefault("provider", {})

        # Zen / pure built-ins: auth only — never write models map (hides free catalog)
        if self._is_opencode_zen(vendor) or (
            not self._is_custom(vendor) and not vendor.get("proxy_target")
        ):
            entry = cfg["provider"].get(pid)
            if entry and (entry.get("options") or {}).get("_managed") == self.MANAGED_TAG:
                del cfg["provider"][pid]
            # Prefer a free catalog model if none set; leave model empty so OpenCode picks
            models = get_enabled_models(key) or []
            if models and not self._is_opencode_zen(vendor):
                cfg["model"] = f"{pid}/{models[0]}"
            self._save_config(cfg)
            return {
                "success": True,
                "active_provider": pid,
                "message": f"OpenCode active → {vendor.get('name') or pid} (auth-only, free catalog)",
                "model": cfg.get("model") or "",
                "vendor_id": str(vendor.get("id") or ""),
                "key_id": str(key.get("id") or ""),
            }

        existing = cfg["provider"].get(pid) or {}
        blocks = {}
        if not existing or (existing.get("options") or {}).get("_managed") == self.MANAGED_TAG or self._is_custom(vendor):
            blocks = self._build_provider_blocks(vendor, key, pid, existing)
            for old_pid, old_entry in list(cfg["provider"].items()):
                if (old_pid == pid or old_pid.startswith(pid + "-")) and \
                        (old_entry.get("options") or {}).get("_managed") == self.MANAGED_TAG \
                        and old_pid not in blocks:
                    del cfg["provider"][old_pid]
            cfg["provider"].update(blocks)
            auth = self._load_auth()
            for auth_pid in list(auth):
                if (auth_pid == pid or auth_pid.startswith(pid + "-")) and auth_pid not in blocks:
                    del auth[auth_pid]
            for block_pid in blocks:
                auth[block_pid] = self._auth_entry(key["api_key"])
            self._save_auth(auth)

        models = get_enabled_models(key) or []
        if not models and key.get("default_model"):
            models = [str(key.get("default_model"))]
        if models:
            usable = [mid for mid in models if self.selected_model_endpoint(vendor, key, mid)]
            if usable:
                mid = usable[0]
                family = self._endpoint_family(self.selected_model_endpoint(vendor, key, mid))
                target_pid = pid if len(blocks) <= 1 else f"{pid}-{family}"
                cfg["model"] = f"{target_pid}/{mid}"
        self._save_config(cfg)

        return {
            "success": True,
            "active_provider": pid,
            "message": f"OpenCode active → {vendor.get('name') or pid}",
            "model": cfg.get("model") or "",
            "vendor_id": str(vendor.get("id") or ""),
            "key_id": str(key.get("id") or ""),
        }

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
        # Built-in / Zen: auth only (no provider.models override)
        auth_only_pids: set[str] = set()
        for v in get_vendors():
            pid = self._provider_id(v)
            if self._is_opencode_zen(v):
                pid = "opencode"
            k = self._pick_best_key(v)
            if not k or not self.should_sync(v, k):
                continue
            # Zen/built-in free catalog does not need per-key model inventory
            if not self._is_opencode_zen(v):
                if (k.get("models") or k.get("disabled_models")) and not get_enabled_models(k):
                    continue
            prev = desired.get(pid)
            if not prev:
                desired[pid] = (v, k)
            else:
                prev_score = len(get_enabled_models(prev[1]))
                cur_score = len(get_enabled_models(k))
                if cur_score > prev_score:
                    desired[pid] = (v, k)
                elif cur_score == prev_score and (v.get("api_url") or "") and not (prev[0].get("api_url") or ""):
                    desired[pid] = (v, k)
            if self._is_opencode_zen(v) or (
                not self._is_custom(v) and not v.get("proxy_target")
            ):
                auth_only_pids.add(pid)

        new_auth = {k: v for k, v in auth.items()
                    if isinstance(v, dict) and v.get("type") in ("oauth", "token")}

        desired_config_pids: set[str] = set()
        for pid, (v, k) in desired.items():
            if pid in auth_only_pids or self._is_opencode_zen(v):
                new_auth[pid] = self._auth_entry(k["api_key"])
                # Keep models.dev catalog — strip our previous managed override
                existing = cfg["provider"].get(pid) or {}
                if existing and (existing.get("options") or {}).get("_managed") == self.MANAGED_TAG:
                    del cfg["provider"][pid]
                continue
            if self._is_custom(v) or k.get("models") or v.get("proxy_target") or v.get("api_url"):
                existing = cfg["provider"].get(pid) or {}
                blocks = self._build_provider_blocks(v, k, pid, existing)
                desired_config_pids.update(blocks)
                for block_pid in blocks:
                    new_auth[block_pid] = self._auth_entry(k["api_key"])
                for old_pid, old_entry in list(cfg["provider"].items()):
                    if (old_pid == pid or old_pid.startswith(pid + "-")) and \
                            (old_entry.get("options") or {}).get("_managed") == self.MANAGED_TAG \
                            and old_pid not in blocks:
                        del cfg["provider"][old_pid]
                cfg["provider"].update(blocks)

        # Drop managed providers no longer desired
        for pid, entry in list(cfg.get("provider", {}).items()):
            if (entry.get("options") or {}).get("_managed") == self.MANAGED_TAG and \
                    pid not in desired and pid not in desired_config_pids:
                del cfg["provider"][pid]
            # Always drop managed override on auth-only pids (e.g. zen free models)
            if pid in auth_only_pids and (entry.get("options") or {}).get("_managed") == self.MANAGED_TAG:
                del cfg["provider"][pid]

        self._save_auth(new_auth)
        self._save_config(cfg)
        self._last_runtime_apply = self._apply_running_server(new_auth, cfg)
        log.info("OpenCode reconcile: %d provider(s), %d credential(s)",
                  len(cfg.get("provider") or {}), len(new_auth))
        return dict(self._last_runtime_apply)

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
