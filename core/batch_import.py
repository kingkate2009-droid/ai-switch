import json
import re
import base64
from typing import Optional

from core.providers import get_provider, recognize_provider

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
# API keys: sk-/tp- prefixes, UUIDs, or long tokens that may include _ and -
KEY_RE = re.compile(
    r"(?:sk-[a-zA-Z0-9_-]{16,}|tp-[a-zA-Z0-9_-]{16,}|"
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|"
    r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_+/=-]))"
)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
_NAME_ONLY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_ .-]{1,30}$")
_KEY_LABEL_RE = re.compile(
    r"^(?:key|api[_-]?key|apikey|token|secret|bearer)\s*[:=]\s*",
    re.IGNORECASE,
)


def _normalize_text(text: str) -> str:
    return text.replace("：", ":").replace("，", ",").replace("（", "(").replace("）", ")")


def _looks_like_api_key(value: str) -> bool:
    value = (value or "").strip()
    if not value or len(value) < 16:
        return False
    if URL_RE.search(value):
        return False
    if KEY_RE.fullmatch(value) or KEY_RE.search(value):
        return True
    # g2a_xxx / xai-xxx style tokens
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{15,}", value):
        return True
    return False


def _try_base64_decode(text: str) -> str:
    """Try to decode Base64 encoded string. Returns decoded string or original if not valid Base64."""
    text = text.strip()
    if not text or len(text) < 10:
        return text

    # Check if it looks like Base64 (only alphanumeric + / + = padding)
    if not _BASE64_RE.match(text):
        return text

    try:
        raw = text
        padding = 4 - len(raw) % 4
        if padding != 4:
            raw = raw + ("=" * padding)

        decoded = base64.b64decode(raw).decode("utf-8", errors="strict")

        # Prefer decoded value when it looks like a key/URL/token
        if URL_RE.search(decoded) or _looks_like_api_key(decoded):
            return decoded

        # Check if decoded result is printable and reasonable length
        if decoded.isprintable() and 5 < len(decoded) < 500 and not decoded.isspace():
            return decoded

        return text
    except Exception:
        return text


def _extract_key_candidates(text: str) -> list[str]:
    """Extract API key candidates from a line/snippet, decoding Base64 when needed."""
    clean_text = (text or "").strip()
    if not clean_text:
        return []

    # Strip labels: KEY: / API_KEY= / token：
    clean_text = _KEY_LABEL_RE.sub("", clean_text)
    # Strip trailing annotations: (Base64) / [base64]
    clean_text = re.sub(r"\s*[\(\[][^)\]]*[Bb]ase64[^)\]]*[\)\]]\s*$", "", clean_text).strip()
    clean_text = re.sub(r"\s*\([^)]*\)\s*$", "", clean_text).strip()

    candidates: list[str] = []
    seen = set()

    def _add(val: str) -> None:
        val = (val or "").strip().strip("\"'`")
        if not val or val in seen:
            return
        if not _looks_like_api_key(val):
            return
        # Prefer decoded form over raw base64 blob
        if _BASE64_RE.match(val):
            decoded = _try_base64_decode(val)
            if decoded != val and _looks_like_api_key(decoded):
                if decoded not in seen:
                    seen.add(decoded)
                    candidates.append(decoded)
                return
        seen.add(val)
        candidates.append(val)

    # Whole line as one token (common: KEY:xxxx)
    if clean_text and " " not in clean_text and "\t" not in clean_text:
        _add(_try_base64_decode(clean_text))
        _add(clean_text)

    for word in re.split(r"[\s,;]+", clean_text):
        word = word.strip().strip("\"'`")
        if not word:
            continue
        word = _KEY_LABEL_RE.sub("", word)
        decoded = _try_base64_decode(word)
        _add(decoded)
        if decoded != word:
            _add(word)

    # Fallback regex scan on decoded text
    words = []
    for word in re.split(r"[\s,;]+", clean_text):
        words.append(_try_base64_decode(word))
    decoded_text = " ".join(words)
    for m in KEY_RE.findall(decoded_text):
        _add(m)

    return candidates


def _find_api_keys(text: str) -> list[str]:
    """Find API keys in text, with Base64 auto-decode."""
    return _extract_key_candidates(text)


def _find_urls(text: str) -> list[str]:
    """Find URLs in text, with Base64 auto-decode."""
    # First try to decode any Base64 segments
    words = text.split()
    decoded_parts = []
    for word in words:
        decoded_parts.append(_try_base64_decode(word))
    decoded_text = ' '.join(decoded_parts)
    
    return [u.rstrip("/") for u in URL_RE.findall(decoded_text)]


def _make_key_name(key: str) -> str:
    suffix = key[-4:] if len(key) >= 4 else key
    return f"key-{suffix}"


def _guess_provider_from_url(url: str) -> str:
    matched = recognize_provider(url)
    if matched:
        return matched["id"]
    host = re.sub(r"^https?://", "", url).split("/")[0]
    parts = host.split(".")
    if parts[0] in ("api", "v1", "v2", "www", "apihub"):
        parts = parts[1:]
    name = re.sub(r"[^a-zA-Z0-9_-]", "", parts[0] if parts else host)
    return name or "provider"


