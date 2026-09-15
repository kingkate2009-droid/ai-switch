import importlib
import pkgutil
import threading
import time
from typing import Optional

from backends.base import BackendAdapter

_adapters: dict[str, BackendAdapter] = {}
_reconcile_lock = threading.Lock()
_reconcile_request_lock = threading.Lock()
_reconcile_requested = False
_reconcile_worker: Optional[threading.Thread] = None
_pending_vendor_ids: list = []
_pending_key_ids: list = []

# Thread-local reconcile scope: when set (list of vendor ids), only those
# vendors are written to backends. None = all vendors (default).
# key_ids: when set (list of "vendor_id:key_id"), only those specific keys
# are written. None = all keys for the scoped vendors.
_tls = threading.local()


def _scope_vendor_ids() -> Optional[set]:
    return getattr(_tls, "vendor_ids", None)


def _scope_key_ids() -> Optional[set]:
    return getattr(_tls, "key_ids", None)


def register(adapter: BackendAdapter) -> None:
    _adapters[adapter.name] = adapter


def get(name: str) -> Optional[BackendAdapter]:
    return _adapters.get(name)


def get_all() -> dict[str, BackendAdapter]:
    return dict(_adapters)


def reconcile_all_async(vendor_ids=None, key_ids=None) -> None:
    """Coalesce background reconcile requests so API writes can return quickly.

    ``vendor_ids``: when given, only those vendors are written (scoped push).
    ``key_ids``: when given (list of "vid:kid"), only those keys are written.
    """
    global _reconcile_requested, _reconcile_worker, _pending_vendor_ids, _pending_key_ids
    with _reconcile_request_lock:
        ids = [str(v) for v in (vendor_ids or []) if v]
        kid_list = [str(k) for k in (key_ids or []) if k]
        _reconcile_requested = True
        if _reconcile_worker and _reconcile_worker.is_alive():
            # An unscoped follow-up supersedes an earlier narrow request.
            if not ids:
                _pending_vendor_ids = None
            elif _pending_vendor_ids is not None:
                _pending_vendor_ids.extend(ids)
            # An unscoped follow-up supersedes an earlier narrow request.
            if not kid_list:
                _pending_key_ids = None
            elif _pending_key_ids is not None:
                _pending_key_ids.extend(kid_list)
            return

        def _worker() -> None:
            global _reconcile_requested, _reconcile_worker, _pending_vendor_ids, _pending_key_ids
            while True:
                with _reconcile_request_lock:
                    if not _reconcile_requested:
                        _reconcile_worker = None
                        return
                    _reconcile_requested = False
                    batch = None if _pending_vendor_ids is None else list(_pending_vendor_ids)
                    _pending_vendor_ids = []
                    key_batch = None if _pending_key_ids is None else list(_pending_key_ids)
                    _pending_key_ids = []
                # A later request may be broader than the first one. In that
                # case do not retain the first request's key restriction.
                reconcile_all(
                    vendor_ids=None if batch is None else (batch or None),
                    key_ids=None if key_batch is None else (key_batch or kid_list or None),
                )

        _pending_vendor_ids = ids
        _pending_key_ids = kid_list
        _reconcile_worker = threading.Thread(
            target=_worker,
            name="ai-switch-backend-reconcile",
            daemon=True,
        )
        _reconcile_worker.start()


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
    """Adapters that may receive add/update for this vendor+key (healthy/syncable only)."""
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


def _removal_adapters(vendor: dict, key: dict) -> list[BackendAdapter]:
    """Adapters that must receive remove events.

    Unlike add/update, removal must NOT require should_sync/health — otherwise
    unhealthy / disabled keys can never be stripped from engine configs
    (should_sync returns False → on_key_removed becomes a no-op).
    Still respect: supports_byok, backend disabled, not installed, sync_vendors.
    """
    from core.data import get_backend_config
    result = []
    for adapter in _adapters.values():
        try:
            if not getattr(adapter, "supports_byok", True):
                continue
            config = get_backend_config(adapter.name) or {}
            if config.get("disabled"):
                continue
            try:
                if not adapter.is_installed():
                    continue
            except Exception:
                continue
            sync = config.get("sync_vendors", "all")
            if isinstance(sync, list):
                if vendor.get("provider") not in sync and vendor.get("id") not in sync:
                    continue
            result.append(adapter)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Backend %s removal filter failed: %s", adapter.name, e
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
    # Always deliver removals even when key is unhealthy/disabled (see _removal_adapters)
    for adapter in _removal_adapters(vendor, key):
        try:
            adapter.on_key_removed(vendor, key)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Backend %s on_key_removed failed: %s", adapter.name, e)


