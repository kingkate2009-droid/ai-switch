"""Notification engine tests.

Covers the pieces that are easy to get subtly wrong and expensive to debug in
production:

* quiet-hour windows (including the wrapping 23:00-08:00 case),
* per-channel payload shapes,
* the gating order (disabled / not subscribed / quiet / no channel) and the
  dedup window that stops a permanently broken key from spamming,
* real HTTP delivery against a local server, including the "HTTP 200 but the
  payload was rejected" replies Slack and Feishu send,
* budget thresholds and the once-per-day dedup,
* the health-check hooks (key_failing / vendor_all_keys_failed).

Nothing here touches the developer's real data: ``HOME`` is redirected before
``core.data`` is imported, and the health tests patch ``get_vendors`` /
``get_all_health_status`` rather than reading a real database.

Run standalone:  python3 tests/test_notify.py
Or with pytest:  pytest tests/test_notify.py
"""
import contextlib
import json
import os
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Must happen before core.data is imported: DATA_DIR is resolved at import time.
_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-notify-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ["AI_SWITCH_NO_SCHEDULER"] = "1"
os.environ["AI_SWITCH_OPEN_BROWSER"] = "0"
os.environ["AI_SWITCH_NO_BROWSER"] = "1"
# Keep the local test HTTP server out of any corporate proxy.
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import core.data as core_data            # noqa: E402
import core.health_checker as health      # noqa: E402
import core.notify as notify              # noqa: E402
import core.budget as budget              # noqa: E402

# ── seams ──────────────────────────────────────────────────────────────────

_ORIGINALS = {}
_STATE = {"data": {"settings": {}, "vendors": [], "backends": {}}}


def _patch(obj, name, value):
    _ORIGINALS.setdefault((obj, name), getattr(obj, name, None))
    setattr(obj, name, value)


def _install_seams():
    # get_settings() -> _load_data()["settings"]; _STATE is the mutable store.
    _patch(core_data, "_load_data", lambda: _STATE["data"])
    _patch(core_data, "_save_data", lambda data: _STATE.__setitem__("data", data))


def _reset(settings=None):
    _STATE["data"] = {"settings": dict(settings or {}), "vendors": [], "backends": {}}
    with notify._lock:
        notify._last_sent.clear()
        notify._history.clear()


_install_seams()


# ── a real HTTP endpoint to deliver to ─────────────────────────────────────

RECEIVED = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        RECEIVED.append({"path": self.path, "body": body, "headers": dict(self.headers)})

        if self.path.startswith("/http500"):
            code, payload = 500, {"error": "boom"}
        elif self.path.startswith("/slackreject"):
            code, payload = 200, {"ok": False, "error": "invalid_token"}
        elif self.path.startswith("/feishureject"):
            code, payload = 200, {"code": 9499, "msg": "sign match fail"}
        else:
            code, payload = 200, {"ok": True}

        blob = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):  # silence the test run
        pass


@contextlib.contextmanager
def _server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _drain():
    """Wait for queued deliveries so assertions are deterministic."""
    notify._queue.join()


# ── quiet hours ────────────────────────────────────────────────────────────

def test_quiet_hours_ranges():
    assert notify.parse_quiet_hours("23:00-08:00") == (datetime(2000, 1, 1, 23).time(),
                                                       datetime(2000, 1, 1, 8).time())
    assert notify.parse_quiet_hours("") is None
    assert notify.parse_quiet_hours("off") is None
    assert notify.parse_quiet_hours("not-a-window") is None
    assert notify.parse_quiet_hours("25:99-08:00") is None


def test_quiet_hours_wrap_midnight():
    s = {"notify_quiet_hours": "23:00-08:00"}
    assert notify.in_quiet_hours(s, now=datetime(2026, 10, 27, 23, 30)) is True
    assert notify.in_quiet_hours(s, now=datetime(2026, 10, 27, 2, 0)) is True
    assert notify.in_quiet_hours(s, now=datetime(2026, 10, 27, 8, 0)) is False
    assert notify.in_quiet_hours(s, now=datetime(2026, 10, 27, 12, 0)) is False


