"""Optional background health-check scheduler with pause/status/history."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

_stop = threading.Event()
_pause = threading.Event()  # set = paused (skip runs, keep thread)
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_run_lock = threading.Lock()

_state: dict[str, Any] = {
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_summary": None,
    "last_run_id": None,
    "running_now": False,
    "next_run_at": None,
    "runs_count": 0,
    "thread_started_at": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch() -> float:
    return time.time()


def _set_next_run(seconds: float) -> None:
    if seconds is None or seconds < 0:
        _state["next_run_at"] = None
        return
    _state["next_run_at"] = datetime.fromtimestamp(
        _epoch() + float(seconds), tz=timezone.utc
    ).isoformat()


def run_health_check(*, source: str = "manual", include_disabled: bool = True) -> dict:
    """Run a full health check once, record history. Thread-safe (skips if busy)."""
    if not _run_lock.acquire(blocking=False):
        return {
            "ok": False,
            "busy": True,
            "error": "health check already running",
            "summary": _state.get("last_summary"),
        }
    try:
        _state["running_now"] = True
        _state["last_started_at"] = _now_iso()
        _state["last_error"] = None
        started = _epoch()
        from core.health_checker import check_all_keys
        from core.health_history import append_run, summarize_results

        results = check_all_keys(include_disabled=include_disabled)
        summary = summarize_results(results)
        elapsed_ms = int((_epoch() - started) * 1000)
        # compact per-key for history (no secrets / large model lists)
        compact = []
        for r in results or []:
            compact.append({
                "vendor_id": r.get("vendor_id"),
                "key_id": r.get("key_id"),
                "healthy": r.get("healthy"),
                "latency_ms": r.get("latency_ms"),
                "error": (r.get("error") or "")[:200] if r.get("healthy") is False else None,
            })
        rec = append_run({
            "source": source,
            "started_at": _state["last_started_at"],
            "finished_at": _now_iso(),
            "duration_ms": elapsed_ms,
            "ok": summary["ok"],
            "fail": summary["fail"],
            "unknown": summary["unknown"],
            "total": summary["total"],
            "failures": summary["failures"],
            "results": compact,
        })
        _state["last_finished_at"] = rec.get("finished_at")
        _state["last_summary"] = {
            "ok": summary["ok"],
            "fail": summary["fail"],
            "unknown": summary["unknown"],
            "total": summary["total"],
            "duration_ms": elapsed_ms,
            "source": source,
        }
        _state["last_run_id"] = rec.get("id")
        _state["runs_count"] = int(_state.get("runs_count") or 0) + 1
        log.info(
            "Health check done source=%s ok=%s fail=%s (%sms)",
            source, summary["ok"], summary["fail"], elapsed_ms,
        )
        return {"ok": True, "busy": False, "run_id": rec.get("id"), "summary": _state["last_summary"], "results": results}
    except Exception as e:
        log.warning("Health check failed: %s", e)
        _state["last_error"] = str(e)[:300]
        _state["last_finished_at"] = _now_iso()
        try:
            from core.health_history import append_run
            rec = append_run({
                "source": source,
                "started_at": _state.get("last_started_at"),
                "finished_at": _state["last_finished_at"],
                "error": str(e)[:300],
                "ok": 0,
                "fail": 0,
                "total": 0,
            })
            _state["last_run_id"] = rec.get("id")
        except Exception:
            pass
        return {"ok": False, "busy": False, "error": str(e), "summary": _state.get("last_summary")}
    finally:
        _state["running_now"] = False
        _run_lock.release()


def _loop() -> None:
    while not _stop.is_set():
        try:
            from core.data import get_settings
            settings = get_settings() or {}
            enabled = bool(settings.get("health_check_enabled"))
            interval = int(settings.get("check_interval_seconds") or 300)
            if interval < 60:
                interval = 60

            if not enabled:
                _set_next_run(None)
                _stop.wait(5)
                continue

            if _pause.is_set():
                _set_next_run(None)
                _stop.wait(2)
                continue

            log.info("Scheduled health check starting (interval=%ss)", interval)
            run_health_check(source="scheduled", include_disabled=True)
            _set_next_run(interval)
            # wait interval, interruptible
            _stop.wait(interval)
        except Exception as e:
            log.warning("Scheduler loop error: %s", e)
            _state["last_error"] = str(e)[:300]
            _stop.wait(10)


def start_scheduler() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="ai-switch-health-scheduler", daemon=True)
        _thread.start()
        _state["thread_started_at"] = _now_iso()
        log.info("Health scheduler thread started (disabled until health_check_enabled=true)")


def stop_scheduler() -> None:
    _stop.set()


def restart_scheduler() -> None:
    """Ensure thread is running; loop re-reads settings."""
    stop_scheduler()
    time.sleep(0.05)
    start_scheduler()


def pause_scheduler() -> dict:
    """Temporarily pause scheduled runs (does not change settings)."""
    _pause.set()
    _set_next_run(None)
    return get_status()


def resume_scheduler() -> dict:
    """Resume after pause; does not force-enable settings."""
    _pause.clear()
    return get_status()


def is_paused() -> bool:
    return _pause.is_set()


def is_thread_alive() -> bool:
    return bool(_thread and _thread.is_alive())


def enable_monitoring(*, interval: Optional[int] = None) -> dict:
    """Turn on scheduled health checks in settings and ensure thread + not paused."""
    from core.data import update_settings, get_settings
    kwargs: dict[str, Any] = {"health_check_enabled": True}
    if interval is not None:
        iv = max(60, int(interval))
        kwargs["check_interval_seconds"] = iv
    update_settings(**kwargs)
    _pause.clear()
    if not is_thread_alive():
        start_scheduler()
    # set approximate next run
    s = get_settings() or {}
    iv = int(s.get("check_interval_seconds") or 300)
    if not _state.get("running_now"):
        _set_next_run(min(iv, 5))  # loop picks up within a few seconds when enabled
    return get_status()


def disable_monitoring() -> dict:
    """Turn off scheduled health checks; keep thread for quick re-enable."""
    from core.data import update_settings
    update_settings(health_check_enabled=False)
    _set_next_run(None)
    return get_status()


def get_status() -> dict:
    from core.data import get_settings
    settings = get_settings() or {}
    enabled = bool(settings.get("health_check_enabled"))
    interval = int(settings.get("check_interval_seconds") or 300)
    if interval < 60:
        interval = 60
    paused = _pause.is_set()
    alive = is_thread_alive()
    # derive human state
    if _state.get("running_now"):
        state = "running"
    elif not enabled:
        state = "stopped"
    elif paused:
        state = "paused"
    elif alive:
        state = "watching"
    else:
        state = "idle"

    return {
        "state": state,
        "enabled": enabled,
        "paused": paused,
        "thread_alive": alive,
        "running_now": bool(_state.get("running_now")),
        "interval_seconds": interval,
        "last_started_at": _state.get("last_started_at"),
        "last_finished_at": _state.get("last_finished_at"),
        "last_error": _state.get("last_error"),
        "last_summary": _state.get("last_summary"),
        "last_run_id": _state.get("last_run_id"),
        "next_run_at": _state.get("next_run_at") if enabled and not paused else None,
        "runs_count_session": int(_state.get("runs_count") or 0),
        "thread_started_at": _state.get("thread_started_at"),
        "health_auto_disable": bool(settings.get("health_auto_disable")),
        "health_auto_failover": bool(settings.get("health_auto_failover")),
    }
