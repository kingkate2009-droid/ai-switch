import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path.home() / ".ai-switch"

log = logging.getLogger(__name__)
DATA_PATH = DATA_DIR / "data.json"
USAGE_PATH = DATA_DIR / "usage.json"
_OLD_DATA_DIR = Path.home() / ".openclaw-auto-manager"
_OLD_DATA_PATH = _OLD_DATA_DIR / "data.json"
_DATA_LOCK = threading.RLock()
_USAGE_LOCK = threading.RLock()
_DEFAULT_DATA = {
    "vendors": [],
    "settings": {
        "check_interval_seconds": 300,
        "health_check_enabled": False,
        "health_auto_disable": False,
        "access_token": "",
        "onboarding_done": False,
    },
    "backends": {},
}


def _migrate_old_data() -> None:
    if not DATA_PATH.exists() and _OLD_DATA_PATH.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_OLD_DATA_PATH, DATA_PATH)
        # Also migrate health cache
        old_cache = _OLD_DATA_DIR / "health_cache.json"
        if old_cache.exists():
            shutil.copy2(old_cache, DATA_DIR / "health_cache.json")
        print(f" Migrated data from {_OLD_DATA_PATH} to {DATA_PATH}")


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _backup_candidates() -> list[Path]:
    return [
        DATA_PATH.with_suffix(DATA_PATH.suffix + ".bak"),
        DATA_PATH.with_suffix(DATA_PATH.suffix + ".bak.1"),
        DATA_PATH.with_suffix(DATA_PATH.suffix + ".bak.2"),
    ]


def _read_json_file(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"data root must be object, got {type(data).__name__}")
    return data


def _quarantine_corrupt(path: Path) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        shutil.copy2(path, dest)
    except OSError:
        dest = path
    return dest


def _mark_data_recovery(data: dict, *, source: str, vendors: int, empty: bool = False) -> dict:
    """Attach a one-shot UI notice that data was restored from backup."""
    if not isinstance(data, dict):
        data = dict(_DEFAULT_DATA)
    settings = data.setdefault("settings", {})
    settings["data_recovery"] = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source or "",
        "vendors": int(vendors or 0),
        "empty": bool(empty),
        "acked": False,
    }
    return data