def on_vendor_removed(vendor: dict) -> None:
    for k in vendor.get("keys", []):
        on_key_removed(vendor, k)


def preview_push_all() -> dict:
    """Dry-run: which backends would be written / skipped on push (no file writes).

    Also reports how many keys are healthy/syncable so the UI is not mistaken
    for a per-key list (rows are backends, not keys).
    """
    from core.data import get_backend_config, get_vendors, list_model_ids
    from core.health_checker import _load_cache, _lock, _parse_iso, get_all_health_status
    from core.endpoints import model_is_verified_usable
    from datetime import datetime, timezone, timedelta

    health = {}
    try:
        health = get_all_health_status() or {}
    except Exception:
        health = {}

    # Load health cache once (is_key_backend_syncable re-reads the file each call).
    with _lock:
        cache = dict(_load_cache() or {})

    vendors = get_vendors()
    total_keys = enabled_keys = healthy_keys = syncable_keys = 0
    syncable_samples = []
    now = datetime.now(timezone.utc)
    for v in vendors:
        vid = str(v.get("id") or "")
        for k in v.get("keys") or []:
            total_keys += 1
            if k.get("enabled") is False or not k.get("api_key"):
                continue
            enabled_keys += 1
            h = cache.get(f"{vid}:{k.get('id')}") or health.get(f"{vid}:{k.get('id')}") or {}
            if h.get("healthy") is True:
                healthy_keys += 1
            # Inline syncable check without reloading the cache file
            if h.get("healthy") is not True:
                continue
            checked = _parse_iso(h.get("checked_at") or "")
            if checked is None:
                continue
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            if now - checked > timedelta(hours=24):
                continue
            try:
                usable = any(model_is_verified_usable(k, mid) for mid in list_model_ids(k)[:20])
            except Exception:
                usable = False
            if not usable:
                continue
            syncable_keys += 1
            if len(syncable_samples) < 8:
                syncable_samples.append({
                    "vendor": v.get("name") or v.get("provider") or vid,
                    "key": k.get("name") or k.get("id"),
                    "models": len(list_model_ids(k)),
                })

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
            # Per-backend filtering (should_sync) is expensive; show pool size instead.
            "key_count": syncable_keys if action == "write" else 0,
        })
    items.sort(key=lambda x: (0 if x.get("action") == "write" else 1, str(x.get("display_name") or "").lower()))
    return {
        "items": items,
        "will_write": will_write,
        "skipped": skipped,
        "total": len(items),
        "keys": {
            "total": total_keys,
            "enabled": enabled_keys,
            "healthy": healthy_keys,
            "syncable": syncable_keys,
            "samples": syncable_samples,
        },
    }


def _run_adapter_reconcile(
    adapter: BackendAdapter,
    *,
    timeout_seconds: float = 90.0,
    vendor_ids=None,
    key_ids=None,
):
    """Run one adapter.reconcile with a wall-clock timeout (thread + join)."""
    box: dict = {"runtime": None, "error": None}

    def _target() -> None:
        prev_vendor_scope = getattr(_tls, "vendor_ids", None)
        prev_key_scope = getattr(_tls, "key_ids", None)
        vendor_scope = {str(v) for v in (vendor_ids or []) if v}
        key_scope = {str(k) for k in (key_ids or []) if k}
        _tls.vendor_ids = vendor_scope or None
        _tls.key_ids = key_scope or None
        try:
            box["runtime"] = adapter.reconcile()
        except Exception as exc:
            box["error"] = exc
        finally:
            if prev_vendor_scope is None:
                if hasattr(_tls, "vendor_ids"):
                    delattr(_tls, "vendor_ids")
            else:
                _tls.vendor_ids = prev_vendor_scope
            if prev_key_scope is None:
                if hasattr(_tls, "key_ids"):
                    delattr(_tls, "key_ids")
            else:
                _tls.key_ids = prev_key_scope

    t = threading.Thread(target=_target, name=f"reconcile-{adapter.name}", daemon=True)
    t.start()
    t.join(timeout=max(5.0, float(timeout_seconds or 90.0)))
    if t.is_alive():
        raise TimeoutError(f"reconcile timed out after {int(timeout_seconds)}s")
    if box["error"] is not None:
        raise box["error"]
    return box["runtime"]


