"""Downstream keys & model routing.

A downstream key is a virtual API key that aggregates healthy upstream
vendor keys. Clients call AI Switch OpenAI/Anthropic-compatible endpoints
with the downstream secret; requests are routed to matching upstream
provider/key/model entries.

Routes auto-rebuild after health checks when auto_update=true:
- remove unhealthy / disabled upstreams
- add healthy upstreams whose models intersect selected_models
  (empty selected_models = all models from healthy keys)
"""
from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from core.data import (
    _load_data,
    _next_id,
    _save_data,
    get_enabled_models,
    get_key,
    get_vendor,
    get_vendors,
    list_model_ids,
)

log = logging.getLogger(__name__)
_lock = threading.Lock()

ENDPOINT_TYPES = ("openai", "anthropic")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_secret() -> str:
    return "sk-aiswitch-" + secrets.token_urlsafe(32)


def _norm_models(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
        raw = parts
    if not isinstance(raw, (list, tuple)):
        return []
    out, seen = [], set()
    for m in raw:
        s = str(m or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s[:200])
    return out[:500]


def _norm_endpoint_types(raw) -> list[str]:
    if raw is None:
        return ["openai"]
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.replace(";", ",").split(",")]
    if not isinstance(raw, (list, tuple)):
        return ["openai"]
    out = []
    for t in raw:
        s = str(t or "").lower().strip()
        if s in ("openai_chat", "openai-compatible"):
            s = "openai"
        if s in ("claude",):
            s = "anthropic"
        if s in ENDPOINT_TYPES and s not in out:
            out.append(s)
    return out or ["openai"]


def list_downstream_keys(*, include_secret: bool = False) -> list[dict]:
    data = _load_data()
    items = list(data.get("downstream_keys") or [])
    if include_secret:
        return items
    out = []
    for it in items:
        d = dict(it)
        sec = d.get("api_key") or ""
        d["api_key_preview"] = (sec[:12] + "…" + sec[-4:]) if len(sec) > 18 else (sec[:8] + "…")
        d.pop("api_key", None)
        out.append(d)
    return out


def get_downstream_key(key_id: str, *, include_secret: bool = False) -> Optional[dict]:
    kid = str(key_id or "")
    for it in (_load_data().get("downstream_keys") or []):
        if str(it.get("id")) == kid:
            d = dict(it)
            if not include_secret:
                sec = d.get("api_key") or ""
                d["api_key_preview"] = (sec[:12] + "…" + sec[-4:]) if len(sec) > 18 else (sec[:8] + "…")
                d.pop("api_key", None)
            return d
    return None


def find_downstream_by_secret(api_key: str) -> Optional[dict]:
    secret = (api_key or "").strip()
    if secret.lower().startswith("bearer "):
        secret = secret[7:].strip()
    if not secret:
        return None
    for it in (_load_data().get("downstream_keys") or []):
        if it.get("enabled") is False:
            continue
        if (it.get("api_key") or "") == secret:
            return dict(it)
    return None


def create_downstream_key(
    name: str,
    *,
    endpoint_types=None,
    selected_models=None,
    auto_update: bool = True,
    notes: str = "",
    enabled: bool = True,
) -> dict:
    with _lock:
        data = _load_data()
        items = data.setdefault("downstream_keys", [])
        entry = {
            "id": _next_id(items),
            "name": (name or "downstream").strip()[:80] or "downstream",
            "api_key": _gen_secret(),
            "enabled": bool(enabled),
            "endpoint_types": _norm_endpoint_types(endpoint_types),
            "selected_models": _norm_models(selected_models),
            "auto_update": bool(auto_update),
            "routes": [],
            "notes": str(notes or "")[:500],
            "created_at": _now(),
            "updated_at": _now(),
        }
        items.append(entry)
        _save_data(data)
    # rebuild outside lock
    return rebuild_downstream_routes(entry["id"]) or entry


