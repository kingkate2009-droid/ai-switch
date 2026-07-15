import re
import warnings

from typing import Optional

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)


def _new_session() -> requests.Session:
    s = requests.Session()
    s.verify = False
    return s

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


def _probe_chat_completions(url: str, headers: dict, models_to_try: Optional[list[str]] = None) -> tuple:
    # Use the URL as-is if it already contains a version path
    base = url.rstrip("/")
    if base.endswith(("/v1", "/v2", "/v3", "/v4")):
        chat_url = base + "/chat/completions"
    else:
        chat_url = base + "/v1/chat/completions"

    base_payload = {
        "messages": [{"role": "user", "content": "hi"}],
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
        for model in candidates:
            payload = dict(base_payload)
            if model:
                payload["model"] = model
            r = _new_session().post(chat_url, json=payload, headers=headers, timeout=PROBE_TIMEOUT)
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
                body_lower = body.lower()
                if "quota" in body_lower or "exhausted" in body_lower or "insufficient" in body_lower:
                    return False, f"Quota exhausted: {body}"
                return True, f"Rate limited: {body}"
            body = r.text[:500]
            if body.lstrip().startswith("<") and "html" in body[:100].lower():
                continue
            if _is_model_error(body):
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
            return False, "No compatible model found"
        return False, "No compatible model found"
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
        elif discovered:
            models_to_try = discovered + [""]
        else:
            models_to_try = _MODEL_CANDIDATES
    
    return _probe_chat_completions(url, headers, models_to_try)


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


def probe_anthropic(url: str, api_key: str) -> tuple:
    root = _strip_version_path(url)
    chat_url = root + "/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    try:
        r = _new_session().post(chat_url, json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
        }, headers=headers, timeout=PROBE_TIMEOUT)
        if r.status_code == 200:
            try:
                msg = r.json().get("content", [{}])[0].get("text", "")
                if not msg:
                    msg = r.text[:200]
            except Exception:
                msg = r.text[:200]
            return True, f"[claude-3-5-sonnet-20241022] {msg[:200]}"
        if r.status_code in (401, 403):
            return False, f"Auth failed (HTTP {r.status_code})"
        if r.status_code == 429:
            body = r.text[:300]
            body_lower = body.lower()
            if "quota" in body_lower or "exhausted" in body_lower or "insufficient" in body_lower:
                return False, f"Quota exhausted: {body}"
            return True, f"Rate limited: {body}"
        body = r.text[:300]
        if _is_model_error(body):
            return False, "No compatible model found"
        return False, f"HTTP {r.status_code}: {body}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)[:200]


def probe_gemini(url: str, api_key: str) -> tuple:
    chat_url = url.rstrip("/") + f"/models/gemini-2.0-flash:generateContent?key={api_key}"
    try:
        r = _new_session().post(chat_url, json={
            "contents": [{"parts": [{"text": "hi"}]}],
        }, timeout=PROBE_TIMEOUT)
        if r.status_code == 200:
            try:
                msg = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                if not msg:
                    msg = r.text[:200]
            except Exception:
                msg = r.text[:200]
            return True, f"[gemini-2.0-flash] {msg[:200]}"
        if r.status_code in (401, 403):
            return False, f"Auth failed (HTTP {r.status_code})"
        if r.status_code == 429:
            body = r.text[:300]
            body_lower = body.lower()
            if "quota" in body_lower or "exhausted" in body_lower or "insufficient" in body_lower:
                return False, f"Quota exhausted: {body}"
            return True, f"Rate limited: {body}"
        body = r.text[:300]
        if _is_model_error(body):
            return False, "No compatible model found"
        return False, f"HTTP {r.status_code}: {body}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)[:200]


_PROBE_FUNCS = {
    "openai_chat": probe_openai_chat,
    "openai_chat_apikey": probe_openai_chat_apikey,
    "anthropic": probe_anthropic,
    "gemini": probe_gemini,
}


def probe_provider(check_type: str, url: str, api_key: str, models_to_try: Optional[list[str]] = None) -> tuple:
    func = _PROBE_FUNCS.get(check_type, probe_openai_chat)
    return func(url, api_key, models_to_try)


# ── Model scanning ────────────────────────────────────────

_MODEL_SKIP_PREFIXES = ("text-embedding", "embed-", "audio-", "tts-", "whisper",
                        "davinci", "babbage", "curie", "ada", "code-",
                        "moderations", "realtime", "video-", "image-")


def _is_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    if any(lower.startswith(p) for p in _MODEL_SKIP_PREFIXES):
        return False
    if lower.endswith("-embedding") or lower.endswith("-embed") or lower.endswith("-moderation"):
        return False
    # Skip video/image/audio/tts/asr models by keyword anywhere in ID
    if any(kw in lower for kw in ("-video", "-image", "-audio", "-tts", "-asr")):
        return False
    return True


def _scan_models_openai(url: str, headers: dict) -> list[str]:
    root = url.rstrip("/")
    # If URL already ends with a version path (e.g., /v3), use it directly
    if root.endswith(("/v1", "/v2", "/v3", "/v4")):
        models_url = root + "/models"
    else:
        models_url = root + "/v1/models"
    try:
        r = _new_session().get(models_url, headers=headers, timeout=PROBE_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            raw = data.get("data", []) if isinstance(data, dict) else data
            ids = [m.get("id", "") for m in raw if isinstance(m, dict) and m.get("id")]
            if ids:
                return [m for m in ids if _is_chat_model(m)]
    except Exception:
        pass
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
        models = _scan_models_openai(url, {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        })
        if not models:
            models_url = url.rstrip("/") + "/v1/models"
            try:
                r = _new_session().get(models_url, headers={
                    "x-api-key": api_key, "anthropic-version": "2023-06-01"
                }, timeout=PROBE_TIMEOUT)
                if r.status_code == 200:
                    data = r.json()
                    raw = data.get("data", []) if isinstance(data, dict) else data
                    models = [m.get("id", "") for m in raw if isinstance(m, dict) and m.get("id")]
            except Exception:
                pass
        return models
    if check_type == "gemini":
        models_url = url.rstrip("/") + f"/models?key={api_key}"
        try:
            r = _new_session().get(models_url, timeout=PROBE_TIMEOUT)
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
