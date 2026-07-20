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


def get_builtin_pricing() -> dict:
    """Return builtin table as {model: {input, output}} USD / 1M tokens."""
    return {k: {"input": float(v[0]), "output": float(v[1])} for k, v in _PRICING.items()}


def get_user_pricing() -> dict:
    """User overrides from settings.pricing: {model: {input, output}}."""
    try:
        from core.data import get_settings
        raw = (get_settings() or {}).get("pricing") or {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            if not k:
                continue
            if isinstance(v, dict):
                inp = v.get("input", v.get("prompt", v.get("in")))
                outp = v.get("output", v.get("completion", v.get("out")))
            elif isinstance(v, (list, tuple)) and len(v) >= 2:
                inp, outp = v[0], v[1]
            else:
                continue
            try:
                out[str(k).strip().lower()] = {
                    "input": float(inp),
                    "output": float(outp),
                }
            except Exception:
                continue
        return out
    except Exception:
        return {}


def get_pricing_table() -> dict:
    """Merged pricing: builtin + user overrides (user wins)."""
    merged = get_builtin_pricing()
    merged.update(get_user_pricing())
    return merged


_RATES_CACHE = None  # type: Optional[dict]
_RATES_MODEL_CACHE = {}  # type: dict
_RATES_CACHE_TS = 0.0


def clear_pricing_cache() -> None:
    global _RATES_CACHE, _RATES_MODEL_CACHE, _RATES_CACHE_TS
    _RATES_CACHE = None
    _RATES_MODEL_CACHE = {}
    _RATES_CACHE_TS = 0.0


def _rates_table(force: bool = False):
    """Cached (input, output) USD/1M rates. Avoid re-reading data.json per record."""
    global _RATES_CACHE, _RATES_MODEL_CACHE, _RATES_CACHE_TS
    import time
    now = time.time()
    # TTL cache: settings rarely change; do not load multi-MB data.json every estimate
    if not force and _RATES_CACHE is not None and (now - _RATES_CACHE_TS) < 30.0:
        return _RATES_CACHE
    table = {}
    for k, v in get_pricing_table().items():
        table[str(k).strip().lower()] = (float(v["input"]), float(v["output"]))
    _RATES_CACHE = table
    _RATES_MODEL_CACHE = {}
    _RATES_CACHE_TS = now
    return table


def _lookup_rates(model: str):
    mid = (model or "").strip().lower()
    if not mid:
        return _DEFAULT
    # strip provider prefix
    if "/" in mid:
        mid = mid.rsplit("/", 1)[-1]

    cached = _RATES_MODEL_CACHE.get(mid)
    if cached is not None:
        return cached

    table = _rates_table()
    if mid in table:
        _RATES_MODEL_CACHE[mid] = table[mid]
        return table[mid]
    # longest prefix match
    best = ""
    rates = _DEFAULT
    for key, val in table.items():
        if mid.startswith(key) and len(key) > len(best):
            best = key
            rates = val
    if not best:
        for key, val in table.items():
            if key in mid and len(key) > len(best):
                best = key
                rates = val
    _RATES_MODEL_CACHE[mid] = rates
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


def fill_missing_costs(records: list) -> int:
    """Estimate cost for records with missing/zero cost. Returns number estimated."""
    if not records:
        return 0
    _rates_table()  # warm cache once
    n = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        try:
            if float(r.get("cost") or 0) > 0:
                continue
        except (TypeError, ValueError):
            pass
        r["cost"] = resolve_record_cost(r)
        r["_cost_estimated"] = True
        n += 1
    return n
