"""Estimate USD cost from token usage when providers report cost=0."""

from __future__ import annotations

# USD per 1M tokens: (input, output). Rough public list prices; used only as fallback.
_PRICING = {
    # xAI Grok
    "grok-4.5": (3.0, 15.0),
    "grok-4.3": (3.0, 15.0),
    "grok-4.3-fast": (0.2, 0.5),
    "grok-4.20": (3.0, 15.0),
    "grok-4": (3.0, 15.0),
    "grok-3": (3.0, 15.0),
    "grok-2": (2.0, 10.0),
    "grok-build": (0.2, 0.5),
    "grok-chat": (0.2, 0.5),
    # Xiaomi MiMo
    "mimo-v2.5-pro": (0.5, 1.5),
    "mimo-v2.5": (0.2, 0.6),
    "mimo-v2-pro": (0.5, 1.5),
    "mimo-v2": (0.2, 0.6),
    # GLM / Zhipu
    "glm-5": (1.0, 3.0),
    "glm-4.6": (0.5, 2.0),
    "glm-4.5": (0.5, 2.0),
    "glm-4": (0.5, 2.0),
    # DeepSeek
    "deepseek-v4": (0.3, 0.6),
    "deepseek-v3": (0.27, 1.1),
    "deepseek-chat": (0.27, 1.1),
    "deepseek-r1": (0.55, 2.19),
    # OpenAI
    "gpt-5": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4": (10.0, 30.0),
    "o1": (15.0, 60.0),
    "o3": (10.0, 40.0),
    # Anthropic
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.8, 4.0),
    "claude-3.5": (3.0, 15.0),
    "claude-3": (3.0, 15.0),
    # Google
    "gemini-2.5": (1.25, 10.0),
    "gemini-2.0": (0.1, 0.4),
    "gemini-1.5": (0.35, 1.05),
    # Others
    "qwen": (0.4, 1.2),
    "doubao": (0.5, 1.5),
    "agnes": (0.2, 0.6),
}

_DEFAULT = (1.0, 3.0)  # generic fallback when model unknown


def _lookup_rates(model: str) -> tuple[float, float]:
    mid = (model or "").strip().lower()
    if not mid:
        return _DEFAULT
    # strip provider prefix
    if "/" in mid:
        mid = mid.rsplit("/", 1)[-1]
    if mid in _PRICING:
        return _PRICING[mid]
    # longest prefix match
    best = ""
    rates = _DEFAULT
    for key, val in _PRICING.items():
        if mid.startswith(key) and len(key) > len(best):
            best = key
            rates = val
    if best:
        return rates
    for key, val in _PRICING.items():
        if key in mid and len(key) > len(best):
            best = key
            rates = val
    return rates


def estimate_cost(
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> float:
    """Estimate USD cost from token counts. Returns 0 if no tokens."""
    pin = int(prompt_tokens or 0)
    pout = int(completion_tokens or 0)
    total = int(total_tokens or 0)
    if pin <= 0 and pout <= 0:
        if total <= 0:
            return 0.0
        # unknown split → treat as all input-ish
        pin, pout = total, 0
    inp_rate, out_rate = _lookup_rates(model)
    cost = (pin / 1_000_000.0) * inp_rate + (pout / 1_000_000.0) * out_rate
    return round(cost, 6)


def resolve_record_cost(record: dict) -> float:
    """Prefer reported cost; fall back to estimate when missing/zero."""
    if not isinstance(record, dict):
        return 0.0
    try:
        reported = float(record.get("cost") or 0)
    except (TypeError, ValueError):
        reported = 0.0
    if reported > 0:
        return round(reported, 6)
    return estimate_cost(
        str(record.get("model") or ""),
        int(record.get("prompt_tokens") or 0),
        int(record.get("completion_tokens") or 0),
        int(record.get("total_tokens") or 0),
    )
