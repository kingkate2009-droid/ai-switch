import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from backends.base import BackendAdapter
from core.data import get_vendor, get_vendors, add_vendor, add_key, update_key, suggest_key_name

log = logging.getLogger(__name__)

OPENCLAW_CONFIG_DIR = Path.home() / ".openclaw"
OPENCLAW_CONFIG_PATH = OPENCLAW_CONFIG_DIR / "openclaw.json"
AGENT_DIR = OPENCLAW_CONFIG_DIR / "agents" / "main" / "agent"
AGENT_AUTH_PATH = AGENT_DIR / "auth-profiles.json"
AGENT_MODELS_PATH = AGENT_DIR / "models.json"
AGENT_SQLITE_PATH = AGENT_DIR / "openclaw-agent.sqlite"

MANAGER_VERSION = "2.0.2"
MIN_OPENCLAW_VERSION = "2026.3.0"
RECOMMENDED_OPENCLAW_VERSION = "2026.6.11"

# OpenClaw rejects certain model ids (e.g. xAI multi-agent). Filter at write boundary
# so health-scan inventory can still list them in the manager UI without breaking gateway.
_OPENCLAW_BLOCKED_MODEL_SUBSTR = (
    "multi-agent",
    "multi_agent",
)


def _is_blocked_openclaw_model(model_id: str, provider_id: str = "") -> bool:
    """Return True if this model must never be written into OpenClaw config."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return True
    if any(s in mid for s in _OPENCLAW_BLOCKED_MODEL_SUBSTR):
        return True
    # refs like "xai/grok-4.20-multi-agent-0309"
    if "/" in mid:
        _, _, rest = mid.partition("/")
        if any(s in rest for s in _OPENCLAW_BLOCKED_MODEL_SUBSTR):
            return True
    pid = (provider_id or "").strip().lower()
    if pid in ("xai", "x-ai", "x_ai") and "multi-agent" in mid.replace("_", "-"):
        return True
    return False


def _filter_openclaw_model_list(models, provider_id: str = "") -> list:
    """Drop blocked models from [{id,name}|str] lists."""
    out = []
    seen = set()
    for m in models or []:
        mid = m["id"] if isinstance(m, dict) else m
        if not mid or mid in seen:
            continue
        if _is_blocked_openclaw_model(str(mid), provider_id):
            continue
        seen.add(mid)
        if isinstance(m, dict):
            out.append({"id": mid, "name": m.get("name") or mid})
        else:
            out.append({"id": mid, "name": mid})
    return out


def _sanitize_openclaw_defaults_models(defaults_models: dict) -> dict:
    """Remove blocked agents.defaults.models entries in-place and return dict."""
    if not isinstance(defaults_models, dict):
        return {}
    for ref in list(defaults_models.keys()):
        if _is_blocked_openclaw_model(str(ref)):
            del defaults_models[ref]
            continue
        # also block by model part after provider/
        if "/" in str(ref):
            prov, _, mid = str(ref).partition("/")
            if _is_blocked_openclaw_model(mid, prov):
                del defaults_models[ref]
    return defaults_models


def _sanitize_openclaw_providers(providers: dict) -> dict:
    """Strip blocked models from models.providers entries (in-place)."""
    if not isinstance(providers, dict):
        return {}
    for ocp_key, entry in list(providers.items()):
        if not isinstance(entry, dict):
            continue
        provider_id = ocp_key.split("@")[0] if "@" in ocp_key else ocp_key
        models = entry.get("models")
        if isinstance(models, list):
            filtered = _filter_openclaw_model_list(models, provider_id)
            if filtered:
                entry["models"] = filtered
            else:
                # keep a placeholder so OpenClaw custom providers still validate
                if any(_is_blocked_openclaw_model(
                    (m["id"] if isinstance(m, dict) else m) or "", provider_id
                ) for m in (models or [])):
                    # all models were blocked — use non-blocked default placeholder
                    entry["models"] = [{"id": "default", "name": "default"}]
                else:
                    entry["models"] = filtered
        elif isinstance(models, dict):
            # rare dict form
            new_m = {}
            for mid, mv in models.items():
                if _is_blocked_openclaw_model(str(mid), provider_id):
                    continue
                new_m[mid] = mv
            entry["models"] = new_m
    return providers


class OpenClawAdapter(BackendAdapter):
    name = "openclaw"
    display_name = "OpenClaw"

    @staticmethod
    def _ocp_url(base_url: str, endpoint_type: str = "") -> str:
        url = (base_url or "").rstrip("/")
        ep = (endpoint_type or "").lower()
        if not url:
            return url
        if "/api/proxy" in url:
            if not any(f"/v{n}" in url for n in (1, 2, 3, 4)):
                url += "/v1"
            return url
        version_paths = ("/v1", "/v2", "/v3", "/v4")
        if any(url.endswith(vp) or f"{vp}/" in url for vp in version_paths):
            return url
        # Anthropic-compatible product roots (…/anthropic) already point at the API root
        if ep == "anthropic" or url.rstrip("/").endswith("/anthropic") or "/anthropic/" in url + "/":
            if not url.endswith("/v1"):
                return url + "/v1"
            return url
        return url + "/v1"

    @staticmethod
    def _load_openclaw_config() -> dict:
        if not OPENCLAW_CONFIG_PATH.exists():
            return {}
        with open(OPENCLAW_CONFIG_PATH) as f:
            raw = f.read()
        try:
            import json5
            return json5.loads(raw)
        except ImportError:
            return json.loads(raw)

    @staticmethod
    def _save_openclaw_config(cfg: dict) -> None:
        OPENCLAW_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmpf = tempfile.NamedTemporaryFile(mode="w", dir=str(OPENCLAW_CONFIG_DIR), delete=False, suffix=".json")
        try:
            json.dump(cfg, tmpf, indent=2, ensure_ascii=False)
            tmpf.close()
            shutil.move(tmpf.name, OPENCLAW_CONFIG_PATH)
        except Exception:
            os.unlink(tmpf.name)
            raise

    @staticmethod
    def _load_agent_auth() -> dict:
        if not AGENT_AUTH_PATH.exists():
            return {}
        with open(AGENT_AUTH_PATH) as f:
            return json.load(f)

    @staticmethod
    def _to_agent_profile(entry: dict, api_key: str) -> dict:
        """Convert our auth entry to the agent's expected format: {type, provider, key}."""
        return {
            "type": entry.get("mode", "api_key"),
            "provider": entry.get("provider", ""),
            "key": api_key or "",
        }

    @staticmethod
    def _build_agent_profiles(cfg: dict) -> dict:
        """Build agent profiles in {version:1, profiles: {key: {type, provider, key}}} format."""
        raw_profiles = cfg.get("auth", {}).get("profiles", {})
        providers = cfg.get("models", {}).get("providers", {})
        profiles = {}
        for ocp_key, profile in raw_profiles.items():
            pk_entry = providers.get(ocp_key, {})
            api_key = pk_entry.get("apiKey", "")
            profiles[ocp_key] = OpenClawAdapter._to_agent_profile(profile, api_key)
        return {"version": 1, "profiles": profiles}

    @staticmethod
    def _save_agent_auth(data: dict) -> None:
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        with open(AGENT_AUTH_PATH, "w") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _load_agent_models() -> dict:
        if not AGENT_MODELS_PATH.exists():
            return {"providers": {}}
        with open(AGENT_MODELS_PATH) as f:
            return json.load(f)

    @staticmethod
    def _save_agent_models(data: dict) -> None:
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
        with open(AGENT_MODELS_PATH, "w") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _sync_auth_to_sqlite(wrapper: dict) -> bool:
        """Write auth profiles to agent SQLite. Returns True on success."""
        try:
            import time
            conn = sqlite3.connect(str(AGENT_SQLITE_PATH), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_profile_store (
                    store_key TEXT NOT NULL PRIMARY KEY,
                    store_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO auth_profile_store (store_key, store_json, updated_at) VALUES (?, ?, ?)",
                ("primary", json.dumps(wrapper), int(time.time() * 1000)),
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                log.warning("Agent SQLite locked, will rely on auth-profiles.json for next reload: %s", e)
            else:
                log.warning("SQLite write failed, agent will pick up auth-profiles.json on restart: %s", e)
            return False
        except Exception as e:
            log.error("Failed to sync auth profiles to SQLite: %s", e)
            return False

    def _save_auth_profiles(self, force_sync: bool = False) -> None:
        cfg = self._load_openclaw_config()
        wrapper = self._build_agent_profiles(cfg)

        # Write auth-profiles.json (agent reads this on restart)
        self._save_agent_auth(wrapper)

        # Write SQLite directly (so running agent picks it up immediately)
        sqlite_ok = self._sync_auth_to_sqlite(wrapper)

        # If SQLite write failed, restart gateway so agent re-reads auth-profiles.json
        if not sqlite_ok and force_sync:
            log.info("SQLite sync failed with force_sync=True, restarting gateway...")
            try:
                import subprocess
                subprocess.run(["openclaw", "gateway", "restart"], capture_output=True, timeout=10)
            except Exception as e:
                log.error("Gateway restart failed: %s", e)

    def on_key_added(self, vendor: dict, key: dict) -> None:
        self._sync_key_to_openclaw(vendor, key)

    def on_key_updated(self, vendor: dict, key: dict) -> None:
        if key.get("enabled", True):
            self._sync_key_to_openclaw(vendor, key)
        else:
            ocp_key = f"{vendor['provider']}@{key['name']}"
            self._remove_from_openclaw(ocp_key)
            old = f"{vendor['provider']}-{key['id']}"
            if old != ocp_key:
                self._remove_from_openclaw(old)
            # Remove from agents.defaults.models
            cfg = self._load_openclaw_config()
            defaults_models = cfg.get("agents", {}).get("defaults", {}).get("models", {})
            for model_ref in list(defaults_models.keys()):
                if model_ref.startswith(f"{vendor['provider']}/"):
                    del defaults_models[model_ref]
            self._save_openclaw_config(cfg)
            self._save_agent_models_config(get_vendors())

    def on_key_removed(self, vendor: dict, key: dict) -> None:
        ocp_key = f"{vendor['provider']}@{key['name']}"
        self._remove_from_openclaw(ocp_key)
        old = f"{vendor['provider']}-{key['id']}"
        if old != ocp_key:
            self._remove_from_openclaw(old)

    def on_vendor_removed(self, vendor: dict) -> None:
        for k in vendor.get("keys", []):
            ocp_key = f"{vendor['provider']}@{k['name']}"
            self._remove_from_openclaw(ocp_key)
            old = f"{vendor['provider']}-{k['id']}"
            if old != ocp_key:
                self._remove_from_openclaw(old)
        # Clean aggregate entry
        self._remove_from_openclaw(vendor["provider"])

    @staticmethod
    def _normalize_models(models, default_model: str = "",
                          disabled_models: Optional[list] = None,
                          provider_id: str = "") -> list[dict]:
        """Normalize model list to [{id, name}, ...]. Only enabled + OpenClaw-supported models.
        Empty result means: either no inventory yet, or all models disabled/blocked."""
        disabled = set(disabled_models or [])
        out = []
        seen = set()
        has_inventory = False
        for m in models or []:
            mid = m["id"] if isinstance(m, dict) else m
            if not mid:
                continue
            has_inventory = True
            if mid in seen or mid in disabled:
                continue
            if _is_blocked_openclaw_model(str(mid), provider_id):
                continue
            seen.add(mid)
            out.append({"id": mid, "name": mid})
        if not out and default_model and default_model not in disabled:
            if not _is_blocked_openclaw_model(str(default_model), provider_id):
                out = [{"id": default_model, "name": default_model}]
        # If inventory exists but everything is disabled/blocked, stay empty (do not invent "default")
        if not out and has_inventory:
            return []
        return out

    def _sync_key_to_openclaw(self, vendor: dict, key: dict,
                               models_override: Optional[list] = None) -> None:
        if not key.get("api_key") or not key.get("enabled", True):
            return

        cfg = self._load_openclaw_config()
        provider_id = vendor["provider"]
        key_name = key["name"]
        ocp_key = f"{provider_id}@{key_name}"
        api_key = key["api_key"]
        api_url = vendor.get("proxy_target", "") or vendor.get("api_url", "")
        ep = vendor.get("endpoint_type", "openai") or "openai"
        ocp_url = self._ocp_url(api_url, ep) if api_url else ""
        api_type = self._get_api_type_for_vendor(vendor)
        raw_models = models_override if models_override is not None else key.get("models", [])
        models = self._normalize_models(
            raw_models, key.get("default_model", ""), key.get("disabled_models") or [],
            provider_id=provider_id,
        )

        # If all known models are disabled, remove from openclaw instead of inventing models
        if not models and (key.get("models") or key.get("disabled_models")):
            self._remove_from_openclaw(ocp_key)
            old = f"{vendor['provider']}-{key['id']}"
            if old != ocp_key:
                self._remove_from_openclaw(old)
            return

        # Keep previous models if scan returned empty (don't wipe working config)
        if not models:
            existing = cfg.get("models", {}).get("providers", {}).get(ocp_key, {})
            models = self._normalize_models(
                existing.get("models", []), key.get("default_model", ""),
                provider_id=provider_id,
            )
        if not models:
            # OpenClaw requires ≥1 model on custom providers
            models = [{"id": "default", "name": "default"}]

        cfg.setdefault("models", {}).setdefault("providers", {})
        cfg["models"]["providers"][ocp_key] = {
            "apiKey": api_key,
            "baseUrl": ocp_url,
            "api": api_type,
            "models": models,
        }

        cfg.setdefault("auth", {}).setdefault("profiles", {})
        cfg["auth"]["profiles"][ocp_key] = {
            "provider": provider_id,
            "mode": "api_key",
        }

        order = cfg.setdefault("auth", {}).setdefault("order", {})
        if provider_id not in order:
            order[provider_id] = [ocp_key]
        elif ocp_key not in order[provider_id]:
            order[provider_id].append(ocp_key)

        cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
        defaults_models = cfg["agents"]["defaults"]["models"]
        # Drop any previously synced blocked refs for this provider
        for ref in list(defaults_models.keys()):
            if ref.startswith(f"{provider_id}/") and _is_blocked_openclaw_model(ref, provider_id):
                del defaults_models[ref]
        for m in models:
            mid = m["id"]
            if _is_blocked_openclaw_model(mid, provider_id):
                continue
            defaults_models[f"{provider_id}/{mid}"] = {}
        _sanitize_openclaw_defaults_models(defaults_models)

        self._rebuild_provider_aggregate(cfg, provider_id)
        _sanitize_openclaw_providers(cfg.get("models", {}).get("providers", {}))

        self._save_openclaw_config(cfg)
        self._save_auth_profiles(force_sync=True)
        self._save_agent_models_config(get_vendors())

    def _get_api_type_for_vendor(self, vendor: dict) -> str:
        ep = (vendor.get("endpoint_type") or "").lower()
        if ep in ("anthropic", "claude"):
            return "anthropic-messages"
        if ep in ("google", "gemini"):
            return "google-generative-ai"
        url = (vendor.get("proxy_target") or vendor.get("api_url") or "").lower()
        if "/anthropic" in url or "api.anthropic.com" in url:
            return "anthropic-messages"
        return "openai-completions"

    def _get_api_type(self, provider_id: str) -> str:
        for v in get_vendors():
            if v["provider"] == provider_id:
                return self._get_api_type_for_vendor(v)
        return "openai-completions"

    def _rebuild_provider_aggregate(self, cfg: dict, provider_id: str) -> None:
        """Build models.providers.<provider> with apiKey (required by OpenClaw auth fallback)."""
        providers = cfg.get("models", {}).get("providers", {})
        api_type = self._get_api_type(provider_id)
        agg = {"api": api_type, "models": {}, "baseUrl": "", "apiKey": ""}
        for ocp_key, entry in providers.items():
            if "@" not in ocp_key or ocp_key.split("@")[0] != provider_id:
                continue
            for m in entry.get("models", []):
                mid = m["id"] if isinstance(m, dict) else m
                if not mid or mid in agg["models"]:
                    continue
                if _is_blocked_openclaw_model(str(mid), provider_id):
                    continue
                agg["models"][mid] = {"id": mid, "name": mid}
            if not agg["baseUrl"] and entry.get("baseUrl"):
                agg["baseUrl"] = entry["baseUrl"]
            # Prefer first non-empty key for aggregate apiKey fallback
            if not agg["apiKey"] and entry.get("apiKey"):
                agg["apiKey"] = entry["apiKey"]
        if agg["models"]:
            providers[provider_id] = {
                "api": agg["api"],
                "baseUrl": agg["baseUrl"],
                "apiKey": agg["apiKey"],
                "models": list(agg["models"].values()),
            }
        elif provider_id in providers and "@" not in provider_id:
            # Drop empty aggregate
            del providers[provider_id]

    def _remove_from_openclaw(self, ocp_key: str) -> None:
        cfg = self._load_openclaw_config()
        changed = False

        models = cfg.get("models", {}).get("providers", {})
        if ocp_key in models:
            del models[ocp_key]
            changed = True

        profiles = cfg.get("auth", {}).get("profiles", {})
        if ocp_key in profiles:
            del profiles[ocp_key]
            changed = True

        order = cfg.get("auth", {}).get("order", {})
        for pname, pkey in list(order.items()):
            pk_list = pkey if isinstance(pkey, list) else [pkey]
            if ocp_key in pk_list:
                pk_list.remove(ocp_key)
                if pk_list:
                    order[pname] = pk_list
                else:
                    del order[pname]
                changed = True
                break

        defaults_models = cfg.get("agents", {}).get("defaults", {}).get("models", {})
        provider_name = ocp_key.split("@")[0]
        for model_ref in list(defaults_models.keys()):
            if model_ref.startswith(f"{provider_name}/"):
                del defaults_models[model_ref]
                changed = True

        if changed:
            provider_name = ocp_key.split("@")[0] if "@" in ocp_key else ocp_key
            self._rebuild_provider_aggregate(cfg, provider_name)
            self._save_openclaw_config(cfg)
            self._save_auth_profiles()
            self._save_agent_models_config(get_vendors())

    def reconcile(self) -> None:
        from core.data import get_backend_config
        if get_backend_config(self.name).get("disabled"):
            return

        cfg = self._load_openclaw_config()
        vendors = get_vendors()

        # Rebuild models.providers, auth.profiles, auth.order, agents.defaults.models
        new_models_provs = {}
        new_profiles = {}
        new_order = {}
        new_defaults = {}

        from core.health_checker import is_key_backend_syncable

        for v in vendors:
            pname = v["provider"]
            api_type = self._get_api_type_for_vendor(v)
            for k in v.get("keys", []):
                if not k.get("enabled", True) or not k.get("api_key"):
                    continue
                # Failed health checks must not remain in OpenClaw config
                if not is_key_backend_syncable(v["id"], k):
                    continue
                if not self.should_sync(v, k):
                    continue
                models_list = self._normalize_models(
                    k.get("models", []), k.get("default_model", ""), k.get("disabled_models") or [],
                    provider_id=pname,
                )
                # Skip keys whose models were all disabled (kept in system only)
                if not models_list and (k.get("models") or k.get("disabled_models")):
                    continue
                if not models_list:
                    models_list = [{"id": "default", "name": "default"}]

                ocp_key = f"{pname}@{k['name']}"
                api_key = k["api_key"]
                api_url = v.get("proxy_target", "") or v.get("api_url", "")
                ocp_url = self._ocp_url(api_url, v.get("endpoint_type", "")) if api_url else ""

                new_models_provs[ocp_key] = {
                    "apiKey": api_key,
                    "baseUrl": ocp_url,
                    "api": api_type,
                    "models": models_list,
                }
                new_profiles[ocp_key] = {"provider": pname, "mode": "api_key"}

                if pname not in new_order:
                    new_order[pname] = [ocp_key]
                elif ocp_key not in new_order[pname]:
                    new_order[pname].append(ocp_key)

                for m in models_list:
                    mid = m["id"]
                    if _is_blocked_openclaw_model(mid, pname):
                        continue
                    new_defaults[f"{pname}/{mid}"] = {}

        # Build provider-level aggregate entries (must include apiKey for OpenClaw fallback)
        provider_aggs = {}
        for ocp_key, entry in new_models_provs.items():
            if "@" not in ocp_key:
                continue
            pname = ocp_key.split("@")[0]
            if pname not in provider_aggs:
                api_type = self._get_api_type(pname)
                provider_aggs[pname] = {"api": api_type, "models": {}, "baseUrl": "", "apiKey": ""}
            for m in entry.get("models", []):
                mid = m["id"] if isinstance(m, dict) else m
                if not mid or _is_blocked_openclaw_model(str(mid), pname):
                    continue
                provider_aggs[pname]["models"][mid] = {"id": mid, "name": mid}
            if not provider_aggs[pname].get("baseUrl") and entry.get("baseUrl"):
                provider_aggs[pname]["baseUrl"] = entry["baseUrl"]
            if not provider_aggs[pname].get("apiKey") and entry.get("apiKey"):
                provider_aggs[pname]["apiKey"] = entry["apiKey"]
        for pname, agg in provider_aggs.items():
            new_models_provs[pname] = {
                "api": agg["api"],
                "baseUrl": agg["baseUrl"],
                "apiKey": agg["apiKey"],
                "models": list(agg["models"].values()),
            }

        _sanitize_openclaw_providers(new_models_provs)
        _sanitize_openclaw_defaults_models(new_defaults)
        cfg.setdefault("models", {})["providers"] = new_models_provs
        cfg.setdefault("auth", {})["profiles"] = new_profiles
        cfg.setdefault("auth", {})["order"] = new_order
        cfg.setdefault("agents", {}).setdefault("defaults", {})["models"] = new_defaults

        self._save_openclaw_config(cfg)
        self._save_auth_profiles(force_sync=True)
        self._save_agent_models_config(vendors)

    def _save_agent_models_config(self, vendors: list[dict]) -> None:
        """Write agent models.json with full provider entries (apiKey/baseUrl/api/models)."""
        mdata = {"providers": {}}
        active_ocp_keys = set()
        active_providers = set()

        for v in vendors:
            pname = v["provider"]
            api_url = v.get("proxy_target", "") or v.get("api_url", "")
            ocp_url = self._ocp_url(api_url, v.get("endpoint_type", "")) if api_url else ""
            api_type = self._get_api_type_for_vendor(v)
            for k in v.get("keys", []):
                if not k.get("enabled", True) or not k.get("api_key"):
                    continue
                if not self.should_sync(v, k):
                    continue
                models = self._normalize_models(
                    k.get("models", []), k.get("default_model", ""), k.get("disabled_models") or [],
                    provider_id=pname,
                )
                if not models and (k.get("models") or k.get("disabled_models")):
                    continue
                if not models:
                    models = [{"id": "default", "name": "default"}]
                active_providers.add(pname)
                ocp_key = f"{pname}@{k['name']}"
                active_ocp_keys.add(ocp_key)
                mdata["providers"][ocp_key] = {
                    "apiKey": k["api_key"],
                    "baseUrl": ocp_url,
                    "api": api_type,
                    "models": models,
                }

        # Aggregate per provider (with apiKey fallback)
        for pname in active_providers:
            agg_models = {}
            base_url = ""
            api_key = ""
            api_type = self._get_api_type(pname)
            for pk, pv in mdata["providers"].items():
                if "@" not in pk or pk.split("@")[0] != pname:
                    continue
                for m in pv.get("models", []):
                    mid = m["id"] if isinstance(m, dict) else m
                    if not mid or mid in agg_models:
                        continue
                    if _is_blocked_openclaw_model(str(mid), pname):
                        continue
                    agg_models[mid] = {"id": mid, "name": mid}
                if not base_url and pv.get("baseUrl"):
                    base_url = pv["baseUrl"]
                if not api_key and pv.get("apiKey"):
                    api_key = pv["apiKey"]
            if agg_models:
                mdata["providers"][pname] = {
                    "apiKey": api_key,
                    "baseUrl": base_url,
                    "api": api_type,
                    "models": list(agg_models.values()),
                }

        _sanitize_openclaw_providers(mdata.get("providers", {}))
        self._save_agent_models(mdata)

    def sync_from_backend(self) -> list[dict]:
        """Return import candidates only — does not write to system data.

        Actual import + dedup is handled by POST /api/sync.
        """
        cfg = self._load_openclaw_config()
        providers = cfg.get("models", {}).get("providers", {})
        profiles = cfg.get("auth", {}).get("profiles", {})
        # Group by provider → vendor candidate
        by_provider: dict = {}

        for ocp_key, entry in providers.items():
            if "@" not in ocp_key:
                continue
            provider, key_name = ocp_key.split("@", 1)
            profile = profiles.get(ocp_key, {}) if isinstance(profiles, dict) else {}
            api_key = entry.get("apiKey", profile.get("apiKey", ""))
            api_url = entry.get("baseUrl", profile.get("baseUrl", ""))
            if not api_key:
                continue

            provider_lower = provider.lower()
            if any(x in provider_lower for x in ("anthropic", "claude")):
                endpoint_type = "anthropic"
            elif any(x in provider_lower for x in ("google", "gemini")):
                endpoint_type = "google"
            elif any(x in provider_lower for x in ("deepseek",)):
                endpoint_type = "deepseek"
            else:
                endpoint_type = "openai"

            if provider not in by_provider:
                by_provider[provider] = {
                    "name": provider.replace("-", " ").title(),
                    "provider": provider,
                    "api_url": api_url or "",
                    "endpoint_type": endpoint_type,
                    "keys": [],
                    "_seen_secrets": set(),
                }
            bucket = by_provider[provider]
            if not bucket.get("api_url") and api_url:
                bucket["api_url"] = api_url
            secret = str(api_key).strip()
            if secret in bucket["_seen_secrets"]:
                continue
            bucket["_seen_secrets"].add(secret)
            bucket["keys"].append({
                "name": key_name or f"from {self.name}",
                "api_key": api_key,
            })

        out = []
        for item in by_provider.values():
            item.pop("_seen_secrets", None)
            if item.get("keys"):
                out.append(item)
        return out

    def get_status(self) -> dict:
        from backends.base import make_status, cli_available, enriched_env, process_running, port_listening

        env = enriched_env()
        installed, version = cli_available("openclaw")
        if not installed:
            # binary may exist under nvm even when default node is too old
            version = self.get_version()
            if version and "not found" not in version.lower():
                installed = True
            elif process_running("openclaw", "openclaw/dist/index.js"):
                installed = True
            else:
                return make_status(installed=False, port=None, message="openclaw CLI not found")
        if not version:
            version = self.get_version()

        port = None
        running = False
        message = ""
        try:
            r = subprocess.run(
                ["openclaw", "gateway", "status"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            output = (r.stdout or "") + (r.stderr or "")
            low = output.lower()
            # Prefer explicit runtime line: "Runtime: running (pid …)"
            if re.search(r"runtime:\s*running", low) or re.search(r"\brunning\s*\(pid", low):
                running = True
            elif ("running" in low or "active" in low) and not any(
                x in low for x in ("not running", "stopped", "inactive", "is not running", "node.js v")
            ):
                running = True
            for line in output.split("\n"):
                if "port" in line.lower():
                    m = re.search(r"(\d{4,5})", line)
                    if m:
                        port = m.group(1)
            message = output.strip()[:200]
        except FileNotFoundError:
            return make_status(installed=False, port=None, message="openclaw CLI not found")
        except subprocess.TimeoutExpired:
            message = "Status check timed out"
        except Exception as e:
            message = str(e)[:200]

        # Fallback: process / port (CLI status may fail due to wrong Node on PATH)
        if not running:
            if process_running("openclaw/dist/index.js gateway", "openclaw gateway"):
                running = True
            elif port_listening(18789) or (port and port_listening(int(port))):
                running = True
        if not port and port_listening(18789):
            port = "18789"
        if not message:
            message = f"Gateway running on :{port}" if running and port else ("Running" if running else "Stopped")
        return make_status(
            installed=True,
            running=running,
            port=port,
            version=version,
            message=message[:200],
        )

    def restart(self) -> dict:
        from backends.base import enriched_env
        try:
            r = subprocess.run(
                ["openclaw", "gateway", "restart"],
                capture_output=True, text=True, timeout=15,
                env=enriched_env(),
            )
            time.sleep(2)
            output = r.stdout + r.stderr
            if r.returncode == 0:
                return {"success": True, "message": output.strip()[:200]}
            return {"success": False, "message": output.strip()[:200]}
        except FileNotFoundError:
            return {"success": False, "message": "openclaw CLI not found"}
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "Restart timed out"}
        except Exception as e:
            return {"success": False, "message": str(e)[:200]}

    @property
    def config_files(self) -> list[dict]:
        return [
            {"path": str(OPENCLAW_CONFIG_PATH), "label": "Main Config", "type": "json"},
            {"path": str(AGENT_AUTH_PATH), "label": "Agent Auth Profiles", "type": "json"},
            {"path": str(AGENT_MODELS_PATH), "label": "Agent Models", "type": "json"},
        ]

    def get_version(self) -> str:
        from backends.base import enriched_env
        try:
            r = subprocess.run(
                ["openclaw", "--version"],
                capture_output=True, text=True, timeout=5,
                env=enriched_env(),
            )
            text = (r.stdout or "") + (r.stderr or "")
            for line in text.splitlines():
                s = line.strip()
                if not s:
                    continue
                if "node.js" in s.lower() and "required" in s.lower():
                    continue
                if s.lower().startswith("if you use nvm"):
                    continue
                return s[:80]
            return text.strip().split("\n")[0][:80] if text.strip() else "unknown"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unknown"
        except Exception:
            return "unknown"