def test_quiet_hours_plain_window_and_off():
    s = {"notify_quiet_hours": "01:00-06:00"}
    assert notify.in_quiet_hours(s, now=datetime(2026, 10, 27, 3, 0)) is True
    assert notify.in_quiet_hours(s, now=datetime(2026, 10, 27, 7, 0)) is False
    assert notify.in_quiet_hours({}, now=datetime(2026, 10, 27, 3, 0)) is False
    # identical start/end is an empty window, not "always quiet"
    assert notify.in_quiet_hours({"notify_quiet_hours": "05:00-05:00"},
                                 now=datetime(2026, 10, 27, 5, 0)) is False


# ── payload shapes ─────────────────────────────────────────────────────────

def test_payload_shapes():
    slack = notify.build_payload("slack", "sync_failed", "warning", "T", "M", {"a": 1})
    assert slack["text"].startswith("T")
    assert slack["attachments"][0]["color"] == notify._LEVEL_COLOR["warning"]
    assert slack["attachments"][0]["fields"][0] == {"title": "a", "value": "1", "short": True}

    feishu = notify.build_payload("feishu", "sync_failed", "critical", "T", "M")
    assert feishu["msg_type"] == "text"
    assert "T" in feishu["content"]["text"] and "M" in feishu["content"]["text"]

    hook = notify.build_payload("webhook", "sync_failed", "critical", "T", "M", {"a": 1})
    assert hook["source"] == "ai-switch"
    assert hook["event"] == "sync_failed"
    assert hook["level"] == "critical"
    assert hook["detail"] == {"a": 1}
    assert hook["timestamp"]


def test_payload_skips_empty_detail_values():
    hook = notify.build_payload("webhook", "e", "info", "T", "M", {"a": "", "b": None, "c": 2})
    assert hook["detail"] == {"a": "", "b": None, "c": 2}  # raw detail preserved
    text = notify.build_payload("slack", "e", "info", "T", "M", {"a": "", "b": None, "c": 2})["text"]
    assert "\na:" not in text and "\nb:" not in text and "c: 2" in text


# ── gating ─────────────────────────────────────────────────────────────────

def test_notify_is_off_by_default():
    _reset({})
    assert notify.notify("sync_failed", "T", "M")["reason"] == "disabled"


def test_each_gate_in_order():
    _reset({"notify_enabled": True})                       # enabled, no channel
    assert notify.notify("sync_failed", "T", "M")["reason"] == "no channel configured"

    _reset({"notify_enabled": False, "notify_channel_webhook": "http://x"})
    assert notify.notify("sync_failed", "T", "M")["reason"] == "disabled"

    _reset({"notify_enabled": True, "notify_channel_webhook": "http://x",
            "notify_events": ["budget_exceeded"]})
    assert notify.notify("sync_failed", "T", "M")["reason"] == "event not subscribed"

    _reset({"notify_enabled": True, "notify_channel_webhook": "http://x",
            "notify_quiet_hours": "00:00-23:59"})
    assert notify.notify("sync_failed", "T", "M")["reason"] == "quiet hours"
    # ...but a critical event ignores quiet hours (it will then fail to deliver
    # against the bogus URL; we only care that it was not suppressed here).
    assert notify.notify("sync_failed", "T", "M", level="critical")["sent"] is True


def test_unsubscribed_event_list_defaults_to_all():
    assert notify.subscribed_events({}) == list(notify.EVENT_CATALOG)
    assert notify.subscribed_events({"notify_events": "nonsense"}) == list(notify.EVENT_CATALOG)
    assert notify.subscribed_events({"notify_events": ["key_failing", "bogus"]}) == ["key_failing"]


def test_min_interval_zero_means_no_limit():
    """0 must disable the rate limit rather than fall back to the default.

    Regression: ``s.get(k) or DEFAULT`` turned an explicit 0 into 60 minutes.
    """
    assert notify.min_interval_seconds({}) == 60 * 60
    assert notify.min_interval_seconds({"notify_min_interval_minutes": 0}) == 0
    assert notify.min_interval_seconds({"notify_min_interval_minutes": ""}) == 60 * 60
    assert notify.min_interval_seconds({"notify_min_interval_minutes": None}) == 60 * 60
    assert notify.min_interval_seconds({"notify_min_interval_minutes": 15}) == 15 * 60
    assert notify.min_interval_seconds({"notify_min_interval_minutes": "junk"}) == 60 * 60
    assert notify.min_interval_seconds({"notify_min_interval_minutes": -5}) == 0

    # ...and the same key really is delivered twice with the limit off.
    _reset({"notify_enabled": True, "notify_channel_webhook": "http://127.0.0.1:1/x",
            "notify_min_interval_minutes": 0})
    assert notify.notify("sync_failed", "T", "M", dedup_key="same")["sent"] is True
    assert notify.notify("sync_failed", "T", "M", dedup_key="same")["sent"] is True


