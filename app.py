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
    add_vendor,
    delete_key,
    delete_vendor,
    get_enabled_models,
    get_key,
    get_keys,
    get_settings,
    get_vendor,
    get_vendors,
    list_model_ids,
    set_model_enabled,
    update_key,
    update_key_data,
    update_settings,
    update_vendor,
)
from core.health_checker import (
    check_all_keys,
    check_key_health,
    check_key_models,
    get_all_health_status,
)
from core.i18n import SUPPORTED_LANGS, get_translations, resolve_lang, t as _t
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
    return jsonify({
        "version": get_version(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
    })


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


# ── Vendors ────────────────────────────────────────────────

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
    )
    return jsonify(v), 201


@app.route("/api/vendors/<vendor_id>", methods=["PUT"])
def api_update_vendor(vendor_id):
    data = request.get_json() or {}
    v = update_vendor(vendor_id, **data)
    if not v:
        return jsonify({"error": "not found"}), 404
    return jsonify(v)


@app.route("/api/vendors/<vendor_id>", methods=["DELETE"])
def api_delete_vendor(vendor_id):
    v = get_vendor(vendor_id)
    if v:
        on_vendor_removed(v)
    if delete_vendor(vendor_id):
        return jsonify({"success": True})
    return jsonify({"error": "not found"}), 404


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
    k = add_key(vendor_id, data["name"], data["api_key"])
    if not k:
        return jsonify({"error": "vendor not found"}), 404

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
        update_key_data(vendor_id, k["id"], enabled=False)
        on_key_removed(v, k)
        k["enabled"] = False
    reconcile_all()

    return jsonify({"key": k, "health": health}), 201


@app.route("/api/vendors/<vendor_id>/keys/<key_id>", methods=["PUT"])
def api_update_key(vendor_id, key_id):
    data = request.get_json() or {}
    k = update_key(vendor_id, key_id, **data)
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
    if delete_key(vendor_id, key_id):
        return jsonify({"success": True})
    return jsonify({"error": "not found"}), 404


@app.route("/api/vendors/<vendor_id>/keys/<key_id>/health", methods=["GET"])
def api_check_key_health(vendor_id, key_id):
    health = check_key_health(vendor_id, key_id)
    v = get_vendor(vendor_id)
    k = get_key(vendor_id, key_id)
    if v and k and health.get("healthy"):
        models = health.get("models", [])
        default_model = health.get("default_model", "") or k.get("default_model", "")
        updates = {"enabled": True}
        # Keep previous models if scan returned empty
        if models:
            updates["models"] = models
        elif not k.get("models") and default_model:
            updates["models"] = [default_model]
        if default_model:
            updates["default_model"] = default_model
        updated_key = update_key_data(vendor_id, key_id, **updates) or k
        # Always re-sync full config to backends after successful probe
        on_key_updated(v, updated_key)
    elif v and k and not health.get("healthy"):
        update_key_data(vendor_id, key_id, enabled=False)
        on_key_removed(v, k)
    reconcile_all()
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