def update_downstream_key(key_id: str, **kwargs) -> Optional[dict]:
    with _lock:
        data = _load_data()
        items = data.get("downstream_keys") or []
        found = None
        for it in items:
            if str(it.get("id")) == str(key_id):
                found = it
                break
        if not found:
            return None
        if kwargs.get("name") is not None:
            found["name"] = str(kwargs["name"]).strip()[:80] or found["name"]
        if "enabled" in kwargs and kwargs["enabled"] is not None:
            found["enabled"] = bool(kwargs["enabled"])
        if "auto_update" in kwargs and kwargs["auto_update"] is not None:
            found["auto_update"] = bool(kwargs["auto_update"])
        if "notes" in kwargs and kwargs["notes"] is not None:
            found["notes"] = str(kwargs.get("notes") or "")[:500]
        if "endpoint_types" in kwargs and kwargs["endpoint_types"] is not None:
            found["endpoint_types"] = _norm_endpoint_types(kwargs["endpoint_types"])
        if "selected_models" in kwargs and kwargs["selected_models"] is not None:
            found["selected_models"] = _norm_models(kwargs["selected_models"])
        if kwargs.get("rotate_secret"):
            found["api_key"] = _gen_secret()
        found["updated_at"] = _now()
        _save_data(data)
        kid = found["id"]
    if kwargs.get("rebuild", True) is not False:
        return rebuild_downstream_routes(kid)
    return get_downstream_key(kid, include_secret=True)


def delete_downstream_key(key_id: str) -> bool:
    with _lock:
        data = _load_data()
        items = data.get("downstream_keys") or []
        new_items = [it for it in items if str(it.get("id")) != str(key_id)]
        if len(new_items) == len(items):
            return False
        data["downstream_keys"] = new_items
        _save_data(data)
        return True


def _upstream_candidates() -> list[dict]:
    """List healthy/enabled upstream key+model inventory for routing."""
    try:
        from core.health_checker import get_all_health_status
        health = get_all_health_status() or {}
    except Exception:
        health = {}

    out = []
    for v in get_vendors():
        vid = str(v.get("id") or "")
        for k in v.get("keys") or []:
            if k.get("enabled") is False:
                continue
            if not k.get("api_key"):
                continue
            kid = str(k.get("id") or "")
            h = health.get(f"{vid}:{kid}") or {}
            # only route to known-healthy or not-yet-checked (treat unchecked as usable if enabled)
            if h.get("healthy") is False:
                continue
            models = get_enabled_models(k) or list_model_ids(k)
            if not models and h.get("models"):
                models = [
                    (m if isinstance(m, str) else (m.get("id") or m.get("name") or ""))
                    for m in (h.get("models") or [])
                ]
                models = [m for m in models if m]
            if not models:
                continue
            ep = (v.get("endpoint_type") or "openai").lower().strip()
            if ep in ("openai_chat",):
                ep = "openai"
            if ep in ("claude",):
                ep = "anthropic"
            out.append({
                "vendor_id": vid,
                "key_id": kid,
                "vendor_name": v.get("name") or "",
                "key_name": k.get("name") or "",
                "provider": v.get("provider") or "",
                "api_url": (v.get("proxy_target") or v.get("api_url") or "").rstrip("/"),
                "endpoint_type": ep or "openai",
                "models": models,
                "healthy": h.get("healthy"),
                "api_key": k.get("api_key"),
            })
    return out


def available_models_catalog() -> dict:
    """Models available from healthy/enabled upstreams (for multi-select UI)."""
    by_model: dict[str, dict] = {}
    for c in _upstream_candidates():
        for mid in c.get("models") or []:
            rec = by_model.setdefault(mid, {
                "model": mid,
                "sources": [],
                "endpoint_types": set(),
            })
            rec["sources"].append({
                "vendor_id": c["vendor_id"],
                "key_id": c["key_id"],
                "vendor_name": c["vendor_name"],
                "key_name": c["key_name"],
                "provider": c["provider"],
                "healthy": c.get("healthy"),
            })
            rec["endpoint_types"].add(c.get("endpoint_type") or "openai")
    items = []
    for mid, rec in by_model.items():
        items.append({
            "model": mid,
            "source_count": len(rec["sources"]),
            "sources": rec["sources"],
            "endpoint_types": sorted(rec["endpoint_types"]),
        })
    items.sort(key=lambda x: (-x["source_count"], str(x["model"]).lower()))
    return {"count": len(items), "models": items}


