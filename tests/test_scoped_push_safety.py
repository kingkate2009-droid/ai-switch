"""Scoped-push safety regression tests.

Scoped pushes (``/api/sync/push`` with ``vendor_ids`` or ``key_ids``) must never
remove another vendor's entries from a backend engine config.  Historically
most adapters rebuilt their managed entry set from the *scoped* key list while
computing the "stale entries to drop" set from **all** vendors, so pushing one
key silently deleted every other vendor's entry.

The system-level guarantee now lives in ``backends.normalize_push_scope``: a
scoped push is promoted to a full reconcile, so adapters never see a scope at
all.  These tests drive the real adapter + real ``reconcile_all`` code with all
config files and persistence replaced by in-memory objects, so nothing on disk
is read or written.

Run standalone:  python3 tests/test_scoped_push_safety.py
Or with pytest:  pytest tests/test_scoped_push_safety.py
"""
import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.data as core_data              # noqa: E402
import core.health_checker as health       # noqa: E402
import backends                            # noqa: E402
from backends.base import BackendAdapter   # noqa: E402
import backends.continue_dev as continue_dev  # noqa: E402


VENDOR_A_ID = "vA"
VENDOR_B_ID = "vB"

FAKE_VENDORS = [
    {
        "id": VENDOR_A_ID,
        "name": "Vendor A",
        "provider": "openai",
        "api_url": "https://a.example/v1",
        "keys": [{"id": "kA", "api_key": "sk-A", "enabled": True, "default_model": "gpt-4o"}],
    },
    {
        "id": VENDOR_B_ID,
        "name": "Vendor B",
        "provider": "anthropic",
        "api_url": "https://b.example",
        "keys": [{"id": "kB", "api_key": "sk-B", "enabled": True, "default_model": "claude-sonnet"}],
    },
]

# What an earlier full reconcile left on disk for the fake vendors.
ALREADY_SYNCED = [
    {"title": "Openai", "provider": "openai", "model": "AUTODETECT", "apiKey": "sk-A"},
    {"title": "Anthropic", "provider": "anthropic", "model": "AUTODETECT", "apiKey": "sk-B"},
]


# --------------------------------------------------------------------------
# Seams: replace install / health / endpoint / persistence with in-memory stubs
# --------------------------------------------------------------------------

_ORIGINALS = {}


def _patch(obj, name, value):
    _ORIGINALS.setdefault((obj, name), getattr(obj, name, None))
    setattr(obj, name, value)


def _install_seams():
    _patch(core_data, "get_vendors", lambda: [dict(v) for v in FAKE_VENDORS])
    _patch(core_data, "get_backend_config", lambda name: {})
    _patch(core_data, "_load_data", lambda: {"backends": {}, "settings": {}})
    _patch(core_data, "_save_data", lambda data: None)
    _patch(health, "is_key_backend_syncable", lambda vid, key, **kw: True)
    _patch(health, "get_health_cache_snapshot", lambda **kw: {})
    _patch(health, "invalidate_health_cache_snapshot", lambda: None)
    # Pretend every backend is installed, and give every key one usable endpoint.
    _patch(BackendAdapter, "is_installed", lambda self: True)
    _patch(BackendAdapter, "selected_model_endpoint", lambda self, v, k, mid: "openai_chat")


_install_seams()


def _make_continue_adapter(existing_models, capture):
    adapter = continue_dev.ContinueAdapter()
    adapter._load_config = lambda: {"models": [dict(m) for m in existing_models]}
    adapter._save_config = lambda cfg: capture.__setitem__("cfg", cfg)
    return adapter


@contextlib.contextmanager
def _use_adapters(mapping):
    """Temporarily register only ``mapping`` so reconcile_all() touches nothing real."""
    previous = backends._adapters
    backends._adapters = mapping
    try:
        yield
    finally:
        backends._adapters = previous


def _titles(cfg):
    return sorted(
        str(m.get("title") or m.get("provider") or "").lower()
        for m in cfg.get("models") or []
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_full_push_writes_both_vendors():
    """Baseline: an unscoped push produces entries for every vendor."""
    capture = {}
    adapter = _make_continue_adapter([], capture)
    with _use_adapters({"continue": adapter}):
        backends.reconcile_all()
    assert _titles(capture["cfg"]) == ["anthropic", "openai"], _titles(capture["cfg"])


def test_scoped_push_vendor_keeps_other_vendors_entries():
    """THE REGRESSION: pushing one vendor must not delete another's entry."""
    capture = {}
    adapter = _make_continue_adapter(ALREADY_SYNCED, capture)
    with _use_adapters({"continue": adapter}):
        backends.reconcile_all(vendor_ids=[VENDOR_A_ID])
    assert "anthropic" in _titles(capture["cfg"]), (
        "scoped push to vendor A deleted vendor B's entry: " + repr(_titles(capture["cfg"]))
    )


def test_single_key_push_keeps_other_vendors_entries():
    """Pushing a single key must not delete another vendor's entry."""
    capture = {}
    adapter = _make_continue_adapter(ALREADY_SYNCED, capture)
    with _use_adapters({"continue": adapter}):
        backends.reconcile_all(key_ids=[f"{VENDOR_A_ID}:kA"])
    assert "anthropic" in _titles(capture["cfg"]), (
        "single-key push deleted vendor B's entry: " + repr(_titles(capture["cfg"]))
    )


def test_normalize_push_scope_promotes_scopes_only():
    """Vendor/key scopes are dropped; model scope is preserved."""
    assert backends.normalize_push_scope([VENDOR_A_ID], None, None) == (None, None, None)
    assert backends.normalize_push_scope(None, [f"{VENDOR_A_ID}:kA"], None) == (None, None, None)
    assert backends.normalize_push_scope(None, None, ["m1"]) == (None, None, ["m1"])
    assert backends.normalize_push_scope(None, None, None) == (None, None, None)


def test_adapter_is_still_destructive_without_promotion():
    """Canary: proves the promotion is load-bearing.

    If per-adapter scoped removal is ever implemented properly, this test will
    fail -- that is the signal to delete ``normalize_push_scope`` and re-enable
    real scoped pushes.
    """
    capture = {}
    adapter = _make_continue_adapter(ALREADY_SYNCED, capture)
    backends._tls.vendor_ids = {VENDOR_A_ID}
    try:
        adapter.reconcile()
    finally:
        backends._tls.__dict__.pop("vendor_ids", None)
    assert "anthropic" not in _titles(capture["cfg"]), (
        "adapter now handles scopes correctly -- remove normalize_push_scope(): "
        + repr(_titles(capture["cfg"]))
    )


TESTS = (
    test_full_push_writes_both_vendors,
    test_scoped_push_vendor_keeps_other_vendors_entries,
    test_single_key_push_keeps_other_vendors_entries,
    test_normalize_push_scope_promotes_scopes_only,
    test_adapter_is_still_destructive_without_promotion,
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
