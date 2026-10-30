"""Account adapter tests (import + switch round-trips).

These are the tests that matter most for a "换号器": they prove we can parse a
tool's real on-disk credential, write a different account back into it, and
leave a backup behind.  Everything runs against a throwaway ``HOME`` with
synthetic fixtures, so no real login is read or overwritten.

Covered:

* registry discovery / alias normalisation,
* per-provider read() + switch() round-trips (claude, codex, gemini, grok),
* API-key-only Codex installs must NOT look like an account,
* VS Code ``state.vscdb`` accessor (read / write / backup / missing table),
* failure modes raise ``SwitchError`` instead of corrupting state.

Run standalone:  python3 tests/test_account_adapters.py
Or with pytest:  pytest tests/test_account_adapters.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-adapters-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ.pop("CODEX_HOME", None)

from core.account_adapters import (  # noqa: E402
    StateVscdb,
    SwitchError,
    describe_all,
    get,
    norm,
    providers,
    switch,
)
from core.account_adapters import claude as claude_mod  # noqa: E402
from core.account_adapters.base import atomic_write_json, read_json  # noqa: E402


def _home() -> Path:
    return Path(_TMP_HOME)


def _write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


# ── registry ───────────────────────────────────────────────────────

def test_registry_discovers_core_providers():
    provs = providers()
    for expected in ("claude", "codex", "gemini", "grok", "kiro", "zed", "cursor", "antigravity"):
        assert expected in provs, f"{expected} missing from {provs}"


def test_alias_normalisation():
    assert norm("claude-code") == "claude"
    assert norm("anthropic") == "claude"
    assert norm("chatgpt") == "codex"
    assert norm("openai") == "codex"
    assert norm("gemini-cli") == "gemini"
    assert norm("xai") == "grok"
    assert norm("nope") == "nope"


def test_describe_all_is_json_safe():
    for entry in describe_all():
        assert isinstance(entry["provider"], str) and entry["provider"]
        assert isinstance(entry["supports_switch"], bool)
        json.dumps(entry)  # must not blow up on a Path


# ── claude ─────────────────────────────────────────────────────────

def test_claude_read_and_switch_roundtrip():
    adapter = get("claude")
    original_is_macos = claude_mod.is_macos
    claude_mod.is_macos = lambda: False  # force the file path, never Keychain
    try:
        path = adapter.credential_path()
        _write(path, {"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-OLD",
            "refreshToken": "rt-old",
            "expiresAt": 1_700_000_000_000,
            "scopes": ["user:inference"],
            "subscriptionType": "pro",
        }})

        parsed = adapter.read()
        assert parsed["provider"] == "claude"
        assert parsed["access_token"] == "sk-ant-oat01-OLD"
        assert parsed["plan_type"] == "pro"
        assert parsed["scopes"] == ["user:inference"]

        result = adapter.switch({
            "provider": "claude",
            "access_token": "sk-ant-oat01-NEW",
            "refresh_token": "rt-new",
            "expires_at": 1_800_000_000_000,
            "plan_type": "max",
        })
        assert result["written"], "switch should report what it wrote"

        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-NEW"
        assert after["claudeAiOauth"]["refreshToken"] == "rt-new"
        assert after["claudeAiOauth"]["subscriptionType"] == "max"
        assert Path(result["backup"]).exists(), "switch must leave a backup"
    finally:
        claude_mod.is_macos = original_is_macos
        shutil.rmtree(_home() / ".claude", ignore_errors=True)


def test_claude_switch_requires_token():
    adapter = get("claude")
    original_is_macos = claude_mod.is_macos
    claude_mod.is_macos = lambda: False
    try:
        try:
            adapter.switch({"provider": "claude"})
        except SwitchError:
            return
        raise AssertionError("switch with no token should raise SwitchError")
    finally:
        claude_mod.is_macos = original_is_macos


# ── codex ──────────────────────────────────────────────────────────

def test_codex_oauth_roundtrip_and_apikey_is_not_an_account():
    adapter = get("codex")
    path = adapter.credential_path()

    # API-key-only install: must not be mistaken for a login account.
    _write(path, {"OPENAI_API_KEY": "sk-live-abc"})
    assert adapter.read() is None

    # ChatGPT (OAuth) install.
    _write(path, {
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": "acc-old",
            "refresh_token": "ref-old",
            "id_token": "",
            "account_id": "acct-1",
        },
        "last_refresh": "2024-01-01T00:00:00Z",
    })
    parsed = adapter.read()
    assert parsed["account_id"] == "acct-1"
    assert parsed["access_token"] == "acc-old"

    adapter.switch({
        "provider": "codex",
        "access_token": "acc-new",
        "refresh_token": "ref-new",
        "account_id": "acct-2",
    })
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["tokens"]["access_token"] == "acc-new"
    assert after["tokens"]["account_id"] == "acct-2"
    assert after["OPENAI_API_KEY"] is None, "OAuth mode must clear the API key"
    shutil.rmtree(_home() / ".codex", ignore_errors=True)


# ── gemini ─────────────────────────────────────────────────────────

def test_gemini_roundtrip():
    adapter = get("gemini")
    path = adapter.credential_path()
    _write(path, {
        "access_token": "g-old",
        "refresh_token": "gr-old",
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "token_type": "Bearer",
        "expiry_date": 1_700_000_000_000,
    })
    parsed = adapter.read()
    assert parsed["access_token"] == "g-old"
    assert parsed["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]

    adapter.switch({
        "provider": "gemini",
        "access_token": "g-new",
        "refresh_token": "gr-new",
        "scopes": ["scope-a", "scope-b"],
    })
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["access_token"] == "g-new"
    assert after["scope"] == "scope-a scope-b"
    assert Path(path.with_name(path.name + ".ai-switch.bak")).exists()
    shutil.rmtree(_home() / ".gemini", ignore_errors=True)


# ── grok ───────────────────────────────────────────────────────────

def test_grok_registry_roundtrip():
    from core.account_adapters.grok import DEFAULT_REGISTRY_KEY

    adapter = get("grok")
    path = adapter.credential_path()
    _write(path, {DEFAULT_REGISTRY_KEY: {
        "key": "gk-old",
        "auth_mode": "oidc",
        "email": "a@example.com",
        "refresh_token": "grt-old",
    }})
    parsed = adapter.read()
    assert parsed["access_token"] == "gk-old"
    assert parsed["email"] == "a@example.com"

    adapter.switch({
        "provider": "grok",
        "access_token": "gk-new",
        "refresh_token": "grt-new",
        "email": "b@example.com",
    })
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after[DEFAULT_REGISTRY_KEY]["key"] == "gk-new"
    assert after[DEFAULT_REGISTRY_KEY]["email"] == "b@example.com"
    assert after[DEFAULT_REGISTRY_KEY]["auth_mode"] == "oidc"
    shutil.rmtree(_home() / ".grok", ignore_errors=True)


# ── kiro ───────────────────────────────────────────────────────────

def test_kiro_read_and_switch_roundtrip():
    from core.account_adapters.kiro import _profile_path, _token_path

    adapter = get("kiro")
    token_path, profile_path = _token_path(), _profile_path()
    _write(token_path, {
        "accessToken": "k-old",
        "refreshToken": "kr-old",
        "tokenType": "Bearer",
        "expiresAt": 1_700_000_000_000,
        "profileArn": "arn:aws:codewhisperer:us-east-1:1:profile/AAA",
        "provider": "BuilderId",
        "loginProvider": "BuilderId",
        "idc_region": "us-east-1",
        "issuer_url": "https://view.awsapps.com/start",
        "client_id": "cid-1",
        "clientIdHash": "eb04fe0de39241e4fa17bec175f7540138ad1bd8",
        "scopes": "codewhisperer:completions codewhisperer:analysis",
        "email": "k@example.com",
        "userId": "user-1",
    })
    _write(profile_path, {"arn": "arn:old", "name": "Old", "email": "k@example.com"})

    parsed = adapter.read()
    assert parsed["access_token"] == "k-old"
    assert parsed["email"] == "k@example.com"
    assert parsed["account_id"] == "user-1"
    assert parsed["scopes"] == ["codewhisperer:completions", "codewhisperer:analysis"]
    # The AWS identity fields must be carried so a switch does not lose them.
    assert parsed["raw_meta"]["kiro_auth_token"]["clientIdHash"].startswith("eb04fe")
    assert adapter.detect() is True

    result = adapter.switch({
        "provider": "kiro",
        "access_token": "k-new",
        "refresh_token": "kr-new",
        "expires_at": 1_800_000_000_000,
        "email": "new@example.com",
        "account_id": "user-2",
        "raw_meta": parsed["raw_meta"],
    })
    assert len(result["written"]) == 2, result

    after = json.loads(token_path.read_text(encoding="utf-8"))
    assert after["accessToken"] == "k-new"
    assert after["refreshToken"] == "kr-new"
    assert after["email"] == "new@example.com"
    assert after["userId"] == "user-2"
    # Identity fields survive the switch.
    assert after["clientIdHash"] == "eb04fe0de39241e4fa17bec175f7540138ad1bd8"
    assert after["profileArn"].endswith("profile/AAA")
    assert Path(result["backup"]).exists()

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile["email"] == "new@example.com"

    shutil.rmtree(_home() / ".aws", ignore_errors=True)
    shutil.rmtree(_home() / "Library", ignore_errors=True)


def test_kiro_requires_token():
    adapter = get("kiro")
    try:
        adapter.switch({"provider": "kiro"})
    except SwitchError:
        return
    raise AssertionError("kiro switch with no token should raise SwitchError")


# ── zed ────────────────────────────────────────────────────────────

def test_zed_switch_is_macos_only():
    from core.account_adapters import zed as zed_mod

    adapter = get("zed")
    original = zed_mod.is_macos
    zed_mod.is_macos = lambda: False
    try:
        assert adapter.describe()["supports_switch"] is False
        try:
            adapter.switch({"provider": "zed", "access_token": "z", "account_id": "u"})
        except SwitchError:
            return
        raise AssertionError("zed switch should be rejected off macOS")
    finally:
        zed_mod.is_macos = original


# ── cursor ─────────────────────────────────────────────────────────

def test_cursor_plaintext_vscdb_roundtrip():
    from core.account_adapters.base import app_support_dir

    adapter = get("cursor")
    db_path = _make_vscdb(app_support_dir("Cursor") / "User" / "globalStorage" / "state.vscdb")
    db = StateVscdb(db_path)
    db.write("cursorAuth/accessToken", "c-old")
    db.write("cursorAuth/refreshToken", "cr-old")
    db.write("cursorAuth/cachedEmail", "old@example.com")

    parsed = adapter.read()
    assert parsed["access_token"] == "c-old"
    assert parsed["refresh_token"] == "cr-old"
    assert parsed["email"] == "old@example.com"

    adapter.switch({
        "provider": "cursor",
        "access_token": "c-new",
        "refresh_token": "cr-new",
        "email": "new@example.com",
        "plan_type": "pro",
    })
    assert db.get("cursorAuth/accessToken") == "c-new"
    assert db.get("cursor.accessToken") == "c-new", "the mirror key must be updated too"
    assert db.get("cursorAuth/refreshToken") == "cr-new"
    assert db.get("cursorAuth/cachedEmail") == "new@example.com"
    assert db.get("cursorAuth/stripeMembershipType") == "pro"
    assert db_path.with_name(db_path.name + ".ai-switch.bak").exists()
    shutil.rmtree(_home() / "Library", ignore_errors=True)


def test_cursor_missing_db_raises():
    adapter = get("cursor")
    assert adapter.read() is None
    try:
        adapter.switch({"provider": "cursor", "access_token": "x"})
    except SwitchError:
        return
    raise AssertionError("cursor switch without a database should raise SwitchError")


# ── antigravity ────────────────────────────────────────────────────

def test_antigravity_payload_roundtrip():
    import base64
    from core.account_adapters import antigravity as ag

    adapter = get("antigravity")
    original = ag.is_macos
    ag.is_macos = lambda: False  # never touch the real Keychain in tests
    try:
        payload = {
            "token": {
                "access_token": "ag-old",
                "token_type": "Bearer",
                "refresh_token": "agr-old",
                "expiry": "2030-01-01T00:00:00.000000Z",
            },
            "auth_method": "consumer",
            "id_token": "",
        }
        blob = ag.PREFIX + base64.b64encode(json.dumps(payload).encode()).decode()
        ag._fallback_path().parent.mkdir(parents=True, exist_ok=True)
        ag._fallback_path().write_text(blob, encoding="utf-8")

        parsed = adapter.read()
        assert parsed["access_token"] == "ag-old"
        assert parsed["refresh_token"] == "agr-old"
        assert parsed["plan_type"] == "consumer"

        result = adapter.switch({
            "provider": "antigravity",
            "access_token": "ag-new",
            "refresh_token": "agr-new",
            "expires_at": 1_900_000_000_000,
            "raw_meta": parsed["raw_meta"],
        })
        assert result["written"], result
        assert ag._fallback_path().with_name(ag._fallback_path().name + ".ai-switch.bak").exists()

        after = adapter.read()
        assert after["access_token"] == "ag-new"
        assert after["refresh_token"] == "agr-new"
        assert after["plan_type"] == "consumer", "auth_method must survive"
    finally:
        ag.is_macos = original
        shutil.rmtree(_home() / ".gemini", ignore_errors=True)


def test_antigravity_rejects_legacy_state_db_only():
    from core.account_adapters import antigravity as ag

    adapter = get("antigravity")
    original = ag.is_macos
    ag.is_macos = lambda: False
    try:
        assert adapter.read() is None
    finally:
        ag.is_macos = original


# ── dispatcher ─────────────────────────────────────────────────────

def test_switch_unknown_provider_raises():
    try:
        switch({"provider": "does-not-exist", "access_token": "x"})
    except SwitchError:
        return
    raise AssertionError("unknown provider should raise SwitchError")


# ── state.vscdb ────────────────────────────────────────────────────

def _make_vscdb(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", ("existing.key", "keep-me"))
    con.commit()
    con.close()
    return path


def test_state_vscdb_read_write_and_backup():
    db_path = _make_vscdb(_home() / "fake-ide" / "globalStorage" / "state.vscdb")
    db = StateVscdb(db_path)
    assert db.exists()

    db.write_json("auth.key", {"accessToken": "abc"})
    assert db.get_json("auth.key") == {"accessToken": "abc"}
    # Pre-existing unrelated rows must survive.
    assert db.get("existing.key") == "keep-me"
    # A backup must exist after the first write.
    assert db_path.with_name(db_path.name + ".ai-switch.bak").exists()

    # Update in place (upsert) rather than duplicating the row.
    db.write_json("auth.key", {"accessToken": "def"})
    assert db.get_json("auth.key") == {"accessToken": "def"}
    assert db.keys().count("auth.key") == 1

    db.delete("auth.key")
    assert db.get("auth.key") is None

    assert "existing.key" in db.find_keys("existing")


def test_state_vscdb_missing_table_is_safe():
    path = _home() / "broken" / "state.vscdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(path)).close()  # empty db, no ItemTable
    db = StateVscdb(path)
    assert db.get("anything") is None
    assert db.keys() == []


def test_state_vscdb_absent_file_is_safe():
    db = StateVscdb(_home() / "nope" / "state.vscdb")
    assert db.exists() is False
    assert db.get("x") is None


# ── helpers ────────────────────────────────────────────────────────

def test_atomic_write_json_backs_up_previous_content():
    path = _home() / "atomic" / "creds.json"
    atomic_write_json(path, {"v": 1})
    assert read_json(path) == {"v": 1}
    atomic_write_json(path, {"v": 2})
    assert read_json(path) == {"v": 2}
    backup = path.with_name(path.name + ".ai-switch.bak")
    assert read_json(backup) == {"v": 1}, "backup must hold the previous content"


TESTS = (
    test_registry_discovers_core_providers,
    test_alias_normalisation,
    test_describe_all_is_json_safe,
    test_claude_read_and_switch_roundtrip,
    test_claude_switch_requires_token,
    test_codex_oauth_roundtrip_and_apikey_is_not_an_account,
    test_gemini_roundtrip,
    test_grok_registry_roundtrip,
    test_kiro_read_and_switch_roundtrip,
    test_kiro_requires_token,
    test_zed_switch_is_macos_only,
    test_cursor_plaintext_vscdb_roundtrip,
    test_cursor_missing_db_raises,
    test_antigravity_payload_roundtrip,
    test_antigravity_rejects_legacy_state_db_only,
    test_switch_unknown_provider_raises,
    test_state_vscdb_read_write_and_backup,
    test_state_vscdb_missing_table_is_safe,
    test_state_vscdb_absent_file_is_safe,
    test_atomic_write_json_backs_up_previous_content,
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
    sys.exit(1 if failures else 0)
