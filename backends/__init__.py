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


def reconcile_all() -> None:
    for adapter in _adapters.values():
        try:
            adapter.reconcile()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Backend %s reconcile failed: %s", adapter.name, e)
