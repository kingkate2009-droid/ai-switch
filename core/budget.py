"""Budget limits, alert evaluation and budget notifications.

The dashboard endpoint and the background scheduler both need to know "are we
over budget, and by how much", so the evaluation lives here instead of inside
the dashboard handler. ``evaluate_alerts`` is a pure function over already
aggregated usage, which keeps it trivially testable.

Settings it reads:

``budget_daily_cost`` / ``budget_monthly_cost``      USD, 0 = off
``budget_daily_tokens`` / ``budget_monthly_tokens``  int, 0 = off
``budget_action``                                    "" | "alert" | "read_only" | "disable_keys"
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

# Alerts fire at 80% (warn) and 100% (crit); a critical alert wins for a period.
WARN_RATIO = 0.8
_DAY_SECONDS = 24 * 3600


def limits(settings: Optional[dict] = None) -> dict:
    from core.data import get_settings

    s = settings if settings is not None else (get_settings() or {})

    def _f(key):
        try:
            return float(s.get(key) or 0)
        except Exception:
            return 0.0

    def _i(key):
        try:
            return int(float(s.get(key) or 0))
        except Exception:
            return 0

    return {
        "daily_cost": _f("budget_daily_cost"),
        "monthly_cost": _f("budget_monthly_cost"),
        "daily_tokens": _i("budget_daily_tokens"),
        "monthly_tokens": _i("budget_monthly_tokens"),
        "action": str(s.get("budget_action") or "alert").strip() or "alert",
    }


def evaluate_alerts(today_s: dict, month_s: dict, budgets: dict) -> list[dict]:
    """Build the alert list for the dashboard / notifier.

    Output shape is part of the dashboard API contract:
    ``{"type", "limit", "used", "level"}`` with level ``warn`` or ``crit``.
    """
    today_s = today_s or {}
    month_s = month_s or {}
    alerts: list[dict] = []

    def _pair(kind: str, limit, used, suffix: str = ""):
        if not limit or limit <= 0:
            return
        if used >= limit:
            alerts.append({"type": kind, "limit": limit, "used": used, "level": "crit"})
        elif used >= limit * WARN_RATIO:
            alerts.append({"type": kind, "limit": limit, "used": used, "level": "warn"})

    _pair("daily_cost", budgets.get("daily_cost"), float(today_s.get("total_cost") or 0))
    _pair("monthly_cost", budgets.get("monthly_cost"), float(month_s.get("total_cost") or 0))
    _pair("daily_tokens", budgets.get("daily_tokens"), int(today_s.get("total_tokens") or 0))
    _pair("monthly_tokens", budgets.get("monthly_tokens"), int(month_s.get("total_tokens") or 0))
    return alerts


def _agg(from_ts: str, to_ts: str) -> dict:
    from core.data import get_usage_records

    recs = get_usage_records(from_ts=from_ts, to_ts=to_ts, auto_import=False)
    cost = sum(float(r.get("cost") or 0) for r in recs)
    tokens = sum(int(r.get("total_tokens") or 0) for r in recs)
    ok = sum(1 for r in recs if r.get("success", True))
    return {
        "count": len(recs),
        "success_count": ok,
        "fail_count": len(recs) - ok,
        "total_tokens": tokens,
        "total_cost": round(cost, 6),
        "cost_estimated": any(r.get("_cost_estimated") for r in recs),
    }


def snapshot(now: Optional[datetime] = None) -> dict:
    """Aggregate usage for today/month and evaluate limits."""
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    next_month = (f"{now.year + 1}-01-01" if now.month == 12
                  else f"{now.year}-{now.month + 1:02d}-01")
    today_s = _agg(today + "T00:00:00", today + "T23:59:59")
    month_s = _agg(month_start + "T00:00:00", tomorrow + "T23:59:59")
    budgets = limits()
    return {
        "budgets": budgets,
        "alerts": evaluate_alerts(today_s, month_s, budgets),
        "today": today_s,
        "month": month_s,
    }


def _fmt_used(kind: str, used, limit) -> str:
    if kind.endswith("cost"):
        return f"${float(used):.4f} / ${float(limit):.2f}"
    return f"{int(used):,} / {int(limit):,} tokens"


def notify_alerts(alerts: list[dict], *, now: Optional[datetime] = None) -> list[dict]:
    """Push budget alerts out. At most one notification per type+level per day."""
    from core.notify import EVENT_BUDGET_EXCEEDED, notify

    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    outcomes = []
    for a in alerts or []:
        kind = str(a.get("type") or "")
        level = str(a.get("level") or "warn")
        used = a.get("used")
        limit = a.get("limit")
        label = {"daily_cost": "Daily cost", "monthly_cost": "Monthly cost",
                 "daily_tokens": "Daily tokens", "monthly_tokens": "Monthly tokens"}.get(kind, kind)
        state = "exceeded" if level == "crit" else f"at {int(WARN_RATIO * 100)}%"
        outcomes.append(notify(
            EVENT_BUDGET_EXCEEDED,
            f"Budget {state}: {label}",
            f"{label} is {_fmt_used(kind, used, limit)}.",
            level="critical" if level == "crit" else "warning",
            detail={"budget": label, "usage": _fmt_used(kind, used, limit),
                    "configured_action": a.get("action") or ""},
            dedup_key=f"budget:{kind}:{level}:{day}",
            dedup_window_seconds=_DAY_SECONDS,
        ))
    return outcomes
