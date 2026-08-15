"""Model-level API endpoint capabilities.

Historically ai-switch stored one ``endpoint_type`` on a vendor.  That is a
useful backwards-compatible hint, but it is not an accurate description of a
key: a gateway can expose Chat Completions and Responses at the same time and
different models behind the same key can expose different APIs.

This module deliberately contains no persistence code.  The capability map is
stored on each key by :mod:`core.data`, while the rules for interpreting it
live here so health checks and backend adapters cannot drift apart.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Iterable


OPENAI_CHAT = "openai_chat"
OPENAI_RESPONSES = "openai_responses"
ANTHROPIC_MESSAGES = "anthropic_messages"
GEMINI_GENERATE = "gemini_generate"

ALL_ENDPOINTS = (
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    ANTHROPIC_MESSAGES,
    GEMINI_GENERATE,
)

# A successful probe is evidence, not a permanent capability declaration.
# Backends fail closed once that evidence is old, until the next health run.
MODEL_HEALTH_TTL_SECONDS = 24 * 60 * 60

ENDPOINT_LABELS = {
    OPENAI_CHAT: "OpenAI Chat Completions (/v1/chat/completions)",
    OPENAI_RESPONSES: "OpenAI Responses (/v1/responses)",
    ANTHROPIC_MESSAGES: "Anthropic Messages (/v1/messages)",
    GEMINI_GENERATE: "Gemini generateContent",
}

_ALIASES = {
    "openai": (OPENAI_CHAT, OPENAI_RESPONSES),
    "openai_chat": (OPENAI_CHAT,),
    "chat": (OPENAI_CHAT,),
    "chat_completions": (OPENAI_CHAT,),
    "openai_responses": (OPENAI_RESPONSES,),
    "responses": (OPENAI_RESPONSES,),
    "codex": (OPENAI_RESPONSES,),
    "anthropic": (ANTHROPIC_MESSAGES,),
    "anthropic_messages": (ANTHROPIC_MESSAGES,),
    "claude": (ANTHROPIC_MESSAGES,),
    "messages": (ANTHROPIC_MESSAGES,),
    "google": (GEMINI_GENERATE,),
    "gemini": (GEMINI_GENERATE,),
    "gemini_generate": (GEMINI_GENERATE,),
}

# ``None`` means the backend can represent either OpenAI API style (the
# adapter still chooses one per model).  A tuple is an explicit capability.
BACKEND_ENDPOINTS = {
    "codex-cli": (OPENAI_RESPONSES,),
    "claude-code": (ANTHROPIC_MESSAGES,),
    "cline": (OPENAI_CHAT, ANTHROPIC_MESSAGES),
    "continue": (OPENAI_CHAT, ANTHROPIC_MESSAGES),
    "continue-dev": (OPENAI_CHAT, ANTHROPIC_MESSAGES),
    "opencode": (OPENAI_CHAT, ANTHROPIC_MESSAGES, GEMINI_GENERATE),
    # Current OpenClaw schema exposes openai-completions, not Responses.
    "openclaw": (OPENAI_CHAT, ANTHROPIC_MESSAGES, GEMINI_GENERATE),
    "kimi-code": (OPENAI_CHAT,),
    "trae-work": (OPENAI_CHAT, ANTHROPIC_MESSAGES),
    "aider": (OPENAI_CHAT,),
    "goose": (OPENAI_CHAT,),
    "hermes": (OPENAI_CHAT,),
    "qwencode": (OPENAI_CHAT,),
}

# Preference is intentionally conservative.  Responses is preferred by Codex,
# Chat Completions by most OpenAI-compatible clients, and Messages by Claude.
BACKEND_PREFERENCES = {
    "codex-cli": (OPENAI_RESPONSES, OPENAI_CHAT),
    "claude-code": (ANTHROPIC_MESSAGES,),
    "openclaw": (OPENAI_CHAT, ANTHROPIC_MESSAGES, GEMINI_GENERATE),
    "opencode": (OPENAI_CHAT, ANTHROPIC_MESSAGES),
    "kimi-code": (OPENAI_CHAT,),
    "trae-work": (OPENAI_CHAT, ANTHROPIC_MESSAGES),
}


def normalize_endpoint(value: str) -> str:
    """Return a canonical endpoint id, or an empty string for unknown input."""
    value = str(value or "").strip().lower()
    if value in ALL_ENDPOINTS:
        return value
    vals = _ALIASES.get(value) or ()
    return vals[0] if len(vals) == 1 else ""


def expand_endpoint_values(values: Iterable[str] | str | None) -> list[str]:
    """Expand legacy values (``openai`` becomes chat + responses)."""
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen = set()
    for value in values or ():
        raw = str(value or "").strip().lower()
        expanded = _ALIASES.get(raw) or ((raw,) if raw in ALL_ENDPOINTS else ())
        for endpoint in expanded:
            if endpoint not in seen:
                seen.add(endpoint)
                out.append(endpoint)
    return out


def endpoint_candidates(vendor: dict) -> list[str]:
    """Return endpoint candidates for a vendor, ordered by safe preference."""
    provider = str(vendor.get("provider") or "").strip().lower()
    url = str(vendor.get("proxy_target") or vendor.get("api_url") or "").lower()
    if (
        provider in {"anthropic", "claude"}
        or "anthropic" in provider
        or "claude" in provider
        or "api.anthropic.com" in url
        or "/anthropic" in url
        or "/claude" in url
    ):
        return [ANTHROPIC_MESSAGES]
    if provider in {"google", "gemini", "google-gemini"} or "generativelanguage.googleapis.com" in url:
        return [GEMINI_GENERATE]

    # Known OpenAI-compatible providers get the two OpenAI API styles.  A
    # genuinely custom gateway has no reliable family hint, so probe Messages
    # as well; the successful model×endpoint result, rather than the old
    # vendor-level field, decides what is eventually synced.
    known_openai = {
        "openai", "deepseek", "openrouter", "groq", "together", "xai",
        "mistral", "cohere", "moonshot", "kimi-code", "zai", "minimax",
        "alibaba", "volcengine", "fireworks", "stepfun", "deepinfra",
        "cerebras", "novita", "venice", "zeroone", "ollama", "qianfan",
    }
    if provider in known_openai:
        return [OPENAI_CHAT, OPENAI_RESPONSES]
    return [OPENAI_CHAT, OPENAI_RESPONSES, ANTHROPIC_MESSAGES]


def model_state(key: dict, model_id: str) -> dict:
    mapping = key.get("endpoint_capabilities")
    if not isinstance(mapping, dict):
        return {}
    state = mapping.get(str(model_id))
    return dict(state) if isinstance(state, dict) else {}


def _fresh_checked_at(value: str, *, now: datetime = None) -> bool:
    try:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        checked = datetime.fromisoformat(raw)
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return now - checked <= timedelta(seconds=MODEL_HEALTH_TTL_SECONDS)
    except (TypeError, ValueError):
        return False


def model_is_verified_usable(key: dict, model_id: str) -> bool:
    """Whether a model has a recent successful model and endpoint probe."""
    mid = str(model_id or "").strip()
    if not mid or key.get("enabled") is False or mid in set(key.get("disabled_models") or []):
        return False
    health = (key.get("model_health") or {}).get(mid) or {}
    if health.get("healthy") is not True or not _fresh_checked_at(health.get("checked_at")):
        return False
    state = model_state(key, mid)
    if not state or not _fresh_checked_at(state.get("checked_at")):
        return False
    mode = str(state.get("mode") or "auto").lower()
    endpoints = expand_endpoint_values(state.get("selected") if mode == "manual" else state.get("detected"))
    checks = state.get("checks") or {}
    return any(
        checks.get(endpoint, {}).get("healthy") is True
        and _fresh_checked_at(checks.get(endpoint, {}).get("checked_at"))
        for endpoint in endpoints
    )


def effective_model_endpoints(vendor: dict, key: dict, model_id: str) -> list[str]:
    """Resolve the endpoints that may be used for one model.

    Manual mode is authoritative, including an intentionally empty selection.
    Auto mode uses only successfully detected endpoints.  For pre-capability
    data, the old vendor hint is used as a compatibility bridge until the next
    model check populates a real detection result.
    """
    if not model_is_verified_usable(key, model_id):
        return []
    candidates = endpoint_candidates(vendor)
    state = model_state(key, model_id)
    mode = str(state.get("mode") or "auto").lower()
    if mode == "manual":
        selected = expand_endpoint_values(state.get("selected"))
        return [x for x in selected if x in candidates]
    detected = expand_endpoint_values(state.get("detected"))
    if detected:
        return [x for x in detected if x in candidates]
    return []


def choose_backend_endpoint(backend_name: str, endpoints: Iterable[str]) -> str:
    available = set(expand_endpoint_values(endpoints))
    supported = BACKEND_ENDPOINTS.get(backend_name)
    if supported:
        available.intersection_update(supported)
    for endpoint in BACKEND_PREFERENCES.get(backend_name, ALL_ENDPOINTS):
        if endpoint in available:
            return endpoint
    for endpoint in ALL_ENDPOINTS:
        if endpoint in available:
            return endpoint
    return ""


def model_supports_backend(vendor: dict, key: dict, model_id: str, backend_name: str) -> bool:
    return bool(choose_backend_endpoint(backend_name, effective_model_endpoints(vendor, key, model_id)))


def filter_models_for_backend(vendor: dict, key: dict, model_ids: Iterable[str], backend_name: str) -> list[str]:
    """Filter model ids and retain one selected endpoint per model."""
    out = []
    for model_id in model_ids or ():
        mid = str(model_id or "").strip()
        if mid and model_supports_backend(vendor, key, mid, backend_name):
            out.append(mid)
    return out


def selected_model_endpoint(vendor: dict, key: dict, model_id: str, backend_name: str) -> str:
    return choose_backend_endpoint(backend_name, effective_model_endpoints(vendor, key, model_id))


def capability_record(*, detected: list[str], checks: dict, mode: str = "auto", selected=None) -> dict:
    return {
        "mode": "manual" if mode == "manual" else "auto",
        "detected": list(detected or []),
        "selected": list(selected or []),
        "checks": dict(checks or {}),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
