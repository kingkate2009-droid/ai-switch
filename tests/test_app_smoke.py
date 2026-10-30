"""End-to-end smoke test: the Flask app boots and serves a usable shell.

This is the cheapest guard against a whole class of regressions -- a template
that no longer renders, a broken route decorator, a syntax error in the inline
UI script, an import-time crash. It needs no browser and no network.

It runs against a **throwaway data directory** (a temporary ``HOME``), so it
never reads or writes the developer's real AI Switch data, and the background
health scheduler is disabled.

Run standalone:  python3 tests/test_app_smoke.py
Or with pytest:  pytest tests/test_app_smoke.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Must happen before core.data is imported: DATA_DIR is resolved at import time.
_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-smoke-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ["AI_SWITCH_NO_SCHEDULER"] = "1"
os.environ["AI_SWITCH_OPEN_BROWSER"] = "0"
os.environ["AI_SWITCH_NO_BROWSER"] = "1"
os.environ.pop("AI_SWITCH_ACCESS_TOKEN", None)
os.environ.pop("AI_SWITCH_REQUIRE_AUTH", None)

from app import app  # noqa: E402
import core.data as _core_data  # noqa: E402

# Fail loudly rather than silently running against the developer's real data if
# some module was imported before HOME was redirected (see tests/conftest.py).
if not str(_core_data.DATA_DIR).startswith(_TMP_HOME):
    raise RuntimeError(
        "smoke test data isolation failed: core.data.DATA_DIR="
        f"{_core_data.DATA_DIR} is outside the throwaway HOME {_TMP_HOME}. "
        "Add tests/conftest.py to your pytest run or set HOME before importing app."
    )


def _client():
    app.config["TESTING"] = True
    return app.test_client()


def test_shell_renders():
    resp = _client().get("/")
    assert resp.status_code == 200, resp.status_code
    html = resp.get_data(as_text=True)
    for marker in ('id="authGate"', "keysTable", "<script>"):
        assert marker in html, f"shell is missing {marker!r}"


def test_static_css_served():
    resp = _client().get("/static/style.css")
    assert resp.status_code == 200, resp.status_code
    assert len(resp.get_data()) > 1000, "style.css looks empty"


def test_version_and_auth_status():
    c = _client()
    version = c.get("/api/version")
    assert version.status_code == 200, version.status_code
    assert version.get_json(), "empty version payload"

    status = c.get("/api/auth/status")
    assert status.status_code == 200, status.status_code
    body = status.get_json()
    # The smoke test runs on a loopback-equivalent config with no token set.
    assert body.get("auth_required") is False, body
    assert body.get("authenticated") is True, body


def test_core_read_endpoints():
    c = _client()
    for path in ("/api/vendors", "/api/backends", "/api/settings", "/api/providers"):
        resp = c.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


def test_vendor_create_round_trip():
    """A write path against the throwaway DB."""
    c = _client()
    created = c.post("/api/vendors", json={
        "name": "Smoke Vendor",
        "provider": "openai",
        "api_url": "https://api.example.com/v1",
    })
    assert created.status_code == 201, (created.status_code, created.get_data(as_text=True)[:300])
    vendor = created.get_json()
    assert vendor.get("id"), vendor

    listed = c.get("/api/vendors").get_json()
    vendors = listed if isinstance(listed, list) else listed.get("vendors", [])
    assert any(v.get("id") == vendor["id"] for v in vendors), "created vendor is not listed"


TESTS = (
    test_shell_renders,
    test_static_css_served,
    test_version_and_auth_status,
    test_core_read_endpoints,
    test_vendor_create_round_trip,
)

if __name__ == "__main__":
    failures = 0
    try:
        for fn in TESTS:
            try:
                fn()
                print(f"PASS  {fn.__name__}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {fn.__name__}\n      {exc}")
        print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    sys.exit(1 if failures else 0)