def reconcile_all(*, timeout_per_backend: float = 120.0, vendor_ids=None, key_ids=None) -> dict:
    """Push system keys to backends. Returns per-backend result summary.

    Each installed backend is bounded by ``timeout_per_backend`` so one slow
    engine (OpenClaw rewrite, hung CLI) cannot block health-check completion
    forever.  Overlapping pushes still serialize on ``_reconcile_lock``.

    ``vendor_ids``: when given (list of vendor ids), only those vendors are
    written. None/empty = all vendors.
    ``key_ids``: when given (list of "vendor_id:key_id"), only those specific
    keys are written. None/empty = all keys (subject to vendor_ids scope).
    """
    from datetime import datetime, timezone
    from core.data import get_backend_config, _load_data, _save_data
    from core.health_checker import get_health_cache_snapshot, invalidate_health_cache_snapshot
    import logging
    log = logging.getLogger(__name__)
    # Flask serves requests concurrently. A full reconcile writes several
    # files per adapter, so overlapping pushes can otherwise interleave and
    # leave a backend with a mixture of two system snapshots.
    with _reconcile_lock:
        # Scoped pushes must not leak their scope into a concurrent full push.
        prev_vendor_scope = getattr(_tls, "vendor_ids", None)
        prev_key_scope = getattr(_tls, "key_ids", None)
        ids = [str(v) for v in (vendor_ids or []) if v]
        kid_list = [str(k) for k in (key_ids or []) if k]
        _tls.vendor_ids = set(ids) if ids else None
        _tls.key_ids = set(kid_list) if kid_list else None
        try:
            # Fresh health snapshot shared by all adapters this round
            invalidate_health_cache_snapshot()
            health_snap = get_health_cache_snapshot(max_age=0)
            results = {}
            at = datetime.now(timezone.utc).isoformat()
            last_sync_by_name = {}
            for adapter in _adapters.values():
                name = adapter.name
                try:
                    # Share snapshot with should_sync / is_key_backend_syncable
                    try:
                        adapter._health_cache_snap = health_snap
                        # Reset per-reconcile installed cache so is_installed() re-checks fresh
                        try:
                            adapter._clear_installed_cache()
                        except Exception:
                            pass
                    except Exception:
                        pass
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
                    # Slow writers get a higher budget; still fail closed on hang.
                    budget = float(timeout_per_backend or 120.0)
                    if name in ("openclaw", "opencode", "codex-cli", "kimi-code", "hermes", "qwencode"):
                        budget = max(budget, 300.0)
                    started = time.monotonic()
                    runtime = _run_adapter_reconcile(
                        adapter,
                        timeout_seconds=budget,
                        vendor_ids=ids,
                        key_ids=kid_list,
                    )
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    results[name] = {"ok": True, "skipped": False, "error": None, "duration_ms": elapsed_ms}
                    if isinstance(runtime, dict):
                        results[name].update({
                            key: runtime[key]
                            for key in ("runtime_applied", "runtime_message")
                            if key in runtime
                        })
                except Exception as e:
                    log.warning("Backend %s reconcile failed: %s", name, e)
                    results[name] = {"ok": False, "skipped": False, "error": str(e)[:300]}
                finally:
                    try:
                        if hasattr(adapter, "_health_cache_snap"):
                            delattr(adapter, "_health_cache_snap")
                        if hasattr(adapter, "_is_installed_cache"):
                            delattr(adapter, "_is_installed_cache")
                    except Exception:
                        pass
                r = results.get(name) or {}
                last_sync_by_name[name] = {
                    "at": at,
                    "ok": bool(r.get("ok")),
                    "skipped": bool(r.get("skipped")),
                    "error": r.get("error"),
                    "duration_ms": r.get("duration_ms"),
                }
            # One DB write for all last_sync + last_push (was N full saves before)
            try:
                data = _load_data()
                backends = data.setdefault("backends", {})
                for name, ls in last_sync_by_name.items():
                    bcfg = dict(backends.get(name) or {})
                    bcfg["last_sync"] = ls
                    backends[name] = bcfg
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
        finally:
            if prev_vendor_scope is None:
                if hasattr(_tls, "vendor_ids"):
                    delattr(_tls, "vendor_ids")
            else:
                _tls.vendor_ids = prev_vendor_scope
            if prev_key_scope is None:
                if hasattr(_tls, "key_ids"):
                    delattr(_tls, "key_ids")
            else:
                _tls.key_ids = prev_key_scope
