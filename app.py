import json
import logging
import os
import sys
import threading
import traceback
from datetime import datetime

import requests as py_requests
from flask import Flask, Response, jsonify, render_template, request, make_response

from core.batch_import import parse_batch_text
from core.providers import get_providers, recognize_provider
from core.data import (
    add_key,
    # dedupe/snapshot helpers used below
    add_vendor,
    delete_key,
    delete_profile,
    delete_vendor,
    failover_primary,
    get_enabled_models,
    get_key,
    get_keys,
    get_models_catalog,
    get_settings,
    get_vendor,
    get_vendors,
    is_read_only,
    list_all_tags,
    list_checkin_vendors,
    list_model_ids,
    list_profiles,
    promote_key,
    save_profile,
    set_model_enabled,
    switch_profile,
    update_key,
    update_key_data,
    update_settings,
    update_vendor,
)
from core.health_checker import (
    apply_health_to_backends,
    check_all_keys,
    check_key_health,
    check_key_models,
    get_all_health_status,
)
from core.i18n import SUPPORTED_LANGS, get_translations, resolve_lang, t as _t
from core.audit import log_event, list_events
from backends import init_backends, get as get_backend, get_all as get_all_backends, \
    on_key_added, on_key_updated, on_key_removed, on_vendor_removed, reconcile_all

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[ai-switch] %(levelname)s %(message)s")
log = logging.getLogger(__name__)

def _create_app() -> Flask:
    """Create Flask app with correct template/static roots for frozen binaries."""
    try:
        from core.paths import resource_root
        root = resource_root()
        return Flask(
            __name__,
            template_folder=str(root / "templates"),
            static_folder=str(root / "static"),
            static_url_path="/static",
        )
    except Exception:
        return Flask(__name__)


app = _create_app()

init_backends()

try:
    from core.scheduler import start_scheduler
    start_scheduler()
except Exception as _e:
    log.warning("Scheduler not started: %s", _e)

# Optional local access token (empty = disabled)
_AUTH_EXEMPT_PREFIXES = (
    "/api/version",
    "/api/auth/",
    "/static/",
    "/favicon.ico",
)


def _get_access_token() -> str:
    try:
        from core.data import get_settings
        return str((get_settings() or {}).get("access_token") or "").strip()
    except Exception:
        return ""


@app.before_request
def _check_access_token():
    token = _get_access_token()
    if not token:
        return None
    path = request.path or ""
    if path == "/" or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return None
    # cookie or header
    provided = request.cookies.get("aiswitch_token") or request.headers.get("X-AI-Switch-Token") or ""
    if provided == token:
        return None
    # also allow Authorization: Bearer <token>
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer ") and auth[7:].strip() == token:
        return None
    if path.startswith("/api/"):
        return jsonify({"error": "Unauthorized", "auth_required": True}), 401
    # HTML: still serve shell so login UI can work; APIs stay protected
    return None


# Mutating methods blocked when read_only=true (except settings/auth/lang)
_READ_ONLY_EXEMPT_PREFIXES = (
    "/api/settings",
    "/api/auth/",
    "/api/lang",
    "/api/version",
    "/api/diagnostics",
    "/api/audit",
    "/api/pricing",  # GET only in practice; POST still blocked via method check below for pricing write? allow pricing write only via settings
)


@app.before_request
def _check_read_only():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    # always allow auth + settings (to turn off read_only) + lang + encrypted backup export
    if (
        path.startswith("/api/auth/")
        or path.startswith("/api/settings")
        or path.startswith("/api/lang")
        or path == "/api/backup/export"
    ):
        return None
    try:
        if not is_read_only():
            return None
    except Exception:
        return None
    return jsonify({
        "error": "Read-only mode is enabled. Disable it in Settings to make changes.",
        "read_only": True,
    }), 403


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(500)
def handle_500(e):
    log.error("Server error: %s", traceback.format_exc())
    return jsonify({"error": "internal server error"}), 500


def _current_lang():
    return resolve_lang(
        request.headers.get("Accept-Language"),
        request.cookies.get("lang"),
    )


