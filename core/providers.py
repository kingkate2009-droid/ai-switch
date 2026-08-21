import random
import re
import threading
import warnings
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

# Short, low-cost probe texts — pick randomly so checks are not identical every run
_PROBE_PROMPTS = (
    "hi",
    "hello",
    "ping",
    "ok?",
    "1+1",
    "test",
    "hey",
    "status",
    "yo",
    "check",
)

# Per-thread probe knobs (bulk/quick health can lower timeout without global races)
_tls = threading.local()
_session_local = threading.local()


def probe_prompt() -> str:
    return random.choice(_PROBE_PROMPTS)


def _new_session() -> requests.Session:
    """Reuse one Session per thread to avoid TLS handshake on every probe."""
    s = getattr(_session_local, "session", None)
    if s is None:
        s = requests.Session()
        s.verify = False
        # Several NewAPI deployments block Requests' default
        # ``python-requests/*`` user agent at their WAF before authentication.
        # Use a stable product UA so probes match normal SDK traffic.
        s.headers.update({
            "User-Agent": "AI-Switch/2.1 (API compatibility probe)",
            "Accept": "application/json",
        })
        _session_local.session = s
    return s


def _is_html_response(response) -> bool:
    content_type = str((response.headers or {}).get("Content-Type") or "").lower()
    body = str(response.text or "").lstrip().lower()
    return "text/html" in content_type or body.startswith(("<!doctype html", "<html"))


def _html_gateway_error(response, path: str) -> str:
    """Describe an upstream WAF/proxy page without misclassifying the model."""
    server = str((response.headers or {}).get("Server") or "").strip()
    detail = f"HTTP {response.status_code} HTML response on {path}"
    if server:
        detail += f" ({server})"
    return detail + ": request blocked by site WAF/proxy before the API handled it"


def get_probe_timeout(default: float = None) -> float:
    override = getattr(_tls, "probe_timeout", None)
    if override is not None:
        try:
            return max(2.0, float(override))
        except Exception:
            pass
    return float(default if default is not None else PROBE_TIMEOUT)


@contextmanager
def probe_profile(timeout: float = None, retries: int = None):
    """Temporarily override probe timeout (and optionally expose retries via TLS)."""
    prev_t = getattr(_tls, "probe_timeout", None)
    prev_r = getattr(_tls, "probe_retries", None)
    try:
        if timeout is not None:
            _tls.probe_timeout = timeout
        if retries is not None:
            _tls.probe_retries = retries
        yield
    finally:
        if prev_t is None:
            if hasattr(_tls, "probe_timeout"):
                delattr(_tls, "probe_timeout")
        else:
            _tls.probe_timeout = prev_t
        if prev_r is None:
            if hasattr(_tls, "probe_retries"):
                delattr(_tls, "probe_retries")
        else:
            _tls.probe_retries = prev_r

# ── Provider Registry ─────────────────────────────────────

PROVIDERS = [
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "check_type": "anthropic",
    },
    {
        "id": "google",
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "check_type": "gemini",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "groq",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "together",
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "xai",
        "name": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "perplexity",
        "name": "Perplexity",
        "base_url": "https://api.perplexity.ai",
        "check_type": "openai_chat",
    },
    {
        "id": "mistral",
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "cohere",
        "name": "Cohere",
        "base_url": "https://api.cohere.ai/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "moonshot",
        "name": "Moonshot AI (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "kimi-code",
        "name": "Kimi Code",
        "base_url": "https://api.kimi.com/coding/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "zai",
        "name": "Z.AI (GLM / Zhipu)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "check_type": "openai_chat",
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "alibaba",
        "name": "Alibaba (Qwen)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "volcengine",
        "name": "Volcengine (Doubao)",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "check_type": "openai_chat",
    },
    {
        "id": "fireworks",
        "name": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "stepfun",
        "name": "StepFun (Step)",
        "base_url": "https://api.stepfun.com/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "deepinfra",
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "check_type": "openai_chat",
    },
    {
        "id": "cerebras",
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "novita",
        "name": "Novita AI",
        "base_url": "https://api.novita.ai/v3/openai",
        "check_type": "openai_chat",
    },
    {
        "id": "venice",
        "name": "Venice AI",
        "base_url": "https://api.venice.ai/api/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "zeroone",
        "name": "01.AI (Yi)",
        "base_url": "https://api.lingyiwanwu.com/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "ollama",
        "name": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "check_type": "openai_chat",
    },
    {
        "id": "qianfan",
        "name": "Baidu Qianfan",
        "base_url": "https://qianfan.baidubce.com/v2",
        "check_type": "openai_chat",
    },
    {
        "id": "xiaomi-token-plan",
        "name": "Xiaomi Token Plan",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "check_type": "openai_chat_apikey",
    },
]

