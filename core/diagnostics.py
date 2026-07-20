"""System diagnostics for local troubleshooting."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
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


def collect_diagnostics() -> dict:
    from core.data import DATA_PATH, DATA_DIR, get_vendors, get_settings
    from core.audit import AUDIT_PATH, list_events
    from core.version import get_version
    from core.paths import is_frozen, resource_root
    from backends import get_all as get_all_backends
    from core.data import get_backend_config

    vendors = get_vendors()
    keys = sum(len(v.get("keys") or []) for v in vendors)
    settings = get_settings() or {}
    backends = []
    for name, adapter in get_all_backends().items():
        cfg = get_backend_config(name)
        st = {}
        try:
            st = adapter.get_status() or {}
        except Exception as e:
            st = {"error": str(e)[:200]}
        backends.append({
            "name": name,
            "display_name": getattr(adapter, "display_name", name),
            "disabled": bool(cfg.get("disabled")),
            "status": st,
        })

    data_size = DATA_PATH.stat().st_size if DATA_PATH.exists() else 0
    return {
        "app": {
            "version": get_version(),
            "frozen": is_frozen(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "resource_root": str(resource_root()),
        },
        "data": {
            "path": str(DATA_PATH),
            "dir": str(DATA_DIR),
            "exists": DATA_PATH.exists(),
            "size_bytes": data_size,
            "vendors": len(vendors),
            "keys": keys,
            "backups": [p.name for p in DATA_DIR.glob("data.json.bak*")],
            "audit_path": str(AUDIT_PATH),
            "audit_events": len(list_events(limit=500)),
        },
        "settings": {
            "health_check_enabled": bool(settings.get("health_check_enabled")),
            "check_interval_seconds": int(settings.get("check_interval_seconds") or 300),
            "access_token_set": bool((settings.get("access_token") or "").strip()),
            "onboarding_done": bool(settings.get("onboarding_done")),
            "last_push_at": ((settings.get("last_push") or {}).get("at")),
        },
        "clis": {
            "openclaw": _cli_version(["openclaw", "--version"]),
            "opencode": _cli_version(["opencode", "--version"]),
            "claude": _cli_version(["claude", "--version"]),
        },
        "backends": backends,
        "env": {
            "AI_SWITCH_PORT": os.environ.get("AI_SWITCH_PORT", ""),
            "AI_SWITCH_HOST": os.environ.get("AI_SWITCH_HOST", ""),
        },
    }