@app.route("/")
def index():
    lang = _current_lang()
    all_locales = {l: get_translations(l) for l in SUPPORTED_LANGS}
    merged = {**all_locales["en"], **all_locales[lang]}
    resp = make_response(render_template(
        "index.html",
        lang=lang,
        supported_langs=SUPPORTED_LANGS,
        t=lambda key: merged.get(key, key),
        all_locales=all_locales,
    ))
    resp.set_cookie("lang", lang, max_age=86400 * 365)
    return resp


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/api/version", methods=["GET"])
def api_version():
    from core.version import get_version
    import platform
    import sys
    token_set = bool(_get_access_token())
    return jsonify({
        "version": get_version(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "auth_required": token_set,
    })


@app.route("/api/auth/status", methods=["GET"])
def api_auth_status():
    token = _get_access_token()
    provided = request.cookies.get("aiswitch_token") or request.headers.get("X-AI-Switch-Token") or ""
    ok = (not token) or (provided == token)
    return jsonify({"auth_required": bool(token), "authenticated": ok})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    token = _get_access_token()
    if not token:
        return jsonify({"success": True, "auth_required": False})
    data = request.get_json(silent=True) or {}
    provided = str(data.get("token") or "").strip()
    if provided != token:
        return jsonify({"success": False, "error": "Invalid token"}), 401
    resp = jsonify({"success": True, "auth_required": True})
    resp.set_cookie("aiswitch_token", token, max_age=86400 * 30, httponly=True, samesite="Lax")
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    resp = jsonify({"success": True})
    resp.set_cookie("aiswitch_token", "", max_age=0)
    return resp


@app.route("/api/diagnostics", methods=["GET"])
def api_diagnostics():
    from core.diagnostics import collect_diagnostics
    for_issue = request.args.get("for_issue", "0") in ("1", "true", "yes")
    pack = collect_diagnostics(for_issue=for_issue)
    if request.args.get("download", "0") in ("1", "true", "yes"):
        import json as _json
        body = _json.dumps(pack, ensure_ascii=False, indent=2)
        from core.version import get_version
        fname = f"ai-switch-diagnostics-v{get_version()}.json"
        resp = app.response_class(body, mimetype="application/json; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    return jsonify(pack)


@app.route("/api/updates/check", methods=["GET"])
def api_updates_check():
    """Compare local version with latest GitHub release (best-effort)."""
    from core.version import get_version
    import re
    local = (get_version() or "").strip().lstrip("v")
    repo = "kingkate2009-droid/ai-switch"
    latest = ""
    notes = ""
    html_url = f"https://github.com/{repo}/releases/latest"
    error = ""
    try:
        r = py_requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=8,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-switch-update-check"},
        )
        if r.status_code == 200:
            data = r.json() or {}
            latest = str(data.get("tag_name") or data.get("name") or "").strip().lstrip("v")
            notes = str(data.get("body") or "")[:2000]
            html_url = str(data.get("html_url") or html_url)
        else:
            error = f"GitHub HTTP {r.status_code}"
    except Exception as e:
        error = str(e)[:200]

    def _parts(v: str):
        nums = [int(x) for x in re.findall(r"\d+", v or "")]
        while len(nums) < 3:
            nums.append(0)
        return tuple(nums[:3])

    update_available = False
    if latest and local:
        try:
            update_available = _parts(latest) > _parts(local)
        except Exception:
            update_available = latest != local

    return jsonify({
        "local": local,
        "latest": latest,
        "update_available": update_available,
        "html_url": html_url,
        "notes": notes,
        "error": error,
        "repo": repo,
    })


@app.route("/api/audit", methods=["GET"])
def api_audit_log():
    limit = min(int(request.args.get("limit", "100") or 100), 500)
    ev = list_events(limit=limit)
    return jsonify({"events": ev, "count": len(ev)})


# ── Providers ──────────────────────────────────────────────

@app.route("/api/providers", methods=["GET"])
def api_list_providers():
    return jsonify({"providers": get_providers()})


@app.route("/api/providers/recognize", methods=["POST"])
def api_recognize_provider():
    data = request.get_json() or {}
    text = data.get("text", "")
    matched = recognize_provider(text)
    if matched:
        return jsonify(matched)
    return jsonify(None)


# ── Backends ───────────────────────────────────────────────

@app.route("/api/backends", methods=["GET"])
def api_list_backends():
    from core.data import get_backend_config
    backends = []
    for name, adapter in get_all_backends().items():
        backends.append({
            "name": adapter.name,
            "display_name": adapter.display_name,
            "status": adapter.get_status(),
            "version": adapter.get_version(),
            "config_files": adapter.config_files,
            "supports_byok": adapter.supports_byok,
            "supports_active_switch": bool(getattr(adapter, "supports_active_switch", False)),
            "config": get_backend_config(adapter.name),
        })
    return jsonify({"backends": backends})


@app.route("/api/backends/<name>", methods=["GET"])
def api_get_backend(name):
    adapter = get_backend(name)
    if not adapter:
        return jsonify({"error": "not found"}), 404
    from core.data import get_backend_config
    config = get_backend_config(adapter.name)
    return jsonify({
        "name": adapter.name,
        "display_name": adapter.display_name,
        "status": adapter.get_status(),
        "version": adapter.get_version(),
        "config_files": adapter.config_files,
        "supports_byok": adapter.supports_byok,
        "supports_active_switch": bool(getattr(adapter, "supports_active_switch", False)),
        "config": config,
    })


@app.route("/api/backends/<name>/sync-config", methods=["PUT"])
def api_save_backend_sync_config(name):
    adapter = get_backend(name)
    if not adapter:
        return jsonify({"error": "not found"}), 404
    body = request.get_json() or {}
    from core.data import get_backend_config, save_backend_config
    config = get_backend_config(adapter.name)
    if "disabled" in body:
        config["disabled"] = body["disabled"]
    if "sync_vendors" in body:
        config["sync_vendors"] = body["sync_vendors"]
    save_backend_config(adapter.name, config)
    return jsonify({"success": True, "config": config})


@app.route("/api/backends/<name>/config", methods=["GET"])
def api_read_backend_config(name):
    adapter = get_backend(name)
    if not adapter:
        return jsonify({"error": "not found"}), 404
    file_path = request.args.get("path", "")
    if not file_path:
        return jsonify({"error": "path query parameter required"}), 400
    path = os.path.expanduser(file_path)
    valid = any(os.path.expanduser(cf["path"]) == path for cf in adapter.config_files)
    if not valid:
        return jsonify({"error": "invalid config file path"}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "file not found", "path": file_path}), 404
    try:
        with open(path) as f:
            content = f.read()
        return jsonify({"path": file_path, "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backends/<name>/config", methods=["PUT"])
def api_write_backend_config(name):
    adapter = get_backend(name)
    if not adapter:
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    file_path = data.get("path", "")
    content = data.get("content", "")
    if not file_path:
        return jsonify({"error": "path is required"}), 400
    path = os.path.expanduser(file_path)
    valid = any(os.path.expanduser(cf["path"]) == path for cf in adapter.config_files)
    if not valid:
        return jsonify({"error": "invalid config file path"}), 400
    try:
        # Backup
        if os.path.isfile(path):
            backup = path + ".bak"
            import shutil
            shutil.copy2(path, backup)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return jsonify({"success": True, "path": file_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Active provider switch (Codex / OpenCode / Claude Code / …) ──

@app.route("/api/backends/<name>/providers", methods=["GET"])
def api_backend_list_providers(name):
    """List switchable providers/slots for a backend that supports active switch."""
    adapter = get_backend(name)
    if not adapter:
        return jsonify({"error": "backend not found"}), 404
    if not getattr(adapter, "supports_active_switch", False):
        return jsonify({"error": "active switch not supported", "supports_active_switch": False}), 400
    try:
        providers = adapter.list_providers() or []
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    try:
        active = adapter.get_active_provider() or {}
    except Exception:
        active = {}
    return jsonify({
        "backend": name,
        "supports_active_switch": True,
        "providers": providers,
        "active_provider": active.get("active_provider", ""),
        "active": active,
    })


@app.route("/api/backends/<name>/switch-provider", methods=["POST"])
def api_backend_switch_provider(name):
    """Switch active provider/slot.

    Body: { "provider_id": "..." } or { "vendor_id": "...", "key_id": "optional" }
    """
    adapter = get_backend(name)
    if not adapter:
        return jsonify({"error": "backend not found"}), 404
    if not getattr(adapter, "supports_active_switch", False):
        return jsonify({"error": "active switch not supported", "supports_active_switch": False}), 400

    body = request.get_json() or {}
    provider_id = body.get("provider_id", "")
    vendor_id = body.get("vendor_id", "")
    key_id = body.get("key_id", "")
    if not provider_id and not vendor_id:
        return jsonify({"error": "provider_id or vendor_id required"}), 400

    try:
        result = adapter.switch_provider(
            provider_id=provider_id,
            vendor_id=vendor_id,
            key_id=key_id,
        ) or {}
    except Exception as e:
        log.exception("switch_provider failed for %s", name)
        return jsonify({"success": False, "error": str(e)}), 500

    if result.get("success"):
        log_event("backend.switch_provider", backend=name, provider=result.get("active_provider"))
        return jsonify(result)
    return jsonify(result if "error" in result else {**result, "error": result.get("message") or "switch failed"}), 400


# Legacy Codex aliases
@app.route("/api/backends/codex-cli/providers", methods=["GET"])
def api_codex_list_providers():
    return api_backend_list_providers("codex-cli")


@app.route("/api/backends/codex-cli/switch-provider", methods=["POST"])
def api_codex_switch_provider():
    return api_backend_switch_provider("codex-cli")


# ── Vendors ────────────────────────────────────────────────



@app.route("/api/vendors/export", methods=["GET"])
def api_vendors_export_csv():
    """Export all vendors and keys as CSV (secrets included — local tool)."""
    import csv
    import io
    from core.data import get_vendors
    from core.health_checker import get_all_health_status
    from core.audit import log_event

    fmt = (request.args.get("format") or "csv").lower()
    vendors = get_vendors()
    health = get_all_health_status()
    rows = []
    for v in vendors:
        for k in v.get("keys") or []:
            h = health.get(f"{v.get('id')}:{k.get('id')}") or {}
            rows.append({
                "vendor_id": v.get("id"),
                "vendor_name": v.get("name"),
                "provider": v.get("provider"),
                "api_url": v.get("api_url"),
                "endpoint_type": v.get("endpoint_type"),
                "key_id": k.get("id"),
                "key_name": k.get("name"),
                "api_key": k.get("api_key"),
                "enabled": "1" if k.get("enabled", True) else "0",
                "default_model": k.get("default_model") or "",
                "models": ",".join(
                    (m.get("id") if isinstance(m, dict) else str(m))
                    for m in (k.get("models") or [])
                ),
                "healthy": "" if h.get("healthy") is None else ("1" if h.get("healthy") else "0"),
                "latency_ms": h.get("latency_ms") or "",
                "error": (h.get("error") or "")[:300],
                "checked_at": h.get("checked_at") or "",
            })
    log_event("vendors.export", format=fmt, rows=len(rows))
    if fmt == "json":
        body = json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False, indent=2)
        resp = make_response(body)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = "attachment; filename=vendors-keys.json"
        return resp

    fields = [
        "vendor_id", "vendor_name", "provider", "api_url", "endpoint_type",
        "key_id", "key_name", "api_key", "enabled", "default_model", "models",
        "healthy", "latency_ms", "error", "checked_at",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=vendors-keys.csv"
    return resp


@app.route("/api/vendors", methods=["GET"])
def api_list_vendors():
    vendors = get_vendors()
    health = get_all_health_status()
    return jsonify({"vendors": vendors, "health": health})


@app.route("/api/vendors", methods=["POST"])
def api_create_vendor():
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"error": "name is required"}), 400
    v = add_vendor(
        name=data["name"],
        provider=data.get("provider", "custom"),
        api_url=data.get("api_url", "https://api.openai.com/v1"),
        endpoint_type=data.get("endpoint_type", "openai"),
        thinking_disabled=data.get("thinking_disabled", False),
        proxy_target=data.get("proxy_target", ""),
        tags=data.get("tags"),
        checkin_url=data.get("checkin_url", ""),
    )
    log_event("vendor.add", vendor_id=v.get("id"), name=v.get("name"), provider=v.get("provider"))
    return jsonify(v), 201


@app.route("/api/vendors/<vendor_id>", methods=["PUT"])
def api_update_vendor(vendor_id):
    data = request.get_json() or {}
    # only pass known fields
    allowed = {k: data[k] for k in (
        "name", "provider", "api_url", "endpoint_type",
        "thinking_disabled", "proxy_target", "tags", "checkin_url",
    ) if k in data}
    v = update_vendor(vendor_id, **allowed)
    if not v:
        return jsonify({"error": "not found"}), 404
    return jsonify(v)


@app.route("/api/tags", methods=["GET"])
def api_list_tags():
    return jsonify({"tags": list_all_tags()})


@app.route("/api/checkin/vendors", methods=["GET"])
def api_checkin_vendors():
    """List vendors for check-in page.

    Query:
      vendor_id: optional filter by vendor id
      support: all | yes | no  (whether checkin_url is set)
    """
    vendor_id = (request.args.get("vendor_id") or "").strip()
    support = (request.args.get("support") or "all").strip().lower()
    if support not in ("all", "yes", "no"):
        support = "all"
    items = list_checkin_vendors(vendor_id=vendor_id, support=support)
    return jsonify({
        "vendors": items,
        "total": len(items),
        "supported": sum(1 for x in items if x.get("supports_checkin")),
        "unsupported": sum(1 for x in items if not x.get("supports_checkin")),
    })


@app.route("/api/models/catalog", methods=["GET"])
def api_models_catalog():
    return jsonify(get_models_catalog())


@app.route("/api/vendors/<vendor_id>", methods=["DELETE"])
def api_delete_vendor(vendor_id):
    v = get_vendor(vendor_id)
    if v:
        on_vendor_removed(v)
    if not delete_vendor(vendor_id):
        return jsonify({"error": "not found"}), 404
    log_event("vendor.delete", vendor_id=vendor_id, name=(v or {}).get("name"))
    return jsonify({"success": True})




@app.route("/api/vendors/empty", methods=["GET"])
def api_empty_vendors_preview():
    from core.data import find_empty_vendors
    items = find_empty_vendors()
    return jsonify({"count": len(items), "items": items})


@app.route("/api/vendors/empty", methods=["POST"])
def api_empty_vendors_delete():
    from core.data import delete_empty_vendors
    body = request.get_json(silent=True) or {}
    dry = body.get("dry_run", False)
    result = delete_empty_vendors(dry_run=bool(dry))
    if not dry:
        log_event("vendors.clean_empty", count=result.get("count", 0))
    return jsonify(result)


@app.route("/api/vendors/merge-urls", methods=["GET"])
def api_vendors_merge_urls_preview():
    from core.data import merge_duplicate_vendors_by_url
    return jsonify(merge_duplicate_vendors_by_url(dry_run=True))


@app.route("/api/vendors/merge-urls", methods=["POST"])
def api_vendors_merge_urls_apply():
    from core.data import merge_duplicate_vendors_by_url
    result = merge_duplicate_vendors_by_url(dry_run=False)
    log_event(
        "vendors.merge_urls",
        groups=result.get("groups", 0),
        merged=result.get("merged_vendors", 0),
        keys_moved=result.get("keys_moved", 0),
    )
    return jsonify(result)


# ── Keys ───────────────────────────────────────────────────

@app.route("/api/vendors/<vendor_id>/keys", methods=["GET"])
def api_list_keys(vendor_id):
    keys = get_keys(vendor_id)
    if keys is None:
        return jsonify({"error": "vendor not found"}), 404
    return jsonify({"keys": keys})


@app.route("/api/vendors/<vendor_id>/keys", methods=["POST"])
def api_create_key(vendor_id):
    data = request.get_json()
    if not data or not data.get("name") or not data.get("api_key"):
        return jsonify({"error": "name and api_key are required"}), 400
    k = add_key(vendor_id, data["name"], data["api_key"], notes=str(data.get("notes") or ""))
    if not k:
        return jsonify({"error": "vendor not found"}), 404
    if not k.get("_existing"):
        extra = {}
        if data.get("role"):
            extra["role"] = data.get("role")
        if "check_model" in data:
            extra["check_model"] = str(data.get("check_model") or "").strip()
        if extra:
            k = update_key(vendor_id, k["id"], **extra) or k

    health = check_key_health(vendor_id, k["id"])
    v = get_vendor(vendor_id)
    if health.get("healthy"):
        models = health.get("models", [])
        default_model = health.get("default_model", "")
        updates = {"enabled": True}
        if models:
            updates["models"] = models
        elif default_model:
            updates["models"] = [default_model]
        if default_model:
            updates["default_model"] = default_model
        updated_key = update_key_data(vendor_id, k["id"], **updates) or k
        on_key_added(v, updated_key)
    else:
        if bool((get_settings() or {}).get("health_auto_disable")):
            update_key_data(vendor_id, k["id"], enabled=False)
            on_key_removed(v, k)
            k["enabled"] = False
    reconcile_all()
    log_event(
        "key.add",
        vendor_id=vendor_id,
        key_id=(k or {}).get("id"),
        name=(k or {}).get("name"),
        healthy=bool(health.get("healthy")),
    )
    return jsonify({"key": k, "health": health}), 201


@app.route("/api/vendors/<vendor_id>/keys/<key_id>", methods=["PUT"])
def api_update_key(vendor_id, key_id):
    data = request.get_json() or {}
    allowed = {k: data[k] for k in (
        "name", "api_key", "enabled", "models", "default_model",
        "check_model", "disabled_models", "model_health", "notes", "role",
    ) if k in data}
    k = update_key(vendor_id, key_id, **allowed)
    if not k:
        return jsonify({"error": "not found"}), 404
    v = get_vendor(vendor_id)
    if v:
        on_key_updated(v, k)
    return jsonify(k)


@app.route("/api/vendors/<vendor_id>/keys/<key_id>", methods=["DELETE"])
def api_delete_key(vendor_id, key_id):
    v = get_vendor(vendor_id)
    k = get_key(vendor_id, key_id)
    if v and k:
        on_key_removed(v, k)
    if not delete_key(vendor_id, key_id):
        return jsonify({"error": "not found"}), 404
    log_event("key.delete", vendor_id=vendor_id, key_id=key_id, name=(k or {}).get("name"))
    return jsonify({"success": True})


@app.route("/api/vendors/<vendor_id>/keys/<key_id>/health", methods=["GET"])
def api_check_key_health(vendor_id, key_id):
    # quick=1: shorter timeout, fewer fallbacks, skip model scan when inventory exists
    # (used by progressive bulk check). Single interactive check stays full by default.
    quick = request.args.get("quick", "0") in ("1", "true", "yes")
    scan = request.args.get("scan_models", "1" if not quick else "0") not in ("0", "false", "no")
    health = check_key_health(vendor_id, key_id, scan_models_flag=scan, quick=quick)
    log_event("health.check", vendor_id=vendor_id, key_id=key_id, healthy=bool(health.get("healthy")), error=(health.get("error") or "")[:200], quick=quick)
    v = get_vendor(vendor_id)
    k = get_key(vendor_id, key_id)
    if v and k:
        # Healthy → auto-enable + push backends; unhealthy → strip from backends (keep in system)
        apply_health_to_backends(v, k, health)
        # reload key after enable/models update so response reflects persisted state
        k = get_key(vendor_id, key_id) or k
        if k:
            health["key_enabled"] = k.get("enabled") is not False
            if k.get("models") is not None:
                health["models"] = k.get("models") or health.get("models") or []
            if k.get("default_model"):
                health["default_model"] = k.get("default_model")
            if k.get("disabled_models") is not None:
                health["disabled_models"] = k.get("disabled_models") or []
        if not health.get("healthy"):
            settings = get_settings() or {}
            # optional: promote backup when primary fails
            if bool(settings.get("health_auto_failover")):
                promoted = failover_primary(vendor_id, key_id)
                if promoted:
                    v2 = get_vendor(vendor_id)
                    if v2:
                        failed = get_key(vendor_id, key_id) or k
                        on_key_removed(v2, failed)
                        on_key_added(v2, promoted)
                    health["failover"] = {
                        "promoted_key_id": promoted.get("id"),
                        "promoted_name": promoted.get("name"),
                    }
                    log_event(
                        "key.failover",
                        vendor_id=vendor_id,
                        failed_key_id=key_id,
                        promoted_key_id=promoted.get("id"),
                    )
    # Progressive full-check skips per-key reconcile (client does one final /api/sync/push)
    if request.args.get("reconcile", "1") != "0":
        reconcile_all()
        try:
            from core.downstream import rebuild_all_downstream_routes
            rebuild_all_downstream_routes()
        except Exception:
            pass
    return jsonify(health)


@app.route("/api/vendors/<vendor_id>/keys/<key_id>/check-models", methods=["POST"])
def api_check_key_models(vendor_id, key_id):
    """Per-model health check. Failed models are disabled for backends but kept in system."""
    result = check_key_models(vendor_id, key_id)
    if result.get("error"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/vendors/<vendor_id>/keys/<key_id>/models/<path:model_id>", methods=["PUT"])
def api_toggle_key_model(vendor_id, key_id, model_id):
    """Enable/disable a model for backend sync. System always keeps the model in inventory."""
    data = request.get_json() or {}
    if "enabled" not in data:
        return jsonify({"error": "enabled required"}), 400
    v = get_vendor(vendor_id)
    k = get_key(vendor_id, key_id)
    if not v or not k:
        return jsonify({"error": "not found"}), 404
    known = set(list_model_ids(k))
    if model_id not in known:
        return jsonify({"error": "model not found on key"}), 404
    updated = set_model_enabled(vendor_id, key_id, model_id, bool(data["enabled"]))
    if not updated:
        return jsonify({"error": "not found"}), 404
    on_key_updated(v, updated)
    reconcile_all()
    return jsonify({
        "key": updated,
        "model": model_id,
        "enabled": model_id not in set(updated.get("disabled_models") or []),
        "enabled_models": get_enabled_models(updated),
        "disabled_models": updated.get("disabled_models") or [],
    })


@app.route("/api/vendors/<vendor_id>/keys/<key_id>/enable", methods=["POST"])
def api_enable_key(vendor_id, key_id):
    v = get_vendor(vendor_id)
    k = get_key(vendor_id, key_id)
    if not v or not k:
        return jsonify({"error": "not found"}), 404
    k = update_key(vendor_id, key_id, enabled=True)
    on_key_added(v, k)
    reconcile_all()
    return jsonify(k)


@app.route("/api/vendors/<vendor_id>/keys/<key_id>/promote", methods=["POST"])
def api_promote_key(vendor_id, key_id):
    """Mark key as primary; demote other primaries on vendor to backup."""
    body = request.get_json(silent=True) or {}
    demote = body.get("demote_others", True)
    v = get_vendor(vendor_id)
    if not v or not get_key(vendor_id, key_id):
        return jsonify({"error": "not found"}), 404
    k = promote_key(vendor_id, key_id, demote_others=bool(demote))
    if not k:
        return jsonify({"error": "not found"}), 404
    # re-sync: primary enabled, others may have been demoted
    v = get_vendor(vendor_id)
    for kk in (v or {}).get("keys") or []:
        if kk.get("enabled") is False:
            on_key_removed(v, kk)
        else:
            on_key_updated(v, kk)
    reconcile_all()
    log_event("key.promote", vendor_id=vendor_id, key_id=key_id, name=k.get("name"))
    return jsonify({"key": k, "vendor": v})


@app.route("/api/vendors/<vendor_id>/keys/<key_id>/disable", methods=["POST"])
def api_disable_key(vendor_id, key_id):
    v = get_vendor(vendor_id)
    k = get_key(vendor_id, key_id)
    if not v or not k:
        return jsonify({"error": "not found"}), 404
    k = update_key(vendor_id, key_id, enabled=False)
    on_key_removed(v, k)
    reconcile_all()
    return jsonify(k)


@app.route("/api/vendors/<vendor_id>/keys/batch", methods=["POST"])
def api_batch_keys(vendor_id):
    data = request.get_json() or {}
    key_ids = data.get("key_ids", [])
    action = data.get("action", "")
    if not key_ids or action not in ("enable", "disable", "delete"):
        return jsonify({"error": "key_ids and action (enable/disable/delete) required"}), 400
    v = get_vendor(vendor_id)
    if not v:
        return jsonify({"error": "vendor not found"}), 404
    results = []
    for kid in key_ids:
        k = get_key(vendor_id, kid)
        if not k:
            results.append({"key_id": kid, "success": False, "error": "not found"})
            continue
        if action == "enable":
            update_key(vendor_id, kid, enabled=True)
            on_key_added(v, k)
            results.append({"key_id": kid, "success": True, "action": "enabled"})
        elif action == "disable":
            update_key(vendor_id, kid, enabled=False)
            on_key_removed(v, k)
            results.append({"key_id": kid, "success": True, "action": "disabled"})
        elif action == "delete":
            on_key_removed(v, k)
            delete_key(vendor_id, kid)
            results.append({"key_id": kid, "success": True, "action": "deleted"})
    return jsonify({"results": results, "count": len(results)})


# ── Health ─────────────────────────────────────────────────

@app.route("/api/health/check-targets", methods=["GET"])
def api_health_check_targets():
    """List vendor/key pairs for progressive health checks.

    Default: all keys (including disabled) so full check covers every vendor.
    Pass include_disabled=0 to only enabled keys.
    """
    include_disabled = request.args.get("include_disabled", "1") != "0"
    targets = []
    for v in get_vendors():
        for k in v.get("keys") or []:
            if not include_disabled and k.get("enabled") is False:
                continue
            targets.append({
                "vendor_id": v["id"],
                "vendor_name": v.get("name") or "",
                "key_id": k["id"],
                "key_name": k.get("name") or "",
                "enabled": k.get("enabled", True) is not False,
            })
    return jsonify({"targets": targets, "count": len(targets)})


@app.route("/api/health/check-all", methods=["POST"])
def api_health_check_all():
    """Bulk check (blocking). Prefer progressive client checks for UI progress."""
    from core.scheduler import run_health_check
    body = request.get_json(silent=True) or {}
    include_disabled = body.get("include_disabled", True)
    # record history via scheduler helper
    out = run_health_check(source="manual", include_disabled=bool(include_disabled))
    if out.get("busy"):
        return jsonify({"success": False, "busy": True, "error": out.get("error")}), 409
    results = out.get("results") or []
    if body.get("reconcile", True) and not out.get("error"):
        reconcile_all()
    log_event(
        "health.check_all",
        ok=(out.get("summary") or {}).get("ok"),
        fail=(out.get("summary") or {}).get("fail"),
        total=len(results),
    )
    return jsonify({
        "results": results,
        "count": len(results),
        "run_id": out.get("run_id"),
        "summary": out.get("summary"),
        "success": bool(out.get("ok")),
        "error": out.get("error"),
    })


@app.route("/api/health/monitor/status", methods=["GET"])
def api_health_monitor_status():
    from core.scheduler import get_status
    from core.health_checker import get_all_health_status
    from core.data import get_vendors
    status = get_status()
    health = get_all_health_status()
    keys = []
    for v in get_vendors():
        for k in v.get("keys") or []:
            ck = f"{v['id']}:{k['id']}"
            h = health.get(ck) or {}
            keys.append({
                "vendor_id": v.get("id"),
                "vendor_name": v.get("name") or "",
                "provider": v.get("provider") or "",
                "key_id": k.get("id"),
                "key_name": k.get("name") or "",
                "enabled": k.get("enabled") is not False,
                "role": k.get("role") or "",
                "healthy": h.get("healthy"),
                "latency_ms": h.get("latency_ms"),
                "error": h.get("error"),
                "checked_at": h.get("checked_at"),
            })
    ok = sum(1 for x in keys if x.get("healthy") is True)
    fail = sum(1 for x in keys if x.get("healthy") is False)
    unchecked = sum(1 for x in keys if x.get("healthy") is None)
    status["current"] = {
        "total_keys": len(keys),
        "healthy": ok,
        "unhealthy": fail,
        "unchecked": unchecked,
        "keys": keys,
    }
    return jsonify(status)


@app.route("/api/health/monitor/start", methods=["POST"])
def api_health_monitor_start():
    from core.scheduler import enable_monitoring, resume_scheduler
    body = request.get_json(silent=True) or {}
    interval = body.get("interval") or body.get("check_interval_seconds")
    try:
        interval_i = int(interval) if interval is not None else None
    except Exception:
        interval_i = None
    status = enable_monitoring(interval=interval_i)
    # if only resume requested
    if body.get("resume_only"):
        status = resume_scheduler()
    log_event("health.monitor_start", interval=status.get("interval_seconds"))
    return jsonify({"ok": True, **status})


@app.route("/api/health/monitor/stop", methods=["POST"])
def api_health_monitor_stop():
    from core.scheduler import disable_monitoring
    status = disable_monitoring()
    log_event("health.monitor_stop")
    return jsonify({"ok": True, **status})


@app.route("/api/health/monitor/pause", methods=["POST"])
def api_health_monitor_pause():
    from core.scheduler import pause_scheduler
    status = pause_scheduler()
    log_event("health.monitor_pause")
    return jsonify({"ok": True, **status})


@app.route("/api/health/monitor/resume", methods=["POST"])
def api_health_monitor_resume():
    from core.scheduler import resume_scheduler, enable_monitoring, get_status
    body = request.get_json(silent=True) or {}
    if body.get("enable"):
        status = enable_monitoring()
    else:
        status = resume_scheduler()
        # if settings still off, report clearly
        if not status.get("enabled"):
            return jsonify({
                "ok": True,
                "warning": "monitoring still disabled in settings; pass enable=true to turn on",
                **status,
            })
    log_event("health.monitor_resume", enabled=status.get("enabled"))
    return jsonify({"ok": True, **status})


@app.route("/api/health/monitor/run", methods=["POST"])
def api_health_monitor_run_now():
    """Trigger one immediate full check (records history)."""
    from core.scheduler import run_health_check
    body = request.get_json(silent=True) or {}
    include_disabled = body.get("include_disabled", True)
    out = run_health_check(source=str(body.get("source") or "manual"), include_disabled=bool(include_disabled))
    if out.get("busy"):
        return jsonify({"success": False, "busy": True, "error": out.get("error")}), 409
    log_event(
        "health.run_now",
        ok=(out.get("summary") or {}).get("ok"),
        fail=(out.get("summary") or {}).get("fail"),
    )
    return jsonify({
        "success": bool(out.get("ok")),
        "busy": False,
        "run_id": out.get("run_id"),
        "summary": out.get("summary"),
        "error": out.get("error"),
        "count": len(out.get("results") or []),
    })


@app.route("/api/health/history", methods=["GET"])
def api_health_history():
    from core.health_history import list_runs
    limit = request.args.get("limit", 50)
    offset = request.args.get("offset", 0)
    try:
        limit_i = int(limit)
    except Exception:
        limit_i = 50
    try:
        offset_i = int(offset)
    except Exception:
        offset_i = 0
    return jsonify(list_runs(limit=limit_i, offset=offset_i))


@app.route("/api/health/history", methods=["POST"])
def api_health_history_append():
    """Record a progressive / UI-driven full check into history.

    Body: { source?, started_at?, results?: [{vendor_id,key_id,healthy,latency_ms,error}] }
    or { ok, fail, total, duration_ms, failures? }
    """
    from core.health_history import append_run, summarize_results
    from datetime import datetime, timezone
    body = request.get_json(silent=True) or {}
    source = str(body.get("source") or "manual").strip() or "manual"
    results = body.get("results") if isinstance(body.get("results"), list) else []
    compact = []
    for r in results:
        if not isinstance(r, dict):
            continue
        compact.append({
            "vendor_id": r.get("vendor_id"),
            "key_id": r.get("key_id"),
            "healthy": r.get("healthy"),
            "latency_ms": r.get("latency_ms"),
            "error": (r.get("error") or "")[:200] if r.get("healthy") is False else None,
        })
    if compact:
        summary = summarize_results(compact)
    else:
        summary = {
            "ok": int(body.get("ok") or 0),
            "fail": int(body.get("fail") or 0),
            "unknown": int(body.get("unknown") or 0),
            "total": int(body.get("total") or 0),
            "failures": body.get("failures") if isinstance(body.get("failures"), list) else [],
        }
    started = body.get("started_at") or datetime.now(timezone.utc).isoformat()
    finished = body.get("finished_at") or datetime.now(timezone.utc).isoformat()
    duration_ms = body.get("duration_ms")
    try:
        duration_ms = int(duration_ms) if duration_ms is not None else None
    except Exception:
        duration_ms = None
    rec = append_run({
        "source": source,
        "started_at": started,
        "finished_at": finished,
        "duration_ms": duration_ms,
        "ok": summary.get("ok", 0),
        "fail": summary.get("fail", 0),
        "unknown": summary.get("unknown", 0),
        "total": summary.get("total", 0),
        "failures": summary.get("failures") or [],
        "results": compact,
    })
    # update in-memory scheduler summary so status page shows last run
    try:
        from core import scheduler as _sched
        _sched._state["last_finished_at"] = rec.get("finished_at")
        _sched._state["last_summary"] = {
            "ok": summary.get("ok", 0),
            "fail": summary.get("fail", 0),
            "unknown": summary.get("unknown", 0),
            "total": summary.get("total", 0),
            "duration_ms": duration_ms,
            "source": source,
        }
        _sched._state["last_run_id"] = rec.get("id")
        _sched._state["runs_count"] = int(_sched._state.get("runs_count") or 0) + 1
    except Exception:
        pass
    log_event("health.history_append", source=source, ok=summary.get("ok"), fail=summary.get("fail"))
    return jsonify({"ok": True, "run_id": rec.get("id"), "summary": summary})


@app.route("/api/health/history/<run_id>", methods=["GET"])
def api_health_history_detail(run_id):
    from core.health_history import get_run
    rec = get_run(run_id)
    if not rec:
        return jsonify({"error": "not found"}), 404
    return jsonify(rec)


@app.route("/api/health/history", methods=["DELETE"])
def api_health_history_clear():
    from core.health_history import clear_history
    n = clear_history()
    log_event("health.history_clear", removed=n)
    return jsonify({"ok": True, "removed": n})


# ── Batch Import ───────────────────────────────────────────

@app.route("/api/batch/parse", methods=["POST"])
def api_batch_parse():
    data = request.get_json() or {}
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"error": "text is required"}), 400
    entries = parse_batch_text(text)
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/api/batch/import", methods=["POST"])
def api_batch_apply():
    """Import only — no health checks, no backend reconcile.

    Writes vendors/keys in one disk save. Client should run health only on
    returned pending keys, then optionally push backends once.
    """
    data = request.get_json() or {}
    entries = data.get("entries", [])
    if not entries:
        entries = parse_batch_text(data.get("text", ""))
    if not entries:
        return jsonify({"error": "no entries to import"}), 400

    from core.data import batch_import_entries, import_transaction

    try:
        with import_transaction({"source": "batch.import", "entries": len(entries)}):
            result = batch_import_entries(entries)
    except Exception as e:
        log.exception("batch import aborted and rolled back")
        return jsonify({
            "error": str(e),
            "rolled_back": True,
            "created": [],
            "errors": [{"entry": "transaction", "error": str(e)}],
            "count": 0,
            "pending_health": 0,
            "focus_vendor_id": "",
            "check_targets": [],
        }), 500

    created = result.get("created") or []
    errors = result.get("errors") or []
    pending = int(result.get("pending_health") or 0)
    focus_vendor_id = result.get("focus_vendor_id") or ""

    # Targets for client-side progressive health (imported keys only)
    check_targets = []
    for c in created:
        if c.get("skipped") or not c.get("pending_health"):
            continue
        if c.get("vendor_id") is None or c.get("key_id") is None:
            continue
        check_targets.append({
            "vendor_id": str(c.get("vendor_id")),
            "key_id": str(c.get("key_id")),
            "vendor_name": c.get("vendor") or "",
            "key_name": c.get("key") or "",
        })

    log_event(
        "batch.import",
        imported=sum(1 for c in created if not c.get("skipped")),
        skipped=sum(1 for c in created if c.get("skipped")),
        errors=len(errors),
        pending_health=pending,
        focus_vendor_id=focus_vendor_id or "",
    )
    return jsonify({
        "created": created,
        "errors": errors,
        "count": len(created),
        "pending_health": pending,
        "focus_vendor_id": focus_vendor_id,
        "check_targets": check_targets,
        "message": (
            f"Imported {sum(1 for c in created if not c.get('skipped'))} key(s); "
            f"health check deferred for {pending}"
            if pending else None
        ),
        "undo_available": True,
    })


