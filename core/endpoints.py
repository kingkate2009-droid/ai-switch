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
OPENAI_IMAGES = "openai_images"
OPENAI_VIDEOS = "openai_videos"
OPENAI_AUDIO = "openai_audio"
OPENAI_TRANSLATIONS = "openai_translations"

# Text / chat family (coding-tool backends only consume these)
CHAT_ENDPOINTS = (
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    ANTHROPIC_MESSAGES,
    GEMINI_GENERATE,
)

# Non-text generation / media family (downstream proxy + catalog)
MEDIA_ENDPOINTS = (
    OPENAI_IMAGES,
    OPENAI_VIDEOS,
    OPENAI_AUDIO,
    OPENAI_TRANSLATIONS,
)

ALL_ENDPOINTS = CHAT_ENDPOINTS + MEDIA_ENDPOINTS

# Coarse downstream protocol buckets (virtual key allow-list)
DOWNSTREAM_ENDPOINT_TYPES = (
    "openai",
    "anthropic",
    "image",
    "video",
    "audio",
    "translation",
)

# Map fine-grained capability ids → downstream allow-list values
ENDPOINT_TO_DOWNSTREAM = {
    OPENAI_CHAT: "openai",
    OPENAI_RESPONSES: "openai",
    ANTHROPIC_MESSAGES: "anthropic",
    GEMINI_GENERATE: "openai",
    OPENAI_IMAGES: "image",
    OPENAI_VIDEOS: "video",
    OPENAI_AUDIO: "audio",
    OPENAI_TRANSLATIONS: "translation",
}

# A successful probe is evidence, not a permanent capability declaration.
# Backends fail closed once that evidence is old, until the next health run.
MODEL_HEALTH_TTL_SECONDS = 24 * 60 * 60

ENDPOINT_LABELS = {
    OPENAI_CHAT: "OpenAI Chat Completions (/v1/chat/completions)",
    OPENAI_RESPONSES: "OpenAI Responses (/v1/responses)",
    ANTHROPIC_MESSAGES: "Anthropic Messages (/v1/messages)",
    GEMINI_GENERATE: "Gemini generateContent",
    OPENAI_IMAGES: "OpenAI Images (/v1/images/generations)",
    OPENAI_VIDEOS: "OpenAI Videos (/v1/videos)",
    OPENAI_AUDIO: "OpenAI Audio (/v1/audio/*)",
    OPENAI_TRANSLATIONS: "OpenAI Translations (/v1/audio/translations)",
}

ENDPOINT_SHORT_LABELS = {
    OPENAI_CHAT: "Chat",
    OPENAI_RESPONSES: "Responses",
    ANTHROPIC_MESSAGES: "Messages",
    GEMINI_GENERATE: "Gemini",
    OPENAI_IMAGES: "Image",
    OPENAI_VIDEOS: "Video",
    OPENAI_AUDIO: "Audio",
    OPENAI_TRANSLATIONS: "Translation",
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
    "image": (OPENAI_IMAGES,),
    "images": (OPENAI_IMAGES,),
    "openai_images": (OPENAI_IMAGES,),
    "dalle": (OPENAI_IMAGES,),
    "video": (OPENAI_VIDEOS,),
    "videos": (OPENAI_VIDEOS,),
    "openai_videos": (OPENAI_VIDEOS,),
    "audio": (OPENAI_AUDIO,),
    "tts": (OPENAI_AUDIO,),
    "stt": (OPENAI_AUDIO,),
    "speech": (OPENAI_AUDIO,),
    "openai_audio": (OPENAI_AUDIO,),
    "translation": (OPENAI_TRANSLATIONS,),
    "translations": (OPENAI_TRANSLATIONS,),
    "openai_translations": (OPENAI_TRANSLATIONS,),
    "whisper": (OPENAI_AUDIO, OPENAI_TRANSLATIONS),
}

# ``None`` means the backend can represent either OpenAI API style (the
# adapter still chooses one per model).  A tuple is an explicit capability.
# Coding backends stay chat-only so media models never sync into them.
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

