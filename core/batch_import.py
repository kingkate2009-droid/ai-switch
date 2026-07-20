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
# Standard + URL-safe base64 alphabet
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/_-]{16,}={0,2}$")
_BASE64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
_NAME_ONLY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_ .-]{1,30}$")
# Labels: KEY: / API_KEY= / key(base64): / token（Base64）： / base64：
_KEY_LABEL_RE = re.compile(
    r"^(?:key|api[_-]?key|apikey|token|secret|bearer|base64|b64)"
    r"(?:\s*[\(\[][^)\]]*[\)\]])?"  # optional (base64) / [Base64]
    r"\s*[:=]\s*",
    re.IGNORECASE,
)
# URL field labels: baseurl: / base_url= / api_url：
_URL_LABEL_RE = re.compile(
    r"^(?:base[_-]?url|api[_-]?url|url|endpoint|host|server)"
    r"(?:\s*[\(\[][^)\]]*[\)\]])?"
    r"\s*[:=]\s*",
    re.IGNORECASE,
)


def _normalize_text(text: str) -> str:
    return text.replace("：", ":").replace("，", ",").replace("（", "(").replace("）", ")")


def _strip_field_label(value: str) -> str:
    """Remove leading field labels like key(base64): or baseurl: / base64：."""
    value = _normalize_text(value or "").strip()
    if not value:
        return value
    value = _KEY_LABEL_RE.sub("", value, count=1).strip()
    value = _URL_LABEL_RE.sub("", value, count=1).strip()
    return value


def _looks_like_api_key(value: str) -> bool:
    value = (value or "").strip()
    if not value or len(value) < 16:
        return False
    if URL_RE.search(value):
        return False
    # Reject labeled leftovers like key(base64):sk-... (label must be stripped first)
    if re.search(r"^[A-Za-z][A-Za-z0-9_-]{0,20}\s*[\(\[].*[:=]", value):
        return False
    if ":" in value and not value.startswith("sk-") and not value.startswith("tp-"):
        # allow only pure keys; labeled forms should already be stripped
        left = value.split(":", 1)[0]
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,24}", left):
            return False
    if KEY_RE.fullmatch(value):
        return True
    if KEY_RE.search(value) and re.fullmatch(r"[A-Za-z0-9_+/=.-]{16,}", value):
        return True
    # g2a_xxx / xai-xxx style tokens
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{15,}", value):
        return True
    return False


def _has_known_key_prefix(text: str) -> bool:
    t = (text or "").strip()
    return bool(
        t.startswith("sk-")
        or t.startswith("tp-")
        or t.startswith("xai-")
        or t.startswith("g2a_")
        or t.startswith("Bearer ")
    )


def _is_already_key_or_url(text: str) -> bool:
    """True if text is already a usable API key or URL (do not base64-decode further)."""
    t = (text or "").strip()
    if not t:
        return False
    if URL_RE.search(t):
        return True
    # Only treat known-prefix secrets as final keys. Generic long alnum tokens
    # often ARE base64(sk-...) and must still be decoded.
    if _has_known_key_prefix(t):
        return True
    return False


def _try_base64_decode(text: str) -> str:
    """Try to decode Base64 / Base64URL string. Returns decoded string or original."""
    text = (text or "").strip()
    if not text or len(text) < 8:
        return text
    # Never re-decode plain keys / URLs (sk-... often matches base64 alphabet)
    if _is_already_key_or_url(text):
        return text
    # labeled forms: decode payload only (key: / base64: / api_key= ...)
    stripped = _strip_field_label(text)
    if stripped != text:
        decoded_payload = _try_base64_decode(stripped)
        if decoded_payload != stripped:
            return decoded_payload
        # labeled base64: still try raw payload as base64 even if "key-like"
        text = stripped

    # Allow whitespace-wrapped blobs
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 16:
        return text
    if not _BASE64_RE.match(compact):
        return text

    candidates = [compact]
    # URL-safe -> standard
    if "-" in compact or "_" in compact:
        candidates.append(compact.replace("-", "+").replace("_", "/"))

    for raw in candidates:
        try:
            pad = (-len(raw)) % 4
            if pad:
                raw = raw + ("=" * pad)
            decoded_bytes = base64.b64decode(raw, validate=False)
            # utf-8 text only (strict) — garbage binary must not replace source
            try:
                decoded = decoded_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue

            decoded = decoded.strip()
            if not decoded or len(decoded) < 4:
                continue
            # Prefer decoded when it contains URL / key / JSON structure
            if (
                URL_RE.search(decoded)
                or _looks_like_api_key(decoded)
                or _has_known_key_prefix(decoded)
                or decoded.startswith("{")
                or decoded.startswith("[")
                or ("http://" in decoded or "https://" in decoded)
                or ("api_key" in decoded.lower() or "base_url" in decoded.lower())
            ):
                return decoded
            # printable fallback only if mostly alnum and length makes sense
            if decoded.isprintable() and 8 <= len(decoded) < 4000 and not decoded.isspace():
                alnum = sum(ch.isalnum() for ch in decoded)
                if alnum >= max(6, int(len(decoded) * 0.7)):
                    return decoded
        except Exception:
            continue
    return text