# Legacy sequential path kept only if something still imports the symbol (tests).
def _batch_import_apply(entries, created, errors):
    from core.data import batch_import_entries
    result = batch_import_entries(entries)
    created.extend(result.get("created") or [])
    errors.extend(result.get("errors") or [])
    return jsonify({
        "created": created,
        "errors": errors,
        "count": len(created),
        "pending_health": result.get("pending_health") or 0,
        "focus_vendor_id": result.get("focus_vendor_id") or "",
        "check_targets": [
            {
                "vendor_id": str(c.get("vendor_id")),
                "key_id": str(c.get("key_id")),
                "vendor_name": c.get("vendor") or "",
                "key_name": c.get("key") or "",
            }
            for c in (result.get("created") or [])
            if c.get("pending_health") and c.get("vendor_id") is not None and c.get("key_id") is not None
        ],
        "undo_available": True,
    })




@app.route("/api/keys/dedupe", methods=["GET"])
def api_keys_dedupe_preview():
    from core.data import dedupe_keys
    result = dedupe_keys(dry_run=True)
    # strip any secrets if present
    return jsonify(result)


@app.route("/api/keys/dedupe", methods=["POST"])
def api_keys_dedupe_apply():
    from core.data import dedupe_keys
    result = dedupe_keys(dry_run=False)
    log_event("keys.dedupe", removed=result.get("removed", 0), groups=result.get("groups", 0))
    return jsonify(result)


