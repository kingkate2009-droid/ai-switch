#!/usr/bin/env python3
"""AI Switch — Unified AI API Key & Backend Management."""
from app import app

if __name__ == "__main__":
    import os
    default_port = int(os.environ.get("AI_SWITCH_PORT", "8787"))
    port = default_port
    for _ in range(20):
        try:
            url = f"http://127.0.0.1:{port}"
            print(f" AI Switch running at {url}")
            app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
            break
        except OSError:
            print(f"Port {port} in use, trying {port + 1}...")
            port += 1