@app.route("/api/health/check-all", methods=["POST"])
def api_health_check_all():
    results = check_all_keys()
    reconcile_all()
    return jsonify({"results": results})


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
    data = request.get_json() or {}
    entries = data.get("entries", [])
    if not entries:
        entries = parse_batch_text(data.get("text", ""))
    if not entries:
        return jsonify({"error": "no entries to import"}), 400
    created = []
    errors = []
    used_names = {(v.get("name") or "").lower() for v in get_vendors()}
    for entry in entries:
        provider = (entry.get("provider") or "custom").strip() or "custom"
        api_url = (entry.get("api_url") or "").strip().rstrip("/")
        api_key = (entry.get("api_key") or "").strip()
        name = (entry.get("name") or "").strip() or (
            f"key-{(api_key[-4:] if len(api_key) >= 4 else api_key)}" if api_key else "key"
        )
        endpoint_type = (entry.get("endpoint_type") or "openai").strip().lower() or "openai"
        if endpoint_type not in ("openai", "anthropic"):
            endpoint_type = "anthropic" if (
                "/anthropic" in api_url.lower() or "api.anthropic.com" in api_url.lower()
            ) else "openai"
        vendor_name = (entry.get("vendor_name") or entry.get("vendor") or "").strip()

        if not api_key or not api_url:
            errors.append({
                "entry": vendor_name or name or provider,
                "error": "api_url and api_key are required",
            })
            continue

        try:
            vendor = None
            # Match only by URL (unique). Never reuse a different URL with same provider name.
            for v in get_vendors():
                existing_url = (v.get("api_url") or "").rstrip("/")
                if existing_url and existing_url == api_url:
                    vendor = v
                    break

            if not vendor:
                base_name = vendor_name or provider.replace("-", " ").title() or "Provider"
                final_name = base_name
                n = 2
                while final_name.lower() in used_names:
                    final_name = f"{base_name}-{n}"
                    n += 1
                used_names.add(final_name.lower())
                vendor = add_vendor(
                    name=final_name,
                    provider=provider,
                    api_url=api_url,
                    endpoint_type=endpoint_type,
                )
            else:
                # Optional renames / provider / endpoint updates from editable import
                from core.data import update_vendor
                updates = {}
                if vendor_name and vendor_name != vendor.get("name"):
                    if vendor_name.lower() not in used_names or vendor_name.lower() == (vendor.get("name") or "").lower():
                        updates["name"] = vendor_name
                        used_names.discard((vendor.get("name") or "").lower())
                        used_names.add(vendor_name.lower())
                if provider and provider != vendor.get("provider"):
                    updates["provider"] = provider
                if endpoint_type and endpoint_type != vendor.get("endpoint_type"):
                    updates["endpoint_type"] = endpoint_type
                if updates:
                    update_vendor(vendor["id"], **updates)
                    vendor = get_vendor(vendor["id"]) or vendor

            key_exists = any(
                k.get("api_key") == api_key or k.get("name") == name
                for k in vendor.get("keys", [])
            )
            if not key_exists:
                k = add_key(vendor["id"], name, api_key)
                if k:
                    health = check_key_health(vendor["id"], k["id"])
                    if health.get("healthy"):
                        models = health.get("models", [])
                        default_model = health.get("default_model", "")
                        updates = {"enabled": True}
                        if models:
                            updates["models"] = models
                        if default_model:
                            updates["default_model"] = default_model
                        updated_key = update_key_data(vendor["id"], k["id"], **updates)
                        on_key_added(vendor, updated_key or k)
                    else:
                        update_key_data(vendor["id"], k["id"], enabled=False)
                        on_key_removed(vendor, k)
                    created.append({
                        "vendor": vendor["name"],
                        "key": name,
                        "api_key": api_key[:8] + "...",
                        "healthy": health.get("healthy"),
                    })
            else:
                created.append({
                    "vendor": vendor["name"],
                    "key": name,
                    "api_key": api_key[:8] + "...",
                    "skipped": True,
                })
        except Exception as e:
            log.error("Batch import entry failed: %s", e)
            errors.append({"entry": name, "error": str(e)})

    return jsonify({"created": created, "errors": errors, "count": len(created)})


# ── Sync ───────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
def api_sync():
    from backends import get_all as get_all_backends
    total = 0
    results = {}
    for name, adapter in get_all_backends().items():
        try:
            imported = adapter.sync_from_backend()
            if imported:
                from core.data import add_vendor, add_key
                for v in imported:
                    vendor_id = None
                    for existing in get_vendors():
                        if existing.get("provider", "").lower() == v.get("provider", "").lower():
                            vendor_id = existing["id"]
                            break
                    if not vendor_id:
                        created = add_vendor(v["name"], v["provider"], v.get("api_url", ""), v.get("endpoint_type", "openai"))
                        if created:
                            vendor_id = created["id"]
                    if vendor_id:
                        for k in v.get("keys", []):
                            add_key(vendor_id, k["name"], k["api_key"])
            total += len(imported)
            results[name] = len(imported)
        except Exception as e:
            results[name] = f"error: {e}"
    return jsonify({"synced": total, "results": results})


# ── Gateway ────────────────────────────────────────────────

@app.route("/api/gateway/status", methods=["GET"])
def api_gateway_status():
    ocp = get_backend("openclaw")
    if ocp:
        status = ocp.get_status()
        health = get_all_health_status()
        status["health"] = health
        status["openclaw_version"] = ocp.get_version()
        status["manager_version"] = "2.0.0"
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
        return jsonify(ssh_test_connection(data))
    elif conn_type == "gateway":
        return jsonify(gateway_test_connection(data))
    return jsonify({"success": False, "message": "unknown type"})


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
    return jsonify(get_settings())


@app.route("/api/settings", methods=["POST"])
def api_update_settings():
    data = request.get_json() or {}
    s = update_settings(**data)
    return jsonify(s)


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


@app.route("/api/stats/import", methods=["POST"])
def api_import_stats():
    """Force re-import usage from OpenClaw session transcripts."""
    from core.usage_import import import_openclaw_usage, purge_synthetic_usage
    purged = purge_synthetic_usage()
    result = import_openclaw_usage()
    result["purged"] = purged
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
            source=source,
        )

    success_count = sum(1 for r in records if r.get("success", True))
    fail_count = len(records) - success_count
    total_tokens = sum(r.get("total_tokens", 0) or 0 for r in records)
    total_cost = sum(r.get("cost", 0) or 0 for r in records)

    out = {
        "summary": summary,
        "total": {
            "count": len(records),
            "success_count": success_count,
            "fail_count": fail_count,
            "success_rate": round(success_count / len(records) * 100, 1) if records else 0,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
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
        out["meta"] = _stats_filter_meta(from_ts, to_ts)
    if include_records or group_by == "request":
        out["records"] = records[:limit]
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
