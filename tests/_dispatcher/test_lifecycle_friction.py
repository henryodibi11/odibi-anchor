"""Regression contracts for the lifecycle friction reported in issue #39.

Each test drives the public dispatcher (or the exact pure decision function) and
fails on v0.3.24 for the reason named in its docstring.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = """---
status: ready
mode: implementation
permissions_needed: []
files_touched:
  - pkg/mod_00.py
---
# Fixture Spec

## Problem
Module values are wrong.

## Design
Change the values; verify with anchor("test") and anchor("gate"). Applies writing-specs.

## Acceptance Criteria
- WHEN a module is imported, VALUE SHALL equal 2.

## Verification
Run the fixture tests.

## Risks
None beyond the disposable fixture.
"""


def _git(target: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=target, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    """A git target with 18 modules and a factory for workflow-bound producers."""
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init

    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    (target / "pkg").mkdir(parents=True)
    paths = [f"pkg/mod_{index:02d}.py" for index in range(18)]
    for path in paths:
        (target / path).write_text("VALUE = 1\n")
    (target / "test_pkg.py").write_text("def test_ok():\n    assert True\n")
    _git(target, "init", "-b", "main")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "base")
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "personal")
    project_action(home, "create", name="alpha", target=target, output_format="dict")
    anchor, _, _ = init(root=str(target), project="alpha", output_format="dict")

    def restart():
        return init(root=str(target), project="alpha", output_format="dict")[0]

    def producer(planned, *, risk="medium", task_risk=None, criteria_targets=("test_pkg.py",)):
        anchor("orient", output_format="dict")
        anchor("new_session", name="plan", inline=True, output_format="dict")
        anchor("task", "Plan module change", goal="Change module values", mode="analysis",
               risk=risk, rigor="direct", trust_domain="personal",
               acceptance_criteria=["A bounded plan exists"], output_format="dict")
        plan = {"schema_version": 1, "goal": "Change module values", "risk": risk,
                "execution_mode": "source_change", "scope": list(planned), "source_paths": list(planned),
                "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
                "unresolved_decisions": [],
                "destination": {"kind": "github_ref", "repository": "acme/app", "ref": "refs/heads/main"},
                "reconciliation": {"requirements": [], "reason": "Disposable fixture"},
                "criteria": [{"id": "ok", "expected": "Fixture tests pass", "method": "pytest",
                              "test_targets": list(criteria_targets)}]}
        draft = anchor("workflow", "create", plan=plan, request_id="draft", output_format="dict")["state"]
        anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
        anchor("new_session", name="produce", inline=True, output_format="dict")
        task = anchor("task", "Implement module change", goal="Change module values",
                      mode="implementation", work_type="change", execution_mode="source_change",
                      risk=task_risk or risk, trust_domain="personal", workflow_id=draft["workflow_id"],
                      acceptance_criteria=["Fixture tests pass"], output_format="dict")
        return draft, task

    return SimpleNamespace(anchor=anchor, target=target, home=home, paths=paths,
                           producer=producer, restart=restart)


def advance(anchor, command, **kwargs):
    current = anchor("workflow", output_format="dict")["state"]
    return anchor("workflow", command, expected_generation=current["generation"],
                  request_id=f"{command}:{current['generation']}", output_format="dict", **kwargs)["state"]


def satisfy_spec(anchor, *, order=("review", "execute")):
    anchor("skill_loaded", "writing-specs", output_format="dict")
    created = anchor("spec", "create", name="FIXTURE_CHANGE", output_format="dict")
    Path(created["path"]).write_text(SPEC)
    for command in order:
        anchor("spec", command, "FIXTURE_CHANGE", output_format="dict")


# ── Item 4: accepted-plan paths do not consume the caps; unplanned paths do ──

def test_workflow_producer_registers_more_than_cap_planned_paths(lifecycle):
    """v0.3.24 blocked the 16th planned `touched` at medium risk (cap 15)."""
    anchor = lifecycle.anchor
    lifecycle.producer(lifecycle.paths)
    advance(anchor, "accept_plan")
    anchor("known_bad", changed_files=lifecycle.paths, output_format="dict")
    for path in lifecycle.paths:
        (lifecycle.target / path).write_text("VALUE = 2\n")
        assert anchor("touched", path, output_format="dict")["registered"] == path
    assert advance(anchor, "implemented")["progress"] == "implemented"


def test_unplanned_paths_keep_the_checkpoint_cap_counted_conservatively():
    from odibi_anchor._dispatcher._enforcement import should_block_checkpoint

    planned = frozenset(f"p{index}" for index in range(10))
    unplanned = {f"u{index}" for index in range(17)}
    # 10 planned + 2 unplanned were registered before the checkpoint (count 12).
    # 17 unplanned now: 15 since the checkpoint plus one new touch is 16 > 15.
    blocked, _ = should_block_checkpoint(set(planned) | unplanned, 12, "u_new", threshold=15,
                                         exempt_paths=planned)
    assert blocked
    assert not should_block_checkpoint(set(planned) | unplanned, 12, "p3", threshold=15,
                                       exempt_paths=planned)[0]
    # Without exemptions the original behavior is unchanged.
    assert should_block_checkpoint({f"f{index}" for index in range(15)}, 0, "f15", threshold=15)[0]
    assert not should_block_checkpoint({f"f{index}" for index in range(14)}, 0, "f14", threshold=15)[0]


def test_unplanned_touches_keep_the_ungated_edit_limit():
    from odibi_anchor._dispatcher._enforcement import should_block_edit_limit

    planned = frozenset(f"p{index}" for index in range(25))
    timings = [{"action": "touched", "error": None, "touched_path": path} for path in sorted(planned)]
    assert not should_block_edit_limit(timings, max_ungated=20, exempt_paths=planned)[0]
    timings += [{"action": "touched", "error": None, "touched_path": f"u{index}"} for index in range(20)]
    assert should_block_edit_limit(timings, max_ungated=20, exempt_paths=planned)[0]
    assert should_block_edit_limit(timings[:20], max_ungated=20)[0]


# ── Item 7: risk downgrade names the required risk with a copy-ready call ──

def test_risk_downgrade_names_required_risk_and_corrected_call(lifecycle):
    """v0.3.24 raised only 'accepted task cannot downgrade workflow risk'."""
    with pytest.raises(RuntimeError) as caught:
        lifecycle.producer(lifecycle.paths[:2], risk="high", task_risk="medium")
    assert caught.value.error_code == "workflow_risk_downgrade"
    assert caught.value.context["required_risk"] == "high"
    assert caught.value.context["requested_risk"] == "medium"
    operation = caught.value.next_operation
    assert operation["action"] == "task" and operation["kwargs"]["risk"] == "high"
    assert "risk='high'" in operation["copy_ready"]


# ── Item 8: Spec requirement at acceptance; either link order works ──

def test_required_spec_surfaces_at_acceptance(lifecycle):
    """v0.3.24 listed only the writing-specs skill; the Spec surfaced first at touched."""
    _, task = lifecycle.producer(lifecycle.paths[:2], risk="high")
    required = {item["id"] for item in task["operating_protocol"]["required_now"]}
    assert "required_spec" in required


def test_review_then_execute_links_an_existing_reviewed_spec(lifecycle):
    """v0.3.24 discarded the review when `spec execute` linked an unlinked spec afterwards."""
    anchor = lifecycle.anchor
    specs = lifecycle.home / "workspace" / "projects" / "alpha" / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "FIXTURE_CHANGE_SPEC.md").write_text(SPEC)  # Authored before this task.
    lifecycle.producer(lifecycle.paths[:2], risk="high")
    anchor("skill_loaded", "writing-specs", output_format="dict")
    anchor("spec", "review", "FIXTURE_CHANGE", output_format="dict")
    anchor("spec", "execute", "FIXTURE_CHANGE", output_format="dict")
    status = anchor("status", output_format="dict")["operating_protocol"]
    assert not [item for item in status["required_now"] if item["id"] == "required_spec"]
    advance(anchor, "accept_plan")
    anchor("known_bad", changed_files=lifecycle.paths[:1], output_format="dict")
    (lifecycle.target / lifecycle.paths[0]).write_text("VALUE = 2\n")
    assert anchor("touched", lifecycle.paths[0], output_format="dict")["registered"] == lifecycle.paths[0]


# ── Item 1: continuity across a dispatcher restart, bound to bytes ──

def _edited_high_risk_producer(lifecycle, count=3):
    anchor = lifecycle.anchor
    _, task = lifecycle.producer(lifecycle.paths[:count], risk="high")
    satisfy_spec(anchor, order=("execute", "review"))
    advance(anchor, "accept_plan")
    anchor("known_bad", changed_files=lifecycle.paths[:count], output_format="dict")
    for path in lifecycle.paths[:count]:
        (lifecycle.target / path).write_text("VALUE = 2\n")
        anchor("touched", path, output_format="dict")
    return task["task_window_id"]


def test_rebind_restores_unchanged_process_state(lifecycle):
    """v0.3.24 required known_bad, touched, skills and spec linking again after restart."""
    window = _edited_high_risk_producer(lifecycle)
    _git(lifecycle.target, "add", ".")
    _git(lifecycle.target, "commit", "-m", "candidate")  # Commits do not change bytes.
    anchor = lifecycle.restart()
    rebound = anchor("task_rebind", output_format="dict")
    assert rebound["task_window_id"] == window
    continuity = rebound["continuity"]
    assert continuity["status"] == "restored" and continuity["not_restored"] == []
    assert continuity["restored"]["touched"] == lifecycle.paths[:3]
    assert continuity["restored"]["known_bad"] == lifecycle.paths[:3]
    assert continuity["restored"]["skills"] == ["writing-specs"]
    assert continuity["restored"]["spec"] == ["FIXTURE_CHANGE"]
    assert [op["action"] for op in rebound["required_next_operations"]] == ["orient"]
    anchor("orient", output_format="dict")
    # No known_bad, skill or spec call is needed before the next planned edit.
    assert anchor("touched", lifecycle.paths[0], output_format="dict")["registered"] == lifecycle.paths[0]


def test_rebind_never_restores_state_for_different_bytes(lifecycle):
    _edited_high_risk_producer(lifecycle)
    (lifecycle.target / lifecycle.paths[2]).write_text("VALUE = 3\n")
    anchor = lifecycle.restart()
    rebound = anchor("task_rebind", output_format="dict")
    continuity = rebound["continuity"]
    assert continuity["restored"]["touched"] == lifecycle.paths[:2]
    assert continuity["restored"]["known_bad"] == []
    assert {(item["kind"], item["reason"]) for item in continuity["not_restored"]} == {
        ("touched", "content_changed"), ("known_bad", "content_changed")}
    actions = [op["action"] for op in rebound["required_next_operations"]]
    assert actions == ["orient", "known_bad", "touched"]
    anchor("orient", output_format="dict")
    with pytest.raises(RuntimeError, match="known-bad check first"):
        anchor("touched", lifecycle.paths[2], output_format="dict")


def test_tampered_continuity_record_restores_nothing(lifecycle):
    window = _edited_high_risk_producer(lifecycle)
    record_path = lifecycle.home / ".anchor_task_continuity" / window / "record.json"
    envelope = json.loads(record_path.read_text())
    envelope["record"]["skills"]["debugging"] = envelope["record"]["skills"]["writing-specs"]
    record_path.write_text(json.dumps(envelope))
    rebound = lifecycle.restart()("task_rebind", output_format="dict")
    assert rebound["continuity"]["status"] == "rejected"
    assert "known_bad" in [op["action"] for op in rebound["required_next_operations"]]


# ── Item 6: continuation after the prior task closed ──

def test_continuation_after_closed_task_in_same_process(lifecycle):
    """v0.3.24 rejected continuation=True after any earlier new_session in the process."""
    anchor = lifecycle.anchor
    anchor("orient", output_format="dict")
    anchor("new_session", name="first", inline=True, output_format="dict")
    with pytest.raises(ValueError, match="replaces the inline new_session"):
        anchor("task", "Read modules", goal="Inspect modules", mode="analysis", risk="low",
               rigor="direct", continuation=True, acceptance_criteria=["read"], output_format="dict")
    first = anchor("task", "Read modules", goal="Inspect modules", mode="analysis", risk="low",
                   rigor="direct", acceptance_criteria=["read"], output_format="dict")
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    second = anchor("task", "Read more modules", goal="Inspect more modules", mode="analysis",
                    risk="low", rigor="direct", continuation=True,
                    acceptance_criteria=["read more"], output_format="dict")
    assert second["task_window_id"] != first["task_window_id"]


# ── Items 15 and 16: closure notes and prose scope ──

def test_failed_closure_attempt_does_not_require_failure_notes(lifecycle):
    """v0.3.24 demanded notes about 'failed action candidates ['learning']'."""
    anchor = lifecycle.anchor
    anchor("orient", output_format="dict")
    anchor("new_session", name="empty", inline=True, output_format="dict")
    anchor("task", "Read modules", goal="Inspect modules", mode="analysis", risk="low",
           rigor="direct", acceptance_criteria=["read"], output_format="dict")
    with pytest.raises(ValueError, match="invalid assessment outcome"):
        anchor("learning", "assess", outcome="bogus", output_format="dict")
    assessed = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    assert assessed["assessment"]["outcome"] == "nothing_reusable_learned"


def test_task_accepts_one_prose_scope(lifecycle):
    """v0.3.24 raised 'unknown task fields: scope' despite the documented prose field."""
    anchor = lifecycle.anchor
    anchor("orient", output_format="dict")
    anchor("new_session", name="scoped", inline=True, output_format="dict")
    with pytest.raises(TypeError, match="one non-empty prose string"):
        anchor("task", "Read modules", goal="Inspect modules", mode="analysis", risk="low",
               rigor="direct", scope=["pkg"], acceptance_criteria=["read"], output_format="dict")
    accepted = anchor("task", "Read modules", goal="Inspect modules", mode="analysis", risk="low",
                      rigor="direct", scope="Only the pkg modules.", in_scope=["test_pkg.py"],
                      acceptance_criteria=["read"], output_format="dict")
    assert accepted["scope"]["in_scope"] == ["Only the pkg modules.", "test_pkg.py"]


# ── Items 9, 10, 13, 17: delivery timeout, retained results, measurement diagnostics ──

def _implemented(lifecycle, criteria_targets=("test_pkg.py",)):
    anchor = lifecycle.anchor
    lifecycle.producer(lifecycle.paths[:1], criteria_targets=criteria_targets)
    advance(anchor, "accept_plan")
    anchor("known_bad", changed_files=lifecycle.paths[:1], output_format="dict")
    (lifecycle.target / lifecycle.paths[0]).write_text("VALUE = 2\n")
    anchor("touched", lifecycle.paths[0], output_format="dict")
    _git(lifecycle.target, "add", ".")
    _git(lifecycle.target, "commit", "-m", "candidate")
    return advance(anchor, "implemented")


def test_rejected_measurement_is_not_executed_evidence(lifecycle):
    """v0.3.24 recorded a pre-run rejection as a failed test the assurance shadow cited."""
    from odibi_anchor.assurance.evaluator import normalize_assurance_evidence

    anchor = lifecycle.anchor
    _implemented(lifecycle)
    with pytest.raises(RuntimeError, match="exactly match") as caught:
        anchor("test", target=["tests/"], workflow_criterion="ok", output_format="dict")
    assert caught.value.error_code == "workflow_measurement_rejected"
    assert caught.value.context["executed"] is False
    assert caught.value.next_operation["kwargs"]["target"] == ["test_pkg.py"]
    from odibi_anchor._utils._session_state import _SESSION_TIMINGS

    rejected = [row for row in _SESSION_TIMINGS if row["action"] == "test"][-1]
    assert rejected["executed"] is False
    context = SimpleNamespace(task_verification_epoch=0)
    control = normalize_assurance_evidence(context, _SESSION_TIMINGS)["control"]
    assert not [item for item in control.values() if item["kind"] == "test"]


def test_strict_criterion_names_missing_optional_dependencies(lifecycle):
    (lifecycle.target / "test_optional.py").write_text(
        "import pytest\npytest.importorskip('anchor_absent_dependency')\n"
        "def test_never():\n    assert True\n"
    )
    _git(lifecycle.target, "add", "test_optional.py")
    _git(lifecycle.target, "commit", "-m", "optional test")
    _implemented(lifecycle, criteria_targets=("test_pkg.py", "test_optional.py"))
    result = lifecycle.anchor("test", target=["test_pkg.py", "test_optional.py"], workflow_criterion="ok",
                              output_format="dict")
    assert result["workflow_measurement"]["status"] == "failed"
    assert result["workflow_measurement"]["missing_dependencies"] == ["anchor_absent_dependency"]


def test_test_request_id_returns_retained_result_without_rerunning(lifecycle):
    """v0.3.24 had no way to retrieve a run whose client call timed out."""
    anchor = lifecycle.anchor
    counter = lifecycle.target / "runs.txt"
    (lifecycle.target / "test_counted.py").write_text(
        "from pathlib import Path\n"
        f"def test_counted():\n    Path({str(counter)!r}).open('a').write('run\\n')\n"
    )
    anchor("orient", output_format="dict")
    anchor("new_session", name="tests", inline=True, output_format="dict")
    anchor("task", "Run the fixture tests", goal="Observe fixture test results", mode="analysis", risk="low",
           rigor="direct", acceptance_criteria=["tests observed"], output_format="dict")
    first = anchor("test", target=["test_counted.py"], request_id="run-1", output_format="dict")
    second = anchor("test", target=["test_counted.py"], request_id="run-1", output_format="dict")
    assert first["request"]["replayed"] is False and second["request"]["replayed"] is True
    assert second["metrics"] == first["metrics"]
    assert counter.read_text().count("run") == 1
    with pytest.raises(ValueError, match="different test arguments") as caught:
        anchor("test", target=["test_pkg.py"], request_id="run-1", output_format="dict")
    assert caught.value.error_code == "test_request_id_conflict"


def test_replayed_measurement_must_match_the_workflow_record(lifecycle):
    anchor = lifecycle.anchor
    _implemented(lifecycle)
    first = anchor("test", target=["test_pkg.py"], workflow_criterion="ok", request_id="m-1",
                   output_format="dict")
    assert first["workflow_measurement"]["status"] == "satisfied"
    retry = anchor("test", target=["test_pkg.py"], workflow_criterion="ok", request_id="m-1",
                   output_format="dict")
    assert retry["request"]["replayed"] is True
    advance(anchor, "implemented")  # A fresh candidate clears the recorded measurements.
    with pytest.raises(ValueError, match="no longer matches workflow") as caught:
        anchor("test", target=["test_pkg.py"], workflow_criterion="ok", request_id="m-1",
               output_format="dict")
    assert caught.value.error_code == "test_request_id_conflict"


def test_delivery_timeout_minutes_is_bounded_and_forwarded(lifecycle, monkeypatch):
    """v0.3.24 rejected timeout_minutes on the workflow action (fixed 5-minute window)."""
    from odibi_anchor import human_input, human_input_owner

    anchor = lifecycle.anchor
    _git(lifecycle.target, "remote", "add", "origin", "https://github.com/acme/app.git")
    _implemented(lifecycle)
    anchor("test", target=["test_pkg.py"], workflow_criterion="ok", output_format="dict")
    advance(anchor, "review", findings=[])
    anchor("preflight", output_format="dict")
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    anchor("learning", "assess", outcome="nothing_reusable_learned",
           notes="Negative paths in this fixture are deliberate.", output_format="dict")
    assert advance(anchor, "qualify")["progress"] == "qualified"
    prepared = anchor("workflow", "prepare_delivery", output_format="dict")
    calls = []
    provider = SimpleNamespace(expected_owner_id="owner", assurance="fixture",
                               transport=SimpleNamespace(name="fixture"))
    monkeypatch.setattr(human_input_owner, "select_owner_approval_provider", lambda: provider)

    def respond(message, **kwargs):
        calls.append(kwargs["timeout_minutes"])
        return SimpleNamespace(response=prepared["approval_response"], response_user_id="owner",
                               transport="fixture", request_id="approval", response_message_id="message")

    monkeypatch.setattr(human_input, "request_human_input_record", respond)
    for invalid in (0, 241, 2.5, True):
        with pytest.raises(ValueError, match="from 1 to 240"):
            advance(anchor, "request_delivery_approval", timeout_minutes=invalid)
    assert calls == []
    assert advance(anchor, "request_delivery_approval", timeout_minutes=90)["progress"] == "approved_for_delivery"
    assert calls == [90]


# ── Item 11: owner-free abandonment of an unrestorable orphan ──

def test_unrestorable_orphan_can_be_abandoned_without_authority(lifecycle):
    """v0.3.24 could neither rebind nor close a task whose base was rewritten."""
    _, task = lifecycle.producer(lifecycle.paths[:1])
    window = task["task_window_id"]
    anchor = lifecycle.restart()
    with pytest.raises(RuntimeError) as restorable:
        anchor("task_rebind", task_window_id=window, abandon=True, reason="not yet orphaned",
               output_format="dict")
    assert restorable.value.error_code == "task_restorable"
    (lifecycle.target / "extra.txt").write_text("x\n")
    _git(lifecycle.target, "add", "extra.txt")
    _git(lifecycle.target, "commit", "--amend", "-m", "rewritten base")
    with pytest.raises(RuntimeError, match="could not be restored") as unrestorable:
        anchor("task_rebind", task_window_id=window, output_format="dict")
    assert unrestorable.value.error_code == "task_authority_unrestorable"
    assert unrestorable.value.next_operation["kwargs"]["abandon"] is True
    abandoned = anchor("task_rebind", task_window_id=window, abandon=True,
                       reason="Base was rewritten; work restarts from a fresh producer.", output_format="dict")
    assert abandoned["status"] == "abandoned" and abandoned["authority_granted"] is False
    from odibi_anchor._utils._session_state import _SESSION_STATE

    assert _SESSION_STATE.task_window_id != window and _SESSION_STATE.active_task_profile is None
    with pytest.raises(RuntimeError, match="no open accepted task"):
        anchor("task_rebind", output_format="dict")


# ── Items 12 and 14: review and learning diagnostics ──

def test_review_reports_workflow_measured_criteria_instead_of_unverified():
    from odibi_anchor.codebase.review_context import review_context

    diff = {"metrics": {"files_changed": 1, "total_additions": 1, "total_deletions": 0},
            "samples": {"per_file": {"pkg/a.py": {"diff": "+VALUE = 2", "additions": 1, "deletions": 0}}}}
    result = review_context(
        ".", session_diff=diff, test_timings=[{"action": "test"}],
        acceptance_criteria=["Fixture tests pass", "Documentation explains rollout"],
        workflow_criteria=[{"id": "ok", "expected": "Fixture tests pass", "status": "satisfied"}],
        output_format="dict",
    )
    unverified = [item for item in result["findings"] if item.startswith("UNVERIFIED CRITERION")]
    assert unverified == ["UNVERIFIED CRITERION: 'Documentation explains rollout' — no matching change found in diff."]


def test_review_rejects_caller_supplied_workflow_criteria(lifecycle):
    anchor = lifecycle.anchor
    lifecycle.producer(lifecycle.paths[:1])
    with pytest.raises(ValueError, match="never caller-supplied"):
        anchor("review", workflow_criteria=[{"id": "ok", "status": "satisfied"}], output_format="dict")


def test_learning_capture_errors_list_required_fields_and_summary_reason(lifecycle):
    """v0.3.24 answered only 'invalid signal_key'/'invalid summary'."""
    anchor = lifecycle.anchor
    anchor("orient", output_format="dict")
    anchor("new_session", name="learn", inline=True, output_format="dict")
    anchor("task", "Read modules", goal="Inspect modules", mode="analysis", risk="low",
           rigor="direct", acceptance_criteria=["read"], output_format="dict")
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    with pytest.raises(ValueError, match="missing required field") as missing:
        anchor("learning", "capture", observation_type="friction", summary="A friction", output_format="dict")
    assert missing.value.error_code == "learning_capture_fields_missing"
    assert missing.value.context["missing_fields"] == ["signal_key", "evidence"]
    with pytest.raises(ValueError, match="absolute host path"):
        anchor("learning", "capture", observation_type="friction",
               summary="Edited /home/user/repo/pkg/mod_00.py", signal_key="fixture.friction",
               evidence=[{"reference_type": "file", "reference": "pkg/mod_00.py"}], output_format="dict")


def test_pytest_runner_reports_bounded_skip_reasons(tmp_path):
    from odibi_anchor.pytest_runner import run_pytest

    (tmp_path / "test_skips.py").write_text(
        "import pytest\npytest.importorskip('anchor_absent_dependency')\n"
    )
    summary, _ = run_pytest(["test_skips.py"], cwd=tmp_path, capture_output=True)
    assert summary["skipped"] == 1
    assert any("anchor_absent_dependency" in reason for reason in summary["skip_reasons"])
