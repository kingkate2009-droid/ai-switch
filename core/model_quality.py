"""Small deterministic model-quality benchmark and vendor rankings."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from core.data import get_vendor, get_vendors, list_model_ids, update_model_quality_score
from core.endpoints import effective_model_endpoints, model_is_verified_usable
from core.providers import (
    _anthropic_messages_url,
    _new_session,
    _requires_agentic_request,
    _responses_url,
    get_probe_timeout,
    official_vendor_info,
)


BENCHMARK_VERSION = 1
BENCHMARKS = (
    ("exact", "Return exactly AISWITCH-7F2A and nothing else.", "AISWITCH-7F2A"),
    ("math", "Compute 37 * 29. Return only the integer.", "1073"),
    (
        "logic",
        "All norks are zibs. No zibs are tars. Can a nork be a tar? Reply only YES or NO.",
        "NO",
    ),
    (
        "json",
        'Return exactly this JSON object with no markdown: {"a":3,"b":[2,4,6]}',
        '{"a":3,"b":[2,4,6]}',
    ),
)


def _extract_text(data: dict, endpoint: str) -> str:
    if endpoint == "openai_chat":
        value = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(value, list):
            return "".join(str(x.get("text") or "") for x in value if isinstance(x, dict))
        return str(value or "")
    if endpoint == "anthropic_messages":
        return "".join(
            str(block.get("text") or "")
            for block in (data.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
    # Responses API non-streaming shape.
    if data.get("output_text"):
        return str(data.get("output_text"))
    parts = []
    for item in data.get("output") or []:
        for block in item.get("content") or [] if isinstance(item, dict) else []:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
    return "".join(parts)


def _request_text(endpoint: str, api_url: str, api_key: str, model: str, prompt: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if endpoint == "anthropic_messages":
        url = _anthropic_messages_url(api_url)
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        payload = {
            "model": model,
            "max_tokens": 80,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif endpoint == "openai_responses":
        url = _responses_url(api_url)
        headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": model, "input": prompt, "max_output_tokens": 80, "stream": False}
    else:
        base = api_url.rstrip("/")
        url = base + "/chat/completions" if base.endswith(("/v1", "/v2", "/v3", "/v4")) else base + "/v1/chat/completions"
        headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 80,
            "messages": [{"role": "user", "content": prompt}],
        }

    started = time.time()
    response = _new_session().post(url, headers=headers, json=payload, timeout=max(20, get_probe_timeout()))
    latency_ms = int((time.time() - started) * 1000)
    if response.status_code != 200 and _requires_agentic_request(response.text):
        if endpoint == "anthropic_messages":
            payload["tools"] = [{
                "name": "get_status",
                "description": "Get current status",
                "input_schema": {"type": "object", "properties": {}},
            }]
        elif endpoint == "openai_chat":
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": "get_status",
                    "description": "Get current status",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
            payload["tool_choice"] = "auto"
        response = _new_session().post(url, headers=headers, json=payload, timeout=max(20, get_probe_timeout()))
        latency_ms = int((time.time() - started) * 1000)
    body = response.text[:500]
    if response.status_code != 200:
        raise RuntimeError(f"{endpoint} HTTP {response.status_code}: {body}")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"{endpoint} returned invalid JSON") from exc
    return {
        "text": _extract_text(data, endpoint).strip(),
        "response_model": str(data.get("model") or ""),
        "latency_ms": latency_ms,
    }


def _answer_correct(case_id: str, answer: str, expected: str) -> bool:
    value = str(answer or "").strip()
    if case_id == "json":
        try:
            return json.loads(value) == json.loads(expected)
        except Exception:
            return False
    if case_id in ("math", "logic", "exact"):
        return value.upper() == expected.upper()
    return False


def _model_identity_matches(requested: str, returned: str) -> bool:
    if not returned:
        return True  # Some compatible gateways omit the response model field.
    norm = lambda value: re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    left, right = norm(requested), norm(returned)
    # Suffixes such as ``-thinking`` are meaningful product variants. A relay
    # returning the base model is a downgrade signal, not a compatible alias.
    return bool(left and right and left == right)


def evaluate_model_quality(vendor_id: str, key_id: str, model_id: str) -> dict:
    vendor = get_vendor(vendor_id)
    key = next(
        (item for item in (vendor or {}).get("keys") or [] if str(item.get("id")) == str(key_id)),
        None,
    )
    model_id = str(model_id or "").strip()
    if not vendor or not key:
        return {"error": "Vendor or key not found", "model": model_id}
    if not model_is_verified_usable(key, model_id):
        return {"error": "Model must pass a current health and endpoint check first", "model": model_id}

    endpoints = effective_model_endpoints(vendor, key, model_id)
    endpoint = next((item for item in ("anthropic_messages", "openai_chat", "openai_responses") if item in endpoints), "")
    if not endpoint:
        return {"error": "No verified endpoint available for quality evaluation", "model": model_id}

    api_url = str(vendor.get("proxy_target") or vendor.get("api_url") or "")
    results = []
    latencies = []
    response_models = []
    for case_id, prompt, expected in BENCHMARKS:
        try:
            response = _request_text(endpoint, api_url, str(key.get("api_key") or ""), model_id, prompt)
            correct = _answer_correct(case_id, response["text"], expected)
            latencies.append(response["latency_ms"])
            if response["response_model"]:
                response_models.append(response["response_model"])
            results.append({
                "id": case_id,
                "correct": correct,
                "latency_ms": response["latency_ms"],
                "answer": response["text"][:160],
            })
        except Exception as exc:
            results.append({"id": case_id, "correct": False, "error": str(exc)[:240]})

    correct_count = sum(1 for item in results if item.get("correct"))
    accuracy_score = correct_count * 20
    avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0
    latency_score = 0 if not avg_latency else 10 if avg_latency <= 1500 else 7 if avg_latency <= 3000 else 4 if avg_latency <= 6000 else 1
    identity_ok = all(_model_identity_matches(model_id, item) for item in response_models)
    identity_score = 10 if identity_ok else 0
    score = accuracy_score + latency_score + identity_score
    record = {
        "score": score,
        "accuracy_score": accuracy_score,
        "latency_score": latency_score,
        "identity_score": identity_score,
        "correct": correct_count,
        "total": len(BENCHMARKS),
        "avg_latency_ms": avg_latency,
        "endpoint": endpoint,
        "response_models": sorted(set(response_models)),
        "identity_match": identity_ok,
        "benchmark_version": BENCHMARK_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    update_model_quality_score(vendor_id, key_id, model_id, record)
    return {"vendor_id": vendor_id, "key_id": key_id, "model": model_id, **record}


def evaluate_key_quality(vendor_id: str, key_id: str) -> dict:
    vendor = get_vendor(vendor_id)
    key = next(
        (item for item in (vendor or {}).get("keys") or [] if str(item.get("id")) == str(key_id)),
        None,
    )
    if not vendor or not key:
        return {"error": "Vendor or key not found", "results": []}
    models = [mid for mid in list_model_ids(key) if model_is_verified_usable(key, mid)]
    results = [evaluate_model_quality(vendor_id, key_id, mid) for mid in models]
    return {
        "vendor_id": vendor_id,
        "key_id": key_id,
        "ok": sum(1 for item in results if not item.get("error")),
        "fail": sum(1 for item in results if item.get("error")),
        "results": results,
    }


def list_quality_targets(*, include_official: bool = False) -> list[dict]:
    """One currently usable key per vendor+model, excluding official APIs by default."""
    targets = []
    for vendor in get_vendors():
        official = official_vendor_info(vendor)
        if official["official"] and not include_official:
            continue
        selected = {}
        for key in vendor.get("keys") or []:
            for model_id in list_model_ids(key):
                if model_id not in selected and model_is_verified_usable(key, model_id):
                    selected[model_id] = key
        for model_id, key in selected.items():
            targets.append({
                "vendor_id": str(vendor.get("id") or ""),
                "vendor_name": vendor.get("name") or vendor.get("provider") or "Vendor",
                "key_id": str(key.get("id") or ""),
                "key_name": key.get("name") or "",
                "model": model_id,
                **official,
            })
    targets.sort(key=lambda item: (item["vendor_name"].lower(), item["model"].lower()))
    return targets


def get_quality_catalog(*, include_official: bool = False) -> dict:
    """List every known model with health, syncability and quality state."""
    from core.health_checker import get_all_health_status
    rows = []
    health = get_all_health_status()
    for vendor in get_vendors():
        official = official_vendor_info(vendor)
        if official["official"] and not include_official:
            continue
        vendor_healthy = any(
            (health.get(f"{vendor.get('id')}:{key.get('id')}") or {}).get("healthy") is True
            for key in vendor.get("keys") or []
        )
        seen = set()
        for key in vendor.get("keys") or []:
            for model_id in list_model_ids(key):
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                record = (key.get("quality_scores") or {}).get(model_id)
                rows.append({
                    "vendor_id": str(vendor.get("id") or ""),
                    "vendor_name": vendor.get("name") or vendor.get("provider") or "Vendor",
                    "key_id": str(key.get("id") or ""),
                    "key_name": key.get("name") or "",
                    "model": model_id,
                    "official": official["official"],
                    "vendor_healthy": vendor_healthy,
                    "model_syncable": model_is_verified_usable(key, model_id),
                    "quality_checked": isinstance(record, dict) and bool(record.get("checked_at")),
                    "quality": record if isinstance(record, dict) else None,
                })
    rows.sort(key=lambda item: (item["vendor_name"].lower(), item["model"].lower()))
    return {"count": len(rows), "checked": sum(1 for row in rows if row["quality_checked"]), "rows": rows}
