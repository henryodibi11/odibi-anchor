"""The suite must use its own Anchor state no matter what the caller exported.

Issue #17, defence-in-depth half. `conftest` set ANCHOR_HOME and ANCHOR_MEMORY_DB
with `os.environ.setdefault`, which does nothing when the variable is already set.
A suite started from inside a bound runtime therefore inherited that routing and
ran against a real store — which is how test-created task windows ended up in an
operator's memory database. `pytest_runner` stripping its child's environment
covers `anchor("test")`; this covers every other way the suite is started.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SENTINEL_HOME = "/nonexistent/operator/home"
SENTINEL_DB = "/nonexistent/operator/home/.agent_memory.db"

_PROBE = '''
import json, os, runpy, sys
os.environ["ANCHOR_HOME"] = {sentinel_home!r}
os.environ["ANCHOR_MEMORY_DB"] = {sentinel_db!r}
runpy.run_path({conftest!r}, run_name="conftest_probe")
print(json.dumps({{
    "home": os.environ["ANCHOR_HOME"],
    "db": os.environ["ANCHOR_MEMORY_DB"],
}}))
'''


def _run_conftest_with_sentinels():
    """Execute tests/conftest.py's module body with the sentinels already exported."""
    script = _PROBE.format(
        sentinel_home=SENTINEL_HOME,
        sentinel_db=SENTINEL_DB,
        conftest=str(PROJECT_ROOT / "tests" / "conftest.py"),
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_exported_anchor_home_is_overridden():
    assert _run_conftest_with_sentinels()["home"] != SENTINEL_HOME


def test_exported_memory_db_is_overridden():
    assert _run_conftest_with_sentinels()["db"] != SENTINEL_DB


def test_override_points_at_a_temporary_store():
    resolved = _run_conftest_with_sentinels()
    assert resolved["home"].startswith(tempfile.gettempdir())
    assert resolved["db"].startswith(tempfile.gettempdir())


def test_the_sentinel_would_otherwise_have_been_inherited():
    """Negative control: without an override, an exported value does survive.

    This is what `setdefault` produced, and why the assertion above is meaningful
    rather than vacuous.
    """
    environment = dict(os.environ)
    environment["ANCHOR_HOME"] = SENTINEL_HOME
    completed = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['ANCHOR_HOME'])"],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    assert completed.stdout.strip() == SENTINEL_HOME


def test_this_session_is_itself_isolated():
    """The running suite must already be pointed at the temp store."""
    assert os.environ["ANCHOR_MEMORY_DB"].startswith(tempfile.gettempdir())
    assert os.environ["ANCHOR_HOME"].startswith(tempfile.gettempdir())
