import logging
import os
import sys
import threading
import traceback

import requests as py_requests
from flask import Flask, Response, jsonify, render_template, request, make_response

from core.batch_import import parse_batch_text
from core.providers import get_providers, recognize_provider
from core.data import (
    add_key,
    add_vendor,
    delete_key,
    delete_vendor,
    get_key,
    get_keys,
    get_settings,
    get_vendor,
    get_vendors,
    update_key,
    update_key_data,
    update_settings,
    update_vendor,
)
from core.health_checker import check_all_keys, check_key_health, get_all_health_status
from core.i18n import SUPPORTED_LANGS, get_translations, resolve_lang, t as _t
from backends import init_backends, get as get_backend, get_all as get_all_backends, \
    on_key_added, on_key_updated, on_key_removed, on_vendor_removed, reconcile_all

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[ai-switch] %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

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
        if default_model:
            updates["default_model"] = default_model
        updated_key = update_key_data(vendor_id, k["id"], **updates)
        on_key_added(v, updated_key or k)
    else:
        update_key_data(vendor_id, k["id"], enabled=False)
        on_key_removed(v, k)
        k["enabled"] = False

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
        default_model = health.get("default_model", "")
        updates = {"enabled": True}
        if models:
            updates["models"] = models
        if default_model:
            updates["default_model"] = default_model
        updated_key = update_key_data(vendor_id, key_id, **updates)
        if updated_key and updated_key.get("models"):
            on_key_updated(v, updated_key)
        else:
            on_key_added(v, k)
    elif v and k and not health.get("healthy"):
        update_key_data(vendor_id, key_id, enabled=False)
        on_key_removed(v, k)
    reconcile_all()
    return jsonify(health)


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

@app.route("/api/batch-import/parse", methods=["POST"])
def api_batch_parse():
    data = request.get_json() or {}
    text = data.get("text", "")
    if not text.strip():
        return jsonify({"error": "text is required"}), 400
    entries = parse_batch_text(text)
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/api/batch-import/apply", methods=["POST"])
def api_batch_apply():
    data = request.get_json() or {}
    entries = data.get("entries", [])
    if not entries:
        return jsonify({"error": "entries are required"}), 400
    created = []
    errors = []
    for entry in entries:
        provider = entry.get("provider", "unknown")
        api_url = entry.get("api_url", "").rstrip("/")
        api_key = entry.get("api_key", "")
        name = entry.get("name", api_key[:12])
        endpoint_type = entry.get("endpoint_type", "openai")

        if not api_key or not api_url:
            continue

        try:
            vendor = None
            for v in get_vendors():
                existing_url = v.get("api_url", "").rstrip("/")
                if existing_url == api_url:
                    vendor = v
                    break
            if not vendor:
                for v in get_vendors():
                    existing_url = v.get("api_url", "").rstrip("/")
                    if v["provider"] == provider and (not api_url or not existing_url):
                        vendor = v
                        break

            if not vendor:
                vendor = add_vendor(
                    name=provider.replace("-", " ").title(),
                    provider=provider,
                    api_url=api_url,
                    endpoint_type=endpoint_type,
                )

            key_exists = any(k["name"] == name or k["api_key"] == api_key for k in vendor.get("keys", []))
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
                    created.append({"vendor": vendor["name"], "key": name, "api_key": api_key[:8] + "...", "healthy": health.get("healthy")})
            else:
                created.append({"vendor": vendor["name"], "key": name, "api_key": api_key[:8] + "...", "skipped": True})
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

@app.route("/api/proxy/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def api_proxy(subpath):
    auth = request.headers.get("Authorization", "")
    target_url = ""
    for v in get_vendors():
        pt = v.get("proxy_target", "")
        if not pt:
            continue
        for k in v.get("keys", []):
            if k.get("api_key") and f"Bearer {k['api_key']}" == auth:
                target_url = pt.rstrip("/")
                break
        if target_url:
            break
    if not target_url:
        for v in get_vendors():
            pt = v.get("proxy_target", "")
            if pt:
                target_url = pt.rstrip("/")
                break

    url = f"{target_url}/{subpath}"
    headers = {k: v for k, v in request.headers if k.lower() not in ("host", "content-length")}
    is_stream = request.headers.get("accept", "") == "text/event-stream" or \
                (request.is_json and request.get_json(silent=True) or {}).get("stream")

    try:
        r = py_requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            stream=is_stream,
            verify=False,
            timeout=120,
        )
        if is_stream:
            def generate():
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
            return Response(generate(), status=r.status_code, headers=dict(r.headers))
        return Response(r.content, status=r.status_code, headers=dict(r.headers))
    except Exception as e:
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
    resp = jsonify({"lang": lang})
    resp.set_cookie("lang", lang, max_age=86400 * 365)
    return resp


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
