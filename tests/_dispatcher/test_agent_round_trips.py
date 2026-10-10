"""Agent round-trip contracts: one rejected call discloses every independent fix.

Each test drives the public ``anchor()`` dispatcher. A rejected submission keeps
its original exception type and first message, still writes nothing, and adds
``context["problems"]`` so an agent can repair every independent defect at once.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _git(target: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=target, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """A clean committed Git target bound to one managed project."""
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init

    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "mod.py").write_text("VALUE = 1\n")
    _git(target, "init", "-b", "main")
    _git(target, "add", ".")
    _git(target, "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture",
         "commit", "-m", "base")
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "personal")
    project_action(home, "create", name="alpha", target=target, output_format="dict")
    anchor, _, _ = init(root=str(target), project="alpha", output_format="dict")
    return SimpleNamespace(anchor=anchor, target=target)


def _keys(problems: list[dict]) -> list[str]:
    return [item.get("prerequisite") or item["field"] for item in problems]


def _assert_problem_shape(problems: list[dict]) -> None:
    assert problems
    for item in problems:
        assert set(item) in ({"field", "problem", "fix"}, {"prerequisite", "problem", "fix"})
        assert all(isinstance(value, str) and value.strip() for value in item.values())


def test_task_prerequisite_block_discloses_every_independent_defect(runtime):
    """Previously only the first missing prerequisite was reported."""
    from odibi_anchor._dispatcher._blocked_action import BlockedActionError

    with pytest.raises(BlockedActionError) as blocked:
        runtime.anchor("task", "x", goal="", mode="bogus", risk="extreme", rigor="bogus",
                       not_a_field=1, output_format="dict")

    message = str(blocked.value)
    assert message.startswith(
        'BLOCKED: anchor("task") requires orientation first. '
        'Run anchor("status") before planning.\nSequence: bootstrap → '
    )
    assert blocked.value.required_action == "status"
    problems = blocked.value.context["problems"]
    _assert_problem_shape(problems)
    keys = _keys(problems)
    # Startup prerequisites come first, in the exact enforced order.
    assert keys[:3] == ["status", "audit_history", "new_session"]
    assert blocked.value.context["prerequisite_order"] == ["status", "audit_history", "new_session"]
    for field in ("task", "goal", "mode", "risk", "rigor", "not_a_field"):
        assert field in keys
    mode_problem = next(item for item in problems if item.get("field") == "mode")
    assert "Unsupported legacy mode: 'bogus'" in mode_problem["problem"]
    # The diagnostic never invents a mode, goal, or other semantic value.
    assert "bogus" not in str(blocked.value.next_operations)


def test_task_goal_rejection_also_names_workflow_and_dirty_source_baseline(runtime):
    """Previously a source task learned about these only after re-submitting."""
    runtime.anchor("orient", output_format="dict")
    runtime.anchor("new_session", name="round_trip", inline=True, output_format="dict")
    (runtime.target / "pkg" / "mod.py").write_text("VALUE = 2\n")

    with pytest.raises(RuntimeError) as rejected:
        runtime.anchor("task", "Implement module change", goal="short",
                       mode="implementation", output_format="dict")

    assert str(rejected.value).startswith(
        "BLOCKED: anchor(\"task\") requires a meaningful goal= kwarg (>=10 chars). Got: goal='short'"
    )
    assert type(rejected.value) is RuntimeError
    problems = rejected.value.context["problems"]
    _assert_problem_shape(problems)
    keys = _keys(problems)
    assert keys[0] == "goal"
    assert "workflow_id" in keys
    assert "clean_worktree" in keys
    assert "acceptance_criteria" in keys  # implementation readiness floor
    dirty = next(item for item in problems if item.get("prerequisite") == "clean_worktree")
    assert "1 dirty path" in dirty["problem"]
    # Read-only probing leaves the dirty file and the task authority untouched.
    assert (runtime.target / "pkg" / "mod.py").read_text() == "VALUE = 2\n"
    assert _git(runtime.target, "status", "--porcelain") == "M pkg/mod.py"


def test_task_without_explicit_source_mode_does_not_probe_git(runtime):
    runtime.anchor("orient", output_format="dict")
    runtime.anchor("new_session", name="round_trip", inline=True, output_format="dict")
    (runtime.target / "pkg" / "mod.py").write_text("VALUE = 2\n")

    with pytest.raises(RuntimeError) as rejected:
        runtime.anchor("task", "Investigate module", goal="short", output_format="dict")

    assert "clean_worktree" not in _keys(rejected.value.context["problems"])


def test_task_validation_error_keeps_type_and_lists_all_input_defects(runtime):
    """Previously validate_prepared_inputs stopped at the first placeholder."""
    runtime.anchor("orient", output_format="dict")
    runtime.anchor("new_session", name="round_trip", inline=True, output_format="dict")

    with pytest.raises(ValueError) as rejected:
        runtime.anchor("task", "Investigate module behavior", goal="Explain module values",
                       mode="analysis", risk="extreme", constraints=["TBD"],
                       acceptance_criteria=["<fill in>"], output_format="dict")

    assert type(rejected.value) is ValueError
    problems = rejected.value.context["problems"]
    _assert_problem_shape(problems)
    assert {"constraints[0]", "acceptance_criteria[0]", "risk"} <= set(_keys(problems))
    # The raised message is still exactly the first validation failure.
    assert str(rejected.value) == f"{problems[0]['field']} contains a fabricated placeholder"


@pytest.fixture
def capture_ready(runtime):
    anchor = runtime.anchor
    anchor("orient", output_format="dict")
    anchor("new_session", name="learn", inline=True, output_format="dict")
    anchor("task", "Read modules", goal="Inspect modules", mode="analysis", risk="low",
           rigor="direct", acceptance_criteria=["read"], output_format="dict")
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    return anchor


def _items(anchor) -> list[dict]:
    return anchor("learning", "list", output_format="dict").get("items", [])


def test_capture_aggregates_unknown_fields_and_nested_evidence_before_any_write(capture_ready):
    """Previously capture reported only the unknown field, then one evidence error per retry."""
    anchor = capture_ready
    before = _items(anchor)

    with pytest.raises(ValueError) as rejected:
        anchor("learning", "capture", observation_type="nope", summary="A friction",
               signal_key="Bad Key", impact="huge", bogus=1,
               provenance={"bogus": "unknown", "source_action": "bad value"},
               evidence=[{"reference": "pkg/mod.py"},
                         {"reference_type": "file"},
                         {"reference_type": "spec", "reference": "lower"}],
               output_format="dict")

    assert str(rejected.value).startswith("invalid capture payload: unknown field(s) bogus; ")
    problems = rejected.value.context["problems"]
    _assert_problem_shape(problems)
    keys = _keys(problems)
    assert keys[0] == "bogus"
    for field in ("observation_type", "signal_key", "impact",
                  "evidence[0].reference_type", "evidence[1].reference",
                  "evidence[2].reference", "provenance", "provenance.source_action"):
        assert field in keys
    assert _items(anchor) == before


def test_capture_evidence_error_preserves_schema_and_lists_every_item(capture_ready):
    anchor = capture_ready
    with pytest.raises(ValueError) as rejected:
        anchor("learning", "capture", observation_type="friction", summary="A friction",
               signal_key="fixture.friction",
               evidence=[{"reference_type": "file"}, {"reference": "pkg/mod.py"}],
               output_format="dict")

    assert str(rejected.value) == "invalid evidence: invalid evidence.reference"
    assert rejected.value.error_code == "learning_evidence_invalid"
    assert rejected.value.context["expected_schema"]["item"]["required"] == [
        "reference_type", "reference",
    ]
    assert _keys(rejected.value.context["problems"]) == [
        "evidence[0].reference", "evidence[1].reference_type",
    ]
    assert rejected.value.next_operation["copy_ready"] == "anchor('help', 'learning')"


def test_capture_missing_fields_keep_original_metadata_and_add_other_defects(capture_ready):
    anchor = capture_ready
    with pytest.raises(ValueError) as rejected:
        anchor("learning", "capture", observation_type="friction", summary="A friction",
               impact="huge", output_format="dict")

    assert rejected.value.error_code == "learning_capture_fields_missing"
    assert rejected.value.context["missing_fields"] == ["signal_key", "evidence"]
    assert _keys(rejected.value.context["problems"]) == ["signal_key", "evidence", "impact"]


def test_unknown_only_capture_supplies_a_working_corrected_call(capture_ready):
    with pytest.raises(ValueError, match="unknown field") as rejected:
        capture_ready(
            "learning", "capture", observation_type="friction", summary="A friction",
            signal_key="fixture.friction", kind="unsupported",
            evidence=[{"reference_type": "file", "reference": "pkg/mod.py"}],
            output_format="dict",
        )
    operation = rejected.value.next_operation
    assert operation["action"] == "learning" and operation["args"] == ["capture"]
    assert "kind" not in operation["kwargs"]
    result = capture_ready(operation["action"], *operation["args"],
                           **operation["kwargs"], output_format="dict")
    assert result["observation_id"] == result["item"]["item_id"]


def test_concurrent_request_id_reserves_before_execution():
    """A duplicate arriving during the old synchronous run must not execute again."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from odibi_anchor._dispatcher._session_tools import run_retained_test

    entered, release = threading.Event(), threading.Event()
    calls = []

    def execute():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return {"metrics": {"exit_code": 0, "passed": 1}}

    def request():
        return run_retained_test("in-flight", task_window_id="concurrent-fixture",
                                 arguments={"target": ["test_one.py"]},
                                 scope_fingerprint=lambda: "same-bytes", execute=execute)

    with ThreadPoolExecutor(max_workers=1) as worker:
        first = worker.submit(request)
        try:
            assert entered.wait(5)
            assert request()["status"] == "running"
        finally:
            release.set()
        assert first.result()["metrics"]["passed"] == 1
    assert request()["request"]["replayed"] is True
    assert calls == [True]


@pytest.mark.parametrize("wait", [-1, 91, True, float("nan"), float("inf"), "1"])
def test_invalid_wait_refuses_without_test_execution(runtime, wait):
    anchor = runtime.anchor
    anchor("orient", output_format="dict")
    anchor("new_session", name="invalid_wait", inline=True, output_format="dict")
    anchor("task", "Inspect module behavior", goal="Observe module behavior", mode="analysis",
           acceptance_criteria=["tests observed"], output_format="dict")
    with pytest.raises(ValueError, match="wait_seconds") as rejected:
        anchor("test", target=["not_present.py"], wait_seconds=wait, output_format="dict")
    assert rejected.value.context["executed"] is False
    assert rejected.value.next_operation["kwargs"]["wait_seconds"] == 0
