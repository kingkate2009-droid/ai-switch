import json
import re
import base64
from typing import Optional

from core.providers import get_provider, recognize_provider

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
KEY_RE = re.compile(
    r"(?:sk-[a-zA-Z0-9_-]{20,}|tp-[a-zA-Z0-9]{20,}|"
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|"
    r"[a-zA-Z0-9]{30,})"
)
_NAME_ONLY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_ .-]{1,30}$")


def _normalize_text(text: str) -> str:
    return text.replace("：", ":").replace("，", ",").replace("（", "(").replace("）", ")")


def _try_base64_decode(text: str) -> str:
    """Try to decode Base64 encoded string. Returns decoded string or original if not valid Base64."""
    text = text.strip()
    if not text or len(text) < 10:
        return text
    
    # Check if it looks like Base64 (only alphanumeric + / + = padding)
    if not re.match(r'^[A-Za-z0-9+/]+={0,2}$', text):
        return text
    
    try:
        # Add padding if needed
        padding = 4 - len(text) % 4
        if padding != 4:
            text += '=' * padding
        
        decoded = base64.b64decode(text).decode('utf-8', errors='ignore')
        
        # Check if decoded result looks useful (contains URL or key pattern)
        if URL_RE.search(decoded) or KEY_RE.search(decoded):
            return decoded
        
        # Check if decoded result is printable and reasonable length
        if decoded.isprintable() and 5 < len(decoded) < 500:
            return decoded
        
        return text
    except Exception:
        return text


def _find_api_keys(text: str) -> list[str]:
    """Find API keys in text, with Base64 auto-decode."""
    # Strip key: prefix and parenthetical annotations from entire text first
    clean_text = text
    # Strip leading "key:" / "api_key:" / etc.
    if ":" in clean_text and clean_text.index(":") < 20:
        prefix = clean_text.split(":", 1)[0].lower()
        if prefix in ("key", "api_key", "apikey", "api-key", "token"):
            clean_text = clean_text.split(":", 1)[1]
    # Strip trailing parenthetical annotations like (Base64)
    clean_text = re.sub(r'\s*\([^)]*\)\s*$', '', clean_text).strip()
    
    words = clean_text.split()
    decoded_parts = []
    for word in words:
        decoded_parts.append(_try_base64_decode(word))
    decoded_text = ' '.join(decoded_parts)
    
    return KEY_RE.findall(decoded_text)


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
            entries.append({
                "provider": provider,
                "name": _make_key_name(key) if key else "(need key)",
                "api_url": url.rstrip("/"),
                "api_key": key,
                "endpoint_type": "openai",
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
            for k in keys:
                entries.append({
                    "provider": prov,
                    "name": _make_key_name(k),
                    "api_url": url,
                    "api_key": k,
                    "endpoint_type": ep_type,
                })
    elif urls_with_names:
        for url, prov, ep_type in urls_with_names:
            entries.append({
                "provider": prov,
                "name": "(need key)",
                "api_url": url,
                "api_key": "",
                "endpoint_type": ep_type,
            })
    elif keys:
        for k in keys:
            entries.append({
                "provider": "unknown",
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