PROVIDER_MAP = {p["id"]: p for p in PROVIDERS}


def get_providers() -> list[dict]:
    return list(PROVIDERS)


def get_provider(provider_id: str) -> Optional[dict]:
    return PROVIDER_MAP.get(provider_id)


def official_vendor_info(vendor: dict) -> dict:
    """Identify direct built-in provider endpoints, not relays using their name."""
    provider = get_provider(str((vendor or {}).get("provider") or "").strip())
    if not provider or str((vendor or {}).get("proxy_target") or "").strip():
        return {"official": False, "official_name": ""}
    try:
        actual = urlparse(str((vendor or {}).get("api_url") or "")).hostname or ""
        expected = urlparse(str(provider.get("base_url") or "")).hostname or ""
    except Exception:
        return {"official": False, "official_name": ""}
    official = bool(actual and expected and actual.lower() == expected.lower())
    return {
        "official": official,
        "official_name": provider.get("name") if official else "",
    }


def _normalize_host(host: str) -> str:
    return re.sub(r"^www\.", "", host.strip().lower())


def recognize_provider(text: str) -> Optional[dict]:
    text = text.strip().lower()

    direct = PROVIDER_MAP.get(text)
    if direct:
        return direct

    host_match = re.search(r"https?://([^\s/\"'<>]+)", text)
    if host_match:
        host = _normalize_host(host_match.group(1))
        for p in PROVIDERS:
            bu_host = _normalize_host(re.search(r"//([^/]+)", p["base_url"]).group(1)) if p["base_url"] else ""
            if host == bu_host or host.endswith("." + bu_host):
                return p
            if bu_host and (bu_host.endswith("." + host) or host in bu_host):
                return p

    for p in PROVIDERS:
        pid = p["id"].lower()
        pname = p["name"].lower()
        if text == pid or text == pname:
            return p
        if text in pid or pid in text or text in pname:
            return p

    return None


# ── Health-check probes ───────────────────────────────────

PROBE_TIMEOUT = 15
# Bulk/progressive health uses a shorter timeout (still overridable)
PROBE_TIMEOUT_QUICK = 8

_MODEL_CANDIDATES = ["gpt-3.5-turbo", "gpt-4o-mini", "deepseek-chat", "glm-4", "qwen-turbo", "mimo-v2.5-pro", ""]

# Volcengine Coding Plan requires specific models
_VOLCENGINE_MODELS = ["ark-code-latest", "Doubao-Seed-2.0-pro", "Doubao-Seed-2.0-Code", "GLM-4.7", "Kimi-K2.5"]


def _is_model_error(body: str) -> bool:
    lower = body.lower()
    if any(kw in lower for kw in [
        "model not found", "model not specified", "model name", "model does not exist",
        "not a supported model", "not supported model", "invalid model", "model cannot be empty",
        "no available channel for model", "model not available", "model access denied",
        "model not supported", "unknown model", "unrecognized model", "unsupportedmodel",
        "unsupported model", "does not support the coding plan",
        "无权访问", "无权限", "no access to model", "cannot access model",
        "令牌无权", "not allowed to access",
    ]):
        return True
    return False


def _is_quota_exhausted_body(body: str) -> bool:
    """True when 429/403 body means usage/billing cap, not a short rate-limit window.

    OpenCode Zen returns GoUsageLimitError / monthly caps as 429 — those must
    mark the key unhealthy so backends strip them.
    """
    bl = (body or "").lower()
    return any(
        x in bl
        for x in (
            "quota", "exhausted", "insufficient", "billing",
            "usagelimit", "usage limit", "usage_limit",
            "gousagelimit", "monthly limit", "credit", "balance",
            "no credits", "out of credits", "payment required",
            "额度",
        )
    )


