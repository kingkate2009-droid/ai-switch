"""Balance access token lifecycle: set, keep, clear.

The balance token is the site's *system* access token (New API / One API /
sub2api), not the ``sk-`` inference key. Two properties matter:

1. Editing a key must not wipe a token the user did not touch — otherwise a
   rename would silently break balance lookups.
2. The token must be removable. It used to be impossible: the client only sent
   ``access_token`` when the field was non-empty, so once set it could be
   replaced but never cleared, which also kept the vendor permanently inside the
   check-in flow (the check-in page keys off ``access_token_set``).

Run standalone:  python3 tests/test_balance_token.py
Or with pytest:  pytest tests/test_balance_token.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-baltok-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ["AI_SWITCH_NO_SCHEDULER"] = "1"
os.environ["AI_SWITCH_OPEN_BROWSER"] = "0"
os.environ["AI_SWITCH_NO_BROWSER"] = "1"
os.environ.pop("AI_SWITCH_ACCESS_TOKEN", None)
os.environ.pop("AI_SWITCH_REQUIRE_AUTH", None)

from app import app                      # noqa: E402
from core.accounts import PROVIDERS     # noqa: E402,F401  (keeps import order stable)


def _client():
    app.config.update(TESTING=True)
    return app.test_client()


def _unique(tag):
    """Route each test through its own vendor.

    ``create_vendor`` reuses an existing vendor with the same ``api_url``, so
    without a unique URL every test would mutate the same row and leak state
    into the next one.
    """
    _unique.seq += 1
    return "%s-%d" % (tag, _unique.seq)


_unique.seq = 0


def _make_vendor(client, name="Relay"):
    tag = _unique(name)
    resp = client.post("/api/vendors", json={
        "name": tag,
        "provider": "openai",
        "api_url": "https://relay-%s.example/v1" % tag,
        "skip_health_check": True,
    })
    assert resp.status_code == 201, resp.status_code
    return resp.get_json()["id"]


def _make_key(client, vendor_id, name="k1", access_token=None):
    tag = _unique(name)
    payload = {
        "name": tag,
        "api_key": "sk-test-%s" % tag,
        "skip_health_check": True,
    }
    if access_token is not None:
        payload["access_token"] = access_token
    resp = client.post("/api/vendors/%s/keys" % vendor_id, json=payload)
    assert resp.status_code == 201, resp.status_code
    return resp.get_json()["key"]["id"]


def _key(client, vendor_id, key_id):
    resp = client.get("/api/vendors")
    assert resp.status_code == 200, resp.status_code
    body = resp.get_json()
    vendors = body.get("vendors") if isinstance(body, dict) else body
    for vendor in vendors or []:
        if str(vendor.get("id")) != str(vendor_id):
            continue
        for k in vendor.get("keys") or []:
            if str(k["id"]) == str(key_id):
                return k
    raise AssertionError("key %s missing from vendor %s" % (key_id, vendor_id))


def test_token_is_never_echoed_but_its_presence_is():
    """The secret itself must not leave the server; only a boolean does."""
    client = _client()
    vid = _make_vendor(client, "Echo")
    kid = _make_key(client, vid, access_token="system-token-abc")

    k = _key(client, vid, kid)
    assert "access_token" not in k, k
    assert k["access_token_set"] is True, k


def test_editing_other_fields_keeps_the_token():
    """A rename must not silently drop a token the user never looked at."""
    client = _client()
    vid = _make_vendor(client, "Keep")
    kid = _make_key(client, vid, access_token="system-token-keep")

    resp = client.put("/api/vendors/%s/keys/%s" % (vid, kid), json={"name": "renamed"})
    assert resp.status_code == 200, resp.status_code
    assert _key(client, vid, kid)["access_token_set"] is True


def test_empty_access_token_clears_it():
    """Explicit ``access_token: ""`` must remove the saved value."""
    client = _client()
    vid = _make_vendor(client, "Clear")
    kid = _make_key(client, vid, access_token="system-token-clear")
    assert _key(client, vid, kid)["access_token_set"] is True

    resp = client.put("/api/vendors/%s/keys/%s" % (vid, kid),
                      json={"name": "k1", "access_token": ""})
    assert resp.status_code == 200, resp.status_code
    cleared = _key(client, vid, kid)
    assert cleared["access_token_set"] is False, cleared


def test_cleared_token_drops_the_vendor_from_checkin():
    """The check-in page keys off ``/api/vendors`` + ``access_token_set``.

    ``_vendorTokenKey()`` in the template only returns a key when that flag is
    true, and both the balance column and the check-in action are gated on it —
    so clearing the token ends the vendor's participation, which is the whole
    point of being able to remove it.
    """
    client = _client()
    vid = _make_vendor(client, "Checkin")
    kid = _make_key(client, vid, access_token="system-token-ci")

    def token_vendors():
        # Mirrors _vendorTokenKey() in templates/index.html.
        rows = client.get("/api/vendors").get_json()["vendors"]
        return [
            v["id"] for v in rows
            if v.get("access_token_set")
            or any(k.get("access_token_set") for k in (v.get("keys") or []))
        ]

    assert vid in token_vendors(), "vendor %s should be in the check-in flow: %r" % (vid, token_vendors())

    client.put("/api/vendors/%s/keys/%s" % (vid, kid), json={"access_token": ""})
    assert vid not in token_vendors(), "vendor %s should leave the check-in flow: %r" % (vid, token_vendors())


def test_checkin_status_never_claims_via_get():
    """The status endpoint must stay read-only; only POST /checkin may claim."""
    import inspect

    from core import balance

    src = inspect.getsource(balance.checkin_status)
    assert ".post(" not in src, "checkin_status must not POST (it would auto check in)"


TESTS = (
    test_token_is_never_echoed_but_its_presence_is,
    test_editing_other_fields_keeps_the_token,
    test_empty_access_token_clears_it,
    test_cleared_token_drops_the_vendor_from_checkin,
    test_checkin_status_never_claims_via_get,
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
