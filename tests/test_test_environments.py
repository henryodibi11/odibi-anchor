"""Named test environments, WSL launch construction and command criterion validation."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from odibi_anchor import pytest_runner
from odibi_anchor.pytest_runner import environment_launch, named_environment, wsl_path


@pytest.mark.parametrize("path, expected", [
    ("C:\\Users\\me\\repo\\tests\\test_a.py", "/mnt/c/Users/me/repo/tests/test_a.py"),
    ("d:/data/x.csv", "/mnt/d/data/x.csv"),
    ("C:\\", "/mnt/c/"),
    ("\\\\wsl$\\Ubuntu\\home\\me\\repo", "/home/me/repo"),
    ("\\\\wsl.localhost\\Ubuntu\\home\\me", "/home/me"),
    ("tests/test_a.py::test_one", "tests/test_a.py::test_one"),
    ("/home/me/venv/bin/python", "/home/me/venv/bin/python"),
    ("--timeout=30", "--timeout=30"),
])
def test_wsl_path_translates_only_absolute_windows_paths(path, expected):
    assert wsl_path(path) == expected


def test_wsl_launch_passes_distro_cwd_variables_and_translated_paths():
    environment = {"name": "wsl-spark", "kind": "wsl", "distro": "Ubuntu",
                   "python": "/home/me/venv/bin/python", "env": {"SPARK_HOME": "/opt/spark"}}
    _, argv = environment_launch(environment, ["python", "-B", "C:\\repo\\runner.py", "tests/test_a.py"],
                                 cwd="C:\\repo", variables={"SUMMARY": "C:\\Temp\\s.json"})
    assert argv[:7] == ["wsl.exe", "-d", "Ubuntu", "--cd", "/mnt/c/repo", "-e", "env"]
    assert argv[-4:] == ["/home/me/venv/bin/python", "-B", "/mnt/c/repo/runner.py", "tests/test_a.py"]
    passed = argv[7:-4]
    assert "SPARK_HOME=/opt/spark" in passed and "SUMMARY=/mnt/c/Temp/s.json" in passed
    # The hermetic child settings cross into the distro explicitly.
    assert "PYTHONDONTWRITEBYTECODE=1" in passed and "GIT_CONFIG_COUNT=3" in passed


def test_pytest_launch_spec_runs_the_runner_inside_wsl():
    environment = {"name": "w", "kind": "wsl", "distro": "Ubuntu", "python": "/usr/bin/python3", "env": {}}
    _, argv = pytest_runner._launch_spec(["C:\\repo\\tests\\test_a.py"], Path("C:\\Temp\\summary.json"),
                                         environment=environment, cwd="C:\\repo")
    runner = wsl_path(str(Path(pytest_runner.__file__).resolve()))
    assert argv[:5] == ["wsl.exe", "-d", "Ubuntu", "--cd", "/mnt/c/repo"]
    assert argv[-6:] == ["/usr/bin/python3", "-B", runner, "-p", "no:cacheprovider", "/mnt/c/repo/tests/test_a.py"]
    assert f"{pytest_runner.SUMMARY_ENV}=/mnt/c/Temp/summary.json" in argv


def test_local_launch_replaces_python_and_adds_variables(monkeypatch):
    monkeypatch.setenv("ANCHOR_HOME", "/operator/home")
    env, argv = environment_launch({"name": "l", "kind": "local", "python": "/venv/bin/python",
                                    "env": {"MODE": "ci"}}, ["python", "-m", "mkdocs", "build"])
    assert argv == ["/venv/bin/python", "-m", "mkdocs", "build"]
    assert env["MODE"] == "ci" and "ANCHOR_HOME" not in env


def test_named_environment_selects_and_refuses_unknown_names(monkeypatch):
    monkeypatch.delenv(pytest_runner.TEST_ENVIRONMENTS_ENV, raising=False)
    with pytest.raises(ValueError, match=r"'windows' is not configured on this host \(configured: none\)"):
        named_environment("windows")
    monkeypatch.setenv(pytest_runner.TEST_ENVIRONMENTS_ENV, json.dumps({
        "windows": {"kind": "local", "python": "C:\\venv\\Scripts\\python.exe"},
        "cluster": {"kind": "runtime"},
        "wsl-spark": {"kind": "wsl", "distro": "Ubuntu", "python": "/usr/bin/python3", "env": {"A": "1"}},
    }))
    assert named_environment("cluster") == {"name": "cluster", "kind": "runtime", "python": sys.executable, "env": {}}
    assert named_environment("wsl-spark")["distro"] == "Ubuntu"
    with pytest.raises(ValueError, match=r"configured: cluster, windows, wsl-spark\)"
                                         r".*projects.<id>.test_environments.<host>.<name>"):
        named_environment("linux")


def test_run_command_records_exit_code_digests_and_redacted_tail(tmp_path):
    from odibi_anchor._dispatcher._session_tools import run_command

    environment = {"name": "local", "kind": "local", "python": sys.executable, "env": {"WORD": "hello"}}
    run = run_command(["python", "-c", "import os, sys; print(os.environ['WORD']); sys.exit(3)"],
                      cwd=tmp_path, timeout=60, environment=environment)
    assert (run["exit_code"], run["timed_out"]) == (3, False)
    assert "hello" in run["output_tail"]
    assert run["stdout_sha256"] == hashlib.sha256(b"hello" + (b"\r\n" if sys.platform == "win32" else b"\n")).hexdigest()
    slow = run_command(["python", "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=1,
                       environment=environment)
    assert slow["timed_out"] is True and slow["exit_code"] is None
    with pytest.raises(ValueError, match="could not start"):
        run_command(["anchor-no-such-program-xyz"], cwd=tmp_path, timeout=5, environment=environment)


def _plan(mode="source_change", criteria=(), **extra):
    plan = {"schema_version": 1, "goal": "Document the rollout", "risk": "low", "execution_mode": mode,
            "scope": ["docs"], "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
            "unresolved_decisions": [], "destination": {"kind": "managed_artifacts"},
            "criteria": list(criteria), **extra}
    if mode == "source_change":
        plan["source_paths"] = ["docs/guide.md"]
        plan["destination"] = {"kind": "local_git_ref", "ref": "refs/heads/main"}
    return plan


def _check(plan):
    from odibi_anchor._dispatcher._workflow_runtime import workflow_action

    return workflow_action(None, session_state=None, command="check_plan", plan=plan)


def test_check_plan_accepts_command_and_review_criteria_without_a_test_file():
    plan = _plan(criteria=[
        {"id": "docs", "expected": "Guide builds", "method": "command", "argv": ["python", "-m", "mkdocs", "build"],
         "environment": "windows", "cwd": "docs", "timeout_seconds": 120, "expected_exit_code": 0},
        {"id": "clear", "expected": "Guide reads clearly", "method": "review"},
    ], rigor="full")
    assert _check(plan)["problems"] == []


def test_check_plan_reports_every_bad_command_criterion_problem_at_once():
    plan = _plan(criteria=[
        {"id": "docs", "expected": "Guide builds", "method": "command", "argv": "mkdocs build",
         "cwd": "../outside", "timeout_seconds": 0, "expected_exit_code": "0", "shell": True,
         "environment": "bad name"},
        {"id": "clear", "expected": "Reads clearly", "method": "review", "argv": ["x"]},
        {"id": "magic", "expected": "Works", "method": "manual"},
    ])
    problems = _check(plan)["problems"]
    for expected in ("criteria[0].environment must name", "unknown key(s): shell", "requires argv",
                     "cwd must be a canonical relative directory", "timeout_seconds must be an integer",
                     "expected_exit_code must be an integer", "criteria[1]: review criterion has unknown key(s): argv",
                     "criteria[2]: criterion method has no supported trusted collector"):
        assert any(expected in problem for problem in problems), (expected, problems)
    assert _check({**_plan(criteria=plan["criteria"][:1]), "rigor": "extreme"})["problems"] == [
        "plan.rigor must be direct, compact or full (omit it for the default)"]


def test_command_criteria_are_refused_where_nothing_can_run():
    plan = _plan(mode="read_only", criteria=[{"id": "c", "expected": "x", "method": "command", "argv": ["x"]}])
    assert any("no supported trusted collector in a read_only plan" in p for p in _check(plan)["problems"])


def test_artifact_digests_must_cover_all_paths_unless_a_command_also_examines_them():
    digest = "a" * 64
    partial = {"id": "bytes", "expected": "Report bytes", "method": "artifact_sha256",
               "expected_sha256": {"notebooks/a.md": digest}}
    base = {"artifact_paths": ["notebooks/a.md", "notebooks/b.md"]}
    review = {"id": "clear", "expected": "Reads clearly", "method": "review"}
    command = {"id": "valid", "expected": "Both validate", "method": "command", "argv": ["python", "check.py"]}
    assert any("cover all planned artifact paths" in p
               for p in _check(_plan("artifact_only", [partial, review], **base))["problems"])
    assert _check(_plan("artifact_only", [partial, review, command], **base))["problems"] == []