def test_dedup_window_suppresses_repeats():
    _reset({"notify_enabled": True, "notify_channel_webhook": "http://127.0.0.1:1/x",
            "notify_min_interval_minutes": 60})
    first = notify.notify("sync_failed", "T", "M", dedup_key="k1")
    second = notify.notify("sync_failed", "T", "M", dedup_key="k1")
    other = notify.notify("sync_failed", "T", "M", dedup_key="k2")
    assert first["sent"] is True
    assert second["reason"] == "rate limited"
    assert other["sent"] is True

    # a per-call window overrides the configured one
    _reset({"notify_enabled": True, "notify_channel_webhook": "http://127.0.0.1:1/x",
            "notify_min_interval_minutes": 0})
    assert notify.notify("sync_failed", "T", "M", dedup_key="k1")["sent"] is True
    assert notify.notify("sync_failed", "T", "M", dedup_key="k1",
                         dedup_window_seconds=900)["reason"] == "rate limited"


# ── real delivery ──────────────────────────────────────────────────────────

def test_webhook_delivery_roundtrip():
    with _server() as base:
        RECEIVED.clear()
        _reset({"notify_enabled": True, "notify_channel_webhook": base + "/hook",
                "notify_min_interval_minutes": 0})
        res = notify.notify("budget_exceeded", "Over budget", "Daily cost is $5.10 / $5.00",
                            level="critical", detail={"budget": "Daily cost"})
        assert res["sent"] is True and res["channels"] == ["webhook"]
        _drain()
        assert len(RECEIVED) == 1, RECEIVED
        body = RECEIVED[0]["body"]
        assert body["event"] == "budget_exceeded"
        assert body["source"] == "ai-switch"
        assert body["title"] == "Over budget"
        assert body["detail"] == {"budget": "Daily cost"}
        entry = notify.history(5)[0]
        assert entry["ok"] is True and entry["channel"] == "webhook"


def test_all_channels_receive_their_own_shape():
    with _server() as base:
        RECEIVED.clear()
        _reset({"notify_enabled": True, "notify_min_interval_minutes": 0,
                "notify_channel_webhook": base + "/hook",
                "notify_channel_slack": base + "/slack",
                "notify_channel_feishu": base + "/feishu"})
        assert notify.notify("key_failing", "T", "M")["sent"] is True
        _drain()
        by_path = {r["path"]: r["body"] for r in RECEIVED}
        assert set(by_path) == {"/hook", "/slack", "/feishu"}
        assert by_path["/hook"]["source"] == "ai-switch"
        assert by_path["/slack"]["attachments"]
        assert by_path["/feishu"]["msg_type"] == "text"


def test_rejected_payload_is_recorded_as_failure():
    """Slack/Feishu answer 200 even when they refuse the message."""
    with _server() as base:
        RECEIVED.clear()
        _reset({"notify_enabled": True, "notify_min_interval_minutes": 0,
                "notify_channel_slack": base + "/slackreject",
                "notify_channel_feishu": base + "/feishureject"})
        notify.notify("sync_failed", "T", "M")
        _drain()
        entries = {e["channel"]: e for e in notify.history(5)}
        assert entries["slack"]["ok"] is False and "invalid_token" in entries["slack"]["error"]
        assert entries["feishu"]["ok"] is False and "sign match fail" in entries["feishu"]["error"]


def test_http_error_and_unreachable_host_never_raise():
    with _server() as base:
        RECEIVED.clear()
        _reset({"notify_enabled": True, "notify_min_interval_minutes": 0,
                "notify_channel_webhook": base + "/http500"})
        notify.notify("sync_failed", "T", "M")
        _drain()
        entry = notify.history(1)[0]
        assert entry["ok"] is False and "500" in entry["error"]

        _reset({"notify_enabled": True, "notify_min_interval_minutes": 0,
                # port 1 is effectively unreachable
                "notify_channel_webhook": "http://127.0.0.1:1/hook"})
        notify.notify("sync_failed", "T", "M")
        _drain()
        assert notify.history(1)[0]["ok"] is False