@app.route("/api/import/undo", methods=["GET"])
def api_import_undo_status():
    from core.data import load_import_snapshot
    snap = load_import_snapshot()
    if not snap:
        return jsonify({"available": False})
    meta = snap.get("meta") or {}
    bak = snap.get("backup") or {}
    return jsonify({
        "available": True,
        "meta": meta,
        "exported_at": (bak.get("exported_at") if isinstance(bak, dict) else None),
    })


@app.route("/api/import/undo", methods=["POST"])
def api_import_undo_apply():
    from core.data import undo_last_import
    try:
        result = undo_last_import()
        log_event("import.undo", **{k: result.get(k) for k in ("restored",) if k in result})
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/vendors/<vendor_id>/export-text", methods=["GET"])
def api_vendor_export_text(vendor_id):
    """Export vendor keys as smart-import compatible text lines."""
    v = get_vendor(vendor_id)
    if not v:
        return jsonify({"error": "not found"}), 404
    lines = []
    provider = v.get("provider") or "custom"
    url = (v.get("api_url") or "").rstrip("/")
    ep = (v.get("endpoint_type") or "openai").lower()
    for k in v.get("keys") or []:
        key = (k.get("api_key") or "").strip()
        if not key:
            continue
        # format: provider url key  (+ endpoint for anthropic)
        if ep == "anthropic":
            lines.append(f"{provider} {url} {key} endpoint: anthropic")
        else:
            lines.append(f"{provider} {url} {key}")
    text = "\n".join(lines)
    return jsonify({
        "vendor_id": vendor_id,
        "vendor_name": v.get("name"),
        "count": len(lines),
        "text": text,
    })


# ── Sync ───────────────────────────────────────────────────

