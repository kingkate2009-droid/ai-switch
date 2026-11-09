"""Vendor custom request headers: storage, round-trip, and OpenCode config.

A vendor may carry a map of extra request headers (User-Agent, originator,
OpenAI-Beta, ...). Those headers must:

1. Survive create/update and be readable from ``/api/vendors``.
2. Be copied into the generated OpenCode provider block so OpenCode sends them.
3. Be normalised (blank names dropped) and never invent values.

Run standalone:  python3 tests/test_vendor_headers.py
Or with pytest:  pytest tests/test_vendor_headers.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-headers-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ["AI_SWITCH_NO_SCHEDULER"] = "1"
os.environ["AI_SWITCH_OPEN_BROWSER"] = "0"
os.environ["AI_SWITCH_NO_BROWSER"] = "1"
os.environ.pop("AI_SWITCH_ACCESS_TOKEN", None)
os.environ.pop("AI_SWITCH_REQUIRE_AUTH", None)

from app import app                      # noqa: E402
from core.data import vendor_headers     # noqa: E402

HEADERS = {
    "User-Agent": "codex_cli_rs/0.167.0 (Windows 11; x86_64)",
    "originator": "codex_cli_rs",
    "x-codex-window-id": "8D419FB6-1B95-0682-147F-8EFB705A582E",
    "version": "0.167.0",
    "session-id": "8D419FB6-1B95-0682-147F-8EFB705A582E",
    "OpenAI-Beta": "responses=experimental",
}


def _client():
    app.config.update(TESTING=True)
    return app.test_client()


_seq = [0]


def _create_vendor(client):
    _seq[0] += 1
    resp = client.post("/api/vendors", json={
        "name": "Headers %d" % _seq[0],
        "provider": "custom",
        "api_url": "https://headers-%d.example/v1" % _seq[0],
        "endpoint_type": "openai",
        "headers": HEADERS,
    })
    assert resp.status_code == 201, resp.status_code
    return resp.get_json()


def _vendor_row(client, vendor_id):
    rows = client.get("/api/vendors").get_json()["vendors"]
    return next(x for x in rows if str(x["id"]) == str(vendor_id))


def test_headers_round_trip():
    """Create a vendor with headers and read them back unchanged."""
    client = _client()
    created = _create_vendor(client)
    assert created["headers"] == HEADERS, created
    assert _vendor_row(client, created["id"])["headers"] == HEADERS


def test_headers_update_and_clear():
    """Editing headers replaces the set; an empty map clears it."""
    client = _client()
    v = _create_vendor(client)

    resp = client.put("/api/vendors/%s" % v["id"], json={"headers": {"X-Test": "1"}})
    assert resp.status_code == 200, resp.status_code
    assert _vendor_row(client, v["id"])["headers"] == {"X-Test": "1"}

    resp = client.put("/api/vendors/%s" % v["id"], json={"headers": {}})
    assert resp.status_code == 200, resp.status_code
    assert _vendor_row(client, v["id"])["headers"] == {}


def test_opencode_config_includes_headers():
    """The generated OpenCode provider must carry options.headers."""
    client = _client()
    v = _create_vendor(client)
    resp = client.post("/api/vendors/%s/keys" % v["id"], json={
        "name": "k1",
        "api_key": "sk-test-headers",
        "skip_health_check": True,
    })
    assert resp.status_code == 201, resp.status_code
    kid = resp.get_json()["key"]["id"]

    cfg = client.get("/api/vendors/%s/keys/%s/opencode-config" % (v["id"], kid))
    assert cfg.status_code == 200, cfg.status_code
    body = cfg.get_json()
    assert body["is_custom"] is True, body
    assert body["provider_block"]["options"]["headers"] == HEADERS, body


def test_blank_header_names_are_dropped():
    """Normalisation drops empty names and returns {} for missing data."""
    assert vendor_headers({"headers": {"  ": "x", "A": "b"}}) == {"A": "b"}
    assert vendor_headers({"headers": {"A": None}}) == {"A": ""}
    assert vendor_headers({}) == {}
    assert vendor_headers(None) == {}


def test_proxy_applies_and_guards_headers():
    """The proxy sends vendor headers, overriding client values, but keeps auth."""
    import app as app_module

    client = _client()
    v = _create_vendor(client)
    vendor = {
        "id": v["id"],
        "endpoint_type": "openai",
        "api_url": "https://headers.example/v1",
        "headers": dict(HEADERS, Authorization="Bearer ATTACKER", Host="evil"),
    }
    key = {"api_key": "sk-real"}

    captured = {}
    original = app_module.py_requests.request

    class _FakeResp:
        status_code = 200

    def _fake_request(**kwargs):
        captured.update(kwargs)
        return _FakeResp()

    app_module.py_requests.request = _fake_request
    try:
        with app_module.app.test_request_context(
            "/v1/chat/completions", method="POST",
            headers={"User-Agent": "client-ua", "X-Client": "1"},
        ):
            app_module._proxy_upstream(
                vendor, key,
                path="chat/completions",
                body=b"{}",
                headers={"User-Agent": "client-ua", "X-Client": "1"},
                stream=False,
            )
    finally:
        app_module.py_requests.request = original

    sent = captured["headers"]
    # Vendor header wins over the forwarded client value.
    assert sent["User-Agent"] == HEADERS["User-Agent"], sent
    assert sent["originator"] == "codex_cli_rs", sent
    # Client headers with no vendor override are preserved.
    assert sent["X-Client"] == "1", sent
    # Auth and transport headers are never clobbered by custom headers.
    assert sent["Authorization"] == "Bearer sk-real", sent
    assert sent.get("Host") != "evil", sent


TESTS = (
    test_headers_round_trip,
    test_headers_update_and_clear,
    test_opencode_config_includes_headers,
    test_blank_header_names_are_dropped,
    test_proxy_applies_and_guards_headers,
)

if __name__ == "__main__":
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}\n      {exc}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)
