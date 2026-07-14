import json
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
from core.data import get_vendor, get_vendors, add_vendor, add_key, update_key

OPENCLAW_CONFIG_DIR = Path.home() / ".openclaw"
OPENCLAW_CONFIG_PATH = OPENCLAW_CONFIG_DIR / "openclaw.json"
AGENT_DIR = OPENCLAW_CONFIG_DIR / "agents" / "main" / "agent"
AGENT_AUTH_PATH = AGENT_DIR / "auth-profiles.json"
AGENT_MODELS_PATH = AGENT_DIR / "models.json"
AGENT_SQLITE_PATH = AGENT_DIR / "openclaw-agent.sqlite"

MANAGER_VERSION = "2.0.0"
MIN_OPENCLAW_VERSION = "2026.3.0"
RECOMMENDED_OPENCLAW_VERSION = "2026.6.11"


class OpenClawAdapter(BackendAdapter):
    name = "openclaw"
    display_name = "OpenClaw"
    display_name = "OpenClaw"

    @staticmethod
    def _ocp_url(base_url: str) -> str:
        url = base_url.rstrip("/")
        if "/api/proxy" in url:
            if not any(f"/v{n}" in url for n in (1, 2, 3, 4)):
                url += "/v1"
            return url
        version_paths = ("/v1", "/v2", "/v3", "/v4")
        if any(url.endswith(vp) or f"{vp}/" in url for vp in version_paths):
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
    def _sync_auth_to_sqlite(profiles: dict) -> None:
        try:
            conn = sqlite3.connect(str(AGENT_SQLITE_PATH))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_profile_store (
                    store_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO auth_profile_store (store_key, value_json) VALUES (?, ?)",
                ("primary", json.dumps(profiles)),
            )
            conn.commit()
        except Exception:
            pass

    def _save_auth_profiles(self, force_sync: bool = False) -> None:
        cfg = self._load_openclaw_config()
        profiles = cfg.get("auth", {}).get("profiles", {})
        self._save_agent_auth(profiles)
        self._sync_auth_to_sqlite(profiles)

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

    def _sync_key_to_openclaw(self, vendor: dict, key: dict,
                               models_override: Optional[list[str]] = None) -> None:
        cfg = self._load_openclaw_config()
        provider_id = vendor["provider"]
        key_name = key["name"]
        ocp_key = f"{provider_id}@{key_name}"
        api_key = key["api_key"]
        api_url = vendor.get("proxy_target", "") or vendor["api_url"]
        ocp_url = self._ocp_url(api_url)
        endpoint_type = vendor.get("endpoint_type", "openai")
        thinking_disabled = vendor.get("thinking_disabled", False)
        models = models_override if models_override is not None else key.get("models", [])

        cfg.setdefault("models", {}).setdefault("providers", {})
        entry = {"apiKey": api_key, "baseUrl": ocp_url}
        if models:
            mids = [m["id"] if isinstance(m, dict) else m for m in models]
            entry["models"] = [{"id": mid, "name": mid} for mid in mids]
        else:
            return
        cfg["models"]["providers"][ocp_key] = entry

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
        if models:
            for m in models:
                mid = m["id"] if isinstance(m, dict) else m
                model_ref = f"{provider_id}/{mid}"
                defaults_models[model_ref] = {}

        self._rebuild_provider_aggregate(cfg, provider_id)

        self._save_openclaw_config(cfg)
        self._save_auth_profiles()

    def _rebuild_provider_aggregate(self, cfg: dict, provider_id: str) -> None:
        providers = cfg.get("models", {}).get("providers", {})
        agg = {"api": "openai-completions", "models": {}}
        for ocp_key, entry in providers.items():
            if "@" in ocp_key and ocp_key.split("@")[0] == provider_id:
                for m in entry.get("models", []):
                    mid = m["id"] if isinstance(m, dict) else m
                    if mid not in agg["models"]:
                        agg["models"][mid] = {"id": mid, "name": mid}
                if not agg.get("baseUrl") and entry.get("baseUrl"):
                    agg["baseUrl"] = entry["baseUrl"]
        if agg["models"]:
            providers[provider_id] = {
                "api": agg["api"],
                "baseUrl": agg["baseUrl"],
                "models": list(agg["models"].values()),
            }

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

    def reconcile(self) -> None:
        cfg = self._load_openclaw_config()
        vendors = get_vendors()

        # Rebuild models.providers, auth.profiles, auth.order, agents.defaults.models
        new_models_provs = {}
        new_profiles = {}
        new_order = {}
        new_defaults = {}

        for v in vendors:
            pname = v["provider"]
            for k in v.get("keys", []):
                if k.get("enabled", True):
                    ocp_key = f"{pname}@{k['name']}"
                    api_key = k["api_key"]
                    api_url = v.get("proxy_target", "") or v["api_url"]
                    ocp_url = self._ocp_url(api_url)
                    models_list = k.get("models", [])

                    existing = new_models_provs.get(ocp_key, {})
                    existing_api_key = existing.get("apiKey", "")
                    if not existing_api_key:
                        existing["apiKey"] = api_key
                        existing["baseUrl"] = ocp_url
                    if models_list:
                        existing_mids = {
                            m["id"] if isinstance(m, dict) else m
                            for m in existing.get("models", [])
                        }
                        new_mids = [
                            m["id"] if isinstance(m, dict) else m
                            for m in models_list
                        ]
                        for mid in new_mids:
                            existing_mids.add(mid)
                        existing["models"] = [
                            {"id": mid, "name": mid}
                            for mid in sorted(existing_mids)
                        ]
                    new_models_provs[ocp_key] = existing

                    new_profiles[ocp_key] = {"provider": pname, "mode": "api_key"}

                    if pname not in new_order:
                        new_order[pname] = [ocp_key]
                    elif ocp_key not in new_order[pname]:
                        new_order[pname].append(ocp_key)

                    for m in models_list:
                        mid = m["id"] if isinstance(m, dict) else m
                        model_ref = f"{pname}/{mid}"
                        new_defaults[model_ref] = {}

        # Filter: OpenClaw requires custom providers to have at least one model
        for ocp_key in list(new_models_provs.keys()):
            models_list = new_models_provs[ocp_key].get("models", [])
            if not models_list:
                del new_models_provs[ocp_key]
                del new_profiles[ocp_key]
                # Also remove from order
                pname = ocp_key.split("@")[0]
                if pname in new_order and ocp_key in new_order[pname]:
                    new_order[pname].remove(ocp_key)
                    if not new_order[pname]:
                        del new_order[pname]

        # Build provider-level aggregate entries for models.providers
        provider_aggs = {}
        for ocp_key, entry in new_models_provs.items():
            if "@" in ocp_key:
                pname = ocp_key.split("@")[0]
                if pname not in provider_aggs:
                    provider_aggs[pname] = {"api": "openai-completions", "models": {}}
                for m in entry.get("models", []):
                    mid = m["id"] if isinstance(m, dict) else m
                    provider_aggs[pname]["models"][mid] = {"id": mid, "name": mid}
                # Pick baseUrl from first per-key entry
                if not provider_aggs[pname].get("baseUrl") and entry.get("baseUrl"):
                    provider_aggs[pname]["baseUrl"] = entry["baseUrl"]
        for pname, agg in provider_aggs.items():
            new_models_provs[pname] = {
                "api": agg["api"],
                "baseUrl": agg["baseUrl"],
                "models": list(agg["models"].values()),
            }

        cfg["models"]["providers"] = new_models_provs
        cfg["auth"]["profiles"] = new_profiles
        cfg["auth"]["order"] = new_order
        cfg["agents"]["defaults"]["models"] = new_defaults

        self._save_openclaw_config(cfg)
        self._save_auth_profiles()
        self._save_agent_models_config(vendors)

    def _save_agent_models_config(self, vendors: list[dict]) -> None:
        mdata = self._load_agent_models()
        mdata.setdefault("providers", {})
        active_ocp_keys = {}
        active_providers = set()

        for v in vendors:
            pname = v["provider"]
            active_providers.add(pname)
            for k in v.get("keys", []):
                if k.get("enabled", True):
                    ocp_key = f"{pname}@{k['name']}"
                    active_ocp_keys[ocp_key] = True
                    models_list = k.get("models", [])
                    entry_models = []
                    for m in models_list:
                        mid = m["id"] if isinstance(m, dict) else m
                        entry_models.append({"id": mid, "name": mid})
                    existing = mdata["providers"].get(ocp_key, {})
                    existing_models = existing.get("models", [])
                    existing_ids = {m["id"] if isinstance(m, dict) else m for m in existing_models}
                    new_ids = {m["id"] if isinstance(m, dict) else m for m in entry_models}
                    merged_ids = existing_ids | new_ids
                    merged = []
                    for mid in merged_ids:
                        merged.append({"id": mid, "name": mid})
                    mdata["providers"][ocp_key] = {"models": merged}

        # Clean stale entries
        for ocp_key in list(mdata["providers"].keys()):
            if "@" in ocp_key:
                base = ocp_key.split("@")[0]
                if ocp_key not in active_ocp_keys and base not in active_providers:
                    del mdata["providers"][ocp_key]
            else:
                if ocp_key not in active_providers:
                    del mdata["providers"][ocp_key]

        # Rebuild aggregate provider entries
        for pname in active_providers:
            agg_models = {}
            for pk, pv in list(mdata["providers"].items()):
                if "@" in pk and pk.split("@")[0] == pname:
                    for m in pv.get("models", []):
                        mid = m["id"] if isinstance(m, dict) else m
                        if mid not in agg_models:
                            agg_models[mid] = m if isinstance(m, dict) else {"id": mid, "name": mid}
            if agg_models:
                mdata["providers"][pname] = {
                    "baseUrl": "",
                    "api": "openai-completions",
                    "models": list(agg_models.values()),
                }
            elif pname in mdata["providers"]:
                del mdata["providers"][pname]

        self._save_agent_models(mdata)

    def sync_from_backend(self) -> list[dict]:
        cfg = self._load_openclaw_config()
        providers = cfg.get("models", {}).get("providers", {})
        profiles = cfg.get("auth", {}).get("profiles", {})
        imported = []

        for ocp_key, entry in providers.items():
            if "@" not in ocp_key:
                continue
            provider, key_name = ocp_key.split("@", 1)
            profile = profiles.get(ocp_key, {})
            api_key = profile.get("apiKey", entry.get("apiKey", ""))
            api_url = profile.get("baseUrl", "")
            endpoint_type = profile.get("type", "openai")

            found_vendor = None
            for v in get_vendors():
                if v["provider"] == provider and (v["api_url"] == api_url or not api_url):
                    found_vendor = v
                    break

            if not found_vendor:
                found_vendor = add_vendor(
                    name=provider.replace("-", " ").title(),
                    provider=provider,
                    api_url=api_url,
                    endpoint_type=endpoint_type,
                )

            if api_key and key_name:
                key_exists = any(
                    k["name"] == key_name or k["api_key"] == api_key
                    for k in found_vendor.get("keys", [])
                )
                if not key_exists:
                    k = add_key(found_vendor["id"], key_name, api_key)
                    if k:
                        imported.append(found_vendor)

        return imported

    def get_status(self) -> dict:
        try:
            r = subprocess.run(
                ["openclaw", "gateway", "status"],
                capture_output=True, text=True, timeout=10,
            )
            output = r.stdout + r.stderr
            running = "running" in output.lower() or "active" in output.lower()
            port = None
            message = output.strip()
            for line in output.split("\n"):
                if "port" in line.lower():
                    m = re.search(r"(\d{4,5})", line)
                    if m:
                        port = m.group(1)
            version = self.get_version()
            return {"running": running, "port": port, "version": version, "message": message[:200]}
        except FileNotFoundError:
            return {"running": False, "port": None, "version": "", "message": "openclaw CLI not found"}
        except subprocess.TimeoutExpired:
            return {"running": False, "port": None, "version": "", "message": "Status check timed out"}
        except Exception as e:
            return {"running": False, "port": None, "version": "", "message": str(e)[:200]}

    def restart(self) -> dict:
        try:
            r = subprocess.run(
                ["openclaw", "gateway", "restart"],
                capture_output=True, text=True, timeout=15,
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
        try:
            r = subprocess.run(
                ["openclaw", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return (r.stdout + r.stderr).strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unknown"