def rebuild_downstream_routes(key_id: str) -> Optional[dict]:
    """Rebuild active routes for one downstream key from health + selected models."""
    with _lock:
        data = _load_data()
        found = None
        for it in (data.get("downstream_keys") or []):
            if str(it.get("id")) == str(key_id):
                found = it
                break
        if not found:
            return None
        selected = list(found.get("selected_models") or [])
        auto = found.get("auto_update") is not False
        # even if auto_update is false, still prune dead routes when rebuild is forced
        candidates = _upstream_candidates()
        # strip secrets for stored routes
        routes = []
        model_map = {}  # model -> first route index
        for c in candidates:
            models = list(c.get("models") or [])
            if selected:
                models = [m for m in models if m in selected]
            if not models:
                continue
            route = {
                "vendor_id": c["vendor_id"],
                "key_id": c["key_id"],
                "vendor_name": c["vendor_name"],
                "key_name": c["key_name"],
                "provider": c["provider"],
                "api_url": c["api_url"],
                "endpoint_type": c.get("endpoint_type") or "openai",
                "models": models,
                "healthy": c.get("healthy"),
            }
            routes.append(route)
            for m in models:
                model_map.setdefault(m, len(routes) - 1)

        if not auto:
            # keep only routes that still exist among candidates and still match selection
            # (manual mode: prune unhealthy, don't invent new sources beyond previous set)
            prev = {(r.get("vendor_id"), r.get("key_id")) for r in (found.get("routes") or [])}
            routes = [r for r in routes if (r.get("vendor_id"), r.get("key_id")) in prev]

        found["routes"] = routes
        found["route_model_count"] = len(model_map)
        found["route_source_count"] = len(routes)
        found["updated_at"] = _now()
        _save_data(data)
        out = dict(found)
    return out


def rebuild_all_downstream_routes() -> dict:
    """Rebuild every auto_update downstream key. Called after health checks."""
    ids = []
    data = _load_data()
    for it in (data.get("downstream_keys") or []):
        if it.get("auto_update") is False:
            continue
        ids.append(str(it.get("id")))
    rebuilt = 0
    for kid in ids:
        if rebuild_downstream_routes(kid):
            rebuilt += 1
    return {"rebuilt": rebuilt, "total": len(ids)}


def resolve_route(downstream: dict, model: str, *, endpoint_type: str = "openai") -> Optional[dict]:
    """Pick an upstream route for model + endpoint type. Returns live secret included."""
    model = (model or "").strip()
    if not model:
        return None
    ep = (endpoint_type or "openai").lower().strip()
    routes = list(downstream.get("routes") or [])
    # prefer routes whose endpoint_type matches request protocol when possible
    ordered = sorted(
        routes,
        key=lambda r: (0 if (r.get("endpoint_type") or "openai") == ep else 1),
    )
    for r in ordered:
        models = r.get("models") or []
        if model not in models:
            # allow suffix match: provider/model vs model
            if not any(str(m) == model or str(m).endswith("/" + model) or model.endswith("/" + str(m)) for m in models):
                continue
        # load live secret
        v = get_vendor(str(r.get("vendor_id")))
        k = get_key(str(r.get("vendor_id")), str(r.get("key_id"))) if v else None
        if not v or not k or not k.get("api_key") or k.get("enabled") is False:
            continue
        return {
            "vendor": v,
            "key": k,
            "route": r,
            "api_url": (v.get("proxy_target") or v.get("api_url") or r.get("api_url") or "").rstrip("/"),
            "endpoint_type": (v.get("endpoint_type") or r.get("endpoint_type") or "openai"),
            "model": model,
        }
    return None


def list_models_for_downstream(downstream: dict) -> list[dict]:
    """Flatten models exposed by a downstream key."""
    seen = set()
    out = []
    for r in downstream.get("routes") or []:
        for m in r.get("models") or []:
            if m in seen:
                continue
            seen.add(m)
            out.append({
                "id": m,
                "object": "model",
                "owned_by": r.get("provider") or "ai-switch",
                "vendor_id": r.get("vendor_id"),
                "key_id": r.get("key_id"),
            })
    out.sort(key=lambda x: str(x["id"]).lower())
    return out
