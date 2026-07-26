import importlib
import pkgutil
from typing import Optional

from backends.base import BackendAdapter

_adapters: dict[str, BackendAdapter] = {}


def register(adapter: BackendAdapter) -> None:
    _adapters[adapter.name] = adapter


def get(name: str) -> Optional[BackendAdapter]:
    return _adapters.get(name)


def get_all() -> dict[str, BackendAdapter]:
    return dict(_adapters)


def _discover_adapters() -> None:
    """Auto-discover all backend adapter modules in the backends package."""
    import backends
    for importer, modname, ispkg in pkgutil.iter_modules(backends.__path__):
        if modname in ("base", "__init__"):
            continue
        try:
            module = importlib.import_module(f"backends.{modname}")
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, BackendAdapter) and attr is not BackendAdapter:
                    instance = attr()
                    if instance.name not in _adapters:
                        register(instance)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Failed to load backend adapter %s: %s", modname, e)


def init_backends(data_dir: str = "") -> None:
    _discover_adapters()


def _filtered_adapters(vendor: dict, key: dict) -> list[BackendAdapter]:
    """Iterates adapters that should receive an event for this vendor+key pair."""
    result = []
    for adapter in _adapters.values():
        try:
            if adapter.supports_byok and adapter.should_sync(vendor, key):
                result.append(adapter)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Backend %s should_sync failed: %s", adapter.name, e
            )
    return result


def on_key_added(vendor: dict, key: dict) -> None:
    for adapter in _filtered_adapters(vendor, key):
        try:
            adapter.on_key_added(vendor, key)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Backend %s on_key_added failed: %s", adapter.name, e)


def on_key_updated(vendor: dict, key: dict) -> None:
    for adapter in _filtered_adapters(vendor, key):
        try:
            adapter.on_key_updated(vendor, key)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Backend %s on_key_updated failed: %s", adapter.name, e)


def on_key_removed(vendor: dict, key: dict) -> None:
    for adapter in _filtered_adapters(vendor, key):
        try:
            adapter.on_key_removed(vendor, key)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Backend %s on_key_removed failed: %s", adapter.name, e)


def on_vendor_removed(vendor: dict) -> None:
    for k in vendor.get("keys", []):
        on_key_removed(vendor, k)


def preview_push_all() -> dict:
    """Dry-run: which backends would be written / skipped on push (no file writes)."""
    from core.data import get_backend_config
    items = []
    will_write = skipped = 0
    for adapter in _adapters.values():
        name = adapter.name
        display = getattr(adapter, "display_name", None) or name
        cfg = get_backend_config(name) or {}
        files = []
        try:
            for cf in (adapter.config_files or []):
                if isinstance(cf, dict):
                    files.append({
                        "path": cf.get("path") or "",
                        "label": cf.get("label") or cf.get("path") or "",
                    })
        except Exception:
            files = []
        installed = False
        try:
            installed = bool(adapter.is_installed())
        except Exception:
            installed = False
        disabled = bool(cfg.get("disabled"))
        byok = True
        try:
            byok = bool(getattr(adapter, "supports_byok", True))
        except Exception:
            byok = True

        action = "write"
        reason = ""
        if not byok:
            action = "skip"
            reason = "readonly"
        elif disabled:
            action = "skip"
            reason = "disabled"
        elif not installed:
            action = "skip"
            reason = "not_installed"

        if action == "write":
            will_write += 1
        else:
            skipped += 1
        items.append({
            "name": name,
            "display_name": display,
            "action": action,
            "reason": reason,
            "installed": installed,
            "disabled": disabled,
            "supports_byok": byok,
            "config_files": files,
            "last_sync": cfg.get("last_sync") or None,
        })
    items.sort(key=lambda x: (0 if x.get("action") == "write" else 1, str(x.get("display_name") or "").lower()))
    return {
        "items": items,
        "will_write": will_write,
        "skipped": skipped,
        "total": len(items),
    }


def reconcile_all() -> dict:
    """Push system keys to backends. Returns per-backend result summary."""
    from datetime import datetime, timezone
    from core.data import get_backend_config, _load_data, _save_data
    import logging
    log = logging.getLogger(__name__)
    results = {}
    at = datetime.now(timezone.utc).isoformat()
    for adapter in _adapters.values():
        name = adapter.name
        try:
            cfg = get_backend_config(name)
            if cfg.get("disabled"):
                results[name] = {"ok": False, "skipped": True, "error": "disabled"}
                continue
            try:
                if not adapter.is_installed():
                    results[name] = {"ok": False, "skipped": True, "error": "not installed"}
                    continue
            except Exception:
                # Fail closed: unknown install state must not write configs
                results[name] = {"ok": False, "skipped": True, "error": "install check failed"}
                continue
            adapter.reconcile()
            results[name] = {"ok": True, "skipped": False, "error": None}
        except Exception as e:
            log.warning("Backend %s reconcile failed: %s", name, e)
            results[name] = {"ok": False, "skipped": False, "error": str(e)[:300]}
        # per-backend last sync summary
        try:
            r = results.get(name) or {}
            data = _load_data()
            backends = data.setdefault("backends", {})
            bcfg = dict(backends.get(name) or {})
            bcfg["last_sync"] = {
                "at": at,
                "ok": bool(r.get("ok")),
                "skipped": bool(r.get("skipped")),
                "error": r.get("error"),
            }
            backends[name] = bcfg
            _save_data(data)
        except Exception as e:
            log.warning("Failed to save last_sync for %s: %s", name, e)
    # persist last push summary in settings
    try:
        data = _load_data()
        settings = data.setdefault("settings", {})
        settings["last_push"] = {
            "at": at,
            "results": results,
            "ok": sum(1 for r in results.values() if r.get("ok")),
            "fail": sum(1 for r in results.values() if not r.get("ok") and not r.get("skipped")),
            "skipped": sum(1 for r in results.values() if r.get("skipped")),
        }
        _save_data(data)
    except Exception as e:
        log.warning("Failed to save last_push: %s", e)
    return results