@app.route("/api/sync/preview", methods=["GET", "POST"])
def api_sync_preview():
    """Preview reverse-import from backends without writing.

    Returns candidates with duplicate flags (system-wide secret match).
    """
    from backends import get_all as get_all_backends
    from core.data import find_key_anywhere, find_vendor_for_import, _norm_secret

    items = []
    totals = {"new": 0, "duplicate": 0, "backends": 0}
    for name, adapter in get_all_backends().items():
        try:
            candidates = adapter.sync_from_backend() or []
        except Exception as e:
            log.warning("sync preview %s failed: %s", name, e)
            continue
        totals["backends"] += 1
        for v in candidates:
            provider = (v.get("provider") or "custom").strip() or "custom"
            api_url = v.get("api_url") or ""
            vendor_name = v.get("name") or provider
            matched = find_vendor_for_import(provider, api_url, vendor_name)
            for k in v.get("keys") or []:
                secret = _norm_secret(k.get("api_key") or "")
                if not secret:
                    continue
                raw_name = (k.get("name") or "").strip() or f"from {name}"
                hit = find_key_anywhere(secret)
                is_dup = bool(hit)
                if is_dup:
                    totals["duplicate"] += 1
                else:
                    totals["new"] += 1
                items.append({
                    "backend": name,
                    "provider": provider,
                    "vendor_name": vendor_name,
                    "api_url": api_url,
                    "endpoint_type": v.get("endpoint_type") or "openai",
                    "key_name": raw_name,
                    "api_key_preview": (secret[:8] + "…" + secret[-4:]) if len(secret) > 14 else secret[:4] + "…",
                    "api_key": secret,  # needed for selective apply; UI should not display full
                    "duplicate": is_dup,
                    "existing_vendor": (hit[0].get("name") if hit else None),
                    "existing_key": (hit[1].get("name") if hit else None),
                    "will_match_vendor": (matched.get("name") if matched else None),
                    "selected": not is_dup,
                })
    return jsonify({"items": items, "totals": totals, "manual_only": True})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Apply reverse-import (manual only).

    Body optional:
      { "items": [ {provider, api_url, vendor_name, endpoint_type, key_name, api_key}, ... ] }
    If items omitted, imports all non-duplicate candidates (legacy behavior).
    """
    from backends import get_all as get_all_backends
    from core.data import (
        add_vendor, add_key, find_vendor_for_import, find_key_anywhere,
        _norm_secret, update_key_data, get_vendor,
    )

    body = request.get_json(silent=True) or {}
    selected = body.get("items")

    from core.data import import_transaction
    try:
        with import_transaction({"source": "sync.import", "selective": isinstance(selected, list)}):
            return _sync_import_apply(selected)
    except Exception as e:
        log.exception("sync import aborted and rolled back")
        return jsonify({
            "error": str(e),
            "rolled_back": True,
            "synced": 0,
            "skipped": 0,
            "results": {},
            "manual_only": True,
            "focus_vendor_id": None,
            "focus_vendor_ids": [],
        }), 500


def _sync_import_apply(selected):
    from backends import get_all as get_all_backends
    from core.data import (
        add_vendor, add_key, find_vendor_for_import, find_key_anywhere,
        _norm_secret, update_key_data,
    )

    total_added = 0
    total_skipped = 0
    results = {}

    focus_vendor_ids = []

    def _import_one(src_backend: str, v: dict, k: dict) -> str:
        """Return 'added' | 'skipped'."""
        nonlocal total_added, total_skipped
        secret = _norm_secret(k.get("api_key") or "")
        if not secret:
            total_skipped += 1
            return "skipped"
        if find_key_anywhere(secret):
            total_skipped += 1
            return "skipped"
        provider = (v.get("provider") or "custom").strip() or "custom"
        api_url = v.get("api_url") or ""
        vendor = find_vendor_for_import(provider, api_url, v.get("name") or v.get("vendor_name") or "")
        if not vendor:
            vendor = add_vendor(
                v.get("name") or v.get("vendor_name") or provider.replace("-", " ").title(),
                provider,
                api_url,
                v.get("endpoint_type") or "openai",
            )
        if not vendor:
            total_skipped += 1
            return "skipped"
        vendor_id = vendor["id"]
        raw_name = (k.get("name") or k.get("key_name") or "").strip() or f"from {src_backend}"
        entry = add_key(vendor_id, raw_name, secret)
        if not entry or entry.get("_existing"):
            total_skipped += 1
            # Still track vendor_id for focus even when key already exists
            if vendor_id and vendor_id not in focus_vendor_ids:
                focus_vendor_ids.append(vendor_id)
            return "skipped"
        total_added += 1
        if vendor_id and vendor_id not in focus_vendor_ids:
            focus_vendor_ids.append(vendor_id)
        models = k.get("models")
        if models:
            try:
                update_key_data(vendor_id, entry["id"], models=models)
            except Exception:
                pass
        return "added"

    if isinstance(selected, list):
        # Selective apply from preview
        by_backend = {}
        for it in selected:
            if not isinstance(it, dict):
                continue
            if it.get("selected") is False or it.get("duplicate"):
                total_skipped += 1
                continue
            b = it.get("backend") or "import"
            v = {
                "provider": it.get("provider"),
                "api_url": it.get("api_url"),
                "name": it.get("vendor_name") or it.get("name"),
                "endpoint_type": it.get("endpoint_type") or "openai",
            }
            k = {
                "name": it.get("key_name") or it.get("name"),
                "api_key": it.get("api_key"),
                "models": it.get("models"),
            }
            status = _import_one(b, v, k)
            by_backend.setdefault(b, {"added": 0, "skipped": 0})
            by_backend[b][status if status in ("added", "skipped") else "skipped"] += 1
        results = by_backend
    else:
        for name, adapter in get_all_backends().items():
            try:
                candidates = adapter.sync_from_backend() or []
                added = skipped = 0
                for v in candidates:
                    for k in v.get("keys") or []:
                        st = _import_one(name, v, k)
                        if st == "added":
                            added += 1
                        else:
                            skipped += 1
                results[name] = {"added": added, "skipped": skipped, "candidates": len(candidates)}
            except Exception as e:
                log.exception("sync_from_backend failed for %s", name)
                # hard failure after partial writes → abort transaction
                raise RuntimeError(f"sync import failed for backend {name}: {e}") from e

    log_event("sync.import", added=total_added, skipped=total_skipped)
    focus_vendor_id = focus_vendor_ids[-1] if focus_vendor_ids else None
    return jsonify({
        "synced": total_added,
        "skipped": total_skipped,
        "results": results,
        "manual_only": True,
        "focus_vendor_id": focus_vendor_id,
        "focus_vendor_ids": focus_vendor_ids,
        "undo_available": True,
    })


@app.route("/api/sync/push/preview", methods=["GET", "POST"])
def api_sync_push_preview():
    """Dry-run push: list backends that would be written or skipped (no writes)."""
    try:
        from backends import preview_push_all
        return jsonify(preview_push_all())
    except Exception as e:
        log.exception("push preview failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync/push", methods=["POST"])
def api_sync_push():
    """Push system keys to all backend engines (reconcile). Manual trigger."""
    try:
        results = reconcile_all()
        log_event("sync.push", backends=len(results or {}))
        ok = sum(1 for r in (results or {}).values() if r.get("ok"))
        fail = sum(1 for r in (results or {}).values() if not r.get("ok") and not r.get("skipped"))
        skipped = sum(1 for r in (results or {}).values() if r.get("skipped"))
        return jsonify({
            "success": fail == 0,
            "message": f"Pushed: {ok} ok, {fail} failed, {skipped} skipped",
            "results": results or {},
            "ok": ok,
            "fail": fail,
            "skipped": skipped,
        })
    except Exception as e:
        log.exception("push failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sync/last-push", methods=["GET"])
def api_last_push():
    from core.data import get_settings
    return jsonify(get_settings().get("last_push") or {})


@app.route("/api/data/recovery", methods=["GET"])
def api_data_recovery():
    from core.data import get_data_recovery_notice
    notice = get_data_recovery_notice()
    return jsonify({"notice": notice})


@app.route("/api/data/recovery/ack", methods=["POST"])
def api_data_recovery_ack():
    from core.data import ack_data_recovery
    return jsonify({"success": True, "notice": ack_data_recovery()})


@app.route("/api/backup/export", methods=["GET", "POST"])
def api_backup_export():
    from core.data import export_backup
    from core.crypto_backup import is_encrypted_backup
    import json as _json
    password = ""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        password = str(body.get("password") or "")
    else:
        password = str(request.args.get("password") or "")
    payload = export_backup(password=password)
    encrypted = is_encrypted_backup(payload)
    if encrypted:
        n_vendors = "?"
        log_event("backup.export", encrypted=True)
        fname = "ai-switch-backup.enc.json"
    else:
        n_vendors = len((payload.get("data") or {}).get("vendors") or [])
        log_event("backup.export", encrypted=False, vendors=n_vendors)
        fname = "ai-switch-backup.json"
    body = _json.dumps(payload, ensure_ascii=False, indent=2)
    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment; filename={fname}"
    return resp


@app.route("/api/backup/import", methods=["POST"])
def api_backup_import():
    from core.data import import_backup, import_transaction
    body = request.get_json(silent=True) or {}
    mode = (body.get("mode") or request.args.get("mode") or "merge").lower()
    password = str(body.get("password") or "")
    # allow either full payload or {backup: {...}, mode}
    payload = body.get("backup") if isinstance(body.get("backup"), dict) else body
    try:
        with import_transaction({"source": "backup_import", "mode": mode}):
            result = import_backup(payload, mode=mode, password=password)
        log_event("backup.import", mode=mode, **{k: result.get(k) for k in result if k != "items"})
        return jsonify({"success": True, "undo_available": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "rolled_back": True}), 400


@app.route("/api/policies", methods=["GET"])
def api_list_policies():
    from core.data import list_policy_templates
    return jsonify(list_policy_templates())


@app.route("/api/policies", methods=["POST"])
def api_save_policy():
    from core.data import save_policy_template
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        entry = save_policy_template(
            name,
            description=str(data.get("description") or ""),
            settings=data.get("settings") if isinstance(data.get("settings"), dict) else None,
        )
        log_event("policy.save", id=entry.get("id"), name=entry.get("name"))
        return jsonify(entry)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/policies/<template_id>/apply", methods=["POST"])
def api_apply_policy(template_id):
    from core.data import apply_policy_template
    from core.scheduler import restart_scheduler
    try:
        settings = apply_policy_template(template_id)
        try:
            restart_scheduler()
        except Exception:
            pass
        log_event("policy.apply", id=template_id)
        # never echo access_token
        out = dict(settings or {})
        if "access_token" in out:
            out["access_token_set"] = bool(out.pop("access_token"))
        return jsonify({"ok": True, "settings": out})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/policies/<template_id>", methods=["DELETE"])
def api_delete_policy(template_id):
    from core.data import delete_policy_template
    try:
        if not delete_policy_template(template_id):
            return jsonify({"error": "not found"}), 404
        log_event("policy.delete", id=template_id)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Gateway ────────────────────────────────────────────────

@app.route("/api/gateway/status", methods=["GET"])
def api_gateway_status():
    ocp = get_backend("openclaw")
    if ocp:
        status = ocp.get_status()
        health = get_all_health_status()
        status["health"] = health
        status["openclaw_version"] = ocp.get_version()
        status["manager_version"] = "2.0.5"
        status["min_openclaw_version"] = "2026.3.0"
        status["recommended_openclaw_version"] = "2026.6.11"
        return jsonify(status)
    return jsonify({"running": False, "enabled": False, "message": "OpenClaw backend not available"})


@app.route("/api/gateway/restart", methods=["POST"])
def api_gateway_restart():
    lang = _current_lang()
    ocp = get_backend("openclaw")
    if ocp:
        result = ocp.restart()
        if result.get("success"):
            result["message"] = _t("alert.restartSuccess", lang)
        else:
            result["message"] = result.get("message") or _t("alert.restartFailed", lang)
        return jsonify(result)
    return jsonify({"success": False, "message": "OpenClaw backend not available"})


@app.route("/api/gateway/config", methods=["GET"])
def api_get_gateway_config():
    settings = get_settings()
    return jsonify(settings.get("gateway", {"mode": "local"}))


@app.route("/api/gateway/config", methods=["POST"])
def api_save_gateway_config():
    data = request.get_json() or {}
    update_settings(gateway=data)
    return jsonify({"ok": True})


@app.route("/api/gateway/remote/test", methods=["POST"])
def api_test_remote_gateway():
    from core.remote import ssh_test_connection, gateway_test_connection
    data = request.get_json() or {}
    conn_type = data.get("type", "")
    if conn_type == "ssh":
        result = ssh_test_connection(data)
        log_event("gateway.remote_test", type="ssh", success=bool(result.get("success")))
        return jsonify(result)
    elif conn_type == "gateway":
        result = gateway_test_connection(data)
        log_event("gateway.remote_test", type="gateway", success=bool(result.get("success")))
        return jsonify(result)
    return jsonify({"success": False, "message": "unknown type"})


@app.route("/api/gateway/remote/save", methods=["POST"])
def api_save_remote_gateway():
    """Persist remote gateway connection into settings.gateway."""
    data = request.get_json() or {}
    conn_type = data.get("type", "ssh")
    gateway = {
        "mode": "remote",
        "type": conn_type,
    }
    if conn_type == "ssh":
        gateway.update({
            "host": str(data.get("host") or "").strip(),
            "port": int(data.get("port") or 22),
            "user": str(data.get("user") or "").strip(),
            "key_file": str(data.get("key_file") or "").strip(),
            "config_path": str(data.get("config_path") or "~/.openclaw").strip(),
        })
        if not gateway["host"]:
            return jsonify({"error": "host required"}), 400
    else:
        gateway.update({
            "url": str(data.get("url") or "").strip(),
            "token": str(data.get("token") or "").strip(),
        })
        if not gateway["url"]:
            return jsonify({"error": "url required"}), 400
    update_settings(gateway=gateway)
    log_event("gateway.remote_save", type=conn_type, mode="remote")
    # never echo secrets fully
    out = dict(gateway)
    if out.get("token"):
        out["token"] = "***"
    return jsonify({"ok": True, "gateway": out})


@app.route("/api/profiles", methods=["GET"])
def api_list_profiles():
    return jsonify(list_profiles())


@app.route("/api/profiles", methods=["POST"])
def api_save_profile():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        result = save_profile(name, label=str(data.get("label") or ""))
        log_event("profile.save", name=result.get("name"))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/profiles/<name>/switch", methods=["POST"])
def api_switch_profile(name):
    try:
        result = switch_profile(name)
        log_event("profile.switch", name=name)
        return jsonify(result)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/profiles/<name>", methods=["DELETE"])
def api_delete_profile(name):
    if not delete_profile(name):
        return jsonify({"error": "not found"}), 404
    log_event("profile.delete", name=name)
    return jsonify({"ok": True})


@app.route("/api/profiles/export", methods=["GET", "POST"])
def api_export_profile():
    """Export named profile or current workspace. Optional password encrypts file."""
    from core.data import export_profile
    from core.crypto_backup import is_encrypted_backup
    import json as _json
    password = ""
    name = ""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        password = str(body.get("password") or "")
        name = str(body.get("name") or "")
    else:
        password = str(request.args.get("password") or "")
        name = str(request.args.get("name") or "")
    try:
        payload = export_profile(name, password=password)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    encrypted = is_encrypted_backup(payload)
    label = name or "workspace"
    fname = f"ai-switch-profile-{label}{'.enc' if encrypted else ''}.json"
    body = _json.dumps(payload, ensure_ascii=False, indent=2)
    resp = app.response_class(body, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    log_event("profile.export", name=label or "workspace", encrypted=encrypted)
    return resp


@app.route("/api/profiles/import", methods=["POST"])
def api_import_profile():
    """Import shared profile file (optionally encrypted).

    Body: { profile|backup, password?, name?, activate?, mode? }
    """
    from core.data import import_profile_file
    body = request.get_json(silent=True) or {}
    password = str(body.get("password") or "")
    name = str(body.get("name") or "")
    activate = bool(body.get("activate"))
    mode = str(body.get("mode") or "replace")
    payload = body.get("profile") if isinstance(body.get("profile"), dict) else body.get("backup")
    if not isinstance(payload, dict):
        payload = body if isinstance(body, dict) else None
    if not isinstance(payload, dict):
        return jsonify({"error": "profile payload required"}), 400
    # strip control fields if full body was used as payload
    if "profile" in payload or "backup" in payload:
        payload = payload.get("profile") or payload.get("backup") or payload
    try:
        result = import_profile_file(
            payload,
            password=password,
            name=name,
            activate=activate,
            mode=mode,
        )
        log_event(
            "profile.import",
            name=result.get("name"),
            activated=bool(result.get("activated")),
            vendors=result.get("vendors"),
        )
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


# ── Downstream keys (routing aggregate) ─────────────────────

@app.route("/api/downstream", methods=["GET"])
def api_list_downstream():
    from core.downstream import list_downstream_keys
    return jsonify({"keys": list_downstream_keys(include_secret=False)})


@app.route("/api/downstream", methods=["POST"])
def api_create_downstream():
    from core.downstream import create_downstream_key
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    entry = create_downstream_key(
        name,
        endpoint_types=data.get("endpoint_types"),
        selected_models=data.get("selected_models"),
        auto_update=bool(data.get("auto_update", True)),
        notes=str(data.get("notes") or ""),
        enabled=bool(data.get("enabled", True)),
    )
    log_event("downstream.create", id=entry.get("id"), name=entry.get("name"))
    # return secret once on create
    return jsonify(entry), 201


@app.route("/api/downstream/models", methods=["GET"])
def api_downstream_available_models():
    from core.downstream import available_models_catalog
    return jsonify(available_models_catalog())


@app.route("/api/downstream/<key_id>", methods=["GET"])
def api_get_downstream(key_id):
    from core.downstream import get_downstream_key
    include = request.args.get("secret") == "1"
    d = get_downstream_key(key_id, include_secret=include)
    if not d:
        return jsonify({"error": "not found"}), 404
    return jsonify(d)


@app.route("/api/downstream/<key_id>", methods=["PUT"])
def api_update_downstream(key_id):
    from core.downstream import update_downstream_key
    data = request.get_json(silent=True) or {}
    entry = update_downstream_key(
        key_id,
        name=data.get("name"),
        enabled=data.get("enabled") if "enabled" in data else None,
        auto_update=data.get("auto_update") if "auto_update" in data else None,
        notes=data.get("notes") if "notes" in data else None,
        endpoint_types=data.get("endpoint_types") if "endpoint_types" in data else None,
        selected_models=data.get("selected_models") if "selected_models" in data else None,
        rotate_secret=bool(data.get("rotate_secret")),
        rebuild=bool(data.get("rebuild", True)),
    )
    if not entry:
        return jsonify({"error": "not found"}), 404
    # strip None-only updates: update_downstream_key treats missing differently
    # Re-call more carefully if only partial fields - already handled inside
    log_event("downstream.update", id=key_id)
    out = dict(entry)
    if not data.get("rotate_secret") and "api_key" in out:
        sec = out.get("api_key") or ""
        out["api_key_preview"] = (sec[:12] + "…" + sec[-4:]) if len(sec) > 18 else (sec[:8] + "…")
        out.pop("api_key", None)
    return jsonify(out)


@app.route("/api/downstream/<key_id>", methods=["DELETE"])
def api_delete_downstream(key_id):
    from core.downstream import delete_downstream_key
    if not delete_downstream_key(key_id):
        return jsonify({"error": "not found"}), 404
    log_event("downstream.delete", id=key_id)
    return jsonify({"ok": True})


@app.route("/api/downstream/<key_id>/rebuild", methods=["POST"])
def api_rebuild_downstream(key_id):
    from core.downstream import rebuild_downstream_routes
    entry = rebuild_downstream_routes(key_id)
    if not entry:
        return jsonify({"error": "not found"}), 404
    log_event("downstream.rebuild", id=key_id, routes=entry.get("route_source_count"))
    out = dict(entry)
    if "api_key" in out:
        sec = out.pop("api_key") or ""
        out["api_key_preview"] = (sec[:12] + "…" + sec[-4:]) if len(sec) > 18 else (sec[:8] + "…")
    return jsonify(out)


@app.route("/api/downstream/rebuild-all", methods=["POST"])
def api_rebuild_all_downstream():
    from core.downstream import rebuild_all_downstream_routes
    result = rebuild_all_downstream_routes()
    log_event("downstream.rebuild_all", **result)
    return jsonify(result)


# ── Public OpenAI / Anthropic compatible API ───────────────

def _extract_bearer():
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Anthropic uses x-api-key
    xk = request.headers.get("x-api-key") or request.headers.get("X-Api-Key") or ""
    if xk:
        return xk.strip()
    return auth.strip()


def _downstream_auth():
    from core.downstream import find_downstream_by_secret
    secret = _extract_bearer()
    if not secret:
        return None, (jsonify({"error": {"message": "Missing API key", "type": "auth_error"}}), 401)
    d = find_downstream_by_secret(secret)
    if not d:
        return None, (jsonify({"error": {"message": "Invalid API key", "type": "auth_error"}}), 401)
    if d.get("enabled") is False:
        return None, (jsonify({"error": {"message": "API key disabled", "type": "auth_error"}}), 403)
    return d, None


def _join_url(base: str, *parts: str) -> str:
    url = (base or "").rstrip("/")
    for p in parts:
        sp = (p or "").lstrip("/")
        if not sp:
            continue
        last = url.split("/")[-1] if url else ""
        if last and sp.startswith(last + "/"):
            sp = sp[len(last) + 1:]
        elif last == sp.split("/")[0] and "/" in sp:
            # avoid /v1/v1/...
            pass
        url = url.rstrip("/") + "/" + sp
    return url


def _proxy_upstream(vendor: dict, key: dict, *, path: str, body: bytes, headers: dict, stream: bool):
    api_url = (vendor.get("proxy_target") or vendor.get("api_url") or "").rstrip("/")
    if not api_url:
        raise ValueError("upstream has no api_url")
    url = _join_url(api_url, path)
    hdrs = {k: v for k, v in headers.items() if k.lower() not in ("host", "content-length", "authorization", "x-api-key")}
    # inject upstream auth
    ep = (vendor.get("endpoint_type") or "openai").lower()
    if ep in ("anthropic", "claude"):
        hdrs["x-api-key"] = key["api_key"]
        hdrs.setdefault("anthropic-version", request.headers.get("anthropic-version") or "2023-06-01")
        hdrs["content-type"] = "application/json"
    else:
        hdrs["Authorization"] = "Bearer " + key["api_key"]
        hdrs["content-type"] = "application/json"
    r = py_requests.request(
        method=request.method if request.method != "GET" else "POST",
        url=url,
        headers=hdrs,
        data=body,
        stream=stream,
        verify=False,
        timeout=120,
    )
    return r, url


@app.route("/v1/models", methods=["GET"])
@app.route("/api/v1/models", methods=["GET"])
def api_public_models():
    d, err = _downstream_auth()
    if err:
        return err
    from core.downstream import list_models_for_downstream
    models = list_models_for_downstream(d)
    return jsonify({"object": "list", "data": models})


@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
@app.route("/api/v1/chat/completions", methods=["POST", "OPTIONS"])
def api_public_chat_completions():
    if request.method == "OPTIONS":
        return Response(status=204)
    d, err = _downstream_auth()
    if err:
        return err
    if "openai" not in (d.get("endpoint_types") or ["openai"]):
        return jsonify({"error": {"message": "This key does not allow OpenAI endpoint", "type": "invalid_request_error"}}), 400
    body_json = request.get_json(silent=True) or {}
    model = (body_json.get("model") or "").strip()
    if not model:
        return jsonify({"error": {"message": "model is required", "type": "invalid_request_error"}}), 400
    from core.downstream import resolve_route
    hit = resolve_route(d, model, endpoint_type="openai")
    if not hit:
        return jsonify({"error": {"message": f"No healthy upstream for model '{model}'", "type": "model_not_found"}}), 404
    stream = bool(body_json.get("stream"))
    raw = request.get_data()
    t0 = datetime.now()
    try:
        r, url = _proxy_upstream(
            hit["vendor"], hit["key"],
            path="chat/completions",
            body=raw,
            headers=dict(request.headers),
            stream=stream,
        )
        elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)
        ok = 200 <= r.status_code < 400
        if stream:
            chunks = []
            def _gen():
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        chunks.append(chunk)
                        yield chunk
            resp = Response(_gen(), status=r.status_code, content_type=r.headers.get("Content-Type", "text/event-stream"))
            @resp.call_on_close
            def _done():
                full = b"".join(chunks)
                last = _parse_sse_last_chunk(full)
                if last:
                    _record_proxy_usage(hit["vendor"], hit["key"], json.dumps(last).encode(), elapsed_ms, model=model, status_code=r.status_code, success=ok)
                else:
                    _record_proxy_usage(hit["vendor"], hit["key"], full or b"{}", elapsed_ms, model=model, status_code=r.status_code, success=ok)
            return resp
        _record_proxy_usage(hit["vendor"], hit["key"], r.content, elapsed_ms, model=model, status_code=r.status_code, success=ok)
        return Response(r.content, status=r.status_code, content_type=r.headers.get("Content-Type", "application/json"))
    except Exception as e:
        _record_proxy_usage(hit["vendor"], hit["key"], json.dumps({"error": str(e)}).encode(), 0, model=model, status_code=502, success=False)
        return jsonify({"error": {"message": str(e), "type": "api_error"}}), 502


@app.route("/v1/messages", methods=["POST", "OPTIONS"])
@app.route("/api/v1/messages", methods=["POST", "OPTIONS"])
def api_public_messages():
    """Anthropic Messages API compatible endpoint."""
    if request.method == "OPTIONS":
        return Response(status=204)
    d, err = _downstream_auth()
    if err:
        return err
    if "anthropic" not in (d.get("endpoint_types") or []):
        return jsonify({"type": "error", "error": {"type": "invalid_request_error", "message": "This key does not allow Anthropic endpoint"}}), 400
    body_json = request.get_json(silent=True) or {}
    model = (body_json.get("model") or "").strip()
    if not model:
        return jsonify({"type": "error", "error": {"type": "invalid_request_error", "message": "model is required"}}), 400
    from core.downstream import resolve_route
    hit = resolve_route(d, model, endpoint_type="anthropic")
    if not hit:
        return jsonify({"type": "error", "error": {"type": "not_found_error", "message": f"No healthy upstream for model '{model}'"}}), 404
    stream = bool(body_json.get("stream"))
    raw = request.get_data()
    t0 = datetime.now()
    try:
        r, url = _proxy_upstream(
            hit["vendor"], hit["key"],
            path="messages" if "anthropic" in (hit.get("endpoint_type") or "") or (hit["vendor"].get("endpoint_type") or "") == "anthropic" else "v1/messages",
            body=raw,
            headers=dict(request.headers),
            stream=stream,
        )
        # if upstream is openai-compatible only, rewrite path to chat/completions is not done here —
        # user should pick models from anthropic-capable vendors
        elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)
        ok = 200 <= r.status_code < 400
        if stream:
            chunks = []
            def _gen():
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        chunks.append(chunk)
                        yield chunk
            resp = Response(_gen(), status=r.status_code, content_type=r.headers.get("Content-Type", "text/event-stream"))
            @resp.call_on_close
            def _done():
                full = b"".join(chunks)
                _record_proxy_usage(hit["vendor"], hit["key"], full or b"{}", elapsed_ms, model=model, status_code=r.status_code, success=ok)
            return resp
        _record_proxy_usage(hit["vendor"], hit["key"], r.content, elapsed_ms, model=model, status_code=r.status_code, success=ok)
        return Response(r.content, status=r.status_code, content_type=r.headers.get("Content-Type", "application/json"))
    except Exception as e:
        return jsonify({"type": "error", "error": {"type": "api_error", "message": str(e)}}), 502


# ── Proxy ──────────────────────────────────────────────────

def _match_vendor_key(auth):
    """Find (vendor, key) matching the Authorization header."""
    for v in get_vendors():
        for k in v.get("keys", []):
            if k.get("api_key") and ("Bearer " + k["api_key"] == auth or k["api_key"] == auth):
                return v, k
    return None, None


def _record_proxy_usage(vendor, key, body_bytes, elapsed_ms, model="",
                        status_code=200, success=None):
    """Parse usage from response body and record stats (incl. failures)."""
    model = model or ""
    provider = vendor.get("provider", "unknown")
    if success is None:
        success = 200 <= int(status_code or 0) < 400

    usage = {}
    data = None
    error_msg = ""
    try:
        body_text = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
        data = json.loads(body_text) if body_text else None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        data = None

    if isinstance(data, dict):
        usage = data.get("usage") or {}
        if not usage:
            for c in data.get("choices", []):
                msg = c.get("message", {}) or c.get("delta", {})
                if isinstance(msg, dict) and msg.get("usage"):
                    usage = msg["usage"]
                    break
        if not success:
            err = data.get("error")
            if isinstance(err, dict):
                error_msg = err.get("message") or err.get("type") or str(err)
            elif err:
                error_msg = str(err)
            else:
                error_msg = data.get("message", "") or f"HTTP {status_code}"

    # Support OpenAI + Anthropic usage field names
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    if usage:
        prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total_tokens = usage.get("total_tokens") or 0
        if not total_tokens:
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)

    resolved_model = model or (usage.get("model", "") if usage else "") or (
        data.get("model", "") if isinstance(data, dict) else ""
    )
    if resolved_model and "/" in str(resolved_model):
        # normalize "provider/model" for stats grouping when possible
        try:
            from core.data import _normalize_model_name
            resolved_model = _normalize_model_name(str(resolved_model)) or resolved_model
        except Exception:
            pass

    record = {
        "timestamp": datetime.now().isoformat(),
        "vendor_id": vendor["id"],
        "vendor_name": vendor.get("name", ""),
        "key_id": key.get("id", ""),
        "key_name": key.get("name", ""),
        "provider": provider,
        "model": resolved_model,
        "total_tokens": total_tokens or 0,
        "prompt_tokens": prompt_tokens or 0,
        "completion_tokens": completion_tokens or 0,
        "cost": 0.0,
        "elapsed_ms": elapsed_ms,
        "success": bool(success),
        "status_code": int(status_code or 0),
        "error": error_msg if not success else "",
        "source": "proxy",
    }
    try:
        from core.pricing import resolve_record_cost
        record["cost"] = resolve_record_cost(record)
    except Exception:
        pass

    try:
        from core.data import add_usage_record
        add_usage_record(record)
    except Exception:
        pass


def _parse_sse_last_chunk(body_bytes):
    """Extract usage from last SSE data chunk of a streaming response."""
    body_text = body_bytes.decode("utf-8", errors="replace")
    for line in reversed(body_text.split("\n")):
        line = line.strip()
        if line.startswith("data: "):
            payload = line[6:].strip()
            if payload == "[DONE]":
                continue
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    return None


@app.route("/api/proxy/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def api_proxy(subpath):
    auth = request.headers.get("Authorization", "")
    matched_vendor, matched_key = _match_vendor_key(auth)

    # Forward to the vendor's actual API URL, not the proxy_target
    target_url = matched_vendor.get("api_url", "").rstrip("/") if matched_vendor else ""
    if not target_url:
        # Fallback: find any vendor with proxy_target
        for v in get_vendors():
            pt = v.get("proxy_target", "")
            if pt:
                target_url = v.get("api_url", "").rstrip("/")
                break

    if not target_url:
        return jsonify({"error": "no matching vendor or api_url"}), 502

    # Avoid duplicating version prefix: api_url may end with /v1 and subpath may start with v1/
    url = target_url.rstrip("/")
    sp = subpath.lstrip("/")
    last_seg = url.split("/")[-1] if "/" in url else ""
    if last_seg and sp.startswith(last_seg + "/"):
        sp = sp[len(last_seg) + 1:]
    url += "/" + sp
    headers = {k: v for k, v in request.headers if k.lower() not in ("host", "content-length")}
    req_body = request.get_data()
    is_stream = request.headers.get("accept", "") == "text/event-stream" or \
                (request.is_json and request.get_json(silent=True) or {}).get("stream")

    model = ""
    if request.is_json:
        body_json = request.get_json(silent=True) or {}
        model = body_json.get("model", "")

    t0 = datetime.now()
    try:
        r = py_requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=req_body,
            stream=is_stream,
            verify=False,
            timeout=120,
        )
        elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)

        if not matched_vendor or not matched_key:
            if is_stream:
                def _passthrough():
                    for chunk in r.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                return Response(_passthrough(), status=r.status_code, headers=dict(r.headers))
            return Response(r.content, status=r.status_code, headers=dict(r.headers))

        ok = 200 <= r.status_code < 400
        if is_stream:
            chunks = []
            def _recorded_stream():
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        chunks.append(chunk)
                        yield chunk
            resp = Response(_recorded_stream(), status=r.status_code, headers=dict(r.headers))

            @resp.call_on_close
            def _on_stream_done():
                full_body = b"".join(chunks)
                last_chunk = _parse_sse_last_chunk(full_body)
                if last_chunk:
                    usage = last_chunk.get("usage") or last_chunk
                    _record_proxy_usage(
                        matched_vendor, matched_key, json.dumps(usage).encode(),
                        elapsed_ms, model=model, status_code=r.status_code, success=ok,
                    )
                else:
                    _record_proxy_usage(
                        matched_vendor, matched_key, full_body or b"{}",
                        elapsed_ms, model=model, status_code=r.status_code, success=ok,
                    )
            return resp

        resp = Response(r.content, status=r.status_code, headers=dict(r.headers))
        _record_proxy_usage(
            matched_vendor, matched_key, r.content, elapsed_ms,
            model=model, status_code=r.status_code, success=ok,
        )
        return resp
    except Exception as e:
        if matched_vendor and matched_key:
            _record_proxy_usage(
                matched_vendor, matched_key, json.dumps({"error": str(e)}).encode(),
                0, model=model, status_code=502, success=False,
            )
        return jsonify({"error": str(e)}), 502


# ── Settings ───────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    s = dict(get_settings() or {})
    # never return raw access token; only whether set
    token = str(s.pop("access_token", "") or "")
    s["access_token_set"] = bool(token.strip())
    s["access_token"] = ""  # client must not echo secrets
    s.pop("secrets_kdf", None)  # never expose salt/verifier details beyond status
    try:
        from core.data import secrets_status
        st = secrets_status()
        s["encrypt_keys_at_rest"] = bool(st.get("encrypt_keys_at_rest"))
        s["secrets_unlocked"] = bool(st.get("unlocked"))
        s["secrets_locked"] = bool(st.get("locked"))
    except Exception:
        s.setdefault("encrypt_keys_at_rest", False)
        s["secrets_unlocked"] = True
        s["secrets_locked"] = False
    return jsonify(s)


@app.route("/api/secrets/status", methods=["GET"])
def api_secrets_status():
    from core.data import secrets_status
    return jsonify(secrets_status())


@app.route("/api/secrets/unlock", methods=["POST"])
def api_secrets_unlock():
    from core.data import unlock_secrets
    body = request.get_json(silent=True) or {}
    try:
        st = unlock_secrets(str(body.get("password") or ""))
        log_event("secrets.unlock", ok=True)
        return jsonify({"success": True, **st})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/secrets/lock", methods=["POST"])
def api_secrets_lock():
    from core.data import lock_secrets
    st = lock_secrets()
    log_event("secrets.lock")
    return jsonify({"success": True, **st})


@app.route("/api/secrets/enable", methods=["POST"])
def api_secrets_enable():
    from core.data import enable_secrets_encryption
    body = request.get_json(silent=True) or {}
    try:
        st = enable_secrets_encryption(str(body.get("password") or ""))
        log_event("secrets.enable")
        return jsonify({"success": True, **st})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/secrets/disable", methods=["POST"])
def api_secrets_disable():
    from core.data import disable_secrets_encryption
    body = request.get_json(silent=True) or {}
    try:
        st = disable_secrets_encryption(str(body.get("password") or ""))
        log_event("secrets.disable")
        return jsonify({"success": True, **st})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    data = request.get_json() or {}
    # normalize types
    if "health_check_enabled" in data:
        data["health_check_enabled"] = bool(data["health_check_enabled"])
    if "health_auto_disable" in data:
        data["health_auto_disable"] = bool(data["health_auto_disable"])
    if "check_interval_seconds" in data:
        try:
            data["check_interval_seconds"] = max(60, int(data["check_interval_seconds"]))
        except Exception:
            data["check_interval_seconds"] = 300
    if "health_network_retries" in data:
        try:
            data["health_network_retries"] = max(1, min(10, int(data["health_network_retries"])))
        except Exception:
            data["health_network_retries"] = 3
    for sk, default, lo, hi in (
        ("health_fail_streak", 3, 1, 50),
        ("health_ok_streak", 3, 1, 50),
        ("health_fail_interval_seconds", 7200, 60, 86400 * 7),
        ("health_ok_interval_seconds", 3600, 60, 86400 * 7),
    ):
        if sk in data:
            try:
                data[sk] = max(lo, min(hi, int(data[sk])))
            except Exception:
                data[sk] = default
    if "access_token" in data:
        # empty string clears token
        data["access_token"] = str(data.get("access_token") or "").strip()
    if "onboarding_done" in data:
        data["onboarding_done"] = bool(data["onboarding_done"])
    if "read_only" in data:
        data["read_only"] = bool(data["read_only"])
    if "health_auto_failover" in data:
        data["health_auto_failover"] = bool(data["health_auto_failover"])
    for bk in ("budget_daily_cost", "budget_monthly_cost"):
        if bk in data:
            try: data[bk] = max(0.0, float(data[bk]))
            except Exception: data[bk] = 0.0
    for bk in ("budget_daily_tokens", "budget_monthly_tokens"):
        if bk in data:
            try: data[bk] = max(0, int(float(data[bk])))
            except Exception: data[bk] = 0
    s = update_settings(**data)
    log_event("settings.update", keys=list(data.keys()))
    out = dict(s)
    tok = str(out.pop("access_token", "") or "")
    out["access_token_set"] = bool(tok)
    out["access_token"] = ""
    return jsonify(out)


@app.route("/api/pricing", methods=["GET"])
def api_get_pricing():
    from core.pricing import get_builtin_pricing, get_user_pricing, get_pricing_table
    return jsonify({
        "builtin": get_builtin_pricing(),
        "user": get_user_pricing(),
        "merged": get_pricing_table(),
        "unit": "USD per 1M tokens",
        "fields": ["input", "output"],
    })


@app.route("/api/pricing", methods=["POST"])
def api_set_pricing():
    """Replace user pricing overrides.

    Body: { "pricing": { "model": {"input": 1.0, "output": 2.0}, ... } }
    Empty pricing clears overrides.
    """
    from core.pricing import get_user_pricing, get_pricing_table
    data = request.get_json(silent=True) or {}
    pricing = data.get("pricing", data)
    if pricing is None:
        pricing = {}
    if not isinstance(pricing, dict):
        return jsonify({"error": "pricing must be an object"}), 400
    cleaned = {}
    for k, v in pricing.items():
        if not k:
            continue
        try:
            if isinstance(v, dict):
                inp = float(v.get("input", v.get("prompt", 0)))
                outp = float(v.get("output", v.get("completion", 0)))
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                inp, outp = float(v[0]), float(v[1])
            else:
                continue
            cleaned[str(k).strip().lower()] = {"input": inp, "output": outp}
        except Exception:
            continue
    update_settings(pricing=cleaned)
    log_event("pricing.update", models=len(cleaned))
    return jsonify({
        "success": True,
        "user": cleaned,
        "merged": get_pricing_table(),
    })





@app.route("/api/lang", methods=["POST"])
def api_set_lang():
    data = request.get_json() or {}
    lang = data.get("lang", "en")
    if lang not in SUPPORTED_LANGS:
        lang = "en"
    # Return fresh locale pack so client can hot-update without full reload
    from core.i18n import get_translations, clear_translation_cache
    clear_translation_cache()
    pack = get_translations(lang)
    en = get_translations("en")
    resp = jsonify({"lang": lang, "strings": {**en, **pack}, "locales": {l: get_translations(l) for l in SUPPORTED_LANGS}})
    resp.set_cookie("lang", lang, max_age=86400 * 365)
    return resp


# ── Usage Statistics ──────────────────────────────────────




@app.route("/api/dashboard/overview", methods=["GET"])
def api_dashboard_overview():
    """Today/month usage + budget status for dashboard."""
    from datetime import datetime, timezone, timedelta
    from core.data import get_usage_records, get_settings

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    # next month rough upper bound
    if now.month == 12:
        next_month = f"{now.year + 1}-01-01"
    else:
        next_month = f"{now.year}-{now.month + 1:02d}-01"

    def _agg(from_ts: str, to_ts: str) -> dict:
        recs = get_usage_records(from_ts=from_ts, to_ts=to_ts, auto_import=False)
        cost = sum(float(r.get("cost") or 0) for r in recs)
        tokens = sum(int(r.get("total_tokens") or 0) for r in recs)
        ok = sum(1 for r in recs if r.get("success", True))
        return {
            "count": len(recs),
            "success_count": ok,
            "fail_count": len(recs) - ok,
            "total_tokens": tokens,
            "total_cost": round(cost, 6),
            "cost_estimated": any(r.get("_cost_estimated") for r in recs),
        }

    today_s = _agg(today + "T00:00:00", today + "T23:59:59")
    month_s = _agg(month_start + "T00:00:00", tomorrow + "T23:59:59")

    s = get_settings() or {}
    def _f(key):
        try:
            return float(s.get(key) or 0)
        except Exception:
            return 0.0
    def _i(key):
        try:
            return int(float(s.get(key) or 0))
        except Exception:
            return 0

    budgets = {
        "daily_cost": _f("budget_daily_cost"),
        "monthly_cost": _f("budget_monthly_cost"),
        "daily_tokens": _i("budget_daily_tokens"),
        "monthly_tokens": _i("budget_monthly_tokens"),
        "action": str(s.get("budget_action") or "alert").strip() or "alert",
    }
    alerts = []
    if budgets["daily_cost"] > 0 and today_s["total_cost"] >= budgets["daily_cost"]:
        alerts.append({"type": "daily_cost", "limit": budgets["daily_cost"], "used": today_s["total_cost"], "level": "crit" if today_s["total_cost"] >= budgets["daily_cost"] else "warn"})
    elif budgets["daily_cost"] > 0 and today_s["total_cost"] >= budgets["daily_cost"] * 0.8:
        alerts.append({"type": "daily_cost", "limit": budgets["daily_cost"], "used": today_s["total_cost"], "level": "warn"})
    if budgets["monthly_cost"] > 0 and month_s["total_cost"] >= budgets["monthly_cost"]:
        alerts.append({"type": "monthly_cost", "limit": budgets["monthly_cost"], "used": month_s["total_cost"], "level": "crit"})
    elif budgets["monthly_cost"] > 0 and month_s["total_cost"] >= budgets["monthly_cost"] * 0.8:
        alerts.append({"type": "monthly_cost", "limit": budgets["monthly_cost"], "used": month_s["total_cost"], "level": "warn"})
    if budgets["daily_tokens"] > 0 and today_s["total_tokens"] >= budgets["daily_tokens"]:
        alerts.append({"type": "daily_tokens", "limit": budgets["daily_tokens"], "used": today_s["total_tokens"], "level": "crit"})
    elif budgets["daily_tokens"] > 0 and today_s["total_tokens"] >= budgets["daily_tokens"] * 0.8:
        alerts.append({"type": "daily_tokens", "limit": budgets["daily_tokens"], "used": today_s["total_tokens"], "level": "warn"})
    if budgets["monthly_tokens"] > 0 and month_s["total_tokens"] >= budgets["monthly_tokens"]:
        alerts.append({"type": "monthly_tokens", "limit": budgets["monthly_tokens"], "used": month_s["total_tokens"], "level": "crit"})
    elif budgets["monthly_tokens"] > 0 and month_s["total_tokens"] >= budgets["monthly_tokens"] * 0.8:
        alerts.append({"type": "monthly_tokens", "limit": budgets["monthly_tokens"], "used": month_s["total_tokens"], "level": "warn"})

    # Enforce budget action once when any critical alert is present
    enforcement = {"applied": False, "action": budgets["action"], "disabled_keys": 0, "read_only": False}
    crit = [a for a in alerts if a.get("level") == "crit"]
    action = budgets["action"]
    if crit and action in ("read_only", "disable_keys"):
        last_enforced = str(s.get("budget_enforced_at") or "")
        day_key = now.strftime("%Y-%m-%d")
        # re-apply at most once per UTC day unless action changed
        already = last_enforced.startswith(day_key) and str(s.get("budget_action") or "") == action
        if not already:
            from core.data import update_settings, get_vendors, update_key
            if action == "read_only":
                update_settings(read_only=True, budget_enforced_at=now.isoformat())
                enforcement = {"applied": True, "action": "read_only", "disabled_keys": 0, "read_only": True}
                log_event("budget.enforce", action="read_only", alerts=len(crit))
            elif action == "disable_keys":
                n = 0
                for v in get_vendors():
                    for k in v.get("keys") or []:
                        if k.get("enabled") is False:
                            continue
                        if update_key(str(v["id"]), str(k["id"]), enabled=False):
                            n += 1
                            try:
                                on_key_removed(v, k)
                            except Exception:
                                pass
                update_settings(budget_enforced_at=now.isoformat())
                enforcement = {"applied": True, "action": "disable_keys", "disabled_keys": n, "read_only": False}
                log_event("budget.enforce", action="disable_keys", disabled_keys=n, alerts=len(crit))

    # last health-check summary from cache
    from core.health_checker import get_all_health_status
    from core.data import get_vendors
    health = get_all_health_status() or {}
    last_at = ""
    h_ok = h_fail = h_unknown = 0
    for v in get_vendors():
        for k in v.get("keys") or []:
            if k.get("enabled") is False:
                continue
            h = health.get(f"{v.get('id')}:{k.get('id')}") or {}
            if h.get("healthy") is True:
                h_ok += 1
            elif h.get("healthy") is False:
                h_fail += 1
            else:
                h_unknown += 1
            ca = str(h.get("checked_at") or "")
            if ca and ca > last_at:
                last_at = ca

    return jsonify({
        "today": today_s,
        "month": month_s,
        "budgets": budgets,
        "alerts": alerts,
        "enforcement": enforcement,
        "as_of": now.isoformat(),
        "last_health": {
            "checked_at": last_at,
            "ok": h_ok,
            "fail": h_fail,
            "unknown": h_unknown,
        },
    })


@app.route("/api/stats/import", methods=["POST"])
def api_import_stats():
    """Force re-import usage from all known backend engines (OpenClaw, OpenCode, …)."""
    from core.usage_import import import_all_usage
    result = import_all_usage()
    log_event(
        "stats.import",
        added=result.get("added"),
        openclaw=int((result.get("openclaw") or {}).get("added") or 0),
        opencode=int((result.get("opencode") or {}).get("added") or 0),
    )
    return jsonify(result)


def _stats_chart_payload(records: list) -> dict:
    """Build lightweight chart series from usage records."""
    from collections import defaultdict
    from core.data import _normalize_model_name

    daily = defaultdict(lambda: {"count": 0, "success": 0, "fail": 0, "tokens": 0})
    by_model = defaultdict(lambda: {"count": 0, "tokens": 0})
    by_vendor = defaultdict(lambda: {"count": 0, "tokens": 0, "name": ""})
    by_source = defaultdict(lambda: {"count": 0, "tokens": 0})

    for r in records:
        ts = str(r.get("timestamp") or "")
        day = ts[:10] if len(ts) >= 10 else "unknown"
        ok = r.get("success", True)
        tokens = r.get("total_tokens", 0) or 0
        daily[day]["count"] += 1
        daily[day]["tokens"] += tokens
        if ok:
            daily[day]["success"] += 1
        else:
            daily[day]["fail"] += 1

        mid = _normalize_model_name(str(r.get("model") or "")) or (r.get("model") or "unknown")
        by_model[mid]["count"] += 1
        by_model[mid]["tokens"] += tokens

        vid = str(r.get("vendor_id") or r.get("vendor_name") or "unknown")
        by_vendor[vid]["count"] += 1
        by_vendor[vid]["tokens"] += tokens
        by_vendor[vid]["name"] = r.get("vendor_name") or vid

        src = str(r.get("source") or "unknown")
        by_source[src]["count"] += 1
        by_source[src]["tokens"] += tokens

    days = sorted(daily.keys())
    models = sorted(by_model.items(), key=lambda x: x[1]["count"], reverse=True)[:12]
    vendors = sorted(by_vendor.items(), key=lambda x: x[1]["count"], reverse=True)[:12]
    sources = sorted(by_source.items(), key=lambda x: x[1]["count"], reverse=True)

    src_labels = {"openclaw": "OpenClaw", "opencode": "OpenCode", "proxy": "Proxy (Manager)", "claude_code": "Claude Code", "codex_cli": "Codex CLI", "unknown": "Unknown"}
    return {
        "daily": {
            "labels": days,
            "count": [daily[d]["count"] for d in days],
            "success": [daily[d]["success"] for d in days],
            "fail": [daily[d]["fail"] for d in days],
            "tokens": [daily[d]["tokens"] for d in days],
        },
        "models": {
            "labels": [m for m, _ in models],
            "count": [v["count"] for _, v in models],
            "tokens": [v["tokens"] for _, v in models],
        },
        "vendors": {
            "labels": [v["name"] or k for k, v in vendors],
            "ids": [k for k, _ in vendors],
            "count": [v["count"] for _, v in vendors],
            "tokens": [v["tokens"] for _, v in vendors],
        },
        "sources": {
            "labels": [src_labels.get(k, k) for k, _ in sources],
            "ids": [k for k, _ in sources],
            "count": [v["count"] for _, v in sources],
            "tokens": [v["tokens"] for _, v in sources],
        },
    }


def _stats_filter_meta(from_ts: str = "", to_ts: str = "") -> dict:
    """Distinct filter options from all usage in range (unfiltered by vendor/model/source)."""
    from core.data import get_usage_records, _normalize_model_name
    records = get_usage_records(from_ts=from_ts, to_ts=to_ts, auto_import=False)
    vendors = {}
    models = set()
    sources = set()
    providers = set()
    for r in records:
        vid = str(r.get("vendor_id") or "")
        vname = r.get("vendor_name") or vid
        if vid or vname:
            vendors[vid or vname] = vname or vid
        mid = _normalize_model_name(str(r.get("model") or "")) or (r.get("model") or "")
        if mid:
            models.add(mid)
        if r.get("source"):
            sources.add(str(r.get("source")))
        if r.get("provider"):
            providers.add(str(r.get("provider")))
    src_labels = {
        "openclaw": "OpenClaw",
        "opencode": "OpenCode",
        "proxy": "Proxy (Manager)",
        "claude_code": "Claude Code",
        "codex_cli": "Codex CLI",
        "unknown": "Unknown",
    }
    # Always expose known engines so filters are complete even with sparse data
    known_sources = ["openclaw", "opencode", "proxy"]
    for s in known_sources:
        sources.add(s)
    ordered = []
    seen = set()
    for s in known_sources + sorted(sources):
        if s in seen:
            continue
        seen.add(s)
        ordered.append(s)
    return {
        "vendors": [{"id": k, "name": v} for k, v in sorted(vendors.items(), key=lambda x: (x[1] or "").lower())],
        "models": sorted(models, key=str.lower),
        "providers": sorted(providers, key=str.lower),
        "sources": [{"id": s, "name": src_labels.get(s, s)} for s in ordered],
    }


def _stats_filter_meta_from_records(records: list, from_ts: str = "", to_ts: str = "") -> dict:
    """Build filter options. Prefer in-range filtered records; fill missing dimensions from range if needed."""
    from core.data import get_usage_records, _normalize_model_name
    vendors = {}
    models = set()
    sources = set()
    providers = set()
    for r in records or []:
        vid = str(r.get("vendor_id") or "")
        vname = r.get("vendor_name") or vid
        if vid or vname:
            vendors[vid or vname] = vname or vid
        mid = _normalize_model_name(str(r.get("model") or "")) or (r.get("model") or "")
        if mid:
            models.add(mid)
        if r.get("source"):
            sources.add(str(r.get("source")))
        if r.get("provider"):
            providers.add(str(r.get("provider")))
    # When filters narrow the view, still expose engines present in the date range
    if from_ts or to_ts:
        try:
            all_in_range = get_usage_records(
                from_ts=from_ts, to_ts=to_ts, auto_import=False, estimate_cost=False,
            )
            for r in all_in_range:
                if r.get("source"):
                    sources.add(str(r.get("source")))
                vid = str(r.get("vendor_id") or "")
                vname = r.get("vendor_name") or vid
                if vid or vname:
                    vendors.setdefault(vid or vname, vname or vid)
                mid = _normalize_model_name(str(r.get("model") or "")) or (r.get("model") or "")
                if mid:
                    models.add(mid)
                if r.get("provider"):
                    providers.add(str(r.get("provider")))
        except Exception:
            pass
    src_labels = {
        "openclaw": "OpenClaw",
        "opencode": "OpenCode",
        "proxy": "Proxy (Manager)",
        "claude_code": "Claude Code",
        "codex_cli": "Codex CLI",
        "unknown": "Unknown",
    }
    known_sources = ["openclaw", "opencode", "proxy"]
    for s in known_sources:
        sources.add(s)
    ordered = []
    seen = set()
    for s in known_sources + sorted(sources):
        if s in seen:
            continue
        seen.add(s)
        ordered.append(s)
    return {
        "vendors": [{"id": k, "name": v} for k, v in sorted(vendors.items(), key=lambda x: (x[1] or "").lower())],
        "models": sorted(models, key=str.lower),
        "providers": sorted(providers, key=str.lower),
        "sources": [{"id": s, "name": src_labels.get(s, s)} for s in ordered],
    }


@app.route("/api/stats", methods=["GET"])
def api_get_stats():
    from core.data import get_usage_records, get_usage_summary
    from_ts = request.args.get("from", "")
    to_ts = request.args.get("to", "")
    vendor_id = request.args.get("vendor_id", "")
    key_id = request.args.get("key_id", "")
    provider = request.args.get("provider", "")
    model = request.args.get("model", "")
    source = request.args.get("source", "")  # openclaw|proxy|...
    group_by = request.args.get("group_by", "vendor")  # vendor|key|provider|model|source|request
    include_records = request.args.get("include_records", "0") == "1"
    include_charts = request.args.get("include_charts", "1") != "0"
    include_meta = request.args.get("include_meta", "1") != "0"
    limit = min(int(request.args.get("limit", "100") or 100), 1000)
    # offset applied when returning records

    # Single pass: load + estimate once (summary/meta reuse this list)
    records = get_usage_records(
        from_ts=from_ts, to_ts=to_ts,
        vendor_id=vendor_id, key_id=key_id, provider=provider, model=model,
        source=source, auto_import=True,
    )
    summary = []
    if group_by != "request":
        summary = get_usage_summary(
            from_ts=from_ts, to_ts=to_ts, group_by=group_by,
            vendor_id=vendor_id, key_id=key_id, provider=provider, model=model,
            source=source, records=records,
        )

    success_count = sum(1 for r in records if r.get("success", True))
    fail_count = len(records) - success_count
    total_tokens = sum(r.get("total_tokens", 0) or 0 for r in records)
    total_cost = sum(r.get("cost", 0) or 0 for r in records)
    estimated_n = sum(1 for r in records if r.get("_cost_estimated"))
    reported_n = sum(1 for r in records if (r.get("cost") or 0) > 0 and not r.get("_cost_estimated"))

    out = {
        "summary": summary,
        "total": {
            "count": len(records),
            "success_count": success_count,
            "fail_count": fail_count,
            "success_rate": round(success_count / len(records) * 100, 1) if records else 0,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "cost_estimated": estimated_n > 0,
            "cost_estimated_count": estimated_n,
            "cost_reported_count": reported_n,
        },
        "filters": {
            "from": from_ts,
            "to": to_ts,
            "vendor_id": vendor_id,
            "key_id": key_id,
            "provider": provider,
            "model": model,
            "source": source,
            "group_by": group_by,
        },
    }
    if include_charts:
        out["charts"] = _stats_chart_payload(records)
    if include_meta:
        # Prefer fast path from already-loaded records; only rescan range without cost
        if not vendor_id and not key_id and not provider and not model and not source:
            out["meta"] = _stats_filter_meta_from_records(records, from_ts="", to_ts="")
        else:
            out["meta"] = _stats_filter_meta_from_records(records, from_ts, to_ts)
    if include_records or group_by == "request":
        q = (request.args.get("q") or request.args.get("search") or "").strip().lower()
        filtered = records
        if q:
            def _match(r: dict) -> bool:
                hay = " ".join([
                    str(r.get("vendor_name") or ""),
                    str(r.get("key_name") or ""),
                    str(r.get("model") or ""),
                    str(r.get("provider") or ""),
                    str(r.get("source") or ""),
                    str(r.get("error") or ""),
                ]).lower()
                return q in hay
            filtered = [r for r in records if _match(r)]
        offset = max(int(request.args.get("offset", "0") or 0), 0)
        page = filtered[offset:offset + limit]
        out["records"] = page
        out["pagination"] = {
            "offset": offset,
            "limit": limit,
            "total": len(filtered),
            "total_unfiltered": len(records),
            "has_more": offset + limit < len(filtered),
            "q": q,
        }
    return jsonify(out)


@app.route("/api/stats/export", methods=["GET"])
def api_export_stats():
    """Export filtered usage as CSV or JSON."""
    import csv
    import io
    from core.data import get_usage_records

    from_ts = request.args.get("from", "")
    to_ts = request.args.get("to", "")
    vendor_id = request.args.get("vendor_id", "")
    key_id = request.args.get("key_id", "")
    provider = request.args.get("provider", "")
    model = request.args.get("model", "")
    source = request.args.get("source", "")
    fmt = (request.args.get("format") or "csv").lower()

    records = get_usage_records(
        from_ts=from_ts, to_ts=to_ts,
        vendor_id=vendor_id, key_id=key_id, provider=provider, model=model,
        source=source, auto_import=True,
    )
    fields = [
        "timestamp", "vendor_id", "vendor_name", "key_id", "key_name",
        "provider", "model", "source", "success", "status_code",
        "total_tokens", "prompt_tokens", "completion_tokens", "cost",
        "elapsed_ms", "error",
    ]
    if fmt == "json":
        body = json.dumps({"count": len(records), "records": records}, ensure_ascii=False, indent=2)
        resp = make_response(body)
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = "attachment; filename=usage-export.json"
        return resp

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in records:
        row = {f: r.get(f, "") for f in fields}
        row["success"] = "1" if r.get("success", True) else "0"
        writer.writerow(row)
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=usage-export.csv"
    return resp


@app.route("/api/stats/record", methods=["POST"])
def api_add_stat_record():
    from core.data import add_usage_record
    body = request.get_json() or {}
    required = ("timestamp", "vendor_id", "key_id", "provider", "total_tokens")
    for f in required:
        if f not in body:
            return jsonify({"error": f"missing field: {f}"}), 400
    record = add_usage_record(body)
    return jsonify({"success": True, "record": record})


@app.route("/api/vendors/simple", methods=["GET"])
def api_list_vendors_simple():
    vendors = get_vendors()
    return jsonify([{"id": v["id"], "name": v["name"], "provider": v["provider"]}
                    for v in vendors])


# ── Startup ────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser

    default_port = int(os.environ.get("AI_SWITCH_PORT", "8787"))
    port = default_port
    last_err = None

    for _ in range(20):
        try:
            url = f"http://127.0.0.1:{port}"
            print(f" AI Switch running at {url}")
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()
            reconcile_all()
            app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
            break
        except OSError as e:
            last_err = e
            log.warning("Port %d in use, trying %d...", port, port + 1)
            port += 1
        except Exception as e:
            last_err = e
            break

    if last_err:
        log.error("Failed to start: %s", last_err)
        print(f"\n Failed to start server: {last_err}")
        print(f" Try: AI_SWITCH_PORT=<port> python3 app.py")
        sys.exit(1)
