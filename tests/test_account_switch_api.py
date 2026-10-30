"""End-to-end account switching through the HTTP API.

This is the "换号器" integration test: it drives the real Flask app against a
throwaway ``HOME`` seeded with synthetic tool credentials, imports an account,
switches to it, and asserts the tool's on-disk credential was actually
rewritten (with a backup).

Nothing here touches the developer's real logins or AI Switch database.

Run standalone:  python3 tests/test_account_switch_api.py
Or with pytest:  pytest tests/test_account_switch_api.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-switch-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ["AI_SWITCH_NO_SCHEDULER"] = "1"
os.environ["AI_SWITCH_OPEN_BROWSER"] = "0"
os.environ["AI_SWITCH_NO_BROWSER"] = "1"
os.environ.pop("AI_SWITCH_ACCESS_TOKEN", None)
os.environ.pop("AI_SWITCH_REQUIRE_AUTH", None)
os.environ.pop("CODEX_HOME", None)

_HOME = Path(_TMP_HOME)


def _seed_codex() -> Path:
    path = _HOME / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": "LOCAL-OLD",
            "refresh_token": "LOCAL-OLD-REF",
            "id_token": "",
            "account_id": "local-acct",
        },
        "last_refresh": "2024-01-01T00:00:00Z",
    }), encoding="utf-8")
    return path


_CODEX_AUTH = _seed_codex()

from app import app  # noqa: E402
import core.data as _core_data  # noqa: E402

if not str(_core_data.DATA_DIR).startswith(_TMP_HOME):
    raise RuntimeError(
        "switch API test data isolation failed: core.data.DATA_DIR="
        f"{_core_data.DATA_DIR} is outside the throwaway HOME {_TMP_HOME}"
    )


def _client():
    app.config["TESTING"] = True
    return app.test_client()


def _import_codex_account(**fields) -> str:
    body = {"provider": "codex", "access_token": "NEW-ACC", "refresh_token": "NEW-REF"}
    body.update(fields)
    resp = _client().post("/api/accounts/import", json={"entries": [body]})
    assert resp.status_code == 200, resp.status_code
    listed = _client().get("/api/accounts").get_json()
    # Pick the newest codex account.
    codex = [a for a in listed["accounts"] if a["provider"] == "codex"]
    assert codex, listed
    return str(codex[-1]["id"])


# ── tests ──────────────────────────────────────────────────────────

def test_accounts_meta_exposes_switch_capability():
    resp = _client().get("/api/accounts")
    assert resp.status_code == 200
    body = resp.get_json()
    providers = {p["provider"]: p for p in body["providers"]}
    assert "codex" in providers, providers
    assert providers["codex"]["supports_switch"] is True, providers["codex"]
    assert "installed" in providers["codex"]


def test_account_providers_driven_by_registry():
    """The provider list must come from the adapter registry, not a hard-coded tuple."""
    body = _client().get("/api/accounts").get_json()
    ids = {p["provider"] for p in body["providers"]}
    from core.account_adapters import providers as registry_providers

    assert ids == set(registry_providers()), (ids, registry_providers())


def test_detect_reports_local_codex_login():
    resp = _client().post("/api/accounts/detect", json={})
    assert resp.status_code == 200, resp.status_code
    detected = resp.get_json()["detected"]
    assert "codex" in detected
    assert detected["codex"]["installed"] is True, detected["codex"]
    assert detected["codex"]["logged_in"] is True, detected["codex"]


def test_import_then_activate_rewrites_credential_with_backup():
    account_id = _import_codex_account(account_id="imported-acct")
    before = json.loads(_CODEX_AUTH.read_text(encoding="utf-8"))
    assert before["tokens"]["access_token"] == "LOCAL-OLD"

    resp = _client().post(f"/api/accounts/{account_id}/activate")
    assert resp.status_code == 200, (resp.status_code, resp.get_data(as_text=True))
    payload = resp.get_json()
    assert payload["ok"] is True, payload

    after = json.loads(_CODEX_AUTH.read_text(encoding="utf-8"))
    assert after["tokens"]["access_token"] == "NEW-ACC", after
    assert after["tokens"]["account_id"] == "imported-acct", after
    assert after["OPENAI_API_KEY"] is None, after

    backup = _CODEX_AUTH.with_name(_CODEX_AUTH.name + ".ai-switch.bak")
    assert backup.exists(), "activate must leave a backup of the previous credential"
    assert json.loads(backup.read_text(encoding="utf-8"))["tokens"]["access_token"] == "LOCAL-OLD"


def test_activate_missing_account_is_404():
    resp = _client().post("/api/accounts/999999/activate")
    assert resp.status_code == 404, resp.status_code


def test_activate_blocked_in_read_only_mode():
    c = _client()
    account_id = _import_codex_account(access_token="RO-ACC")
    assert c.post("/api/settings", json={"read_only": True}).status_code == 200
    try:
        resp = c.post(f"/api/accounts/{account_id}/activate")
        assert resp.status_code == 403, (resp.status_code, resp.get_data(as_text=True))
        assert resp.get_json().get("read_only") is True
        # The credential must be untouched while read-only.
        assert json.loads(_CODEX_AUTH.read_text(encoding="utf-8"))["tokens"]["access_token"] != "RO-ACC"
    finally:
        c.post("/api/settings", json={"read_only": False})


TESTS = (
    test_accounts_meta_exposes_switch_capability,
    test_account_providers_driven_by_registry,
    test_detect_reports_local_codex_login,
    test_import_then_activate_rewrites_credential_with_backup,
    test_activate_missing_account_is_404,
    test_activate_blocked_in_read_only_mode,
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
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    sys.exit(1 if failures else 0)
