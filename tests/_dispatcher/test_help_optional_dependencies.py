"""anchor("help") must work on a base install with no optional extras.

The help table previously materialized every action callable eagerly, which
imported odibi_anchor.validation (numpy) and odibi_anchor.tables (pandas). Those
ship only in optional extras, so every help variant raised ModuleNotFoundError on
a plain `pip install odibi-anchor`. Databricks masked this because DBR preinstalls
numpy and pandas, and the test suite masked it because the dev extra installs both.
"""
import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BLOCKED = ("numpy", "pandas")

_BLOCKER = textwrap.dedent(
    """
    import sys
    from importlib.abc import MetaPathFinder

    BLOCKED = {blocked!r}

    class _Blocked(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError("No module named " + repr(fullname))
            return None

    for _name in [n for n in sys.modules if n.split(".")[0] in BLOCKED]:
        del sys.modules[_name]
    sys.meta_path.insert(0, _Blocked())
    """
)


def _run_help(*args):
    """Run `anchor exec help [...]` in a subprocess where the extras are unimportable."""
    script = _BLOCKER.format(blocked=BLOCKED) + textwrap.dedent(
        """
        import sys
        from odibi_anchor.cli import main
        sys.argv = ["anchor", "exec", "help"] + {args!r}
        raise SystemExit(main())
        """
    ).format(args=list(args))
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300
    )
    return completed


def test_optional_dependency_carriers_are_actually_optional():
    """Guard the premise: these modules import the extras at module level.

    Resolve through importlib rather than `from odibi_anchor.validation import
    duplicate_key_context`: the package re-exports a *function* of that name, which
    shadows the submodule once __init__ has run, making the plain form order-dependent.
    """
    module = importlib.import_module("odibi_anchor.validation.duplicate_key_context")

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "import numpy" in source


def test_blocker_actually_blocks():
    """A negative control — without this the other tests could pass vacuously."""
    script = _BLOCKER.format(blocked=BLOCKED) + "\nimport numpy\n"
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode != 0
    assert "ModuleNotFoundError" in completed.stderr


@pytest.mark.parametrize(
    "args",
    [
        pytest.param([], id="overview"),
        pytest.param(['{"args": ["task"]}'], id="dependency-free-action"),
        pytest.param(['{"args": ["duplicate"]}'], id="numpy-bound-action"),
        pytest.param(['{"args": ["diff"]}'], id="pandas-bound-action"),
    ],
)
def test_help_succeeds_without_optional_dependencies(args):
    completed = _run_help(*args)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True, payload.get("error")
    assert payload["result"]


def test_dependency_bound_action_degrades_to_usage_and_examples():
    """A numpy-bound action still documents itself, minus the introspected signature."""
    completed = _run_help('{"args": ["duplicate"]}')
    assert completed.returncode == 0, completed.stderr
    text = json.loads(completed.stdout)["result"]
    assert 'anchor("duplicate")' in text
    assert "**Usage:**" in text
    assert "Full signature" not in text


def test_dependency_free_action_keeps_signature_introspection():
    """The lazy table must not silently degrade actions that *can* resolve."""
    completed = _run_help('{"args": ["task"]}')
    assert completed.returncode == 0, completed.stderr
    text = json.loads(completed.stdout)["result"]
    assert "Full signature" in text
    assert "**Parameters:**" in text
