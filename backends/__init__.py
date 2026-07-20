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


def reconcile_all() -> dict:
    """Push system keys to backends. Returns per-backend result summary."""
    from datetime import datetime, timezone
    from core.data import get_backend_config, _load_data, _save_data
    import logging
    log = logging.getLogger(__name__)
    results = {}
    for adapter in _adapters.values():
        name = adapter.name
        try:
            cfg = get_backend_config(name)
            if cfg.get("disabled"):
                results[name] = {"ok": False, "skipped": True, "error": "disabled"}
                continue
            adapter.reconcile()
            results[name] = {"ok": True, "skipped": False, "error": None}
        except Exception as e:
            log.warning("Backend %s reconcile failed: %s", name, e)
            results[name] = {"ok": False, "skipped": False, "error": str(e)[:300]}
    # persist last push summary in settings
    try:
        data = _load_data()
        settings = data.setdefault("settings", {})
        settings["last_push"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "results": results,
            "ok": sum(1 for r in results.values() if r.get("ok")),
            "fail": sum(1 for r in results.values() if not r.get("ok") and not r.get("skipped")),
            "skipped": sum(1 for r in results.values() if r.get("skipped")),
        }
        _save_data(data)
    except Exception as e:
        log.warning("Failed to save last_push: %s", e)
    return results
