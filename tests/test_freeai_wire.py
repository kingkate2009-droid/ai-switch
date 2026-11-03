"""free.ai support: a wire profile example.

free.ai is OpenAI-compatible in *payload shape* but not in *paths*:

    canonical OpenAI                    free.ai
    POST /v1/chat/completions     ->    POST /v1/chat/
    POST /v1/images/generations   ->    POST /v1/image/generate/
    GET  /v1/models {"data": []}  ->    GET  /v1/models {"models": []}

It also only implements chat completions: ``/v1/responses`` and
``/v1/messages`` both answer 404, so advertising them would make health checks
probe dead endpoints.

Nothing here touches the network — sessions and the upstream HTTP call are
faked.

Run standalone:  python3 tests/test_freeai_wire.py
Or with pytest:  pytest tests/test_freeai_wire.py
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

_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-freeai-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ["AI_SWITCH_NO_SCHEDULER"] = "1"
os.environ["AI_SWITCH_OPEN_BROWSER"] = "0"
os.environ["AI_SWITCH_NO_BROWSER"] = "1"
os.environ.pop("AI_SWITCH_ACCESS_TOKEN", None)
os.environ.pop("AI_SWITCH_REQUIRE_AUTH", None)

import core.providers as providers                       # noqa: E402
import app as app_module                                 # noqa: E402
from core.endpoints import endpoint_candidates           # noqa: E402

FREEAI_BASE = "https://api.free.ai/v1"


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = {"Content-Type": "application/json"}
        self.content = self.text.encode()

    def json(self):
        return self._payload

    def close(self):
        pass


class _FakeSession:
    def __init__(self, get_payload=None, post_payload=None):
        self.get_payload = get_payload
        self.post_payload = post_payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return _FakeResp(200, self.get_payload)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs.get("json")))
        return _FakeResp(200, self.post_payload)


def _with_session(session, fn):
    original = providers._new_session
    providers._new_session = lambda: session
    try:
        return fn()
    finally:
        providers._new_session = original


def test_provider_preset_exists():
    from core.providers import get_provider
    preset = get_provider("freeai")
    assert preset is not None
    assert preset["base_url"] == FREEAI_BASE, preset


def test_profile_matches_by_host():
    profile = providers.wire_profile(FREEAI_BASE)
    assert profile["id"] == "freeai"
    assert profile["media_shape"] == "freeai"
    assert providers.wire_profile("https://api.free.ai")["id"] == "freeai"
    assert providers.wire_profile("https://api.openai.com/v1") == {}
    # a lookalike host must not match
    assert providers.wire_profile("https://api.free.ai.evil.test/v1") == {}


def test_paths_are_remapped():
    assert providers.wire_path(FREEAI_BASE, "chat/completions") == "chat/"
    assert providers.wire_path(FREEAI_BASE, "images/generations") == "image/generate/"
    # unknown paths and other hosts are untouched
    assert providers.wire_path(FREEAI_BASE, "embeddings") == "embeddings"
    assert providers.wire_path("https://api.openai.com/v1", "chat/completions") == "chat/completions"


def test_probe_targets_chat_slash():
    session = _FakeSession(post_payload={"choices": [{"message": {"content": "ok"}}]})
    healthy, _ = _with_session(session, lambda: providers._probe_chat_completions(
        FREEAI_BASE, {"Authorization": "Bearer x"}, ["qwen7b"]))
    assert healthy is True
    assert session.calls[0][1] == "https://api.free.ai/v1/chat/", session.calls
    # and the canonical path is still used everywhere else
    session2 = _FakeSession(post_payload={"choices": [{"message": {"content": "ok"}}]})
    _with_session(session2, lambda: providers._probe_chat_completions(
        "https://api.openai.com/v1", {"Authorization": "Bearer x"}, ["gpt-4o-mini"]))
    assert session2.calls[0][1] == "https://api.openai.com/v1/chat/completions", session2.calls


def test_model_inventory_shape_is_understood():
    payload = {
        "models": [
            {"id": "premium/claude", "type": "chat", "self_hosted": False},
            {"id": "qwen7b", "type": "chat", "self_hosted": True},
            {"id": "sdxl", "type": "image", "self_hosted": True},
            {"id": "kokoro", "type": "tts", "self_hosted": True},
            {"id": "deepseek-r1", "type": "chat", "self_hosted": True},
        ],
        "total": 5,
    }
    session = _FakeSession(get_payload=payload)
    ids = _with_session(session, lambda: providers._scan_models_openai(
        FREEAI_BASE, {"Authorization": "Bearer x"}))
    # tts entries are dropped; chat sorts before image, and free (self-hosted)
    # models sort first so health checks never spend premium tokens
    assert ids == ["qwen7b", "deepseek-r1", "premium/claude", "sdxl"], ids


def test_image_models_are_routed_as_media():
    from core.endpoints import model_modality
    assert model_modality("sdxl") == "image"
    assert model_modality("qwen7b") == "chat"

    session = _FakeSession(post_payload={"url": "https://x/y.png"})
    healthy, _ = _with_session(session, lambda: providers.probe_openai_images(
        FREEAI_BASE, "k", "sdxl"))
    assert healthy is True
    # the trailing slash is part of free.ai's route
    assert session.calls[0][1] == "https://api.free.ai/v1/image/generate/", session.calls


def test_probe_prefers_free_models():
    session = _FakeSession(
        get_payload={"models": [{"id": "premium/x", "type": "chat", "self_hosted": False}]},
        post_payload={"choices": [{"message": {"content": "ok"}}]},
    )
    seen = []
    original = providers._probe_chat_completions
    providers._probe_chat_completions = lambda url, headers, models: (
        seen.extend(models), original(url, headers, models))[1]
    try:
        _with_session(session, lambda: providers.probe_openai_chat(
            FREEAI_BASE, "k"))
    finally:
        providers._probe_chat_completions = original
    # free models are tried before the premium one discovered from inventory
    assert seen and seen.index("qwen7b") < seen.index("premium/x"), seen


def test_only_chat_endpoint_is_advertised():
    from core.endpoints import _chat_candidates_for_vendor
    vendor = {"provider": "freeai", "api_url": FREEAI_BASE, "endpoint_type": "openai"}
    chat = _chat_candidates_for_vendor(vendor)
    assert chat == ["openai_chat"], chat
    # /v1/responses and /v1/messages both 404 upstream, so they must not be probed
    assert "openai_responses" not in chat and "anthropic_messages" not in chat, chat


def test_proxy_upstream_rewrites_the_path():
    """The downstream proxy must forward to /chat/, not /chat/completions."""
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        return _FakeResp(200, {"choices": [{"message": {"content": "ok"}}]})

    original = app_module.py_requests.request
    app_module.py_requests.request = fake_request
    try:
        vendor = {"api_url": FREEAI_BASE, "endpoint_type": "openai"}
        with app_module.app.test_request_context("/v1/chat/completions", method="POST", json={}):
            _, url = app_module._proxy_upstream(
                vendor, {"api_key": "sk-x"},
                path="chat/completions", body=b"{}", headers={}, stream=False,
            )
    finally:
        app_module.py_requests.request = original

    assert url == "https://api.free.ai/v1/chat/", (url, calls)
    assert calls[0][1] == "https://api.free.ai/v1/chat/", calls


def test_proxy_keeps_canonical_path_for_other_providers():
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        return _FakeResp(200, {"choices": []})

    original = app_module.py_requests.request
    app_module.py_requests.request = fake_request
    try:
        vendor = {"api_url": "https://api.deepseek.com/v1", "endpoint_type": "openai"}
        with app_module.app.test_request_context("/v1/chat/completions", method="POST", json={}):
            _, url = app_module._proxy_upstream(
                vendor, {"api_key": "sk-x"},
                path="chat/completions", body=b"{}", headers={}, stream=False,
            )
    finally:
        app_module.py_requests.request = original
    assert calls[0][1] == "https://api.deepseek.com/v1/chat/completions", calls
    assert url.endswith("/chat/completions")


def test_image_response_is_reshaped_to_openai():
    native = json.dumps({
        "job_id": "abc",
        "url": "https://api.free.ai/static/outputs/abc.png",
        "image_url": "https://api.free.ai/static/outputs/abc.png",
        "share_url": "https://free.ai/share/x",
    }).encode()
    out = json.loads(app_module._adapt_freeai_media(native))
    assert out["data"][0]["url"] == "https://api.free.ai/static/outputs/abc.png", out
    assert "created" in out

    # a body without a url is passed through untouched rather than emptied
    passthrough = b'{"error": "boom"}'
    assert app_module._adapt_freeai_media(passthrough) == passthrough


TESTS = (
    test_provider_preset_exists,
    test_profile_matches_by_host,
    test_paths_are_remapped,
    test_probe_targets_chat_slash,
    test_model_inventory_shape_is_understood,
    test_image_models_are_routed_as_media,
    test_probe_prefers_free_models,
    test_only_chat_endpoint_is_advertised,
    test_proxy_upstream_rewrites_the_path,
    test_proxy_keeps_canonical_path_for_other_providers,
    test_image_response_is_reshaped_to_openai,
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