# Model-id keywords → preferred non-chat endpoints (ordered).
_IMAGE_HINTS = (
    "dall-e", "dalle", "gpt-image", "imagen", "stable-diffusion", "sdxl",
    "flux", "midjourney", "image-gen", "image_gen", "text-to-image", "t2i",
    "imagine", "cogview", "kolors", "playground-v", "ideogram",
)
_VIDEO_HINTS = (
    "sora", "runway", "kling", "luma", "pika", "veo", "minimax-video",
    "text-to-video", "t2v", "video-gen", "video_gen", "hailuo", "vidu",
    "wanx-video", "cogvideo",
)
_AUDIO_HINTS = (
    "tts", "speech", "whisper", "asr", "transcri", "audio", "voice",
    "sonic", "melody", "realtime", "fish-speech", "cosyvoice",
)
_TRANSLATION_HINTS = (
    "translat", "whisper",
)


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


def model_modality(model_id: str) -> str:
    """Heuristic modality for a model id: chat|image|video|audio|translation|other."""
    lower = str(model_id or "").strip().lower()
    if not lower:
        return "other"
    # Chat-first overrides: audio-named chat models (stepaudio-*-chat) stay chat
    if lower.endswith(("-chat", "-instruct", "-turbo")) and not any(
        k in lower for k in ("tts", "whisper", "dall-e", "dalle", "sora", "image-gen", "t2i", "t2v")
    ):
        # still allow pure media prefixes below; only soft-names hit this
        if "realtime" not in lower and "-asr" not in lower and "transcri" not in lower:
            if any(h in lower for h in ("audio", "voice", "speech")):
                return "chat"
    # Explicit path-style prefixes first
    if any(lower.startswith(p) for p in ("dall-e", "dalle", "gpt-image", "imagen")):
        return "image"
    if any(lower.startswith(p) for p in ("sora", "veo")):
        return "video"
    if any(lower.startswith(p) for p in ("tts-", "whisper")):
        if "translat" in lower:
            return "translation"
        return "audio"
    # Keyword anywhere (prefer more specific)
    if any(h in lower for h in _VIDEO_HINTS) or "-video" in lower or lower.startswith("video-") or "-t2v" in lower:
        return "video"
    if any(h in lower for h in _IMAGE_HINTS) or "-image" in lower or lower.startswith("image-") or "-t2i" in lower:
        return "image"
    if any(h in lower for h in _TRANSLATION_HINTS) and "whisper" in lower and "translat" in lower:
        return "translation"
    if any(h in lower for h in _AUDIO_HINTS) or "-audio" in lower or "-tts" in lower or "-asr" in lower:
        return "audio"
    if "translat" in lower:
        return "translation"
    # Skip pure embedding/moderation from "chat" label
    if any(lower.startswith(p) for p in ("text-embedding", "embed-", "moderations")):
        return "other"
    if lower.endswith(("-embedding", "-embed", "-moderation")):
        return "other"
    return "chat"


def modality_endpoints(modality: str) -> list[str]:
    """Default endpoint set for a modality (OpenAI-compatible gateways)."""
    m = str(modality or "chat").lower()
    if m == "image":
        return [OPENAI_IMAGES]
    if m == "video":
        return [OPENAI_VIDEOS]
    if m == "audio":
        return [OPENAI_AUDIO, OPENAI_TRANSLATIONS]
    if m == "translation":
        return [OPENAI_TRANSLATIONS, OPENAI_AUDIO]
    if m == "other":
        return []
    return []