def _expand_base64_in_text(text: str) -> str:
    """Decode whole-text base64 and/or embedded base64 tokens before parsing."""
    text = (text or "").strip()
    if not text:
        return text

    # 1) Entire payload is base64 (single line or whitespace-wrapped)
    #    Skip multi-line labeled configs (baseurl: / key: ...)
    if "\n" not in text and not re.search(r"(?i)\b(?:base[_-]?url|api[_-]?url|key|token)\s*[:=]", text):
        whole = _try_base64_decode(text)
        if whole != text:
            return whole

    # 2) Replace base64-looking tokens in place (not whole keys/urls)
    def _repl(m: re.Match) -> str:
        token = m.group(0)
        if _is_already_key_or_url(token):
            return token
        decoded = _try_base64_decode(token)
        return decoded if decoded != token else token

    return _BASE64_CHUNK_RE.sub(_repl, text)


def _extract_key_candidates(text: str) -> list[str]:
    """Extract API key candidates from a line/snippet, decoding Base64 when needed."""
    clean_text = (text or "").strip()
    if not clean_text:
        return []

    # Strip labels: KEY: / API_KEY= / key(base64): / token：
    clean_text = _strip_field_label(clean_text)
    # Strip trailing annotations: (Base64) / [base64]
    clean_text = re.sub(r"\s*[\(\[][^)\]]*[Bb]ase64[^)\]]*[\)\]]\s*$", "", clean_text).strip()
    clean_text = re.sub(r"\s*\([^)]*\)\s*$", "", clean_text).strip()

    candidates: list[str] = []
    seen = set()

    def _add(val: str) -> None:
        val = (val or "").strip().strip("\"'`")
        if not val or val in seen:
            return
        # peel residual labels once more (e.g. after inline base64 expand)
        stripped = _strip_field_label(val)
        if stripped != val:
            _add(stripped)
            return
        # If value is a whole line with URL+key, pull keys out of it
        if URL_RE.search(val) and (" " in val or "\t" in val or "\n" in val or ":" in val):
            for m in KEY_RE.findall(val):
                _add(m)
            for part in re.split(r"[\s,;]+", val):
                if part and not URL_RE.search(part):
                    _add(part)
            return
        if not _looks_like_api_key(val):
            # still try KEY_RE extraction from messy strings
            for m in KEY_RE.findall(val):
                if m != val:
                    _add(m)
            return
        # Prefer decoded form over raw base64 blob
        if _BASE64_RE.match(val):
            decoded = _try_base64_decode(val)
            if decoded != val:
                if _looks_like_api_key(decoded):
                    if decoded not in seen:
                        seen.add(decoded)
                        candidates.append(decoded)
                    return
                # decoded multi-field line
                if URL_RE.search(decoded) or " " in decoded:
                    _add(decoded)
                    return
        seen.add(val)
        candidates.append(val)

    # Whole line as one token (common: KEY:xxxx or pure base64)
    if clean_text and " " not in clean_text and "\t" not in clean_text and "\n" not in clean_text:
        decoded_whole = _try_base64_decode(clean_text)
        _add(decoded_whole)
        if decoded_whole != clean_text:
            _add(clean_text)

    for word in re.split(r"[\s,;]+", clean_text):
        word = word.strip().strip("\"'`")
        if not word:
            continue
        word = _strip_field_label(word)
        decoded = _try_base64_decode(word)
        _add(decoded)
        if decoded != word:
            _add(word)

    # Fallback regex scan on decoded text
    words = []
    for word in re.split(r"[\s,;]+", clean_text):
        words.append(_try_base64_decode(_strip_field_label(word)))
    decoded_text = " ".join(words)
    for m in KEY_RE.findall(decoded_text):
        _add(m)

    return candidates


def _find_api_keys(text: str) -> list[str]:
    """Find API keys in text, with Base64 auto-decode."""
    return _extract_key_candidates(text)


