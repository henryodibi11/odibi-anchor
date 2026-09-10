"""Focused contracts for canonical pytest execution."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import odibi_anchor.pytest_runner as pytest_runner
from odibi_anchor._dispatcher import _session_tools
from odibi_anchor.pytest_runner import child_environment, run_pytest


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_arbitrary_arguments_are_forwarded_unchanged_with_current_python(monkeypatch, tmp_path):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    args = ["tests/example.py::test_one", "-k", "one or two", "--maxfail=7"]
    run_pytest(args, cwd=tmp_path, capture_output=True)
    assert observed["command"][0] == sys.executable
    assert observed["command"][-len(args):] == args
    assert observed["command"][2] == str(Path(pytest_runner.__file__).resolve())


def test_child_environment_preserves_existing_pythonpath():
    env = child_environment({"PYTHONPATH": "caller/source"})

    assert env["PYTHONPATH"] == "caller/source"


def test_safe_path_preserves_distinct_first_inherited_pythonpath_entry(monkeypatch, tmp_path):
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    _write(inherited / "inherited_only.py", "VALUE = 'inherited'\n")
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "target_only.py", "VALUE = 'target'\n")
    _write(
        target / "test_safe_path.py",
        "import inherited_only\n"
        "import target_only\n\n"
        "def test_imports():\n"
        "    assert inherited_only.VALUE == 'inherited'\n"
        "    assert target_only.VALUE == 'target'\n",
    )
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(inherited), str(tmp_path / "unused"))))

    summary, proc = run_pytest(["test_safe_path.py", "-q"], cwd=target, capture_output=True)

    assert proc.returncode == 0, proc.stderr
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 0, 0)


def test_safe_path_preserves_runner_parent_as_first_inherited_entry(
    monkeypatch, tmp_path
):
    runner_parent = tmp_path / "runner-parent"
    runner_parent.mkdir()
    copied_runner = runner_parent / "pytest_runner.py"
    copied_runner.write_bytes(Path(pytest_runner.__file__).read_bytes())
    _write(runner_parent / "inherited_only.py", "VALUE = 'inherited'\n")
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "target_only.py", "VALUE = 'target'\n")
    _write(
        target / "test_safe_path.py",
        "import inherited_only\n"
        "import target_only\n\n"
        "def test_imports():\n"
        "    assert inherited_only.VALUE == 'inherited'\n"
        "    assert target_only.VALUE == 'target'\n",
    )
    monkeypatch.setattr(pytest_runner, "__file__", str(copied_runner))
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv("PYTHONPATH", str(runner_parent))

    summary, proc = run_pytest(["test_safe_path.py", "-q"], cwd=target, capture_output=True)

    assert proc.returncode == 0, proc.stderr
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 0, 0)


@pytest.mark.parametrize("ambient_request", ["pytest_plugins_env", "pytest_addopts", "conftest"])
def test_ambient_canonical_plugin_requests_resolve_exact_runner(
    monkeypatch, tmp_path, ambient_request
):
    (tmp_path / "odibi_anchor").mkdir()
    _write(
        tmp_path / "odibi_anchor" / "__init__.py",
        "TARGET_PACKAGE = True\n",
    )
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    if ambient_request == "pytest_plugins_env":
        monkeypatch.setenv("PYTEST_PLUGINS", "odibi_anchor.pytest_runner")
    elif ambient_request == "pytest_addopts":
        monkeypatch.setenv("PYTEST_ADDOPTS", "-p odibi_anchor.pytest_runner")
    else:
        _write(
            tmp_path / "conftest.py",
            "pytest_plugins = ('odibi_anchor.pytest_runner',)\n",
        )
    monkeypatch.setenv("EXPECTED_RUNNER", str(Path(pytest_runner.__file__).resolve()))
    _write(
        tmp_path / "test_smoke.py",
        "import os\n"
        "from pathlib import Path\n\n"
        "import odibi_anchor\n\n"
        "def test_smoke(pytestconfig):\n"
        "    manager = pytestconfig.pluginmanager\n"
        "    plugin = manager.get_plugin('odibi_anchor.pytest_runner')\n"
        "    summaries = [p for p in manager.get_plugins() if p.__class__.__name__ == '_SummaryPlugin']\n"
        "    assert Path(plugin.__file__).resolve() == Path(os.environ['EXPECTED_RUNNER']).resolve()\n"
        "    assert len(summaries) == 1\n"
        "    assert manager.get_plugin('odibi-anchor-summary') is summaries[0]\n"
        "    assert odibi_anchor.TARGET_PACKAGE is True\n",
    )

    summary, proc = run_pytest(["test_smoke.py", "-q"], cwd=tmp_path, capture_output=True)

    assert proc.returncode == 0, proc.stderr
    assert (summary["passed"], summary["failed"], summary["errors"], summary["skipped"]) == (
        1,
        0,
        0,
        0,
    )


def test_summary_counts_failure_skip_and_setup_error(tmp_path):
    _write(
        tmp_path / "test_cases.py",
        "import pytest\n"
        "def test_pass(): pass\n"
        "def test_fail(): assert False\n"
        "@pytest.mark.skip\ndef test_skip(): pass\n"
        "@pytest.fixture\ndef broken(): raise RuntimeError('setup')\n"
        "def test_error(broken): pass\n",
    )
    summary, proc = run_pytest(["-q"], cwd=tmp_path, capture_output=True)
    assert proc.args[0] == sys.executable
    assert summary["schema_version"] == 1
    assert (summary["passed"], summary["failed"], summary["errors"], summary["skipped"]) == (1, 1, 1, 1)


def test_collection_error_and_timeout_are_bounded(tmp_path):
    _write(tmp_path / "test_bad.py", "this is invalid python !!!")
    summary, _ = run_pytest([], cwd=tmp_path, capture_output=True)
    assert summary["exit_code"] != 0 and summary["errors"] == 1

    _write(tmp_path / "test_bad.py", "import time\ndef test_slow(): time.sleep(2)\n")
    summary, _ = run_pytest([], cwd=tmp_path, timeout=0.1, capture_output=True)
    assert summary["timed_out"] is True and summary["exit_code"] == -1


def test_failed_test_result_includes_redacted_stderr_tail(monkeypatch, tmp_path):
    proc = subprocess.CompletedProcess(
        [sys.executable, "-m", "pytest"],
        1,
        "pytest stdout context\n",
        "plugin import failed\ntoken=secret-value\n",
    )
    monkeypatch.setattr(
        _session_tools,
        "run_pytest",
        lambda *_args, **_kwargs: (
            {
                "passed": 0,
                "failed": 0,
                "errors": 1,
                "skipped": 0,
                "duration_s": 0.01,
                "timed_out": False,
            },
            proc,
        ),
    )

    result = _session_tools._test_run(tmp_path, target="nested/tests/test_smoke.py")

    output_tail = result["samples"]["output_tail"]
    assert output_tail.index("pytest stdout context") < output_tail.index("plugin import failed")
    assert "secret-value" not in output_tail
    assert "token=<redacted>" in output_tail


def test_timed_out_test_result_includes_bounded_redacted_byte_output(monkeypatch, tmp_path):
    proc = subprocess.CompletedProcess(
        [sys.executable, str(Path(pytest_runner.__file__).resolve())],
        -1,
        b"partial stdout\n",
        b"partial stderr\n" + b"x" * 9000 + b"\npassword=visible-secret\n",
    )
    monkeypatch.setattr(
        _session_tools,
        "run_pytest",
        lambda *_args, **_kwargs: (
            {
                "passed": 0,
                "failed": 0,
                "errors": 1,
                "skipped": 0,
                "duration_s": 600.0,
                "timed_out": True,
            },
            proc,
        ),
    )

    result = _session_tools._test_run(tmp_path, target="nested/tests/test_slow.py")

    output_tail = result["samples"]["output_tail"]
    assert result["metrics"]["timed_out"] is True
    assert len(output_tail) == 8000
    assert "visible-secret" not in output_tail
    assert "password=<redacted>" in output_tail


def test_git_environment_is_child_only_and_disables_signing(tmp_path):
    before = subprocess.run(
        ["git", "config", "--local", "--list"], cwd=tmp_path,
        text=True, capture_output=True,
    )
    env = child_environment({})
    assert env["GIT_CONFIG_VALUE_0"] == "false"
    assert env["GIT_AUTHOR_EMAIL"] == "tests@odibi-anchor.invalid"
    after = subprocess.run(
        ["git", "config", "--local", "--list"], cwd=tmp_path,
        text=True, capture_output=True,
    )
    assert (before.returncode, before.stdout, before.stderr) == (after.returncode, after.stdout, after.stderr)