def _requires_agentic_request(body: str) -> bool:
    lower = str(body or "").lower()
    return any(marker in lower for marker in (
        "non_agentic_blocked",
        "only serves agentic",
        "agentic (tool-calling) clients",
        "connect with an agentic client",
    ))


def _probe_chat_completions(url: str, headers: dict, models_to_try: Optional[list[str]] = None) -> tuple:
    # Use the URL as-is if it already contains a version path
    base = url.rstrip("/")
    if base.endswith(("/v1", "/v2", "/v3", "/v4")):
        chat_url = base + "/chat/completions"
    else:
        chat_url = base + "/v1/chat/completions"

    base_payload = {
        "messages": [{"role": "user", "content": probe_prompt()}],
    }
    candidates = models_to_try if models_to_try is not None else _MODEL_CANDIDATES
    # Filter out empty strings if we have other candidates
    if len(candidates) > 1:
        candidates = [m for m in candidates if m]
    try:
        all_model_errors = True
        any_403 = False
        last_403_body = None
        last_model_name = None
        last_model_err = ""
        for model in candidates:
            payload = dict(base_payload)
            if model:
                payload["model"] = model
            r = _new_session().post(chat_url, json=payload, headers=headers, timeout=get_probe_timeout())
            last_model_name = model or "(none)"
            if r.status_code == 200:
                try:
                    data = r.json()
                    msg = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if not msg:
                        msg = json.dumps(data)[:200]
                except Exception:
                    msg = r.text[:200]
                return True, f"[{model}] {msg[:200]}"
            if r.status_code == 429:
                body = r.text[:300]
                if _is_quota_exhausted_body(body):
                    return False, f"Quota exhausted: {body}"
                return True, f"Rate limited: {body}"
            body = r.text[:500]
            if _is_html_response(r):
                return False, _html_gateway_error(r, "/chat/completions")
            if _requires_agentic_request(body):
                agentic_payload = dict(payload)
                agentic_payload["tools"] = [{
                    "type": "function",
                    "function": {
                        "name": "get_status",
                        "description": "Get current status",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }]
                agentic_payload["tool_choice"] = "auto"
                r = _new_session().post(
                    chat_url,
                    json=agentic_payload,
                    headers=headers,
                    timeout=get_probe_timeout(),
                )
                if r.status_code == 200:
                    return True, f"[{model}] agentic tool-call request ok"
                body = r.text[:500]
            if _is_model_error(body):
                last_model_err = f"[{model or '(none)'}] {body[:220]}"
                continue
            if r.status_code == 401:
                return False, f"Auth failed (HTTP 401)"
            if r.status_code == 403:
                body_lower = body.lower()
                if "blocked" in body_lower:
                    return False, f"Access blocked: {body}"
                any_403 = True
                last_403_body = body
                all_model_errors = False
                continue
            all_model_errors = False
            return False, f"HTTP {r.status_code}: {body}"
        if any_403:
            msg = last_403_body or "Access denied"
            return False, f"Model '{last_model_name}': {msg}"
        if all_model_errors:
            # Prefer last concrete channel/model error over generic phrase
            if last_model_err:
                return False, last_model_err
            return False, "No compatible model found (key group may have no channel)"
        return False, last_model_err or "No compatible model found"
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)[:200]


def probe_openai_chat(url: str, api_key: str, models_to_try: Optional[list[str]] = None) -> tuple:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if models_to_try is None:
        discovered = _scan_models_openai(url, headers)
        if "volces.com" in url or "volcengine" in url.lower():
            models_to_try = discovered + _VOLCENGINE_MODELS + [""]
        else:
            models_to_try = discovered + [""] if discovered else _MODEL_CANDIDATES
    
    return _probe_chat_completions(url, headers, models_to_try)


def _responses_url(url: str) -> str:
    base = (url or "").rstrip("/")
    if base.endswith(("/v1", "/v2", "/v3", "/v4")):
        return base + "/responses"
    if re.search(r"/v\d+/", base + "/"):
        # already has a version path mid-url; append responses under same host path
        return base + "/responses"
    return base + "/v1/responses"


