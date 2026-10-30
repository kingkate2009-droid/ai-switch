"""Access-policy tests: when a token is required, and where it comes from.

The rule is "loopback bind = no token, routable bind = token".  These tests
cover host classification and token resolution with the data layer stubbed, so
nothing is written to the real data file.

Run standalone:  python3 tests/test_access_policy.py
Or with pytest:  pytest tests/test_access_policy.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.data as core_data   # noqa: E402
from core import access         # noqa: E402


# --------------------------------------------------------------------------
# In-memory settings store + env isolation
# --------------------------------------------------------------------------

_SETTINGS = {}
_ORIGINAL_UPDATE = core_data.update_settings
_ORIGINAL_GET = core_data.get_settings


def _install_seams():
    core_data.get_settings = lambda: dict(_SETTINGS)

    def _update(**kwargs):
        _SETTINGS.update(kwargs)
        return dict(_SETTINGS)

    core_data.update_settings = _update


def _clear_env():
    for name in ("AI_SWITCH_REQUIRE_AUTH", "AI_SWITCH_ACCESS_TOKEN"):
        os.environ.pop(name, None)


_install_seams()
_clear_env()


# --------------------------------------------------------------------------
# Host classification
# --------------------------------------------------------------------------

def test_loopback_hosts_detected():
    for host in ("127.0.0.1", "localhost", "LOCALHOST", "127.0.0.5", "::1", "[::1]", ""):
        assert access.is_loopback_host(host) is True, host


def test_routable_hosts_not_loopback():
    for host in ("0.0.0.0", "::", "192.168.1.20", "10.0.0.7", "ai-switch.lan", "100.64.0.3"):
        assert access.is_loopback_host(host) is False, host


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

def test_loopback_bind_needs_no_token():
    _SETTINGS.clear()
    _clear_env()
    info = access.resolve_access_token("127.0.0.1")
    assert info["auth_required"] is False, info
    assert info["token"] == "", info
    assert info["created"] is False, info


def test_routable_bind_creates_and_persists_token():
    _SETTINGS.clear()
    _clear_env()
    info = access.resolve_access_token("0.0.0.0")
    assert info["auth_required"] is True, info
    assert info["created"] is True, info
    assert len(info["token"]) >= 20, info
    assert _SETTINGS.get("access_token") == info["token"], "token was not persisted"


def test_token_is_stable_across_restarts():
    _SETTINGS.clear()
    _clear_env()
    first = access.resolve_access_token("192.168.1.20")
    second = access.resolve_access_token("192.168.1.20")
    assert first["token"] == second["token"], (first, second)
    assert second["created"] is False, second


def test_env_token_wins_and_is_not_persisted():
    _SETTINGS.clear()
    _clear_env()
    os.environ["AI_SWITCH_ACCESS_TOKEN"] = "from-env-token"
    try:
        info = access.resolve_access_token("0.0.0.0")
    finally:
        _clear_env()
    assert info == {"auth_required": True, "token": "from-env-token",
                    "created": False, "source": "env"}, info
    assert "access_token" not in _SETTINGS, _SETTINGS


def test_require_auth_override_forces_policy():
    _SETTINGS.clear()
    _clear_env()
    os.environ["AI_SWITCH_REQUIRE_AUTH"] = "0"
    try:
        off = access.resolve_access_token("0.0.0.0")
    finally:
        _clear_env()
    assert off["auth_required"] is False, off

    _SETTINGS.clear()
    os.environ["AI_SWITCH_REQUIRE_AUTH"] = "1"
    try:
        on = access.resolve_access_token("127.0.0.1")
    finally:
        _clear_env()
    assert on["auth_required"] is True, on


def test_existing_token_still_enforced_on_loopback():
    """A user who set a token keeps it, even on a local-only bind."""
    _SETTINGS.clear()
    _clear_env()
    _SETTINGS["access_token"] = "kept-token"
    info = access.resolve_access_token("localhost")
    assert info["auth_required"] is True, info
    assert info["token"] == "kept-token", info
    assert info["created"] is False, info


# --------------------------------------------------------------------------
# Console notice
# --------------------------------------------------------------------------

def test_notice_silent_when_no_token_needed():
    assert access.format_access_notice("127.0.0.1", 8787,
                                       {"auth_required": False, "token": ""}) == ""


def test_notice_shows_token_and_bind():
    text = access.format_access_notice(
        "0.0.0.0", 8787,
        {"auth_required": True, "token": "tok-123", "created": True, "source": "generated"},
    )
    assert "0.0.0.0:8787" in text, text
    assert "tok-123" in text, text
    assert "AI_SWITCH_REQUIRE_AUTH=0" in text, text


TESTS = (
    test_loopback_hosts_detected,
    test_routable_hosts_not_loopback,
    test_loopback_bind_needs_no_token,
    test_routable_bind_creates_and_persists_token,
    test_token_is_stable_across_restarts,
    test_env_token_wins_and_is_not_persisted,
    test_require_auth_override_forces_policy,
    test_existing_token_still_enforced_on_loopback,
    test_notice_silent_when_no_token_needed,
    test_notice_shows_token_and_bind,
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
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)