def _find_urls(text: str) -> list[str]:
    """Find URLs in text, with Base64 auto-decode."""
    # peel baseurl: / api_url= labels so URL_RE can match cleanly
    raw = _strip_field_label(text or "")
    expanded = _expand_base64_in_text(raw)
    words = re.split(r"[\s,;]+", expanded)
    decoded_parts = []
    for word in words:
        w = _strip_field_label(word)
        decoded_parts.append(_try_base64_decode(w))
    decoded_text = " ".join(decoded_parts)
    # also scan expanded full text
    urls = URL_RE.findall(decoded_text) + URL_RE.findall(expanded) + URL_RE.findall(raw)
    # unique preserve order
    seen = set()
    out = []
    for u in urls:
        u = u.rstrip("/").rstrip("\",'")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


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
    # If base64 JSON, decode first
    decoded = _try_base64_decode(text)
    if decoded != text and (decoded.startswith("{") or decoded.startswith("[")):
        text = decoded
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
        url = item.get("url") or item.get("api_url") or item.get("base_url") or item.get("baseURL") or ""
        key = (
            item.get("key")
            or item.get("api_key")
            or item.get("apiKey")
            or item.get("apikey")
            or item.get("token")
            or item.get("secret")
            or ""
        )
        if not url and not key:
            # Try nested objects
            for v in item.values():
                if isinstance(v, dict):
                    url = url or v.get("url") or v.get("api_url") or v.get("base_url") or v.get("baseURL") or ""
                    key = key or v.get("key") or v.get("api_key") or v.get("apiKey") or v.get("token") or ""
        # decode base64 fields if needed
        if key:
            key = _try_base64_decode(str(key))
            if not _looks_like_api_key(key):
                # maybe still nested text
                keys = _find_api_keys(str(key))
                if keys:
                    key = keys[0]
        if url:
            url = _try_base64_decode(str(url))
            urls = _find_urls(str(url))
            if urls:
                url = urls[0]
        if url or key:
            provider = item.get("provider") or item.get("model") or common_type or (
                _guess_provider_from_url(url) if url else "unknown"
            )
            ep = (item.get("endpoint_type") or item.get("endpoint") or "openai").lower()
            if ep not in ("openai", "anthropic"):
                low = (url or "").lower()
                ep = "anthropic" if ("/anthropic" in low or "api.anthropic.com" in low) else "openai"
            vendor_name = item.get("vendor") or item.get("vendor_name") or item.get("name") or ""
            # if "name" looks like key name and vendor empty, don't use as vendor
            if vendor_name and key and vendor_name == key:
                vendor_name = ""
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
    text = _normalize_text(text or "")
    # Expand whole-input / embedded base64 before any parsing
    expanded = _expand_base64_in_text(text)
    if expanded != text:
        text = expanded

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

        # peel baseurl:/key(base64): labels, then decode base64 payloads
        labeled = _strip_field_label(stripped)
        decoded_line = _try_base64_decode(labeled)
        work = decoded_line if decoded_line != labeled else labeled

        found_urls = _find_urls(work) or _find_urls(stripped)
        found_keys = _find_api_keys(stripped) or _find_api_keys(work)

        # IMPORTANT: a single line may contain both URL and key
        if found_urls or found_keys:
            if found_urls:
                for u in found_urls:
                    prov = _guess_provider_from_url(u)
                    if pending_name:
                        prov = re.sub(r"[^a-zA-Z0-9_-]", "", pending_name.split()[0].lower())
                        pending_name = ""
                    # provider token before URL on same line
                    # e.g. "openai https://... sk-..."
                    head = work.split()
                    if head and not head[0].startswith("http") and _NAME_ONLY_RE.match(head[0]):
                        if not URL_RE.search(head[0]) and not _looks_like_api_key(head[0]):
                            prov = re.sub(r"[^a-zA-Z0-9_-]", "", head[0].lower()) or prov
                    urls_with_names.append((u, prov, pending_endpoint_type))
                pending_endpoint_type = "openai"
            if found_keys:
                keys.extend(found_keys)
                pending_name = ""
        elif _is_provider_name(work):
            pending_name = work

    if not urls_with_names and not keys:
        return []

    entries: list[dict] = []
    if urls_with_names and keys:
        for url, prov, ep_type in urls_with_names:
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
        dedup_key = (e.get("api_url") or "", e.get("api_key") or "")
        if dedup_key not in seen:
            seen.add(dedup_key)
            deduped.append(e)
    return deduped