def _probe_responses(url: str, headers: dict, models_to_try: Optional[list[str]] = None) -> tuple:
    """Probe OpenAI Responses API (Codex wire_api=responses).

    Prefer streaming SSE so we verify the path Codex actually uses.
    """
    resp_url = _responses_url(url)
    candidates = models_to_try if models_to_try is not None else [
        "gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "o4-mini", "o3-mini", "gpt-5-mini",
    ]
    if isinstance(candidates, str):
        candidates = [candidates]
    candidates = [m for m in (candidates or []) if m] or [""]
    try:
        last_err = ""
        for model in candidates:
            payload = {
                "input": probe_prompt(),
                "stream": True,
            }
            if model:
                payload["model"] = model
            r = _new_session().post(
                resp_url, json=payload, headers=headers, timeout=get_probe_timeout(), stream=True,
            )
            if r.status_code == 200:
                # Read a small chunk of SSE to ensure stream works
                try:
                    chunk = next(r.iter_content(chunk_size=256), b"")
                    text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
                except Exception:
                    text = ""
                finally:
                    try:
                        r.close()
                    except Exception:
                        pass
                preview = (text or "stream ok")[:160].replace("\n", " ")
                return True, f"[responses:{model or 'default'}] {preview}"
            body = ""
            try:
                body = (r.text or "")[:400]
            except Exception:
                body = ""
            if _is_html_response(r):
                return False, _html_gateway_error(r, "/responses")
            last_err = f"HTTP {r.status_code}: {body}"
            if r.status_code == 401:
                return False, "Auth failed (HTTP 401) on /responses"
            if r.status_code == 403:
                bl = body.lower()
                if "quota" in bl or "balance" in bl or "insufficient" in bl or "额度" in body:
                    return False, f"Quota/billing on /responses: {body}"
                # may be model/group issue — try next model
                if _is_model_error(body):
                    continue
                return False, f"Access denied on /responses: {body}"
            if r.status_code == 429:
                if _is_quota_exhausted_body(body):
                    return False, f"Quota exhausted on /responses: {body}"
                return True, f"Rate limited on /responses: {body}"
            if r.status_code == 404:
                return False, "Responses API not found (HTTP 404) — endpoint may only support chat"
            if _is_model_error(body):
                continue
            # non-model hard error
            if r.status_code >= 500:
                return False, last_err
            continue
        return False, last_err or "No compatible model for /responses"
    except requests.exceptions.Timeout:
        return False, "Timeout on /responses"
    except Exception as e:
        return False, str(e)[:200]


