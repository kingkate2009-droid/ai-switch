#!/usr/bin/env python3
"""AI Switch — Unified AI API Key & Backend Management."""
from __future__ import annotations

import os
import sys
import threading
import webbrowser

from app import app
from core.version import get_version

app.config["TEMPLATES_AUTO_RELOAD"] = True
try:
    app.jinja_env.auto_reload = True
except Exception:
    pass


@app.after_request
def _no_cache_static(resp):
    # Avoid stale UI after rapid frontend edits (dev only)
    if os.environ.get("AI_SWITCH_NO_CACHE", "1") == "1":
        try:
            from flask import request
            if request.path.startswith("/static/") or request.path == "/":
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                resp.headers["Pragma"] = "no-cache"
        except Exception:
            pass
    return resp


def _open_browser(url: str) -> None:
    if os.environ.get("AI_SWITCH_NO_BROWSER", "").lower() in ("1", "true", "yes"):
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("AI_SWITCH_PORT", "8787"))
    host = os.environ.get("AI_SWITCH_HOST", "127.0.0.1")
    version = get_version()
    url = f"http://{host}:{port}"
    print(f"AI Switch v{version}")
    print(f"Running at {url}")
    if os.environ.get("AI_SWITCH_OPEN_BROWSER", "1").lower() in ("1", "true", "yes"):
        threading.Timer(0.8, _open_browser, args=(url,)).start()
    try:
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except OSError as e:
        print(f"Port {port} is already in use.")
        print(f"Stop the process using port {port}, then start AI Switch again.")
        sys.exit(1)
