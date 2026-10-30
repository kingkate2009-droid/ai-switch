"""Pytest fixtures/guards shared by the test suite.

Loaded by pytest *before* any test module is imported, which is the only point
at which redirecting ``HOME`` still isolates every data path: ``core.data``,
``core.audit``, ``core.health_checker`` and ``core.health_history`` all resolve
``~/.ai-switch`` at import time.

Without this, running the suite could write to the developer's real AI Switch
database (vendor creation, audit rows, health cache).
"""
import os
import shutil
import tempfile

_TMP_HOME = tempfile.mkdtemp(prefix="aiswitch-pytest-")

os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
# Don't let an imported app module start the real background health scheduler.
os.environ["AI_SWITCH_NO_SCHEDULER"] = "1"
# Never pop a browser window during tests.
os.environ["AI_SWITCH_OPEN_BROWSER"] = "0"
os.environ["AI_SWITCH_NO_BROWSER"] = "1"
# Auth tests manage these explicitly.
os.environ.pop("AI_SWITCH_ACCESS_TOKEN", None)
os.environ.pop("AI_SWITCH_REQUIRE_AUTH", None)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
