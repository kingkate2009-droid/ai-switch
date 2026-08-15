"""System diagnostics for local troubleshooting."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _cli_version(cmd: list[str]) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        out = ((r.stdout or "") + (r.stderr or "")).strip().split("\n")[0][:120]
        return {
            "installed": r.returncode == 0 or bool(out),
            "version": out,
            "path": shutil.which(cmd[0]) or "",
        }
    except FileNotFoundError:
        return {"installed": False, "version": "", "path": ""}
    except Exception as e:
        return {"installed": False, "version": "", "path": "", "error": str(e)[:200]}


def _recent_health_failures(limit: int = 20) -> list[dict]:
    """Collect recent unhealthy key results without secrets."""
    try:
        from core.health_checker import get_all_health_status
        from core.data import get_vendors
    except Exception:
        return []
    health = get_all_health_status() or {}
    rows = []
    vendors = {str(v.get("id")): v for v in get_vendors()}
    for ck, h in health.items():
        if not isinstance(h, dict) or h.get("healthy") is not False:
            continue
        parts = str(ck).split(":", 1)
        vid = parts[0] if parts else ""
        kid = parts[1] if len(parts) > 1 else ""
        v = vendors.get(vid) or {}
        key_name = ""
        for k in v.get("keys") or []:
            if str(k.get("id")) == str(kid):
                key_name = k.get("name") or ""
                break
        rows.append({
            "vendor_id": vid,
            "vendor_name": v.get("name") or "",
            "key_id": kid,
            "key_name": key_name,
            "error": str(h.get("error") or "")[:300],
            "error_code": h.get("error_code"),
            "suggestion": h.get("suggestion"),
            "check_layer": h.get("check_layer"),
            "checked_at": h.get("checked_at"),
            "latency_ms": h.get("latency_ms"),
        })
    rows.sort(key=lambda x: str(x.get("checked_at") or ""), reverse=True)
    return rows[:limit]


def collect_diagnostics(*, for_issue: bool = False) -> dict:
    """Collect local diagnostics.

    for_issue=True includes extra fields useful for GitHub issues
    (recent health failures, last push summary, usage counts) without secrets.
    """
    from core.data import DATA_PATH, SQLITE_PATH, DATA_DIR, get_vendors, get_settings
    try:
        from core.data import USAGE_PATH, _load_usage_raw
    except Exception:
        USAGE_PATH = DATA_DIR / "usage.json"
        def _load_usage_raw():
            return []
    from core.audit import AUDIT_PATH, list_events
    from core.version import get_version
    from core.paths import is_frozen, resource_root
    from backends import get_all as get_all_backends, init_backends
    from core.data import get_backend_config

    # Ensure adapters are loaded even when called outside request context
    try:
        if not get_all_backends():
            init_backends()
    except Exception:
        try:
            init_backends()
        except Exception:
            pass

    vendors = get_vendors()
    keys = sum(len(v.get("keys") or []) for v in vendors)
    enabled_keys = sum(
        1
        for v in vendors
        for k in (v.get("keys") or [])
        if k.get("enabled") is not False and k.get("api_key")
    )
    settings = get_settings() or {}
    backends = []
    for name, adapter in get_all_backends().items():
        cfg = get_backend_config(name)
        st = {}
        try:
            st = adapter.get_status() or {}
        except Exception as e:
            st = {"error": str(e)[:200]}
        last_sync = cfg.get("last_sync") if isinstance(cfg, dict) else None
        backends.append({
            "name": name,
            "display_name": getattr(adapter, "display_name", name),
            "disabled": bool(cfg.get("disabled")),
            "supports_byok": bool(getattr(adapter, "supports_byok", True)),
            "supports_active_switch": bool(getattr(adapter, "supports_active_switch", False)),
            "status": st,
            "last_sync": last_sync,
        })

    data_size = SQLITE_PATH.stat().st_size if SQLITE_PATH.exists() else 0
    usage_size = USAGE_PATH.stat().st_size if USAGE_PATH.exists() else 0
    try:
        usage_n = len(_load_usage_raw())
    except Exception:
        usage_n = 0

    last_push = settings.get("last_push") or {}
    if isinstance(last_push, dict):
        # strip nothing sensitive — results only have ok/error strings
        last_push_out = {
            "at": last_push.get("at"),
            "ok": last_push.get("ok"),
            "fail": last_push.get("fail"),
            "skipped": last_push.get("skipped"),
            "results": last_push.get("results") or {},
        }
    else:
        last_push_out = {}

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format": "ai-switch-diagnostics",
        "version": 1,
        "app": {
            "version": get_version(),
            "frozen": is_frozen(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "resource_root": str(resource_root()),
        },
        "data": {
            "path": str(SQLITE_PATH),
            "dir": str(DATA_DIR),
            "exists": SQLITE_PATH.exists(),
            "size_bytes": data_size,
            "usage_path": str(USAGE_PATH),
            "usage_size_bytes": usage_size,
            "usage_records": usage_n,
            "vendors": len(vendors),
            "keys": keys,
            "enabled_keys": enabled_keys,
            "legacy_json": [p.name for p in DATA_DIR.glob("data.json*")],
            "audit_path": str(AUDIT_PATH),
            "audit_events": len(list_events(limit=500)),
        },
        "settings": {
            "health_check_enabled": bool(settings.get("health_check_enabled")),
            "health_auto_disable": bool(settings.get("health_auto_disable")),
            "check_interval_seconds": int(settings.get("check_interval_seconds") or 300),
            "access_token_set": bool((settings.get("access_token") or "").strip()),
            "read_only": bool(settings.get("read_only")),
            "onboarding_done": bool(settings.get("onboarding_done")),
            "budget_action": settings.get("budget_action") or "alert",
            "last_push_at": ((settings.get("last_push") or {}).get("at")),
        },
        "clis": {
            "openclaw": _cli_version(["openclaw", "--version"]),
            "opencode": _cli_version(["opencode", "--version"]),
            "claude": _cli_version(["claude", "--version"]),
            "codex": _cli_version(["codex", "--version"]),
        },
        "backends": backends,
        "env": {
            "AI_SWITCH_PORT": os.environ.get("AI_SWITCH_PORT", ""),
            "AI_SWITCH_HOST": os.environ.get("AI_SWITCH_HOST", ""),
        },
    }
    if for_issue:
        out["last_push"] = last_push_out
        out["recent_health_failures"] = _recent_health_failures(25)
        out["recent_audit"] = list_events(limit=30)
        out["note"] = (
            "This pack intentionally excludes API keys and secrets. "
            "Safe to attach to GitHub issues."
        )
    return out