def test_send_test_reports_per_channel_outcome():
    with _server() as base:
        RECEIVED.clear()
        _reset({"notify_channel_webhook": base + "/ok",
                "notify_channel_slack": base + "/http500"})
        res = notify.send_test()
        assert res["ok"] is False  # one of the two failed
        outcomes = {r["channel"]: r["ok"] for r in res["results"]}
        assert outcomes == {"slack": False, "webhook": True}

    _reset({})
    assert notify.send_test()["error"] == "no channel configured"
    assert notify.send_test("nope")["ok"] is False


def test_status_reports_configuration():
    _reset({"notify_enabled": True, "notify_channel_slack": "http://x",
            "notify_events": ["sync_failed"], "notify_min_interval_minutes": 15,
            "notify_quiet_hours": "23:00-08:00"})
    st = notify.status()
    assert st["enabled"] is True
    assert st["channels"] == ["slack"]
    assert st["events"] == ["sync_failed"]
    assert st["min_interval_minutes"] == 15
    assert {e["id"] for e in st["event_catalog"]} == set(notify.EVENT_CATALOG)


# ── budget ─────────────────────────────────────────────────────────────────

def _usage(cost=0.0, tokens=0):
    return {"total_cost": cost, "total_tokens": tokens}


def test_budget_thresholds():
    limits = {"daily_cost": 10.0, "monthly_cost": 100.0, "daily_tokens": 0, "monthly_tokens": 0}
    assert budget.evaluate_alerts(_usage(0), _usage(0), limits) == []
    warn = budget.evaluate_alerts(_usage(8.0), _usage(0), limits)
    assert len(warn) == 1 and warn[0]["level"] == "warn" and warn[0]["type"] == "daily_cost"
    crit = budget.evaluate_alerts(_usage(12.0), _usage(0), limits)
    assert len(crit) == 1 and crit[0]["level"] == "crit"
    # zero limit means "off"
    assert budget.evaluate_alerts(_usage(999), _usage(999),
                                  dict(limits, daily_cost=0, monthly_cost=0)) == []


def test_budget_token_limits_and_ordering():
    limits = {"daily_cost": 0, "monthly_cost": 0, "daily_tokens": 1000, "monthly_tokens": 5000}
    alerts = budget.evaluate_alerts(_usage(0, 900), _usage(0, 6000), limits)
    assert [a["type"] for a in alerts] == ["daily_tokens", "monthly_tokens"]
    assert alerts[0]["level"] == "warn" and alerts[1]["level"] == "crit"


def test_budget_limits_reads_settings():
    _reset({"budget_daily_cost": 3.5, "budget_monthly_tokens": 12345,
            "budget_action": "read_only"})
    lim = budget.limits()
    assert lim["daily_cost"] == 3.5
    assert lim["monthly_tokens"] == 12345
    assert lim["action"] == "read_only"
    _reset({"budget_daily_cost": "not-a-number"})
    assert budget.limits()["daily_cost"] == 0.0


def test_budget_alert_fires_once_per_day():
    _reset({"notify_enabled": True, "notify_channel_webhook": "http://127.0.0.1:1/x",
            "notify_min_interval_minutes": 0})
    alerts = [{"type": "daily_cost", "limit": 5.0, "used": 5.5, "level": "crit"}]
    day1 = datetime(2026, 10, 27, 10, 0)
    assert budget.notify_alerts(alerts, now=day1)[0]["sent"] is True
    assert budget.notify_alerts(alerts, now=day1)[0]["reason"] == "rate limited"
    # next day it is allowed again
    assert budget.notify_alerts(alerts, now=datetime(2026, 10, 28, 10, 0))[0]["sent"] is True