def probe_openai_responses(url: str, api_key: str, models_to_try: Optional[list[str]] = None) -> tuple:
    """OpenAI-compatible Responses API probe (Bearer auth)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return _probe_responses(url, headers, models_to_try)


def probe_openai_chat_apikey(url: str, api_key: str, models_to_try: Optional[list[str]] = None) -> tuple:
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    if models_to_try is None:
        discovered = _scan_models_openai(url, headers)
        models_to_try = discovered + [""]
    return _probe_chat_completions(url, headers, models_to_try)


def _strip_version_path(url: str) -> str:
    root = url.rstrip("/")
    for v in ("/v4", "/v3", "/v2", "/v1"):
        if root.endswith(v):
            return root[:-len(v)]
    return root


_ANTHROPIC_MODELS = [
    "claude-sonnet-4-20250514",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
    "claude-opus-4-20250514",
    "claude-3-5-sonnet-latest",
    "claude-3-5-haiku-latest",
]


def _anthropic_messages_url(url: str) -> str:
    root = url.rstrip("/")
    if root.endswith("/messages"):
        return root
    if root.endswith(("/v1", "/v2", "/v3", "/v4")):
        return root + "/messages"
    # Paths like .../anthropic or .../claude already imply the product root
    return root + "/v1/messages"


def probe_anthropic(url: str, api_key: str, models_to_try: Optional[list[str]] = None) -> tuple:
    chat_url = _anthropic_messages_url(url)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    # When a concrete model was supplied, do not silently fall back to another
    # model: model-level endpoint detection must answer for that exact model.
    # Legacy key-level probes (models_to_try=None) retain discovery/fallbacks.
    candidates = []
    seen = set()
    for m in (models_to_try or []):
        if m and m not in seen:
            seen.add(m)
            candidates.append(m)
    if models_to_try is not None:
        candidates = candidates or [""]
    else:
        # Prefer inventory → discovered (gateway-specific) → Claude defaults → common chat models
        for m in _scan_models_anthropic(url, api_key):
            if m and m not in seen:
                seen.add(m)
                candidates.append(m)
        for m in list(_ANTHROPIC_MODELS) + ["mimo-v2.5-pro", "mimo-v2.5"] + [x for x in _MODEL_CANDIDATES if x]:
            if m and m not in seen:
                seen.add(m)
                candidates.append(m)
    try:
        last_body = ""
        last_code = 0
        all_model_errors = True
        for model in candidates:
            payload = {
                "model": model,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": probe_prompt()}],
            }
            r = _new_session().post(chat_url, json=payload, headers=headers, timeout=get_probe_timeout())
            last_code = r.status_code
            last_body = r.text[:300]
            if r.status_code == 200:
                try:
                    content = r.json().get("content", [])
                    msg = ""
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text" and block.get("text"):
                                msg = block.get("text")
                                break
                            if block.get("text"):
                                msg = block.get("text")
                                break
                            if block.get("thinking"):
                                msg = block.get("thinking")
                                break
                    if not msg:
                        msg = r.text[:200]
                except Exception:
                    msg = r.text[:200]
                return True, f"[{model}] {msg[:200]}"
            if _is_html_response(r):
                return False, _html_gateway_error(r, "/messages")
            if _requires_agentic_request(last_body):
                agentic_payload = dict(payload)
                agentic_payload["tools"] = [{
                    "name": "get_status",
                    "description": "Get current status",
                    "input_schema": {"type": "object", "properties": {}},
                }]
                r = _new_session().post(
                    chat_url,
                    json=agentic_payload,
                    headers=headers,
                    timeout=get_probe_timeout(),
                )
                last_code = r.status_code
                last_body = r.text[:300]
                if r.status_code == 200:
                    return True, f"[{model}] agentic tool-call request ok"
            if r.status_code in (401, 403):
                body_lower = last_body.lower()
                if r.status_code == 403 and _is_model_error(last_body):
                    continue
                if "blocked" in body_lower:
                    return False, f"Access blocked: {last_body}"
                return False, f"Auth failed (HTTP {r.status_code})"
            if r.status_code == 429:
                if _is_quota_exhausted_body(last_body):
                    return False, f"Quota exhausted: {last_body}"
                return True, f"Rate limited: {last_body}"
            if _is_model_error(last_body):
                continue
            all_model_errors = False
            return False, f"HTTP {r.status_code}: {last_body}"
        if all_model_errors:
            return False, "No compatible model found"
        return False, f"HTTP {last_code}: {last_body}" if last_body else "No compatible model found"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)[:200]


_GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]


def probe_gemini(url: str, api_key: str, models_to_try: Optional[list[str]] = None) -> tuple:
    base = url.rstrip("/")
    candidates = [m for m in (models_to_try or []) if m] or list(_GEMINI_MODELS)
    try:
        last_body = ""
        last_code = 0
        for model in candidates:
            chat_url = base + f"/models/{model}:generateContent?key={api_key}"
            r = _new_session().post(chat_url, json={
                "contents": [{"parts": [{"text": probe_prompt()}]}],
            }, timeout=get_probe_timeout())
            last_code = r.status_code
            last_body = r.text[:300]
            if r.status_code == 200:
                try:
                    msg = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if not msg:
                        msg = r.text[:200]
                except Exception:
                    msg = r.text[:200]
                return True, f"[{model}] {msg[:200]}"
            if r.status_code in (401, 403):
                if _is_model_error(last_body):
                    continue
                return False, f"Auth failed (HTTP {r.status_code})"
            if r.status_code == 429:
                if _is_quota_exhausted_body(last_body):
                    return False, f"Quota exhausted: {last_body}"
                return True, f"Rate limited: {last_body}"
            if _is_model_error(last_body):
                continue
            return False, f"HTTP {r.status_code}: {last_body}"
        return False, "No compatible model found" if not last_body else f"HTTP {last_code}: {last_body}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)[:200]


_PROBE_FUNCS = {
    "openai_chat": probe_openai_chat,
    "openai_chat_apikey": probe_openai_chat_apikey,
    "openai_responses": probe_openai_responses,
    "anthropic": probe_anthropic,
    "gemini": probe_gemini,
}


def probe_provider(check_type: str, url: str, api_key: str, models_to_try: Optional[list[str]] = None) -> tuple:
    func = _PROBE_FUNCS.get(check_type, probe_openai_chat)
    return func(url, api_key, models_to_try)


def probe_single_model(check_type: str, url: str, api_key: str, model: str) -> tuple:
    """Probe one specific model. Returns (healthy, message)."""
    if not model:
        return False, "Model id required"
    if check_type == "anthropic":
        return probe_anthropic(url, api_key, [model])
    if check_type == "gemini":
        return probe_gemini(url, api_key, [model])
    headers = {"Content-Type": "application/json"}
    if check_type == "openai_chat_apikey":
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    if check_type == "openai_responses":
        return _probe_responses(url, headers, [model])
    return _probe_chat_completions(url, headers, [model])


def _openai_versioned_path(url: str, *parts: str) -> str:
    """Join OpenAI-style path under a base that may already end with /vN."""
    base = (url or "").rstrip("/")
    suffix = "/".join(p.strip("/") for p in parts if p and str(p).strip("/"))
    if not suffix:
        return base
    if base.endswith(("/v1", "/v2", "/v3", "/v4")):
        return base + "/" + suffix
    if re.search(r"/v\d+/", base + "/"):
        return base + "/" + suffix
    return base + "/v1/" + suffix


def _probe_json_post(url: str, headers: dict, payload: dict, *, label: str) -> tuple:
    """Shared POST probe for non-chat JSON endpoints."""
    try:
        r = _new_session().post(url, json=payload, headers=headers, timeout=get_probe_timeout())
        body = ""
        try:
            body = (r.text or "")[:400]
        except Exception:
            body = ""
        if r.status_code == 200:
            return True, f"[{label}] ok"
        if _is_html_response(r):
            return False, _html_gateway_error(r, url.rsplit("/", 1)[-1])
        if r.status_code == 401:
            return False, f"Auth failed (HTTP 401) on {label}"
        if r.status_code == 429:
            if _is_quota_exhausted_body(body):
                return False, f"Quota exhausted on {label}: {body}"
            return True, f"Rate limited on {label}: {body}"
        if r.status_code == 404:
            return False, f"{label} not found (HTTP 404)"
        if r.status_code == 403:
            if _is_model_error(body):
                return False, f"[{label}] model error: {body}"
            bl = body.lower()
            if "quota" in bl or "balance" in bl or "insufficient" in bl or "额度" in body:
                return False, f"Quota/billing on {label}: {body}"
            return False, f"Access denied on {label}: {body}"
        if _is_model_error(body):
            return False, f"[{label}] model error: {body}"
        # Some gateways reject missing/invalid image params but still prove the route exists
        if r.status_code in (400, 422) and any(
            k in body.lower()
            for k in ("prompt", "size", "quality", "n ", '"n"', "image", "input", "file", "audio", "model")
        ):
            return True, f"[{label}] endpoint reachable (HTTP {r.status_code})"
        return False, f"HTTP {r.status_code} on {label}: {body}"
    except requests.exceptions.Timeout:
        return False, f"Timeout on {label}"
    except requests.exceptions.ConnectionError:
        return False, f"Connection refused on {label}"
    except Exception as e:
        return False, str(e)[:200]


def probe_openai_images(url: str, api_key: str, model: str = "") -> tuple:
    """Probe OpenAI Images generations API."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    endpoint_url = _openai_versioned_path(url, "images/generations")
    payload = {
        "prompt": "a red circle on white background",
        "n": 1,
        "size": "256x256",
    }
    if model:
        payload["model"] = model
    return _probe_json_post(endpoint_url, headers, payload, label=f"images:{model or 'default'}")