def classify_model_endpoints(vendor: dict | None, model_id: str) -> list[str]:
    """Heuristic endpoint classes for a model (no network).

    Used by the catalog filter so every inventory model has at least one
    endpoint tag even before a live probe.  Media ids map to media APIs;
    chat ids inherit the vendor's chat protocol family.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return []
    modality = model_modality(mid)
    media = modality_endpoints(modality)
    if media:
        return list(media)
    if modality == "other":
        return []
    vendor = vendor or {}
    return list(_chat_candidates_for_vendor(vendor))


def _chat_candidates_for_vendor(vendor: dict) -> list[str]:
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


def endpoint_candidates(vendor: dict, model_id: str = "") -> list[str]:
    """Return endpoint candidates for a vendor (and optional model), ordered by preference.

    When ``model_id`` looks like a media model, only the matching media endpoints
    are returned so health runs stay cheap.  Chat models keep the historical
    chat/protocol matrix.  With no model id, return chat candidates plus all
    media endpoints (used for coarse vendor-level UI lists).
    """
    chat = _chat_candidates_for_vendor(vendor)
    mid = str(model_id or "").strip()
    if not mid:
        # Vendor-level: chat family + media (UI may list all known types)
        out = list(chat)
        for ep in MEDIA_ENDPOINTS:
            if ep not in out:
                out.append(ep)
        return out

    modality = model_modality(mid)
    media = modality_endpoints(modality)
    if media:
        # Media models: only probe media APIs (plus chat if id is ambiguous)
        return list(media)
    if modality == "other":
        return []
    return list(chat)


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


def _model_explicitly_failed(key: dict, model_id: str) -> bool:
    """True when a recent probe recorded this model as unhealthy."""
    mid = str(model_id or "").strip()
    if not mid:
        return True
    if mid in set(key.get("disabled_models") or []):
        return True
    health = (key.get("model_health") or {}).get(mid) or {}
    if health.get("healthy") is False and _fresh_checked_at(health.get("checked_at")):
        return True
    return False


def effective_model_endpoints(vendor: dict, key: dict, model_id: str) -> list[str]:
    """Resolve the endpoints that may be used for one model.

    Priority:
    1. Manual selection (even if empty)
    2. Fresh successful probe (detected)
    3. Fallback for inventory models on an enabled key: classified tags or
       vendor/model candidates — so backend sync is not limited to the 1–2
       models key-health happened to probe.

    Explicitly failed / disabled models still return no endpoints.
    """
    mid = str(model_id or "").strip()
    if not mid or key.get("enabled") is False:
        return []
    if mid in set(key.get("disabled_models") or []):
        return []

    candidates = set(endpoint_candidates(vendor, mid))
    state = model_state(key, mid)
    mode = str(state.get("mode") or "auto").lower()

    if mode == "manual":
        selected = expand_endpoint_values(state.get("selected"))
        # Manual empty selection is intentional
        return [x for x in selected if x in candidates or x in ALL_ENDPOINTS]

    # Fresh probe wins
    if model_is_verified_usable(key, mid):
        detected = expand_endpoint_values(state.get("detected"))
        if detected:
            return [x for x in detected if x in candidates or x in ALL_ENDPOINTS]

    # Recent explicit failure → do not invent endpoints
    if _model_explicitly_failed(key, mid) and not model_is_verified_usable(key, mid):
        return []

    # Fallback: classified tags from inventory classification, then heuristics
    classified = expand_endpoint_values(state.get("classified") or [])
    if classified:
        return [x for x in classified if x in candidates or x in ALL_ENDPOINTS]
    guessed = classify_model_endpoints(vendor, mid)
    if guessed:
        return list(guessed)
    return [x for x in endpoint_candidates(vendor, mid) if x in CHAT_ENDPOINTS or x in MEDIA_ENDPOINTS]


def choose_backend_endpoint(backend_name: str, endpoints: Iterable[str]) -> str:
    available = set(expand_endpoint_values(endpoints))
    supported = BACKEND_ENDPOINTS.get(backend_name)
    if supported:
        available.intersection_update(supported)
    for endpoint in BACKEND_PREFERENCES.get(backend_name, CHAT_ENDPOINTS):
        if endpoint in available:
            return endpoint
    for endpoint in CHAT_ENDPOINTS:
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


def catalog_endpoint_options() -> list[dict]:
    """Stable list for UI filters: id + short/long labels."""
    return [
        {"id": ep, "label": ENDPOINT_SHORT_LABELS.get(ep, ep), "description": ENDPOINT_LABELS.get(ep, ep)}
        for ep in ALL_ENDPOINTS
    ]


def downstream_types_for_endpoints(endpoints: Iterable[str]) -> list[str]:
    out, seen = [], set()
    for ep in expand_endpoint_values(endpoints):
        bucket = ENDPOINT_TO_DOWNSTREAM.get(ep) or ""
        if bucket and bucket not in seen:
            seen.add(bucket)
            out.append(bucket)
    return out
