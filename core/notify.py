"""Outbound notifications: webhook / Slack / Feishu.

AI Switch only tells you something is wrong when you open the page.  This module
pushes the events that actually matter out to wherever you already are.

Design notes
------------
* ``notify()`` never blocks and never raises. Deliveries run on a small daemon
  worker thread, so a slow or dead webhook cannot stall a health check.
* Every event has a ``dedup_key``. The same key is not re-sent within
  ``notify_min_interval_minutes`` (default 60), which is what keeps a
  permanently broken key from spamming you every health-check round.
* ``notify_quiet_hours`` (e.g. ``"23:00-08:00"``, wraps midnight) suppresses
  non-critical events. ``level="critical"`` always goes through.

Settings (all optional, under ``settings`` in the data store):

``notify_enabled``              bool, default False
``notify_channel_feishu``       Feishu bot webhook URL
``notify_channel_slack``        Slack incoming webhook URL
``notify_channel_webhook``      any JSON endpoint
``notify_events``               list of event names (default: all)
``notify_quiet_hours``          "HH:MM-HH:MM" or "" for off
``notify_min_interval_minutes`` int, default 60
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, time as dtime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Events ────────────────────────────────────────────────

EVENT_BUDGET_EXCEEDED = "budget_exceeded"
EVENT_KEY_FAILING = "key_failing"
EVENT_VENDOR_ALL_KEYS_FAILED = "vendor_all_keys_failed"
EVENT_SYNC_FAILED = "sync_failed"

EVENT_CATALOG = (
    EVENT_BUDGET_EXCEEDED,
    EVENT_KEY_FAILING,
    EVENT_VENDOR_ALL_KEYS_FAILED,
    EVENT_SYNC_FAILED,
)

EVENT_LABELS = {
    EVENT_BUDGET_EXCEEDED: "Budget exceeded",
    EVENT_KEY_FAILING: "Key keeps failing health checks",
    EVENT_VENDOR_ALL_KEYS_FAILED: "Vendor has no working key left",
    EVENT_SYNC_FAILED: "Backend sync failed",
}

CHANNEL_FEISHU = "feishu"
CHANNEL_SLACK = "slack"
CHANNEL_WEBHOOK = "webhook"
CHANNELS = (CHANNEL_FEISHU, CHANNEL_SLACK, CHANNEL_WEBHOOK)

DEFAULT_MIN_INTERVAL_MINUTES = 60
_MAX_HISTORY = 50
_POST_TIMEOUT = 8

# ── Result cache / rate limiting ──────────────────────────

_lock = threading.RLock()
_last_sent: dict[str, float] = {}
_history: list[dict] = []
_queue: "queue.Queue[dict]" = queue.Queue()
_worker: Optional[threading.Thread] = None


def _settings() -> dict:
    try:
        from core.data import get_settings

        return get_settings() or {}
    except Exception:
        return {}


def event_catalog() -> list[dict]:
    return [{"id": e, "label": EVENT_LABELS.get(e, e)} for e in EVENT_CATALOG]


def subscribed_events(settings: Optional[dict] = None) -> list[str]:
    s = settings if settings is not None else _settings()
    raw = s.get("notify_events")
    if not isinstance(raw, list):
        return list(EVENT_CATALOG)
    picked = [str(e) for e in raw if str(e) in EVENT_CATALOG]
    return picked


def configured_channels(settings: Optional[dict] = None) -> list[str]:
    s = settings if settings is not None else _settings()
    out = []
    for ch in CHANNELS:
        if str(s.get(f"notify_channel_{ch}") or "").strip():
            out.append(ch)
    return out


def is_enabled(settings: Optional[dict] = None) -> bool:
    s = settings if settings is not None else _settings()
    return bool(s.get("notify_enabled"))


def min_interval_seconds(settings: Optional[dict] = None) -> int:
    s = settings if settings is not None else _settings()
    raw = s.get("notify_min_interval_minutes")
    # NB: `raw or default` would turn an explicit 0 ("no limit") into the
    # default, so only None/"" fall back.
    if raw is None or raw == "":
        raw = DEFAULT_MIN_INTERVAL_MINUTES
    try:
        minutes = int(float(raw))
    except Exception:
        minutes = DEFAULT_MIN_INTERVAL_MINUTES
    return max(0, minutes) * 60


def parse_quiet_hours(value: str):
    """Return (start, end) as ``datetime.time`` or None when unset/invalid.

    Supports windows that wrap midnight, e.g. ``"23:00-08:00"``.
    """
    raw = str(value or "").strip()
    if not raw or raw.lower() in ("off", "none", "0"):
        return None
    if "-" not in raw:
        return None
    a, b = raw.split("-", 1)
    try:
        sh, sm = [int(x) for x in a.strip().split(":")]
        eh, em = [int(x) for x in b.strip().split(":")]
        return dtime(sh, sm), dtime(eh, em)
    except Exception:
        return None


def in_quiet_hours(settings: Optional[dict] = None, *, now: Optional[datetime] = None) -> bool:
    s = settings if settings is not None else _settings()
    window = parse_quiet_hours(str(s.get("notify_quiet_hours") or ""))
    if not window:
        return False
    start, end = window
    if start == end:
        return False
    local = (now or datetime.now()).time()
    if start < end:
        return start <= local < end
    # wraps midnight
    return local >= start or local < end


# ── Payload builders ──────────────────────────────────────

_LEVEL_COLOR = {"info": "#3b82f6", "warning": "#f59e0b", "critical": "#ef4444"}


def _flatten(title: str, message: str, detail: Optional[dict]) -> str:
    lines = [title]
    if message:
        lines.append(message)
    for k, v in (detail or {}).items():
        if v in (None, "", [], {}):
            continue
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


def build_payload(channel: str, event: str, level: str, title: str, message: str,
                  detail: Optional[dict] = None, ts: Optional[str] = None) -> dict:
    detail = detail or {}
    ts = ts or datetime.now(timezone.utc).isoformat()
    text = _flatten(title, message, detail)
    if channel == CHANNEL_SLACK:
        return {
            "text": text,
            "attachments": [{
                "color": _LEVEL_COLOR.get(level, "#6b7280"),
                "title": title,
                "text": message or "",
                "fields": [
                    {"title": str(k), "value": str(v), "short": True}
                    for k, v in detail.items() if v not in (None, "", [], {})
                ],
                "footer": "AI Switch",
                "ts": int(datetime.now(timezone.utc).timestamp()),
            }],
        }
    if channel == CHANNEL_FEISHU:
        # Plain text is the only payload every Feishu custom bot accepts.
        return {"msg_type": "text", "content": {"text": f"[AI Switch] {text}"}}
    return {
        "source": "ai-switch",
        "event": event,
        "level": level,
        "title": title,
        "message": message,
        "detail": detail,
        "timestamp": ts,
    }


# ── Delivery ──────────────────────────────────────────────

def _record(entry: dict) -> None:
    with _lock:
        _history.append(entry)
        if len(_history) > _MAX_HISTORY:
            del _history[: len(_history) - _MAX_HISTORY]


def _deliver(channel: str, url: str, payload: dict) -> dict:
    result = {"channel": channel, "ok": False, "error": ""}
    try:
        import requests

        resp = requests.post(url, json=payload, timeout=_POST_TIMEOUT)
        result["status"] = resp.status_code
        if resp.status_code >= 400:
            result["error"] = f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        else:
            # Slack/Feishu reply 200 with {"ok": false} on a rejected payload.
            try:
                body = resp.json()
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("ok") is False:
                result["error"] = f"rejected: {str(body.get('error') or body)[:200]}"
            elif isinstance(body, dict) and body.get("code") not in (None, 0):
                result["error"] = f"rejected: {str(body.get('msg') or body)[:200]}"
            else:
                result["ok"] = True
    except Exception as exc:  # network, DNS, TLS, timeout
        result["error"] = str(exc)[:200]
    return result


def _worker_loop() -> None:
    while True:
        job = _queue.get()
        try:
            entry = _deliver(job["channel"], job["url"], job["payload"])
            entry.update({
                "event": job["event"],
                "level": job["level"],
                "title": job["title"],
                "at": datetime.now(timezone.utc).isoformat(),
            })
            _record(entry)
            if not entry["ok"]:
                log.warning("Notify via %s failed: %s", job["channel"], entry["error"])
        except Exception as exc:
            log.warning("Notify worker error: %s", exc)
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="ai-switch-notify", daemon=True)
        _worker.start()


def notify(event: str, title: str, message: str = "", *, level: str = "warning",
           detail: Optional[dict] = None, dedup_key: str = "", force: bool = False,
           dedup_window_seconds: Optional[int] = None) -> dict:
    """Queue a notification. Returns a small dict describing what happened.

    ``dedup_window_seconds`` overrides the configured rate-limit window for this
    call (used by budget alerts, which should fire at most once per day).

    Never raises and never blocks on the network.
    """
    try:
        s = _settings()
        if not force:
            if not is_enabled(s):
                return {"sent": False, "reason": "disabled", "channels": []}
            if event not in subscribed_events(s):
                return {"sent": False, "reason": "event not subscribed", "channels": []}
            if level != "critical" and in_quiet_hours(s):
                return {"sent": False, "reason": "quiet hours", "channels": []}

        channels = configured_channels(s)
        if not channels:
            return {"sent": False, "reason": "no channel configured", "channels": []}

        key = dedup_key or event
        now_ts = datetime.now(timezone.utc).timestamp()
        window = min_interval_seconds(s) if dedup_window_seconds is None else max(0, int(dedup_window_seconds))
        if not force and window > 0:
            with _lock:
                last = _last_sent.get(key)
            if last is not None and (now_ts - last) < window:
                return {"sent": False, "reason": "rate limited", "channels": []}

        _ensure_worker()
        queued = []
        for ch in channels:
            url = str(s.get(f"notify_channel_{ch}") or "").strip()
            if not url:
                continue
            payload = build_payload(ch, event, level, title, message, detail)
            _queue.put({"channel": ch, "url": url, "payload": payload,
                        "event": event, "level": level, "title": title})
            queued.append(ch)

        with _lock:
            _last_sent[key] = now_ts
            if len(_last_sent) > 500:
                # Drop the oldest half; keys are cheap to re-create.
                for k in sorted(_last_sent, key=lambda x: _last_sent[x])[:250]:
                    _last_sent.pop(k, None)

        try:
            from core.audit import log_event

            log_event("notify.sent", event=event, level=level, channels=queued, dedup_key=key)
        except Exception:
            pass

        return {"sent": True, "channels": queued, "dedup_key": key}
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("notify(%s) failed: %s", event, exc)
        return {"sent": False, "reason": f"error: {exc}", "channels": []}


def send_test(channel: Optional[str] = None) -> dict:
    """Deliver a test message synchronously so the UI can report the outcome."""
    s = _settings()
    targets = [channel] if channel else configured_channels(s)
    if not targets:
        return {"ok": False, "error": "no channel configured", "results": []}
    results = []
    for ch in targets:
        url = str(s.get(f"notify_channel_{ch}") or "").strip()
        if not url:
            results.append({"channel": ch, "ok": False, "error": "no url configured"})
            continue
        payload = build_payload(
            ch, "test", "info", "AI Switch test notification",
            "If you can read this, notifications are wired up correctly.",
            {"sent_at": datetime.now(timezone.utc).isoformat()},
        )
        entry = _deliver(ch, url, payload)
        entry.update({"event": "test", "level": "info", "title": "Test notification",
                      "at": datetime.now(timezone.utc).isoformat()})
        _record(entry)
        results.append(entry)
    return {"ok": all(r.get("ok") for r in results), "results": results}


def history(limit: int = 20) -> list[dict]:
    with _lock:
        items = list(_history)
    items.reverse()
    return items[: max(1, int(limit or 20))]


def status() -> dict:
    s = _settings()
    return {
        "enabled": is_enabled(s),
        "channels": configured_channels(s),
        "events": subscribed_events(s),
        "event_catalog": event_catalog(),
        "quiet_hours": str(s.get("notify_quiet_hours") or ""),
        "in_quiet_hours": in_quiet_hours(s),
        "min_interval_minutes": min_interval_seconds(s) // 60,
    }