def _recover_data_from_backups(err: Exception) -> dict:
    """Quarantine corrupt data.json and restore the newest valid backup."""
    corrupt = _quarantine_corrupt(DATA_PATH) if DATA_PATH.exists() else None
    log.error("data.json is corrupt (%s); attempting backup restore (quarantine=%s)", err, corrupt)
    for bak in _backup_candidates():
        if not bak.exists():
            continue
        try:
            data = _read_json_file(bak)
            n = len(data.get("vendors") or [])
            data = _mark_data_recovery(data, source=bak.name, vendors=n, empty=False)
            # restore recovered file as active data.json (include recovery notice)
            tmp = DATA_PATH.with_suffix(DATA_PATH.suffix + ".recover-tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(DATA_PATH)
            log.warning("Restored data.json from %s (%s vendors)", bak.name, n)
            return data
        except Exception as e:
            log.warning("backup %s unusable: %s", bak.name, e)
    log.error("No valid backup found; starting with empty data")
    data = _mark_data_recovery(dict(_DEFAULT_DATA), source="", vendors=0, empty=True)
    try:
        tmp = DATA_PATH.with_suffix(DATA_PATH.suffix + ".recover-tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(DATA_PATH)
    except Exception:
        pass
    return data


def _atomic_write_json(path: Path, payload) -> None:
    """Atomic JSON write with fsync (no rotating backups)."""
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _load_usage_raw() -> list:
    """Load usage records from usage.json (empty list if missing/corrupt)."""
    with _USAGE_LOCK:
        _ensure_dirs()
        if not USAGE_PATH.exists():
            return []
        try:
            raw = _read_json_file(USAGE_PATH)
            recs = raw.get("records") if isinstance(raw, dict) else raw
            if not isinstance(recs, list):
                return []
            return recs
        except Exception as e:
            log.warning("usage.json unreadable (%s); starting empty", e)
            return []


def _save_usage_raw(records: list) -> None:
    with _USAGE_LOCK:
        if not isinstance(records, list):
            raise TypeError("usage records must be a list")
        # light rotate: keep one bak
        try:
            if USAGE_PATH.exists():
                bak = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".bak")
                try:
                    shutil.copy2(USAGE_PATH, bak)
                except OSError:
                    pass
        except Exception:
            pass
        _atomic_write_json(USAGE_PATH, {
            "format": "ai-switch-usage",
            "version": 1,
            "records": records,
        })


def _migrate_usage_out_of_data(data: dict) -> dict:
    """Move legacy data['usage'] into usage.json and strip from main config."""
    if not isinstance(data, dict) or "usage" not in data:
        return data
    legacy = data.get("usage")
    if not isinstance(legacy, list):
        data = dict(data)
        data.pop("usage", None)
        return data
    try:
        existing = _load_usage_raw()
        if legacy:
            # merge: prefer existing usage.json, append legacy not already present
            seen = set()
            for r in existing:
                if not isinstance(r, dict):
                    continue
                sid = str(r.get("source_id") or "")
                if sid:
                    seen.add("sid:" + sid)
                else:
                    seen.add("fp:%s|%s|%s|%s" % (
                        r.get("timestamp"), r.get("model"), r.get("total_tokens"), r.get("provider"),
                    ))
            merged = list(existing)
            for r in legacy:
                if not isinstance(r, dict):
                    continue
                sid = str(r.get("source_id") or "")
                key = ("sid:" + sid) if sid else ("fp:%s|%s|%s|%s" % (
                    r.get("timestamp"), r.get("model"), r.get("total_tokens"), r.get("provider"),
                ))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(r)
            # re-id sequentially for stability
            for i, r in enumerate(merged, start=1):
                if isinstance(r, dict):
                    r["id"] = str(i)
            _save_usage_raw(merged)
            log.info(
                "Migrated usage out of data.json → usage.json (%d legacy + %d existing → %d)",
                len(legacy), len(existing), len(merged),
            )
        elif not existing and not USAGE_PATH.exists():
            _save_usage_raw([])
    except Exception as e:
        log.warning("usage migration failed: %s", e)
        return data
    data = dict(data)
    data.pop("usage", None)
    try:
        # persist stripped main config immediately so next load stays lean
        _save_data_core(data)
    except Exception as e:
        log.warning("failed to strip usage from data.json: %s", e)
    return data


def _load_data() -> dict:
    with _DATA_LOCK:
        _migrate_old_data()
        _ensure_dirs()
        if not DATA_PATH.exists():
            data = dict(_DEFAULT_DATA)
        else:
            try:
                data = _read_json_file(DATA_PATH)
            except Exception as e:
                data = _recover_data_from_backups(e)
        # one-time / residual: split usage out of main config
        if isinstance(data, dict) and "usage" in data:
            data = _migrate_usage_out_of_data(data)
        return data


def _save_data_core(data: dict) -> None:
    """Atomic write with rotating backups (data.json.bak, .bak.1, .bak.2)."""
    _ensure_dirs()
    # never persist clearly broken payload
    if not isinstance(data, dict):
        raise TypeError("data must be a dict")
    # never keep usage in main config (separate usage.json)
    if "usage" in data:
        data = dict(data)
        data.pop("usage", None)
    # rotate backups before overwrite
    try:
        if DATA_PATH.exists():
            # only back up if current file is valid JSON
            try:
                _read_json_file(DATA_PATH)
                bak0 = DATA_PATH.with_suffix(DATA_PATH.suffix + ".bak")
                bak1 = DATA_PATH.with_suffix(DATA_PATH.suffix + ".bak.1")
                bak2 = DATA_PATH.with_suffix(DATA_PATH.suffix + ".bak.2")
                if bak1.exists():
                    try:
                        if bak2.exists():
                            bak2.unlink()
                        bak1.replace(bak2)
                    except OSError:
                        pass
                if bak0.exists():
                    try:
                        bak0.replace(bak1)
                    except OSError:
                        pass
                try:
                    shutil.copy2(DATA_PATH, bak0)
                except OSError:
                    pass
            except Exception:
                # current data.json is already corrupt — don't rotate it into backups
                pass
    except Exception:
        pass
    tmp = DATA_PATH.with_suffix(DATA_PATH.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(DATA_PATH)


def _save_data(data: dict) -> None:
    """Atomic write with rotating backups (data.json.bak, .bak.1, .bak.2)."""
    with _DATA_LOCK:
        _save_data_core(data)


def _next_id(items: list) -> str:
    return str(max((int(i.get("id", 0)) for i in items), default=0) + 1)


def get_vendors() -> list[dict]:
    return _load_data().get("vendors", [])


def get_vendor(vendor_id: str) -> Optional[dict]:
    for v in get_vendors():
        if v["id"] == vendor_id:
            return v
    return None


def _normalize_tags(raw) -> list[str]:
    """Normalize tags list: strip, lower for dedupe key, preserve display casing of first seen."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
        raw = parts
    if not isinstance(raw, (list, tuple)):
        return []
    out, seen = [], set()
    for t in raw:
        s = str(t or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:48])
    return out[:20]


def add_vendor(name: str, provider: str, api_url: str, endpoint_type: str = "openai",
               thinking_disabled: bool = False, proxy_target: str = "", tags=None,
               checkin_url: str = "", *, force_new: bool = False) -> dict:
    """Create vendor. Same normalized API URL auto-merges into existing (unless force_new)."""
    url = (api_url or "").rstrip("/")
    if not force_new and url:
        existing = find_vendor_by_url(url)
        if existing:
            patch = {}
            if name and not (existing.get("name") or "").strip():
                patch["name"] = name
            if checkin_url and not (existing.get("checkin_url") or "").strip():
                patch["checkin_url"] = checkin_url
            if proxy_target and not (existing.get("proxy_target") or "").strip():
                patch["proxy_target"] = proxy_target
            if tags:
                merged_tags = _normalize_tags(list(existing.get("tags") or []) + list(tags or []))
                if merged_tags != (existing.get("tags") or []):
                    patch["tags"] = merged_tags
            # Prefer /v1 OpenAI URL when existing lacks version path
            cur = (existing.get("api_url") or "").rstrip("/")
            if url.lower().endswith("/v1") and not cur.lower().endswith("/v1"):
                if vendor_url_merge_key(url) == vendor_url_merge_key(cur):
                    patch["api_url"] = url
            if patch:
                updated = update_vendor(str(existing["id"]), **patch) or existing
            else:
                updated = existing
            out = dict(updated)
            out["_existing"] = True
            return out

    data = _load_data()
    vendor = {
        "id": _next_id(data.get("vendors", [])),
        "name": name,
        "provider": provider,
        "api_url": url,
        "endpoint_type": endpoint_type,
        "thinking_disabled": thinking_disabled,
        "proxy_target": proxy_target,
        "checkin_url": (checkin_url or "").strip(),
        "tags": _normalize_tags(tags),
        "keys": [],
    }
    data["vendors"].append(vendor)
    _save_data(data)
    return vendor


def update_vendor(vendor_id: str, **kwargs) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            for key in ("name", "provider", "api_url", "endpoint_type", "thinking_disabled", "proxy_target", "checkin_url"):
                if key in kwargs:
                    v[key] = kwargs[key]
            if "api_url" in kwargs:
                v["api_url"] = kwargs["api_url"].rstrip("/")
            if "checkin_url" in kwargs:
                v["checkin_url"] = (kwargs.get("checkin_url") or "").strip()
            if "tags" in kwargs:
                v["tags"] = _normalize_tags(kwargs["tags"])
            _save_data(data)
            return v
    return None


def delete_vendor(vendor_id: str) -> Optional[dict]:
    data = _load_data()
    removed = None
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            removed = v
            break
    if not removed:
        return None
    data["vendors"] = [v for v in data["vendors"] if v["id"] != vendor_id]
    _save_data(data)
    return removed


def get_keys(vendor_id: str) -> list[dict]:
    v = get_vendor(vendor_id)
    return v.get("keys", []) if v else []


def get_key(vendor_id: str, key_id: str) -> Optional[dict]:
    for k in get_keys(vendor_id):
        if k["id"] == key_id:
            return k
    return None


def _norm_secret(s: str) -> str:
    return (s or "").strip()


def suggest_key_name(api_key: str, length: int = 10) -> str:
    """Default display name from API key prefix (reverse-import / missing name)."""
    s = _norm_secret(api_key)
    if not s:
        return "key"
    n = max(4, min(int(length or 10), 24))
    return s[:n]


def find_key_by_secret(vendor: dict, api_key: str) -> Optional[dict]:
    """Find key on a vendor by exact API secret (trimmed)."""
    want = _norm_secret(api_key)
    if not want:
        return None
    for k in vendor.get("keys") or []:
        if _norm_secret(k.get("api_key", "")) == want:
            return k
    return None


def find_key_anywhere(api_key: str) -> Optional[tuple]:
    """Find (vendor, key) anywhere in the system by API secret.

    Used by reverse-import so the same secret is never imported twice,
    even under a different vendor name.
    """
    want = _norm_secret(api_key)
    if not want:
        return None
    for v in get_vendors():
        k = find_key_by_secret(v, want)
        if k:
            return v, k
    return None


def find_vendor_by_url(api_url: str) -> Optional[dict]:
    """Match vendor by normalized API URL (with/without trailing /v1)."""
    want = vendor_url_merge_key(api_url or "")
    if not want:
        return None
    for v in get_vendors():
        if vendor_url_merge_key(v.get("api_url") or "") == want:
            return v
    return None


def find_vendor_for_import(provider: str, api_url: str = "", name: str = "") -> Optional[dict]:
    """Match an existing vendor for import / reverse-import.

    Priority: normalized URL → exact provider (only if unique) → name.
    URL-first prevents same-host /v1 vs bare duplicates and wrong provider collapse.
    """
    provider = (provider or "").strip()
    api_url = (api_url or "").rstrip("/")
    name_l = (name or "").strip().lower()
    vendors = get_vendors()

    # 1) normalized URL (primary — same rule as merge-urls)
    if api_url:
        hit = find_vendor_by_url(api_url)
        if hit:
            return hit
        # loose fallback for partial paths
        for v in vendors:
            vu = (v.get("api_url") or "").rstrip("/")
            if vu and (vu == api_url or vu.endswith(api_url) or api_url.endswith(vu)):
                return v

    # 2) provider only when unique among vendors (avoid collapsing multi-URL openai clones)
    if provider:
        matches = [v for v in vendors if (v.get("provider") or "") == provider]
        if not matches:
            matches = [v for v in vendors if (v.get("provider") or "").lower() == provider.lower()]
        if len(matches) == 1:
            return matches[0]

    # 3) name match
    if name_l:
        for v in vendors:
            if (v.get("name") or "").strip().lower() == name_l:
                return v
    return None


def add_key(vendor_id: str, name: str, api_key: str, *, allow_duplicate: bool = False, notes: str = "") -> Optional[dict]:
    """Add a key. By default skips if the same api_key already exists on this vendor.

    Returns existing key (with `_existing`: True) when deduped, or new entry, or None.
    """
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] != vendor_id:
            continue
        if not allow_duplicate:
            existing = find_key_by_secret(v, api_key)
            if existing:
                out = dict(existing)
                out["_existing"] = True
                return out
        entry = {
            "id": _next_id(v.get("keys", [])),
            "name": name,
            "api_key": api_key,
            "enabled": True,
            "notes": str(notes or "")[:500],
        }
        v.setdefault("keys", []).append(entry)
        _save_data(data)
        return entry
    return None


_KEY_FIELDS = (
    "name", "api_key", "enabled", "models", "default_model",
    "check_model",  # primary model for health / scheduled checks; empty = auto
    "disabled_models", "model_health", "notes", "role",
)

# role: "" | "primary" | "backup"


def _normalize_role(role) -> str:
    r = str(role or "").strip().lower()
    if r in ("primary", "backup"):
        return r
    return ""


def model_id_of(m) -> str:
    if isinstance(m, dict):
        return str(m.get("id") or m.get("name") or "")
    return str(m or "")


def list_model_ids(key: dict) -> list[str]:
    out, seen = [], set()
    for m in key.get("models") or []:
        mid = model_id_of(m)
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    dm = key.get("default_model") or ""
    if dm and dm not in seen:
        out.append(str(dm))
    return out


def get_enabled_models(key: dict) -> list[str]:
    """Models that should be synced to backends (system keeps full models list)."""
    ids = list_model_ids(key)
    if not ids:
        return []
    disabled = set(key.get("disabled_models") or [])
    return [m for m in ids if m not in disabled]


def set_model_enabled(vendor_id: str, key_id: str, model: str, enabled: bool) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] != vendor_id:
            continue
        for k in v["keys"]:
            if k["id"] != key_id:
                continue
            disabled = list(k.get("disabled_models") or [])
            if enabled:
                disabled = [m for m in disabled if m != model]
            elif model not in disabled:
                disabled.append(model)
            k["disabled_models"] = disabled
            _save_data(data)
            return k
    return None


def _apply_key_fields(k: dict, kwargs: dict) -> None:
    for key in _KEY_FIELDS:
        if key not in kwargs:
            continue
        val = kwargs[key]
        if key == "notes":
            val = str(val or "")[:500]
        elif key == "role":
            val = _normalize_role(val)
        k[key] = val


def promote_key(vendor_id: str, key_id: str, *, demote_others: bool = True) -> Optional[dict]:
    """Mark key as primary; optionally demote other primaries on same vendor to backup."""
    data = _load_data()
    target = None
    for v in data["vendors"]:
        if v["id"] != vendor_id:
            continue
        for k in v.get("keys") or []:
            if k["id"] == key_id:
                target = k
                break
        if not target:
            return None
        for k in v.get("keys") or []:
            if k["id"] == key_id:
                k["role"] = "primary"
                k["enabled"] = True
            elif demote_others and _normalize_role(k.get("role")) == "primary":
                k["role"] = "backup"
        _save_data(data)
        return target
    return None


def failover_primary(vendor_id: str, failed_key_id: str) -> Optional[dict]:
    """If failed key is primary, promote first enabled backup on same vendor.

    Returns the newly promoted key, or None if no failover performed.
    """
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] != vendor_id:
            continue
        keys = v.get("keys") or []
        failed = next((k for k in keys if k.get("id") == failed_key_id), None)
        if not failed or _normalize_role(failed.get("role")) != "primary":
            return None
        # pick first backup that is enabled or any backup
        backups = [
            k for k in keys
            if k.get("id") != failed_key_id and _normalize_role(k.get("role")) == "backup"
        ]
        if not backups:
            # fall back: any other enabled key
            backups = [k for k in keys if k.get("id") != failed_key_id]
        if not backups:
            return None
        # prefer already-enabled backups
        backups.sort(key=lambda k: (0 if k.get("enabled") is not False else 1, str(k.get("id"))))
        chosen = backups[0]
        failed["role"] = "backup"
        failed["enabled"] = False
        chosen["role"] = "primary"
        chosen["enabled"] = True
        _save_data(data)
        return chosen
    return None


def update_key(vendor_id: str, key_id: str, **kwargs) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            for k in v["keys"]:
                if k["id"] == key_id:
                    _apply_key_fields(k, kwargs)
                    _save_data(data)
                    return k
    return None


def update_key_data(vendor_id: str, key_id: str, **kwargs) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            for k in v["keys"]:
                if k["id"] == key_id:
                    _apply_key_fields(k, kwargs)
                    _save_data(data)
                    return k
    return None


def delete_key(vendor_id: str, key_id: str) -> Optional[dict]:
    data = _load_data()
    for v in data["vendors"]:
        if v["id"] == vendor_id:
            removed = None
            for k in v["keys"]:
                if k["id"] == key_id:
                    removed = k
                    break
            if removed:
                v["keys"] = [k for k in v["keys"] if k["id"] != key_id]
                _save_data(data)
                return removed
    return None


def get_settings() -> dict:
    return _load_data().get("settings", {})


def get_data_recovery_notice() -> Optional[dict]:
    """Return unacked data recovery notice, or None."""
    rec = (get_settings() or {}).get("data_recovery")
    if not isinstance(rec, dict) or rec.get("acked"):
        return None
    return dict(rec)


def ack_data_recovery() -> dict:
    """Mark recovery notice as acknowledged."""
    data = _load_data()
    settings = data.setdefault("settings", {})
    rec = settings.get("data_recovery")
    if isinstance(rec, dict):
        rec = dict(rec)
        rec["acked"] = True
        settings["data_recovery"] = rec
        _save_data(data)
        return rec
    return {}


_SETTINGS_KEYS = (
    "check_interval_seconds",
    "health_check_enabled",  # bool, default False — optional scheduled health checks
    "health_auto_disable",   # bool, default False — auto-disable keys on failed health check
    "health_network_retries",  # int, default 3 — max attempts on network/SSL/timeout failures
    "health_fail_streak",    # int, default 3 — consecutive fails before slow interval
    "health_ok_streak",      # int, default 3 — consecutive oks before slow interval
    "health_fail_interval_seconds",  # int, default 7200 — interval after fail streak
    "health_ok_interval_seconds",    # int, default 3600 — interval after ok streak
    "access_token",          # str, empty = no auth
    "onboarding_done",       # bool
    "read_only",             # bool, default False — block mutating APIs except settings/auth
    "health_auto_failover",  # bool, default False — promote backup when primary fails check
    "pricing",              # dict model -> {input,output} USD/1M
    "budget_daily_cost",    # float USD, 0 = off
    "budget_monthly_cost",  # float USD, 0 = off
    "budget_daily_tokens",  # int, 0 = off
    "budget_monthly_tokens",# int, 0 = off
    "budget_action",        # "" | "alert" | "read_only" | "disable_keys"
    "budget_enforced_at",   # iso timestamp of last enforcement
    "gateway",
    "last_push",
    "usage_imported_at",
    "data_recovery",  # one-shot notice after corrupt data.json restore
)


def is_read_only() -> bool:
    return bool((get_settings() or {}).get("read_only"))


def list_all_tags() -> list[str]:
    """Distinct vendor tags, sorted case-insensitively."""
    seen, out = set(), []
    for v in get_vendors():
        for t in v.get("tags") or []:
            s = str(t or "").strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
    out.sort(key=lambda x: x.lower())
    return out


def get_models_catalog() -> dict:
    """Aggregate models across all keys for a quick catalog view.

    locations are unique vendors (full vendor name), preferring enabled keys.
    """
    by_model: dict[str, dict] = {}
    for v in get_vendors():
        vid = v.get("id")
        vname = (v.get("name") or "").strip() or (v.get("provider") or "vendor")
        for k in v.get("keys") or []:
            kid, kname = k.get("id"), k.get("name") or ""
            enabled_key = k.get("enabled") is not False
            disabled = set(k.get("disabled_models") or [])
            ids = list_model_ids(k)
            for mid in ids:
                if not mid:
                    continue
                rec = by_model.setdefault(mid, {
                    "model": mid,
                    "key_count": 0,
                    "vendor_count": 0,
                    "enabled_count": 0,
                    "locations": [],
                    "_vendors": set(),
                    "_loc_by_vendor": {},
                })
                rec["key_count"] += 1
                rec["_vendors"].add(vid)
                model_on = mid not in disabled and enabled_key
                if model_on:
                    rec["enabled_count"] += 1
                # one location entry per vendor; prefer enabled key as jump target
                prev = rec["_loc_by_vendor"].get(vid)
                if not prev or (model_on and not prev.get("active")):
                    rec["_loc_by_vendor"][vid] = {
                        "vendor_id": vid,
                        "vendor_name": vname,
                        "key_id": kid,
                        "key_name": kname,
                        "key_enabled": enabled_key,
                        "model_enabled": mid not in disabled,
                        "active": bool(model_on),
                        "key_count": 1,
                    }
                else:
                    prev["key_count"] = int(prev.get("key_count") or 1) + 1
                    if model_on:
                        prev["active"] = True
                        prev["key_enabled"] = True
                        prev["model_enabled"] = True
    items = []
    for mid, rec in by_model.items():
        loc_map = rec.pop("_loc_by_vendor", {}) or {}
        # active (normal/enabled) vendors first, then full name
        locations = list(loc_map.values())
        locations.sort(key=lambda x: (0 if x.get("active") else 1, str(x.get("vendor_name") or "").lower()))
        # only show normal (active) vendors in locations; keep count of all
        active_locs = [x for x in locations if x.get("active")]
        rec["locations"] = active_locs if active_locs else locations
        rec["vendor_count"] = len(rec.pop("_vendors", set()))
        items.append(rec)
    items.sort(key=lambda r: (-r["key_count"], str(r["model"]).lower()))
    return {"count": len(items), "models": items}


def update_settings(**kwargs) -> dict:
    data = _load_data()
    data.setdefault("settings", {})
    for key in _SETTINGS_KEYS:
        if key in kwargs:
            data["settings"][key] = kwargs[key]
    # also allow explicit extra keys used by features
    for key, val in kwargs.items():
        if key not in data["settings"] and key.startswith("_"):
            continue
        if key not in _SETTINGS_KEYS and key in kwargs:
            # permit unknown settings for forward-compat (except private)
            if not str(key).startswith("_"):
                data["settings"][key] = val
    _save_data(data)
    return data["settings"]


def get_backend_config(backend_name: str) -> dict:
    return _load_data().get("backends", {}).get(backend_name, {})


def get_backend_configs() -> dict:
    return _load_data().get("backends", {})


def save_backend_config(backend_name: str, config: dict) -> None:
    data = _load_data()
    data.setdefault("backends", {})
    data["backends"][backend_name] = config
    _save_data(data)


# ── Usage Statistics ────────────────────────


def add_usage_record(record: dict) -> dict:
    records = _load_usage_raw()
    record["id"] = _next_id(records)
    if "success" not in record:
        record["success"] = True
    if "status_code" not in record:
        record["status_code"] = 200 if record.get("success", True) else 500
    records.append(record)
    _save_usage_raw(records)
    return record


_FAKE_SAMPLE_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "deepseek-chat", "deepseek-reasoner",
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3.5-sonnet",
    "gemini-1.5-pro", "gemini-1.5-flash",
}


def _all_key_models() -> set:
    models = set()
    for v in get_vendors():
        for k in v.get("keys", []):
            models.update(list_model_ids(k))
    return models


def _purge_stale_sample_usage() -> None:
    """Drop demo/sample usage. Detects old synthetic seed (same µs stamp + fake models)."""
    records = _load_usage_raw()
    if not records:
        return

    kept = [r for r in records if not r.get("_sample") and not r.get("_synthetic")]
    if not kept:
        if len(kept) != len(records):
            _save_usage_raw([])
            log.info("Purged %d sample usage records", len(records))
        return

    fracs = {str(r.get("timestamp") or "").split(".")[-1] for r in kept}
    usage_models = {str(r.get("model") or "") for r in kept}
    usage_models.discard("")
    key_models = _all_key_models()
    all_fake_models = bool(usage_models) and usage_models.issubset(_FAKE_SAMPLE_MODELS)
    # Seed generator used identical fractional seconds ".630931" on every row
    synthetic_stamp = len(fracs) == 1 and next(iter(fracs)) == "630931"
    no_real_model = not key_models or not (usage_models & key_models)
    # Drop entire batch when it is clearly the old demo seed
    if synthetic_stamp and all_fake_models and no_real_model:
        removed = len(kept)
        real = [r for r in records if r.get("source") == "openclaw" or r.get("source") == "proxy"]
        _save_usage_raw(real)
        log.info("Purged %d synthetic demo usage records", removed)
        return

    if len(kept) == len(records):
        return
    removed = len(records) - len(kept)
    _save_usage_raw(kept)
    log.info("Purged %d sample/stale usage records", removed)


def _normalize_model_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    # Drop common provider prefixes used by some gateways: "openai/gpt-4o" -> "gpt-4o"
    if "/" in s and not s.startswith("http"):
        left, right = s.split("/", 1)
        if right and left and not left.startswith("models"):
            if _is_provider_prefix(left):
                s = right
    return s


def _is_provider_prefix(left: str) -> bool:
    low = left.lower()
    if low in (
        "openai", "anthropic", "google", "gemini", "deepseek", "openrouter",
        "xai", "groq", "mistral", "cohere", "moonshot", "qwen", "alibaba",
        "volcengine", "doubao", "claude", "meta", "meta-llama", "google-vertex",
        "azure", "bedrock", "vertex", "z-ai", "deepseek-ai", "qwen", "meta-llama",
    ):
        return True
    # org/model style: short slug without spaces
    if low.endswith("-ai") or low.endswith("-org"):
        return True
    return False


def _norm_ts_for_compare(ts: str) -> str:
    """Normalize timestamps so Z / +00:00 / local naive compare consistently (lexicographic ISO)."""
    s = (ts or "").strip()
    if not s:
        return s
    s = s.replace("Z", "+00:00")
    # Drop timezone for string compare after converting offset-aware to comparable local-naive form
    try:
        from datetime import datetime
        if "T" in s:
            # Try parse common forms
            for fmt in (None,):
                try:
                    if s.endswith("+00:00") or (len(s) > 19 and s[19] in "+-"):
                        dt = datetime.fromisoformat(s)
                        # store as UTC naive ISO for ordering
                        if dt.tzinfo is not None:
                            from datetime import timezone
                            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                        return dt.isoformat(timespec="seconds")
                    return s[:19]
                except Exception:
                    pass
    except Exception:
        pass
    return s[:19] if len(s) >= 19 else s


def get_usage_records(from_ts: str = "", to_ts: str = "",
                      vendor_id: str = "", key_id: str = "",
                      provider: str = "", model: str = "",
                      source: str = "", auto_import: bool = True,
                      estimate_cost: bool = True) -> list[dict]:
    _purge_stale_sample_usage()
    if auto_import:
        try:
            from core.usage_import import import_all_usage
            # Throttle: import at most once per minute unless empty
            data0 = _load_data()
            last = (data0.get("settings") or {}).get("usage_imported_at", "")
            need = not _load_usage_raw()
            if last:
                try:
                    from datetime import datetime, timezone
                    prev = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    if prev.tzinfo is None:
                        prev = prev.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - prev).total_seconds()
                    need = need or age > 60
                except Exception:
                    need = True
            else:
                need = True
            if need:
                import_all_usage()
        except Exception as e:
            log.warning("Usage import skipped: %s", e)

    records = _load_usage_raw()
    from_n = _norm_ts_for_compare(from_ts) if from_ts else ""
    to_n = _norm_ts_for_compare(to_ts) if to_ts else ""
    filtered = []
    for r in records:
        if r.get("_sample") or r.get("_synthetic"):
            continue
        rts = _norm_ts_for_compare(str(r.get("timestamp", "") or ""))
        if from_n and rts and rts < from_n:
            continue
        if to_n and rts and rts > to_n:
            continue
        if vendor_id and str(r.get("vendor_id", "")) != str(vendor_id):
            continue
        if key_id and str(r.get("key_id", "")) != str(key_id):
            continue
        if provider and r.get("provider", "") != provider:
            continue
        if source:
            rs = str(r.get("source") or "unknown")
            if rs != str(source):
                continue
        if model:
            want = _normalize_model_name(str(model)) or str(model)
            raw = str(r.get("model", "") or "")
            got = _normalize_model_name(raw) or raw
            if got != want and raw != str(model):
                if not (raw.endswith("/" + want) or want.endswith("/" + raw) or raw == want):
                    continue
        filtered.append(r)
    filtered.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    # Fill missing/zero cost with token-based estimate (display + aggregation)
    if estimate_cost:
        try:
            from core.pricing import fill_missing_costs
            fill_missing_costs(filtered)
        except Exception:
            pass
    return filtered


def get_usage_summary(from_ts: str = "", to_ts: str = "",
                      group_by: str = "vendor",
                      vendor_id: str = "", key_id: str = "",
                      provider: str = "", model: str = "",
                      source: str = "", records: Optional[list] = None) -> list[dict]:
    """Summarize usage grouped by vendor, key, provider, model, or source (backend engine)."""
    if records is None:
        records = get_usage_records(
            from_ts, to_ts, vendor_id=vendor_id, key_id=key_id,
            provider=provider, model=model, source=source, auto_import=False,
        )
    key_models = _all_key_models() if group_by == "model" else set()
    groups = {}
    for r in records:
        if group_by == "vendor":
            gkey = str(r.get("vendor_id", r.get("vendor_name", "unknown")))
            name = r.get("vendor_name", gkey) or gkey
        elif group_by == "key":
            gkey = str(r.get("key_id", r.get("key_name", "unknown")))
            name = r.get("key_name", gkey) or gkey
        elif group_by == "provider":
            gkey = r.get("provider", "unknown") or "unknown"
            name = gkey
        elif group_by == "model":
            raw = r.get("model", "") or "unknown"
            gkey = _normalize_model_name(raw) or raw or "unknown"
            name = gkey
        elif group_by == "source":
            gkey = str(r.get("source") or "unknown")
            name = {
                "openclaw": "OpenClaw",
                "opencode": "OpenCode",
                "proxy": "Proxy",
                "claude_code": "Claude Code",
                "codex_cli": "Codex CLI",
                "unknown": "Unknown",
            }.get(gkey) or gkey.replace("_", " ").title()
        else:
            gkey = str(r.get("vendor_id", "unknown"))
            name = r.get("vendor_name", gkey)
        if gkey not in groups:
            groups[gkey] = {
                "id": gkey,
                "name": name,
                "vendor_id": r.get("vendor_id", ""),
                "vendor_name": r.get("vendor_name", ""),
                "key_id": r.get("key_id", "") if group_by == "key" else "",
                "key_name": r.get("key_name", "") if group_by == "key" else "",
                "model": gkey if group_by == "model" else "",
                "provider": r.get("provider", ""),
                "source": gkey if group_by == "source" else (r.get("source") or ""),
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_cost": 0.0,
                "count": 0,
                "success_count": 0,
                "fail_count": 0,
            }
        g = groups[gkey]
        g["total_tokens"] += r.get("total_tokens", 0) or 0
        g["prompt_tokens"] += r.get("prompt_tokens", 0) or 0
        g["completion_tokens"] += r.get("completion_tokens", 0) or 0
        g["total_cost"] += r.get("cost", 0) or 0
        g["count"] += 1
        ok = r.get("success", True)
        if ok:
            g["success_count"] += 1
        else:
            g["fail_count"] += 1
    result = []
    for g in groups.values():
        g["total_cost"] = round(g["total_cost"], 6)
        g["success_rate"] = round(g["success_count"] / g["count"] * 100, 1) if g["count"] else 0
        result.append(g)

    # When grouping by model, only show models that appear in usage (already true).
    # Prefer matching inventory names so used models are recognizable.
    if group_by == "model" and key_models:
        inv_lower = {m.lower(): m for m in key_models}
        for g in result:
            mid = str(g.get("name") or g.get("id") or "")
            if mid in key_models:
                continue
            # exact case-insensitive
            if mid.lower() in inv_lower:
                canon = inv_lower[mid.lower()]
                g["id"] = canon
                g["name"] = canon
                g["model"] = canon
                continue
            # suffix match: usage "xxx/gpt-4o" vs inventory "gpt-4o"
            for inv in key_models:
                if mid.endswith("/" + inv) or inv.endswith("/" + mid) or mid.endswith(inv) and len(inv) > 4:
                    g["id"] = inv
                    g["name"] = inv
                    g["model"] = inv
                    break
        # merge buckets that collapsed to same model id after canonicalization
        merged = {}
        for g in result:
            mid = g["id"]
            if mid not in merged:
                merged[mid] = g
            else:
                m = merged[mid]
                m["total_tokens"] += g["total_tokens"]
                m["prompt_tokens"] += g["prompt_tokens"]
                m["completion_tokens"] += g["completion_tokens"]
                m["total_cost"] = round(m["total_cost"] + g["total_cost"], 6)
                m["count"] += g["count"]
                m["success_count"] += g["success_count"]
                m["fail_count"] += g["fail_count"]
                m["success_rate"] = round(m["success_count"] / m["count"] * 100, 1) if m["count"] else 0
        result = list(merged.values())

    return sorted(result, key=lambda x: x["count"], reverse=True)


def export_backup(*, password: str = "") -> dict:
    """Full system backup payload (vendors + settings + backends).

    If password is non-empty, wrap with optional encryption (stdlib crypto).
    """
    from datetime import datetime, timezone
    data = _load_data()
    payload = {
        "format": "ai-switch-backup",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "vendors": data.get("vendors") or [],
            "settings": data.get("settings") or {},
            "backends": data.get("backends") or {},
        },
    }
    pwd = (password or "").strip()
    if pwd:
        from core.crypto_backup import encrypt_payload
        return encrypt_payload(payload, pwd)
    return payload


def import_backup(payload: dict, *, mode: str = "merge", password: str = "") -> dict:
    """Import backup. mode=merge|replace.

    merge: upsert vendors by provider/name, skip duplicate secrets
    replace: overwrite vendors/settings/backends entirely
    password: required when payload is encrypted

    Also accepts MetaAPI backups (accounts.sites + accounts.accounts).
    """
    if not isinstance(payload, dict):
        raise ValueError("invalid backup")
    from core.crypto_backup import is_encrypted_backup, maybe_decrypt
    if is_encrypted_backup(payload):
        payload = maybe_decrypt(payload, (password or "").strip() or None)
    # MetaAPI / metapi backup format
    try:
        from core.metapi_import import is_metapi_backup, import_metapi_backup
        if is_metapi_backup(payload):
            return import_metapi_backup(payload, include_disabled=True)
    except ImportError:
        pass
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(body, dict):
        raise ValueError("invalid backup data")
    vendors_in = body.get("vendors")
    if vendors_in is not None and not isinstance(vendors_in, list):
        raise ValueError("vendors must be a list")

    mode = (mode or "merge").lower()
    if mode not in ("merge", "replace"):
        mode = "merge"

    data = _load_data()
    added_v = added_k = skipped_k = 0

    if mode == "replace":
        if vendors_in is not None:
            data["vendors"] = vendors_in
        if isinstance(body.get("settings"), dict):
            data["settings"] = body["settings"]
        if isinstance(body.get("backends"), dict):
            data["backends"] = body["backends"]
        _save_data(data)
        return {
            "mode": "replace",
            "vendors": len(data.get("vendors") or []),
            "keys": sum(len(v.get("keys") or []) for v in (data.get("vendors") or [])),
        }

    # merge
    for vin in vendors_in or []:
        if not isinstance(vin, dict):
            continue
        provider = (vin.get("provider") or "custom").strip() or "custom"
        name = (vin.get("name") or provider).strip()
        api_url = (vin.get("api_url") or "").rstrip("/")
        existing = find_vendor_for_import(provider, api_url, name)
        if not existing:
            nv = add_vendor(
                name,
                provider,
                api_url,
                vin.get("endpoint_type") or "openai",
                bool(vin.get("thinking_disabled")),
                vin.get("proxy_target") or "",
                tags=vin.get("tags"),
                checkin_url=vin.get("checkin_url") or "",
            )
            existing = nv
            if not nv.get("_existing"):
                added_v += 1
        vid = existing["id"]
        # reload
        existing = get_vendor(vid) or existing
        for kin in vin.get("keys") or []:
            if not isinstance(kin, dict):
                continue
            secret = _norm_secret(kin.get("api_key") or "")
            if not secret:
                continue
            if find_key_anywhere(secret):
                skipped_k += 1
                continue
            kn = (kin.get("name") or suggest_key_name(secret)).strip() or suggest_key_name(secret)
            entry = add_key(vid, kn, secret, notes=str(kin.get("notes") or ""))
            if entry and not entry.get("_existing"):
                added_k += 1
                fields = {}
                for f in ("models", "default_model", "check_model", "disabled_models", "enabled", "role", "notes"):
                    if f in kin:
                        fields[f] = kin[f]
                if fields:
                    update_key_data(vid, entry["id"], **fields)
            else:
                skipped_k += 1

    if isinstance(body.get("settings"), dict):
        # shallow merge settings
        cur = data.get("settings") or {}
        cur.update(body["settings"])
        data = _load_data()
        data["settings"] = cur
        _save_data(data)
    if isinstance(body.get("backends"), dict):
        data = _load_data()
        cur_b = data.get("backends") or {}
        cur_b.update(body["backends"])
        data["backends"] = cur_b
        _save_data(data)

    return {
        "mode": "merge",
        "vendors_added": added_v,
        "keys_added": added_k,
        "keys_skipped": skipped_k,
    }


# ── Policy templates (settings presets) ────


_POLICY_FIELD_KEYS = (
    "check_interval_seconds",
    "health_check_enabled",
    "health_auto_disable",
    "health_auto_failover",
    "health_network_retries",
    "health_fail_streak",
    "health_ok_streak",
    "health_fail_interval_seconds",
    "health_ok_interval_seconds",
    "read_only",
    "budget_daily_cost",
    "budget_monthly_cost",
    "budget_daily_tokens",
    "budget_monthly_tokens",
)


def _builtin_policy_templates() -> list[dict]:
    return [
        {
            "id": "safe_manual",
            "name": "Safe Manual",
            "description": "All automation off. Manual health checks only.",
            "builtin": True,
            "settings": {
                "health_check_enabled": False,
                "health_auto_disable": False,
                "health_auto_failover": False,
                "check_interval_seconds": 300,
                "read_only": False,
                "budget_daily_cost": 0,
                "budget_monthly_cost": 0,
                "budget_daily_tokens": 0,
                "budget_monthly_tokens": 0,
            },
        },
        {
            "id": "auto_watch",
            "name": "Auto Watch",
            "description": "Scheduled health checks every 5 min; never auto-disable or failover.",
            "builtin": True,
            "settings": {
                "health_check_enabled": True,
                "health_auto_disable": False,
                "health_auto_failover": False,
                "check_interval_seconds": 300,
                "read_only": False,
            },
        },
        {
            "id": "strict_failover",
            "name": "Strict Failover",
            "description": "Scheduled checks + auto-disable failed keys + promote backups.",
            "builtin": True,
            "settings": {
                "health_check_enabled": True,
                "health_auto_disable": True,
                "health_auto_failover": True,
                "check_interval_seconds": 180,
                "read_only": False,
            },
        },
        {
            "id": "budget_guard",
            "name": "Budget Guard",
            "description": "Daily $5 / monthly $50 cost alerts; scheduled health checks.",
            "builtin": True,
            "settings": {
                "health_check_enabled": True,
                "health_auto_disable": False,
                "health_auto_failover": False,
                "check_interval_seconds": 300,
                "budget_daily_cost": 5,
                "budget_monthly_cost": 50,
                "budget_daily_tokens": 0,
                "budget_monthly_tokens": 0,
                "read_only": False,
            },
        },
        {
            "id": "read_only_share",
            "name": "Read-only Share",
            "description": "Block mutations (view-only). Turn off in Settings when done.",
            "builtin": True,
            "settings": {
                "read_only": True,
                "health_check_enabled": False,
                "health_auto_disable": False,
                "health_auto_failover": False,
            },
        },
    ]


def _custom_policies_path() -> Path:
    return DATA_DIR / "policy_templates.json"


def _load_custom_policies() -> list[dict]:
    path = _custom_policies_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("templates") or []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            settings = it.get("settings") if isinstance(it.get("settings"), dict) else {}
            out.append({
                "id": str(it.get("id") or _sanitize_policy_id(name)),
                "name": name[:80],
                "description": str(it.get("description") or "")[:200],
                "builtin": False,
                "settings": {k: settings[k] for k in _POLICY_FIELD_KEYS if k in settings},
            })
        return out
    except Exception:
        return []


def _save_custom_policies(items: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _custom_policies_path()
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        settings = it.get("settings") if isinstance(it.get("settings"), dict) else {}
        clean.append({
            "id": str(it.get("id") or _sanitize_policy_id(name)),
            "name": name[:80],
            "description": str(it.get("description") or "")[:200],
            "settings": {k: settings[k] for k in _POLICY_FIELD_KEYS if k in settings},
        })
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"templates": clean}, f, ensure_ascii=False, indent=2)
        f.flush()
    tmp.replace(path)


def _sanitize_policy_id(name: str) -> str:
    import re
    s = re.sub(r"[^\w.\-]+", "_", (name or "").strip(), flags=re.UNICODE).strip("._-")[:48]
    return s or "custom"


def list_policy_templates() -> dict:
    """Builtin + custom policy templates."""
    builtins = _builtin_policy_templates()
    custom = _load_custom_policies()
    # avoid id clash: prefix custom if needed
    used = {b["id"] for b in builtins}
    for c in custom:
        if c["id"] in used:
            c["id"] = "custom_" + c["id"]
        used.add(c["id"])
    return {"templates": builtins + custom}


def get_policy_template(template_id: str) -> Optional[dict]:
    tid = (template_id or "").strip()
    for t in list_policy_templates().get("templates") or []:
        if t.get("id") == tid:
            return t
    return None


def apply_policy_template(template_id: str) -> dict:
    """Apply a policy template's settings onto current settings. Returns updated settings."""
    t = get_policy_template(template_id)
    if not t:
        raise ValueError(f"policy not found: {template_id}")
    settings = t.get("settings") or {}
    kwargs = {k: settings[k] for k in _POLICY_FIELD_KEYS if k in settings}
    if not kwargs:
        raise ValueError("policy has no settings")
    return update_settings(**kwargs)


def save_policy_template(name: str, *, description: str = "", settings: Optional[dict] = None) -> dict:
    """Save current (or provided) policy fields as a custom template."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    cur = get_settings() or {}
    src = settings if isinstance(settings, dict) else cur
    payload_settings = {}
    for k in _POLICY_FIELD_KEYS:
        if k in src:
            payload_settings[k] = src[k]
    if not payload_settings:
        # snapshot from current
        for k in _POLICY_FIELD_KEYS:
            if k in cur:
                payload_settings[k] = cur[k]
    items = _load_custom_policies()
    pid = _sanitize_policy_id(name)
    # replace same name/id
    items = [it for it in items if it.get("id") != pid and (it.get("name") or "").lower() != name.lower()]
    entry = {
        "id": pid,
        "name": name[:80],
        "description": (description or "").strip()[:200],
        "settings": payload_settings,
    }
    items.append(entry)
    _save_custom_policies(items)
    entry["builtin"] = False
    return entry


def delete_policy_template(template_id: str) -> bool:
    tid = (template_id or "").strip()
    if not tid:
        return False
    # never delete builtins
    if any(b["id"] == tid for b in _builtin_policy_templates()):
        raise ValueError("cannot delete builtin policy")
    items = _load_custom_policies()
    new_items = [it for it in items if it.get("id") != tid]
    if len(new_items) == len(items):
        return False
    _save_custom_policies(new_items)
    return True


def find_duplicate_key_groups() -> list[dict]:
    """Group keys by API secret across all vendors. Only groups with count>=2."""
    groups = {}
    for v in get_vendors():
        for k in v.get("keys") or []:
            secret = _norm_secret(k.get("api_key") or "")
            if not secret:
                continue
            groups.setdefault(secret, []).append({
                "vendor_id": v.get("id"),
                "vendor_name": v.get("name"),
                "provider": v.get("provider"),
                "key_id": k.get("id"),
                "key_name": k.get("name"),
                "enabled": k.get("enabled", True),
                "has_models": bool(k.get("models")),
                "preview": (secret[:8] + "…" + secret[-4:]) if len(secret) > 14 else secret[:6] + "…",
            })
    out = []
    for secret, items in groups.items():
        if len(items) < 2:
            continue
        out.append({
            "api_key_preview": items[0]["preview"],
            "count": len(items),
            "keys": items,
            "_secret": secret,  # server-only use; strip before jsonify if needed
        })
    out.sort(key=lambda g: -g["count"])
    return out


def dedupe_keys(*, dry_run: bool = True) -> dict:
    """Keep one key per secret (prefer enabled + has_models + shorter name), remove others.

    Returns summary. When dry_run=True, no writes.
    """
    groups = find_duplicate_key_groups()
    to_remove = []
    kept = []
    for g in groups:
        items = list(g["keys"])
        def score(it):
            s = 0
            if it.get("enabled", True):
                s += 10
            if it.get("has_models"):
                s += 5
            # prefer non "from " names
            n = (it.get("key_name") or "").lower()
            if not n.startswith("from "):
                s += 2
            return s
        items_sorted = sorted(items, key=score, reverse=True)
        keep = items_sorted[0]
        kept.append(keep)
        for it in items_sorted[1:]:
            to_remove.append(it)

    removed = []
    if not dry_run:
        for it in to_remove:
            if delete_key(str(it["vendor_id"]), str(it["key_id"])):
                removed.append(it)
        # also remove empty vendors? optional - skip to be safe
    else:
        removed = to_remove

    return {
        "dry_run": dry_run,
        "groups": len(groups),
        "would_remove" if dry_run else "removed": len(removed),
        "kept": len(kept),
        "items": [
            {
                "vendor_id": it["vendor_id"],
                "vendor_name": it["vendor_name"],
                "key_id": it["key_id"],
                "key_name": it["key_name"],
                "preview": it["preview"],
            }
            for it in removed
        ],
    }


def save_import_snapshot(meta: dict) -> None:
    """Save snapshot of vendors/keys before a bulk import for undo."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "last_import_snapshot.json"
    payload = {
        "meta": meta,
        "backup": export_backup(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_import_snapshot() -> Optional[dict]:
    path = DATA_DIR / "last_import_snapshot.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_import_snapshot() -> None:
    path = DATA_DIR / "last_import_snapshot.json"
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def undo_last_import() -> dict:
    """Restore vendors from last import snapshot (full data vendors restore merge-safe).

    Strategy: replace vendors/settings/backends from snapshot backup data (vendors only replace
    is safer for undo of batch import). We restore full snapshot data.vendors list.
    """
    snap = load_import_snapshot()
    if not snap or not isinstance(snap.get("backup"), dict):
        raise ValueError("no import snapshot")
    backup = snap["backup"]
    # use replace mode for vendors to fully undo
    result = import_backup(backup, mode="replace")
    clear_import_snapshot()
    return {"restored": True, "snapshot_meta": snap.get("meta") or {}, **result}


def rollback_last_import_snapshot(*, keep_snapshot: bool = True) -> dict:
    """Restore from last import snapshot without clearing it (transaction abort).

    Used when a bulk import fails mid-way so the main data returns to pre-import state.
    keep_snapshot=True leaves undo available; False clears after restore.
    """
    snap = load_import_snapshot()
    if not snap or not isinstance(snap.get("backup"), dict):
        raise ValueError("no import snapshot")
    backup = snap["backup"]
    result = import_backup(backup, mode="replace")
    if not keep_snapshot:
        clear_import_snapshot()
    return {
        "rolled_back": True,
        "snapshot_meta": snap.get("meta") or {},
        **result,
    }


class import_transaction:
    """Context manager: snapshot before bulk import; auto-rollback on exception.

    Usage:
        with import_transaction({"source": "batch.import"}) as tx:
            ... mutate vendors/keys ...
            # on exception: restore snapshot automatically
        # success: snapshot kept for user Undo toast
    """

    def __init__(self, meta: Optional[dict] = None, *, rollback_on_error: bool = True):
        self.meta = meta or {}
        self.rollback_on_error = rollback_on_error
        self._entered = False
        self.rolled_back = False

    def __enter__(self):
        save_import_snapshot(self.meta)
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None and self.rollback_on_error and self._entered:
            try:
                rollback_last_import_snapshot(keep_snapshot=False)
                self.rolled_back = True
                log.warning(
                    "import transaction rolled back (%s): %s",
                    (self.meta or {}).get("source") or "import",
                    exc,
                )
            except Exception as e:
                log.error("import transaction rollback failed: %s", e)
        return False


def find_empty_vendors() -> list[dict]:
    """Vendors with zero keys."""
    out = []
    for v in get_vendors():
        if not (v.get("keys") or []):
            out.append({
                "id": v.get("id"),
                "name": v.get("name"),
                "provider": v.get("provider"),
                "api_url": v.get("api_url"),
            })
    return out


def delete_empty_vendors(*, dry_run: bool = True) -> dict:
    empties = find_empty_vendors()
    removed = []
    if not dry_run:
        for e in empties:
            if delete_vendor(str(e["id"])):
                removed.append(e)
    else:
        removed = empties
    return {
        "dry_run": dry_run,
        "count": len(removed),
        "items": removed,
    }


def vendor_url_merge_key(url: str) -> str:
    """Normalize API URL so host + path match with/without trailing /v1."""
    from urllib.parse import urlparse

    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        if not p.scheme or not p.netloc:
            return u.rstrip("/").lower()
        scheme = p.scheme.lower()
        host = p.netloc.lower()
        path = (p.path or "").rstrip("/")
        low = path.lower()
        if low.endswith("/v1"):
            path = path[:-3].rstrip("/")
        return f"{scheme}://{host}{path}".lower()
    except Exception:
        return u.rstrip("/").lower()


def _vendor_keep_score(v: dict) -> tuple:
    keys = v.get("keys") or []
    enabled = sum(1 for k in keys if k.get("enabled") is not False and k.get("api_key"))
    with_models = sum(1 for k in keys if k.get("models"))
    name = (v.get("name") or "").strip()
    name_l = name.lower()
    generic = name_l in (
        "openai", "anthropic", "custom", "gemini", "google", "base", "ase", "api",
    ) or name_l.startswith("https://") or name_l.startswith("http://")
    url = (v.get("api_url") or "").rstrip("/")
    has_v1 = url.lower().endswith("/v1")
    try:
        vid = int(str(v.get("id") or 0))
    except Exception:
        vid = 0
    return (
        len(keys),
        enabled,
        with_models,
        1 if (v.get("checkin_url") or "").strip() else 0,
        0 if generic else 1,
        1 if has_v1 else 0,
        -vid,
    )


def _prefer_api_url(urls: list[str], endpoint_type: str = "openai") -> str:
    cands = [(u or "").strip().rstrip("/") for u in urls if (u or "").strip()]
    if not cands:
        return ""
    ep = (endpoint_type or "openai").lower()
    if ep in ("openai", "openai_chat", "openai_responses", "responses", "codex", ""):
        for u in cands:
            if u.lower().endswith("/v1"):
                return u
    # longest path often more specific
    return max(cands, key=lambda u: (len(u), u))


def _merge_key_into(dst: dict, src: dict) -> None:
    """Enrich destination key with useful fields from source (same secret)."""
    if not (dst.get("models") or []) and (src.get("models") or []):
        dst["models"] = list(src.get("models") or [])
    if not dst.get("default_model") and src.get("default_model"):
        dst["default_model"] = src.get("default_model")
    if not dst.get("check_model") and src.get("check_model"):
        dst["check_model"] = src.get("check_model")
    if src.get("enabled") is True and dst.get("enabled") is False:
        dst["enabled"] = True
    if not (dst.get("notes") or "").strip() and (src.get("notes") or "").strip():
        dst["notes"] = src.get("notes")
    if not dst.get("role") and src.get("role"):
        dst["role"] = src.get("role")
    # merge model health / disabled lists lightly
    if src.get("model_health") and not dst.get("model_health"):
        dst["model_health"] = src.get("model_health")
    src_dis = list(src.get("disabled_models") or [])
    if src_dis:
        cur = list(dst.get("disabled_models") or [])
        for m in src_dis:
            if m not in cur:
                cur.append(m)
        dst["disabled_models"] = cur
    sn = (src.get("name") or "").strip()
    dn = (dst.get("name") or "").strip().lower()
    if sn and (dn.startswith("from ") or dn.startswith("key-") or not dn):
        if not sn.lower().startswith("from "):
            dst["name"] = sn


def _remap_vendor_refs(data: dict, id_map: dict) -> int:
    """Rewrite vendor_id references (downstream routes etc). Returns rewrite count."""
    if not id_map:
        return 0
    n = 0
    for dk in data.get("downstream_keys") or []:
        for r in dk.get("routes") or []:
            old = str(r.get("vendor_id") or "")
            if old in id_map:
                new_id = id_map[old]
                r["vendor_id"] = new_id
                # refresh name if keep vendor present
                keep = next((v for v in data.get("vendors") or [] if str(v.get("id")) == str(new_id)), None)
                if keep:
                    r["vendor_name"] = keep.get("name") or r.get("vendor_name")
                n += 1
    return n


def find_duplicate_vendor_url_groups() -> list[dict]:
    """Group vendors whose api_url matches after /v1 normalization."""
    buckets: dict[str, list] = {}
    for v in get_vendors():
        key = vendor_url_merge_key(v.get("api_url") or "")
        if not key:
            continue
        buckets.setdefault(key, []).append(v)

    groups = []
    for merge_key, items in buckets.items():
        if len(items) < 2:
            continue
        ranked = sorted(items, key=_vendor_keep_score, reverse=True)
        keep = ranked[0]
        merge = ranked[1:]
        groups.append({
            "merge_key": merge_key,
            "count": len(items),
            "keep": {
                "id": keep.get("id"),
                "name": keep.get("name"),
                "provider": keep.get("provider"),
                "api_url": keep.get("api_url"),
                "endpoint_type": keep.get("endpoint_type") or "openai",
                "key_count": len(keep.get("keys") or []),
            },
            "merge": [
                {
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "provider": m.get("provider"),
                    "api_url": m.get("api_url"),
                    "endpoint_type": m.get("endpoint_type") or "openai",
                    "key_count": len(m.get("keys") or []),
                }
                for m in merge
            ],
            "urls": sorted({(x.get("api_url") or "").rstrip("/") for x in items if x.get("api_url")}),
        })
    groups.sort(key=lambda g: -g["count"])
    return groups


def merge_duplicate_vendors_by_url(*, dry_run: bool = True) -> dict:
    """Merge vendors that share the same normalized API URL (with/without /v1).

    - Keep the strongest vendor (most keys / enabled / models / checkin)
    - Move unique secrets into keep; same secret enriches keep key
    - Prefer canonical api_url with /v1 for OpenAI-compatible endpoints
    - Remap downstream route vendor_id to keep id
    - Delete merged vendors
    """
    groups = find_duplicate_vendor_url_groups()
    preview = []
    total_merge_vendors = 0
    total_move_keys = 0
    total_skip_dup_keys = 0

    if dry_run:
        for g in groups:
            move = sum(m["key_count"] for m in g["merge"])
            total_merge_vendors += len(g["merge"])
            total_move_keys += move
            preview.append({
                "merge_key": g["merge_key"],
                "keep": g["keep"],
                "merge": g["merge"],
                "urls": g["urls"],
                "keys_from_merged": move,
            })
        return {
            "dry_run": True,
            "groups": len(groups),
            "would_merge_vendors": total_merge_vendors,
            "would_touch_keys": total_move_keys,
            "items": preview,
        }

    data = _load_data()
    vendors = data.setdefault("vendors", [])
    by_id = {str(v.get("id")): v for v in vendors}
    id_map: dict[str, str] = {}
    removed_ids: set[str] = set()
    merged_groups = 0

    for g in groups:
        keep_id = str(g["keep"]["id"])
        keep = by_id.get(keep_id)
        if not keep:
            continue
        merge_list = []
        for m in g["merge"]:
            mid = str(m["id"])
            mv = by_id.get(mid)
            if mv and mid != keep_id:
                merge_list.append(mv)
        if not merge_list:
            continue

        # unify api_url
        all_urls = [(keep.get("api_url") or "")] + [(m.get("api_url") or "") for m in merge_list]
        keep["api_url"] = _prefer_api_url(all_urls, keep.get("endpoint_type") or "openai")

        # fill missing metadata from merge candidates
        if not (keep.get("checkin_url") or "").strip():
            for m in merge_list:
                cu = (m.get("checkin_url") or "").strip()
                if cu:
                    keep["checkin_url"] = cu
                    break
        tags = list(keep.get("tags") or [])
        tag_seen = {t.lower() for t in tags if isinstance(t, str)}
        for m in merge_list:
            for t in m.get("tags") or []:
                s = str(t or "").strip()
                if s and s.lower() not in tag_seen:
                    tag_seen.add(s.lower())
                    tags.append(s)
            if not (keep.get("proxy_target") or "").strip() and (m.get("proxy_target") or "").strip():
                keep["proxy_target"] = m.get("proxy_target")
        keep["tags"] = tags[:20]

        # better name if keep is generic protocol brand
        _generic_names = ("openai", "anthropic", "custom", "base", "ase", "api", "gemini", "google")
        kn = (keep.get("name") or "").strip().lower()
        if kn in _generic_names or kn.startswith("http"):
            for m in sorted(merge_list, key=_vendor_keep_score, reverse=True):
                mn = (m.get("name") or "").strip()
                ml = mn.lower()
                if mn and ml not in _generic_names and not ml.startswith("http"):
                    keep["name"] = mn
                    if m.get("provider") and (keep.get("provider") or "").lower() in _generic_names:
                        keep["provider"] = m.get("provider")
                    break

        keep.setdefault("keys", [])
        moved = 0
        skipped = 0
        for m in merge_list:
            for k in list(m.get("keys") or []):
                secret = _norm_secret(k.get("api_key") or "")
                if not secret:
                    continue
                existing = find_key_by_secret(keep, secret)
                if existing:
                    _merge_key_into(existing, k)
                    skipped += 1
                    continue
                new_k = dict(k)
                new_k["id"] = _next_id(keep.get("keys") or [])
                keep.setdefault("keys", []).append(new_k)
                moved += 1
            mid = str(m.get("id"))
            id_map[mid] = keep_id
            removed_ids.add(mid)

        total_move_keys += moved
        total_skip_dup_keys += skipped
        total_merge_vendors += len(merge_list)
        merged_groups += 1
        preview.append({
            "merge_key": g["merge_key"],
            "keep": {
                "id": keep.get("id"),
                "name": keep.get("name"),
                "provider": keep.get("provider"),
                "api_url": keep.get("api_url"),
                "key_count": len(keep.get("keys") or []),
            },
            "merged_ids": [str(m.get("id")) for m in merge_list],
            "keys_moved": moved,
            "keys_deduped": skipped,
        })

    if removed_ids:
        data["vendors"] = [v for v in vendors if str(v.get("id")) not in removed_ids]
        refs = _remap_vendor_refs(data, id_map)
        _save_data(data)
    else:
        refs = 0

    return {
        "dry_run": False,
        "groups": merged_groups,
        "merged_vendors": total_merge_vendors,
        "keys_moved": total_move_keys,
        "keys_deduped": total_skip_dup_keys,
        "refs_remapped": refs,
        "items": preview,
    }


# ── Named config profiles ─────────────────

PROFILES_DIR = DATA_DIR / "profiles"
ACTIVE_PROFILE_PATH = DATA_DIR / "active_profile.json"


def _sanitize_profile_name(name: str) -> str:
    import re
    s = (name or "").strip()
    s = re.sub(r"[^\w.\-]+", "_", s, flags=re.UNICODE)
    s = s.strip("._-")[:64]
    return s or "default"


def list_profiles() -> dict:
    """List named profiles under ~/.ai-switch/profiles/."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    active = get_active_profile_name()
    items = []
    for p in sorted(PROFILES_DIR.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        name = p.stem
        items.append({
            "name": name,
            "label": meta.get("label") or name,
            "saved_at": meta.get("saved_at"),
            "vendors": len((meta.get("data") or {}).get("vendors") or []),
            "active": name == active,
        })
    return {"active": active, "profiles": items}


def get_active_profile_name() -> str:
    try:
        if ACTIVE_PROFILE_PATH.exists():
            with open(ACTIVE_PROFILE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return str(d.get("name") or "default")
    except Exception:
        pass
    return "default"


def set_active_profile_name(name: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ACTIVE_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump({"name": _sanitize_profile_name(name)}, f)


def save_profile(name: str, *, label: str = "") -> dict:
    """Snapshot current data.json into a named profile file."""
    from datetime import datetime, timezone
    name = _sanitize_profile_name(name)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    payload = export_backup()
    payload["label"] = (label or name).strip()[:80]
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    path = PROFILES_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"name": name, "label": payload["label"], "path": str(path), "saved_at": payload["saved_at"]}


def load_profile(name: str, *, mode: str = "replace") -> dict:
    """Load a named profile into current data (replace recommended)."""
    name = _sanitize_profile_name(name)
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"profile not found: {name}")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    result = import_backup(payload, mode=mode)
    set_active_profile_name(name)
    return {"name": name, "mode": mode, **result}


def delete_profile(name: str) -> bool:
    name = _sanitize_profile_name(name)
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        return False
    path.unlink()
    if get_active_profile_name() == name:
        set_active_profile_name("default")
    return True


def switch_profile(name: str) -> dict:
    """Save current as previous active (if any file), then load target profile."""
    # Auto-save current workspace into currently active profile name before switch
    current = get_active_profile_name()
    try:
        if current:
            save_profile(current)
    except Exception:
        pass
    return load_profile(name, mode="replace")


def export_profile(name: str = "", *, password: str = "") -> dict:
    """Export a named profile (or current workspace) as shareable backup.

    If password is set, wrap with encrypted envelope (same format as backup).
    """
    from datetime import datetime, timezone
    name = _sanitize_profile_name(name) if name else ""
    if name:
        path = PROFILES_DIR / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"profile not found: {name}")
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            raise ValueError("invalid profile file")
    else:
        payload = export_backup()
        name = get_active_profile_name() or "workspace"
        payload["label"] = name
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()

    # Normalize to shareable profile envelope
    out = {
        "format": "ai-switch-profile",
        "version": 1,
        "profile_name": name,
        "label": payload.get("label") or name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else {
            "vendors": payload.get("vendors") or (payload.get("data") or {}).get("vendors") or [],
            "settings": payload.get("settings") or (payload.get("data") or {}).get("settings") or {},
            "backends": payload.get("backends") or (payload.get("data") or {}).get("backends") or {},
        },
    }
    # If original was already a full backup export, prefer its data block
    if payload.get("format") == "ai-switch-backup" and isinstance(payload.get("data"), dict):
        out["data"] = payload["data"]
    elif isinstance(payload.get("data"), dict) and ("vendors" in payload["data"] or "settings" in payload["data"]):
        out["data"] = payload["data"]

    pwd = (password or "").strip()
    if pwd:
        from core.crypto_backup import encrypt_payload
        return encrypt_payload(out, pwd)
    return out


def import_profile_file(
    payload: dict,
    *,
    password: str = "",
    name: str = "",
    activate: bool = False,
    mode: str = "replace",
) -> dict:
    """Import a shared profile file into local profiles store.

    - Decrypt if encrypted
    - Save as named profile under ~/.ai-switch/profiles/
    - Optionally activate (replace current workspace)
    """
    from datetime import datetime, timezone
    from core.crypto_backup import is_encrypted_backup, maybe_decrypt

    if not isinstance(payload, dict):
        raise ValueError("invalid profile payload")
    if is_encrypted_backup(payload):
        payload = maybe_decrypt(payload, (password or "").strip() or None)

    # Accept profile envelope, plain backup, or raw data
    if payload.get("format") in ("ai-switch-profile", "ai-switch-backup") and isinstance(payload.get("data"), dict):
        data_block = payload["data"]
        default_name = str(payload.get("profile_name") or payload.get("label") or "imported")
        label = str(payload.get("label") or default_name)
    elif isinstance(payload.get("data"), dict):
        data_block = payload["data"]
        default_name = str(payload.get("name") or payload.get("label") or "imported")
        label = str(payload.get("label") or default_name)
    elif isinstance(payload.get("vendors"), list) or isinstance(payload.get("settings"), dict):
        data_block = {
            "vendors": payload.get("vendors") or [],
            "settings": payload.get("settings") or {},
            "backends": payload.get("backends") or {},
        }
        default_name = str(payload.get("name") or "imported")
        label = default_name
    else:
        raise ValueError("unrecognized profile format")

    profile_name = _sanitize_profile_name(name or default_name)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    stored = {
        "format": "ai-switch-backup",
        "version": 1,
        "label": (label or profile_name)[:80],
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "vendors": data_block.get("vendors") or [],
            "settings": data_block.get("settings") or {},
            "backends": data_block.get("backends") or {},
        },
    }
    path = PROFILES_DIR / f"{profile_name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False, indent=2)

    result = {
        "name": profile_name,
        "label": stored["label"],
        "path": str(path),
        "vendors": len(stored["data"]["vendors"]),
        "activated": False,
    }
    if activate:
        load_result = load_profile(profile_name, mode=mode or "replace")
        result["activated"] = True
        result["load"] = load_result
    return result


# ── Check-in (签到) ──────────────────────────────────────────


def vendor_supports_checkin(vendor: dict) -> bool:
    return bool(((vendor or {}).get("checkin_url") or "").strip())


def list_checkin_vendors(
    *,
    vendor_id: str = "",
    support: str = "all",
) -> list[dict]:
    """List vendors for check-in page with filters.

    support: all | yes | no
    """
    out = []
    for v in get_vendors():
        if vendor_id and str(v.get("id")) != str(vendor_id):
            continue
        url = (v.get("checkin_url") or "").strip()
        supports = bool(url)
        if support == "yes" and not supports:
            continue
        if support == "no" and supports:
            continue
        keys = v.get("keys") or []
        enabled_keys = [k for k in keys if k.get("enabled") is not False and k.get("api_key")]
        out.append({
            "id": v.get("id"),
            "name": v.get("name") or "",
            "provider": v.get("provider") or "",
            "api_url": v.get("api_url") or "",
            "checkin_url": url,
            "supports_checkin": supports,
            "key_count": len(keys),
            "enabled_key_count": len(enabled_keys),
            "tags": v.get("tags") or [],
        })
    out.sort(key=lambda x: (0 if x["supports_checkin"] else 1, (x.get("name") or "").lower()))
    return out