def test_budget_notify_message_mentions_amounts():
    _reset({"notify_enabled": True, "notify_channel_webhook": "http://127.0.0.1:1/x"})
    sent = []

    def _capture(event, title, message="", **kw):
        sent.append({"event": event, "title": title, "message": message, **kw})
        return {"sent": True}

    original = notify.notify
    notify.notify = _capture
    try:
        budget.notify_alerts([{"type": "daily_cost", "limit": 5.0, "used": 5.5, "level": "crit"}])
        budget.notify_alerts([{"type": "daily_tokens", "limit": 1000, "used": 850, "level": "warn"}])
    finally:
        notify.notify = original
    assert sent[0]["event"] == "budget_exceeded"
    assert "$5.5000 / $5.00" in sent[0]["message"]
    assert sent[0]["level"] == "critical"
    assert sent[1]["level"] == "warning"
    assert "850 / 1,000 tokens" in sent[1]["message"]


# ── health hooks ───────────────────────────────────────────────────────────

def _vendor(vid, name, keys):
    rows = []
    for i, k in enumerate(keys):
        row = dict(k)
        row.setdefault("id", f"k{i + 1}")
        rows.append(row)
    return {"id": vid, "name": name, "provider": "openai", "keys": rows}


def _with_health(vendors, status):
    _patch(health, "get_vendors", lambda: vendors)
    _patch(health, "get_all_health_status", lambda **kw: status)


def test_key_failing_fires_at_threshold():
    sent = []
    _patch(notify, "notify", lambda ev, t, m="", **kw: (sent.append({"event": ev, "title": t, **kw}),
                                                       {"sent": True})[1])
    try:
        vendors = [_vendor("v1", "Vendor One",
                           [{"name": "main", "api_key": "sk", "enabled": True},
                            {"name": "backup", "api_key": "sk2", "enabled": True}])]
        _with_health(vendors, {})
        # below threshold -> silent
        health.notify_health_failures([
            {"vendor_id": "v1", "key_id": "k1", "healthy": False, "fail_streak": 2, "error": "e"}])
        assert sent == []
        # at threshold -> exactly one key_failing
        health.notify_health_failures([
            {"vendor_id": "v1", "key_id": "k1", "healthy": False, "fail_streak": 3, "error": "401"}])
        assert len(sent) == 1 and sent[0]["event"] == "key_failing"
        assert "Vendor One / main" in sent[0]["title"]
        assert sent[0]["dedup_key"] == "key_fail:v1:k1"
        # healthy or skipped rows are ignored
        health.notify_health_failures([
            {"vendor_id": "v1", "key_id": "k1", "healthy": True, "fail_streak": 0},
            {"vendor_id": "v1", "key_id": "k2", "healthy": False, "fail_streak": 9, "skipped": True}])
        assert len(sent) == 1
    finally:
        notify.notify = _ORIGINALS.get((notify, "notify"), notify.notify)


def test_vendor_all_keys_failed_requires_every_key_down():
    sent = []
    _patch(notify, "notify", lambda ev, t, m="", **kw: (sent.append({"event": ev, "title": t, **kw}),
                                                       {"sent": True})[1])
    try:
        vendors = [_vendor("v1", "Vendor One",
                           [{"name": "main", "api_key": "sk", "enabled": True},
                            {"name": "backup", "api_key": "sk2", "enabled": True}]),
                   _vendor("v2", "Single Key", [{"name": "only", "api_key": "sk", "enabled": True}])]
        # one key still healthy -> no vendor-level alert
        _with_health(vendors, {"v1:k1": {"healthy": False}, "v1:k2": {"healthy": True},
                               "v2:k1": {"healthy": False}})
        health.notify_health_failures([])
        assert [s["event"] for s in sent] == []

        # all keys of v1 down -> alert; v2 has only one key so key_failing covers it
        sent.clear()
        _with_health(vendors, {"v1:k1": {"healthy": False}, "v1:k2": {"healthy": False},
                               "v2:k1": {"healthy": False}})
        health.notify_health_failures([])
        assert [s["event"] for s in sent] == ["vendor_all_keys_failed"]
        assert sent[0]["level"] == "critical"
        assert sent[0]["dedup_key"] == "vendor_all_failed:v1"

        # unknown health (never checked) must not be treated as failed
        sent.clear()
        _with_health(vendors, {"v1:k1": {"healthy": False}})
        health.notify_health_failures([])
        assert sent == []
    finally:
        notify.notify = _ORIGINALS.get((notify, "notify"), notify.notify)