def probe_openai_videos(url: str, api_key: str, model: str = "") -> tuple:
    """Probe video generation-style endpoints (OpenAI / gateway variants)."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # Try a few common paths used by OpenAI-compatible gateways
    paths = ("videos", "videos/generations", "video/generations")
    last = (False, "No video endpoint")
    for path in paths:
        endpoint_url = _openai_versioned_path(url, path)
        payload = {"prompt": "a cat waving", "model": model} if model else {"prompt": "a cat waving"}
        if model:
            payload["model"] = model
        ok, msg = _probe_json_post(endpoint_url, headers, payload, label=f"videos:{path}:{model or 'default'}")
        if ok:
            return ok, msg
        last = (ok, msg)
        # 404 → try next path; auth errors stop early
        if "Auth failed" in (msg or "") or "Quota" in (msg or ""):
            return ok, msg
        if "not found" not in (msg or "").lower() and "404" not in (msg or ""):
            # non-404 hard-ish error still useful as result
            last = (ok, msg)
    return last


def probe_openai_audio(url: str, api_key: str, model: str = "") -> tuple:
    """Probe TTS (speech) and optionally transcriptions route presence."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    speech_url = _openai_versioned_path(url, "audio/speech")
    payload = {
        "model": model or "tts-1",
        "input": "hi",
        "voice": "alloy",
    }
    ok, msg = _probe_json_post(speech_url, headers, payload, label=f"audio/speech:{model or 'tts-1'}")
    if ok:
        return ok, msg
    # Fallback: transcriptions without file often 400 — still proves route
    tr_url = _openai_versioned_path(url, "audio/transcriptions")
    # multipart would be ideal; JSON 400/422 is enough for reachability
    ok2, msg2 = _probe_json_post(
        tr_url,
        headers,
        {"model": model or "whisper-1"},
        label=f"audio/transcriptions:{model or 'whisper-1'}",
    )
    if ok2:
        return ok2, msg2
    return ok, msg


