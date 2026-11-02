"""Provider preset tests, with Volcengine Ark as the worked example.

Volcengine Ark is one host serving two products with different base URLs and
different model naming:

* ``/api/v3``      pay-as-you-go, dated model IDs (``doubao-seed-2-1-pro-...``)
* ``/api/coding/v3`` Coding Plan subscription, short aliases (``ark-code-latest``)

Using the wrong base URL is not merely a 404 — the docs are explicit that
``/api/v3`` silently bills pay-as-you-go instead of drawing down the Coding
Plan. The presets therefore have to stay distinct, and ``recognize_provider``
must not collapse them onto whichever one happens to be listed first.

Run standalone:  python3 tests/test_provider_presets.py
Or with pytest:  pytest tests/test_provider_presets.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.providers import (          # noqa: E402
    PROVIDERS,
    _is_volcengine_url,
    _volcengine_models,
    get_provider,
    recognize_provider,
)


ARK_CODING = "https://ark.cn-beijing.volces.com/api/coding/v3"
ARK_PAYG = "https://ark.cn-beijing.volces.com/api/v3"
ARK_CODING_ANTHROPIC = "https://ark.cn-beijing.volces.com/api/coding"
ARK_PAYG_ANTHROPIC = "https://ark.cn-beijing.volces.com/api/compatible"


def test_volcengine_presets_are_distinct():
    """Every Ark product must be reachable as its own preset."""
    seen = {}
    for p in PROVIDERS:
        if p["id"].startswith("volcengine"):
            seen[p["id"]] = p["base_url"]
    assert seen, "no volcengine presets at all"
    assert len(set(seen.values())) == len(seen), f"duplicate base URLs: {seen}"
    assert ARK_CODING in seen.values(), seen
    assert ARK_PAYG in seen.values(), seen
    assert ARK_CODING_ANTHROPIC in seen.values(), seen
    assert ARK_PAYG_ANTHROPIC in seen.values(), seen


def test_coding_plan_keeps_its_original_id():
    """`volcengine` is the id existing installs already wrote to disk."""
    assert get_provider("volcengine")["base_url"] == ARK_CODING


def test_anthropic_presets_declare_anthropic_protocol():
    for pid in ("volcengine-coding-anthropic", "volcengine-ark-anthropic"):
        p = get_provider(pid)
        assert p is not None, pid
        assert p["check_type"] == "anthropic", p


def test_recognize_provider_matches_full_base_url_not_just_host():
    """Coding Plan and pay-as-you-go share a host, so host-only matching is wrong."""
    assert recognize_provider(ARK_PAYG)["base_url"] == ARK_PAYG
    assert recognize_provider(ARK_CODING)["base_url"] == ARK_CODING
    assert recognize_provider(ARK_PAYG + "/chat/completions")["base_url"] == ARK_PAYG
    assert recognize_provider(ARK_CODING + "/chat/completions")["base_url"] == ARK_CODING


def test_recognize_provider_still_handles_bare_ids():
    assert recognize_provider("volcengine")["id"] == "volcengine"
    assert recognize_provider("volcengine-ark")["id"] == "volcengine-ark"


def test_model_candidates_follow_the_product():
    """Aliases and dated IDs are not interchangeable between the two products."""
    coding = _volcengine_models(ARK_CODING)
    payg = _volcengine_models(ARK_PAYG)

    assert "ark-code-latest" in coding
    assert "ark-code-latest" not in payg, "pay-as-you-go has no console alias"

    assert any(m.startswith("doubao-seed-") and m[-6:].isdigit() for m in payg), payg
    assert not any(m[-6:].isdigit() for m in coding), coding


def test_volcengine_url_detection():
    assert _is_volcengine_url(ARK_CODING)
    assert _is_volcengine_url(ARK_PAYG)
    assert not _is_volcengine_url("https://api.openai.com/v1")


TESTS = (
    test_volcengine_presets_are_distinct,
    test_coding_plan_keeps_its_original_id,
    test_anthropic_presets_declare_anthropic_protocol,
    test_recognize_provider_matches_full_base_url_not_just_host,
    test_recognize_provider_still_handles_bare_ids,
    test_model_candidates_follow_the_product,
    test_volcengine_url_detection,
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