def test_archived_and_disabled_keys_do_not_alert():
    """Archived keys are excluded from checks; disabled ones are already handled.

    Probing them still happens (with include_disabled=True), and both report
    healthy=False, so without the in-rotation guard the user would be nagged
    forever about keys they already retired.
    """
    sent = []
    _patch(notify, "notify", lambda ev, t, m="", **kw: (sent.append({"event": ev, "title": t, **kw}),
                                                       {"sent": True})[1])
    try:
        vendors = [_vendor("v1", "Vendor One",
                           [{"name": "live", "api_key": "sk", "enabled": True},
                            {"name": "retired", "api_key": "sk", "enabled": True, "archived": True},
                            {"name": "off", "api_key": "sk", "enabled": False},
                            {"name": "keyless", "api_key": "", "enabled": True}])]
        _with_health(vendors, {})
        health.notify_health_failures([
            {"vendor_id": "v1", "key_id": "k1", "healthy": False, "fail_streak": 5, "error": "401"},
            {"vendor_id": "v1", "key_id": "k2", "healthy": False, "fail_streak": 5,
             "archived": True, "error": "Archived — excluded from health checks"},
            {"vendor_id": "v1", "key_id": "k3", "healthy": False, "fail_streak": 5, "error": "401"},
            {"vendor_id": "v1", "key_id": "k4", "healthy": False, "fail_streak": 5, "error": "401"},
        ])
        assert [s["title"] for s in sent] == ["Key failing: Vendor One / live"]
    finally:
        notify.notify = _ORIGINALS.get((notify, "notify"), notify.notify)


def test_notify_health_failures_never_raises():
    def _boom(*a, **kw):
        raise RuntimeError("no vendors table")

    _patch(health, "get_vendors", _boom)
    try:
        assert health.notify_health_failures([{"vendor_id": "v", "key_id": "k",
                                               "healthy": False, "fail_streak": 99}]) == []
    finally:
        health.get_vendors = _ORIGINALS[(health, "get_vendors")]


# ── backend sync hook ──────────────────────────────────────────────────────

@contextlib.contextmanager
def _only_adapter(adapter):
    import backends as backends_pkg

    previous = backends_pkg._adapters
    backends_pkg._adapters = {adapter.name: adapter}
    try:
        yield backends_pkg
    finally:
        backends_pkg._adapters = previous


def test_sync_failure_notification():
    """A backend that fails to write must raise exactly one sync_failed alert."""
    import backends as backends_pkg
    from backends.base import BackendAdapter

    class _Boom(BackendAdapter):
        name = "boom"
        display_name = "Boom"

        def is_installed(self):
            return True

        def reconcile(self):
            raise RuntimeError("permission denied: /etc/boom.conf")

    class _Fine(BackendAdapter):
        name = "fine"
        display_name = "Fine"

        def is_installed(self):
            return True

        def reconcile(self):
            return None

    sent = []
    _patch(notify, "notify", lambda ev, t, m="", **kw: (sent.append({"event": ev, "title": t,
                                                                     "message": m, **kw}),
                                                       {"sent": True})[1])
    _patch(core_data, "get_vendors", lambda: [])
    _patch(core_data, "get_backend_config", lambda name: {})
    saved_original = core_data._save_data
    _patch(core_data, "_save_data", lambda data: None)
    try:
        with _only_adapter(_Boom()):
            results = backends_pkg.reconcile_all()
        assert results["boom"]["ok"] is False
        assert [s["event"] for s in sent] == ["sync_failed"]
        assert "permission denied" in sent[0]["message"]
        assert sent[0]["detail"]["failed_backends"] == "boom"
        assert sent[0]["dedup_key"] == "sync_failed"

        # A clean reconcile sends nothing.
        sent.clear()
        with _only_adapter(_Fine()):
            backends_pkg.reconcile_all()
        assert sent == []

        # Skipped backends (not installed / disabled) are not failures.
        sent.clear()
        with _only_adapter(_Boom()) as pkg:
            pkg._adapters["boom"].is_installed = lambda: False
            pkg.reconcile_all()
        assert sent == []
    finally:
        core_data._save_data = saved_original
        notify.notify = _ORIGINALS.get((notify, "notify"), notify.notify)
        core_data.get_vendors = _ORIGINALS.get((core_data, "get_vendors"), core_data.get_vendors)
        core_data.get_backend_config = _ORIGINALS.get((core_data, "get_backend_config"),
                                                      core_data.get_backend_config)