def _is_provider_name(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 40:
        return False
    if _find_urls(stripped) or _find_api_keys(stripped):
        return False
    return bool(_NAME_ONLY_RE.match(stripped))


def _try_parse_json(text: str) -> Optional[list[dict]]:
    """Try to parse input as JSON. Returns list of entry dicts or None."""
    text = text.strip()
    if not (text.startswith("{") or text.startswith("[")):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    
    items = data if isinstance(data, list) else [data]
    
    # Try to derive provider from a common prefix across items
    common_type = None
    for item in items:
        if isinstance(item, dict) and item.get("_type"):
            t = str(item["_type"])
            if "_channel_conn" in t:
                common_type = t.split("_channel_conn")[0]
    
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("api_url") or item.get("base_url") or ""
        key = item.get("key") or item.get("api_key") or item.get("apikey") or item.get("token") or ""
        if not url and not key:
            # Try nested objects
            for v in item.values():
                if isinstance(v, dict):
                    url = url or v.get("url") or v.get("api_url") or ""
                    key = key or v.get("key") or v.get("api_key") or ""
        if url or key:
            provider = item.get("provider") or item.get("model") or common_type or (_guess_provider_from_url(url) if url else "unknown")
            ep = (item.get("endpoint_type") or item.get("endpoint") or "openai").lower()
            if ep not in ("openai", "anthropic"):
                # Heuristic from URL
                low = (url or "").lower()
                ep = "anthropic" if ("/anthropic" in low or "api.anthropic.com" in low) else "openai"
            vendor_name = item.get("name") if not key else (item.get("vendor") or item.get("vendor_name") or "")
            entries.append({
                "provider": str(provider),
                "vendor_name": vendor_name or str(provider).replace("-", " ").title(),
                "name": _make_key_name(key) if key else "(need key)",
                "api_url": url.rstrip("/") if url else "",
                "api_key": key,
                "endpoint_type": ep,
            })
    return entries if entries else None


def parse_batch_text(text: str) -> list[dict]:
    text = _normalize_text(text)
    
    # Try JSON parsing first
    json_entries = _try_parse_json(text)
    if json_entries is not None:
        return _dedupe_entries(json_entries)
    
    lines = text.strip().split("\n")
    urls_with_names: list[tuple[str, str, str]] = []  # (url, provider, endpoint_type)
    keys: list[str] = []
    pending_name = ""
    pending_endpoint_type = "openai"

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue

        # Check for endpoint type directive
        lower_stripped = stripped.lower()
        if lower_stripped.startswith("endpoint:"):
            ep_type = lower_stripped.split(":", 1)[1].strip()
            if ep_type in ("openai", "anthropic"):
                pending_endpoint_type = ep_type
            continue

        found_urls = _find_urls(stripped)
        found_keys = _find_api_keys(stripped)

        if found_urls:
            for u in found_urls:
                prov = _guess_provider_from_url(u)
                if pending_name:
                    prov = re.sub(r"[^a-zA-Z0-9_-]", "", pending_name.split()[0].lower())
                    pending_name = ""
                urls_with_names.append((u, prov, pending_endpoint_type))
                pending_endpoint_type = "openai"  # Reset after use
        elif found_keys:
            keys.extend(found_keys)
            pending_name = ""
        elif _is_provider_name(stripped):
            pending_name = stripped

    if not urls_with_names and not keys:
        return []

    entries: list[dict] = []
    if urls_with_names and keys:
        for url, prov, ep_type in urls_with_names:
            # Infer anthropic from URL if not set
            ep = ep_type
            if ep == "openai" and ("/anthropic" in url.lower() or "api.anthropic.com" in url.lower()):
                ep = "anthropic"
            for k in keys:
                entries.append({
                    "provider": prov,
                    "vendor_name": str(prov).replace("-", " ").title(),
                    "name": _make_key_name(k),
                    "api_url": url,
                    "api_key": k,
                    "endpoint_type": ep,
                })
    elif urls_with_names:
        for url, prov, ep_type in urls_with_names:
            ep = ep_type
            if ep == "openai" and ("/anthropic" in url.lower() or "api.anthropic.com" in url.lower()):
                ep = "anthropic"
            entries.append({
                "provider": prov,
                "vendor_name": str(prov).replace("-", " ").title(),
                "name": "(need key)",
                "api_url": url,
                "api_key": "",
                "endpoint_type": ep,
            })
    elif keys:
        for k in keys:
            entries.append({
                "provider": "unknown",
                "vendor_name": "Unknown",
                "name": _make_key_name(k),
                "api_url": "",
                "api_key": k,
                "endpoint_type": "openai",
            })

    return _dedupe_entries(entries)


def _dedupe_entries(entries: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for e in entries:
        dedup_key = (e["api_url"], e["api_key"])
        if dedup_key not in seen:
            seen.add(dedup_key)
            deduped.append(e)
    return deduped