def probe_openai_translations(url: str, api_key: str, model: str = "") -> tuple:
    """Probe audio translations endpoint (Whisper-style)."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    endpoint_url = _openai_versioned_path(url, "audio/translations")
    return _probe_json_post(
        endpoint_url,
        headers,
        {"model": model or "whisper-1"},
        label=f"audio/translations:{model or 'whisper-1'}",
    )


def probe_single_model_endpoint(endpoint: str, url: str, api_key: str, model: str) -> tuple:
    """Probe one concrete endpoint for one concrete model.

    Unlike the legacy ``check_type`` API this function never falls back to a
    different endpoint.  That distinction is important when a gateway
    exposes both OpenAI APIs and we need to persist a model×endpoint matrix.
    """
    endpoint = str(endpoint or "").strip().lower()
    if endpoint == "openai_chat":
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        return _probe_chat_completions(url, headers, [model])
    if endpoint == "openai_responses":
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        return _probe_responses(url, headers, [model])
    if endpoint == "anthropic_messages":
        return probe_anthropic(url, api_key, [model])
    if endpoint == "gemini_generate":
        return probe_gemini(url, api_key, [model])
    if endpoint == "openai_images":
        return probe_openai_images(url, api_key, model)
    if endpoint == "openai_videos":
        return probe_openai_videos(url, api_key, model)
    if endpoint == "openai_audio":
        return probe_openai_audio(url, api_key, model)
    if endpoint == "openai_translations":
        return probe_openai_translations(url, api_key, model)
    return False, f"Unknown endpoint: {endpoint}"


# ── Model scanning ────────────────────────────────────────

# Drop only inventory noise that is never useful as a routed model.
_MODEL_SKIP_PREFIXES = (
    "text-embedding", "embed-", "davinci", "babbage", "curie", "ada",
    "code-", "moderations", "realtime",
)


def _is_inventory_model(model_id: str) -> bool:
    """Keep chat + media models; drop embeddings/moderation/legacy completions."""
    lower = str(model_id or "").lower()
    if not lower:
        return False
    if any(lower.startswith(p) for p in _MODEL_SKIP_PREFIXES):
        return False
    if lower.endswith("-embedding") or lower.endswith("-embed") or lower.endswith("-moderation"):
        return False
    return True


def _is_chat_model(model_id: str) -> bool:
    """Backward-compatible alias: inventory models (chat + media)."""
    return _is_inventory_model(model_id)


def _scan_models_openai(url: str, headers: dict) -> list[str]:
    root = url.rstrip("/")
    # If URL already ends with a version path (e.g., /v3), use it directly
    if root.endswith(("/v1", "/v2", "/v3", "/v4")):
        models_url = root + "/models"
    else:
        models_url = root + "/v1/models"
    try:
        r = _new_session().get(models_url, headers=headers, timeout=get_probe_timeout())
        if r.status_code == 200:
            data = r.json()
            raw = data.get("data", []) if isinstance(data, dict) else data
            ids = [m.get("id", "") for m in raw if isinstance(m, dict) and m.get("id")]
            if ids:
                return [m for m in ids if _is_inventory_model(m)]
    except Exception:
        pass
    return []


def _scan_models_anthropic(url: str, api_key: str) -> list[str]:
    """List models for Anthropic-compatible gateways.

    Some providers (e.g. Xiaomi Token Plan) expose Anthropic at ``.../anthropic``
    but only serve ``/v1/models`` on the host root with OpenAI-style listing.
    """
    headers_variants = [
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    ]
    root = url.rstrip("/")
    candidates = []
    if root.endswith(("/v1", "/v2", "/v3", "/v4")):
        candidates.append(root + "/models")
    else:
        candidates.append(root + "/v1/models")
        candidates.append(root + "/models")
    # Strip trailing version once
    stripped = _strip_version_path(root)
    if stripped != root:
        candidates.append(stripped + "/v1/models")
        candidates.append(stripped + "/models")
    # Strip product path (.../anthropic, .../claude) → host root /v1/models
    product_root = stripped
    for suffix in ("/anthropic", "/claude"):
        if product_root.endswith(suffix):
            product_root = product_root[: -len(suffix)]
            break
    if product_root and product_root != stripped:
        candidates.append(product_root + "/v1/models")
        candidates.append(product_root + "/models")

    seen_urls = set()
    for models_url in candidates:
        if models_url in seen_urls:
            continue
        seen_urls.add(models_url)
        for headers in headers_variants:
            try:
                r = _new_session().get(models_url, headers=headers, timeout=get_probe_timeout())
                if r.status_code != 200:
                    continue
                data = r.json()
                raw = data.get("data", []) if isinstance(data, dict) else data
                if not isinstance(raw, list):
                    continue
                ids = [m.get("id", "") for m in raw if isinstance(m, dict) and m.get("id")]
                if ids:
                    return [m for m in ids if _is_inventory_model(m)]
            except Exception:
                continue
    return []


def scan_models(check_type: str, url: str, api_key: str) -> list[str]:
    if check_type in ("openai_chat", "openai_chat_apikey"):
        headers = {"Content-Type": "application/json"}
        if check_type == "openai_chat_apikey":
            headers["api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
        return _scan_models_openai(url, headers)
    if check_type == "anthropic":
        return _scan_models_anthropic(url, api_key)
    if check_type == "gemini":
        models_url = url.rstrip("/") + f"/models?key={api_key}"
        try:
            r = _new_session().get(models_url, timeout=get_probe_timeout())
            if r.status_code == 200:
                data = r.json()
                raw = data.get("models", []) if isinstance(data, dict) else data
                return [m.get("name", "").replace("models/", "") for m in raw
                        if isinstance(m, dict) and m.get("name")]
        except Exception:
            pass
        return []
    return []


def pick_default_model(models: list[str]) -> str:
    for prefix in ("gpt-4o", "gpt-4", "claude-sonnet-4", "claude-3.5", "deepseek-chat",
                   "gemini-2.0", "qwen-turbo", "glm-4", "mimo-v2.5", "agnes-2.0"):
        for m in models:
            if m.startswith(prefix):
                return m
    return models[0] if models else ""