# ── settings API round-trip ────────────────────────────────────────────────

def _app_client():
    from app import app
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_api_roundtrip_and_normalization():
    c = _app_client()
    body = {
        "notify_enabled": True,
        "notify_channel_slack": "  https://hooks.slack.com/services/x  ",
        "notify_quiet_hours": "23:00-08:00",
        "notify_min_interval_minutes": 15,
        "notify_events": ["sync_failed", "key_failing", "not-a-real-event"],
    }
    res = c.post("/api/settings", json=body)
    assert res.status_code == 200, res.status_code
    got = res.get_json()
    assert got["notify_enabled"] is True
    assert got["notify_channel_slack"] == "https://hooks.slack.com/services/x"
    assert got["notify_min_interval_minutes"] == 15
    assert got["notify_events"] == ["sync_failed", "key_failing"]

    # garbage is normalized, not stored
    res2 = c.post("/api/settings", json={"notify_quiet_hours": "banana",
                                         "notify_min_interval_minutes": 999999,
                                         "notify_events": "sync_failed,key_failing"})
    got2 = res2.get_json()
    assert got2["notify_quiet_hours"] == ""
    assert got2["notify_min_interval_minutes"] == 10080
    assert got2["notify_events"] == ["sync_failed", "key_failing"]

    # a comma-separated string is accepted too
    assert c.get("/api/settings").get_json()["notify_events"] == ["sync_failed", "key_failing"]


def test_notify_endpoints_smoke():
    c = _app_client()
    c.post("/api/settings", json={"notify_enabled": True, "notify_events": []})
    st = c.get("/api/notify/status")
    assert st.status_code == 200
    payload = st.get_json()
    assert "event_catalog" in payload and payload["events"] == []
    assert isinstance(payload["history"], list)

    no_channel = c.post("/api/notify/test", json={})
    assert no_channel.status_code == 200
    assert no_channel.get_json()["ok"] is False

    bad = c.post("/api/notify/test", json={"channel": "telegram"})
    assert bad.status_code == 400


def test_notify_test_works_in_read_only_mode():
    """"Test my webhook" mutates nothing here, so read-only must not block it."""
    c = _app_client()
    c.post("/api/settings", json={"read_only": True})
    try:
        res = c.post("/api/notify/test", json={})
        assert res.status_code == 200, res.status_code
        assert res.get_json()["ok"] is False  # no channel configured, but not a 403
    finally:
        c.post("/api/settings", json={"read_only": False})


def test_notification_settings_card_is_rendered():
    html = _app_client().get("/").get_data(as_text=True)
    for marker in ('id="setNotifyEnabled"', 'id="setNotifyWebhook"', 'id="notifyEventsList"',
                   'id="notifyHistoryList"', "sendNotifyTest()"):
        assert marker in html, f"settings page is missing {marker!r}"


TESTS = (
    test_quiet_hours_ranges,
    test_quiet_hours_wrap_midnight,
    test_quiet_hours_plain_window_and_off,
    test_payload_shapes,
    test_payload_skips_empty_detail_values,
    test_notify_is_off_by_default,
    test_each_gate_in_order,
    test_unsubscribed_event_list_defaults_to_all,
    test_min_interval_zero_means_no_limit,
    test_dedup_window_suppresses_repeats,
    test_webhook_delivery_roundtrip,
    test_all_channels_receive_their_own_shape,
    test_rejected_payload_is_recorded_as_failure,
    test_http_error_and_unreachable_host_never_raise,
    test_send_test_reports_per_channel_outcome,
    test_status_reports_configuration,
    test_budget_thresholds,
    test_budget_token_limits_and_ordering,
    test_budget_limits_reads_settings,
    test_budget_alert_fires_once_per_day,
    test_budget_notify_message_mentions_amounts,
    test_key_failing_fires_at_threshold,
    test_vendor_all_keys_failed_requires_every_key_down,
    test_archived_and_disabled_keys_do_not_alert,
    test_notify_health_failures_never_raises,
    test_sync_failure_notification,
    test_settings_api_roundtrip_and_normalization,
    test_notify_endpoints_smoke,
    test_notify_test_works_in_read_only_mode,
    test_notification_settings_card_is_rendered,
)

if __name__ == "__main__":
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}\n      {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)
