"""Candidate identity and runtime task separation are not caller attestations."""

from pathlib import Path

import pytest

from odibi_anchor._dispatcher._workflow_admission import bind_workflow, workflow_owner
from odibi_anchor._dispatcher._workflow_evidence import (
    bind_review,
    collect_candidate,
    collect_review,
    collect_test_measurement,
    qualify_recorded,
    runtime_environment,
)
from odibi_anchor.codebase._task_authority import persist_accepted_task
from odibi_anchor.codebase._workflow import WorkflowError, create_workflow, transition_workflow
from odibi_anchor.planning._task_profile import normalize_task_profile
from tests.codebase.test_task_authority import fresh_state, result, state


@pytest.fixture
def work(tmp_path, request):
    producer = state(tmp_path)
    producer.trust_domain = "personal"
    mode = getattr(request, "param", "source_change")
    producer.active_task_profile = normalize_task_profile(
        work_type="change", execution_mode=mode, risk="high", rigor="full",
    )
    plan = {"schema_version": 1, "goal": "Change source constant", "risk": "high",
            "execution_mode": mode, "scope": ["constant"], "source_paths": ["source.py"],
            "artifact_paths": ["notebooks/result.md"],
            "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
            "unresolved_decisions": [], "destination": {"kind": "git", "ref": "main"},
            "criteria": [{"id": "constant", "expected": "VALUE is 2", "method": "pytest",
                          "test_targets": ["tests/test_constant.py"]}]}
    db = tmp_path / "authority.db"
    workflow = create_workflow(db, owner=workflow_owner(producer), request_id="create", plan=plan)
    producer.workflow_binding = bind_workflow(db, session_state=producer, workflow_id=workflow["workflow_id"])
    persist_accepted_task(db, session_state=producer, task_stage={"trust_domain": "personal"}, task_result=result())
    workflow = transition_workflow(
        db, owner=workflow_owner(producer), workflow_id=workflow["workflow_id"],
        expected_generation=0, request_id="plan", operation="accept_plan",
        payload={"baseline": {"task_window_id": producer.task_window_id}, "authority_ref": "owner:fixture"},
    )
    if mode == "source_change":
        (Path(producer.target_root) / "source.py").write_text("VALUE = 2\n")
    else:
        artifact = Path(producer.artifact_root) / "notebooks/result.md"
        artifact.parent.mkdir()
        artifact.write_text("Result: 2\n")
    candidate = collect_candidate(db, session_state=producer, workflow_id=workflow["workflow_id"])
    workflow = transition_workflow(
        db, owner=workflow_owner(producer), workflow_id=workflow["workflow_id"],
        expected_generation=1, request_id="implemented", operation="implemented",
        payload={"candidate": candidate},
    )
    return db, producer, workflow


def reviewer(work, *, mode="review", binding_changes=None):
    db, producer, workflow = work
    observer = fresh_state(
        producer, task_window_id="ltw_review", session_id="review-session",
        active_task_profile=normalize_task_profile(legacy_mode=mode), workflow_binding=None,
        bps_kernel=producer.bps_kernel,
    )
    if mode == "review":
        observer.workflow_binding = bind_review(db, session_state=observer, workflow_id=workflow["workflow_id"])
        observer.workflow_binding.update(binding_changes or {})
    persist_accepted_task(db, session_state=observer, task_stage={"trust_domain": "personal"}, task_result=result())
    return observer


def test_source_candidate_uses_observed_identity_and_changes_with_bytes(work):
    db, producer, workflow = work
    first = workflow["candidate"]
    assert first["producer"] == producer.task_window_id
    assert first["kind"] == "git"
    assert first["snapshot"]["changed_paths"] == ["source.py"]
    (Path(producer.target_root) / "source.py").write_text("VALUE = 3\n")
    changed = collect_candidate(db, session_state=producer, workflow_id=workflow["workflow_id"])
    assert changed["identity"] != first["identity"]


def test_out_of_band_scope_expansion_cannot_be_qualified(work):
    db, producer, workflow = work
    (Path(producer.target_root) / "outside.py").write_text("SECRET = 4\n")
    with pytest.raises(WorkflowError, match="exceed the accepted plan"):
        collect_candidate(db, session_state=producer, workflow_id=workflow["workflow_id"])


def test_same_task_cannot_supply_high_risk_review(work):
    db, producer, workflow = work
    with pytest.raises(WorkflowError, match="cannot review its own"):
        collect_review(db, session_state=producer, workflow_id=workflow["workflow_id"], findings=[])


def test_review_records_task_separation_without_inventing_authentication(work):
    db, _, workflow = work
    observer = reviewer(work)
    review = collect_review(db, session_state=observer, workflow_id=workflow["workflow_id"], findings=[])
    assert review["kind"] == "independent"
    assert review["reviewer"] == "ltw_review"
    assert review["independence"] == "separate_read_only_accepted_task"
    assert review["separate_read_only_accepted_task"] is True
    assert review["reviewer_authentication"] == "none"
    assert review["distinct_session"] is True
    assert review["evidence_kind"] == "agent_judgment"


@pytest.mark.parametrize("field", ["plan_sha256", "review_candidate_sha256"])
def test_review_task_cannot_accept_wrong_subject(work, field):
    from odibi_anchor.codebase._workflow import WorkflowError

    with pytest.raises(WorkflowError, match="exact plan and candidate"):
        reviewer(work, binding_changes={field: "other"})


def test_review_task_cannot_accept_unknown_workflow(work):
    from odibi_anchor.codebase._workflow import WorkflowError

    with pytest.raises(WorkflowError, match="workflow not found"):
        reviewer(work, binding_changes={"workflow_id": "other"})


def test_write_capable_reviewer_is_not_independent(work):
    db, _, workflow = work
    observer = reviewer(work, mode="implementation")
    with pytest.raises(WorkflowError, match="read-only verification task"):
        collect_review(db, session_state=observer, workflow_id=workflow["workflow_id"], findings=[])


def test_review_rejects_candidate_change_after_implementation(work):
    db, producer, workflow = work
    observer = reviewer(work)
    (Path(producer.target_root) / "source.py").write_text("VALUE = 4\n")
    with pytest.raises(WorkflowError, match="candidate changed"):
        collect_review(db, session_state=observer, workflow_id=workflow["workflow_id"], findings=[])


def test_open_review_finding_remains_failed(work):
    db, _, workflow = work
    review = collect_review(db, session_state=reviewer(work), workflow_id=workflow["workflow_id"],
                            findings=[{"summary": "Missing boundary case", "status": "open"}])
    assert review["status"] == "failed"


@pytest.mark.parametrize("changes", [{}, {"skipped": 1}, {"passed": 0}, {"failed": 1},
                                     {"errors": 1}, {"timed_out": True}, {"exit_code": 2}])
def test_measurements_preserve_unsatisfied_results(work, changes):
    _, _, workflow = work
    metrics = {"passed": 3, "failed": 0, "errors": 0, "skipped": 0, "exit_code": 0, "timed_out": False}
    metrics.update(changes)
    candidate = workflow["candidate"]
    check = collect_test_measurement(state=workflow, before=candidate, after=candidate,
                                     targets=["tests/test_constant.py"], environment_before=runtime_environment(),
                                     criterion_id="constant", result={"kind": "test_run", "metrics": metrics})
    assert check["status"] == ("failed" if changes else "satisfied")
    assert check["counts"]["passed"] == metrics["passed"]


def test_measurements_require_complete_counts_and_stable_candidate(work):
    _, _, workflow = work
    candidate = workflow["candidate"]
    with pytest.raises(WorkflowError, match="complete exact counts"):
        collect_test_measurement(state=workflow, before=candidate, after=candidate,
                                 targets=["tests/test_constant.py"], environment_before=runtime_environment(),
                                 criterion_id="constant", result={"kind": "test_run", "metrics": {"exit_code": 0}})
    with pytest.raises(WorkflowError, match="changed across"):
        collect_test_measurement(state=workflow, before=candidate, after={**candidate, "identity": "other"},
                                 targets=["tests/test_constant.py"], environment_before=runtime_environment(),
                                 criterion_id="constant", result={})


def retain_evidence(work, *, skipped=0):
    db, producer, workflow = work
    candidate = workflow["candidate"]
    measurement = collect_test_measurement(
        state=workflow, before=candidate, after=candidate, criterion_id="constant",
        targets=["tests/test_constant.py"], environment_before=runtime_environment(),
        result={"kind": "test_run", "metrics": {"passed": 2, "failed": 0, "errors": 0,
                                                 "skipped": skipped, "exit_code": 0, "timed_out": False}},
    )
    transition_workflow(db, owner=workflow_owner(producer), workflow_id=workflow["workflow_id"],
                        expected_generation=2, request_id="measurement", operation="record_check", payload=measurement)
    review = collect_review(db, session_state=reviewer(work), workflow_id=workflow["workflow_id"], findings=[])
    return transition_workflow(db, owner=workflow_owner(producer), workflow_id=workflow["workflow_id"],
                               expected_generation=3, request_id="review", operation="record_review", payload=review)


def test_only_retained_fresh_evidence_qualifies_and_retry_is_idempotent(work):
    db, producer, workflow = work
    with pytest.raises(WorkflowError, match="no retained review"):
        qualify_recorded(db, session_state=producer, workflow_id=workflow["workflow_id"],
                          expected_generation=2, request_id="qualify")
    retain_evidence(work)
    first = qualify_recorded(db, session_state=producer, workflow_id=workflow["workflow_id"],
                              expected_generation=4, request_id="qualify")
    repeated = qualify_recorded(db, session_state=producer, workflow_id=workflow["workflow_id"],
                                 expected_generation=4, request_id="qualify")
    assert first == repeated
    assert first["progress"] == "qualified"
    assert first["completed"] is False
    assert first["qualification"]["review"]["reviewer_authentication"] == "none"


def test_retained_skips_block_qualification(work):
    from odibi_anchor.codebase._workflow import WorkflowError

    db, producer, workflow = work
    retained = retain_evidence(work, skipped=1)
    assert retained["measurements"]["constant"]["status"] == "failed"
    with pytest.raises(WorkflowError, match="required evidence is not satisfied"):
        qualify_recorded(db, session_state=producer, workflow_id=workflow["workflow_id"],
                          expected_generation=4, request_id="qualify")


def test_measurement_rejects_wrong_test_targets(work):
    _, _, workflow = work
    candidate = workflow["candidate"]
    with pytest.raises(WorkflowError, match="test targets differ"):
        collect_test_measurement(
            state=workflow, before=candidate, after=candidate, criterion_id="constant",
            targets=["tests/test_unrelated.py"], environment_before=runtime_environment(), result={},
        )


def test_measurement_rejects_environment_change(work):
    _, _, workflow = work
    candidate = workflow["candidate"]
    with pytest.raises(WorkflowError, match="environment changed"):
        collect_test_measurement(
            state=workflow, before=candidate, after=candidate, criterion_id="constant",
            targets=["tests/test_constant.py"], environment_before={"python": "other"}, result={},
        )


def test_qualification_rejects_environment_change(work, monkeypatch):
    from odibi_anchor._dispatcher import _workflow_evidence
    from odibi_anchor.codebase._workflow import WorkflowError

    db, producer, workflow = work
    retain_evidence(work)
    monkeypatch.setattr(_workflow_evidence, "runtime_environment", lambda: {"python": "other"})
    with pytest.raises(WorkflowError, match="differs from retained measurement"):
        _workflow_evidence.qualify_recorded(db, session_state=producer, workflow_id=workflow["workflow_id"],
                                           expected_generation=4, request_id="qualify")


def test_new_implementation_clears_previous_measurements_and_review(work):
    db, producer, workflow = work
    retain_evidence(work)
    (Path(producer.target_root) / "source.py").write_text("VALUE = 9\n")
    candidate = collect_candidate(db, session_state=producer, workflow_id=workflow["workflow_id"])
    changed = transition_workflow(db, owner=workflow_owner(producer), workflow_id=workflow["workflow_id"],
                                  expected_generation=4, request_id="new-candidate", operation="implemented",
                                  payload={"candidate": candidate})
    assert changed["measurements"] == {}
    assert changed["review_result"] is None
    assert changed["candidate"]["identity"] != workflow["candidate"]["identity"]


@pytest.mark.parametrize("work", ["artifact_only"], indirect=True)
def test_managed_artifact_candidate_observes_bytes_and_rejects_links(work, tmp_path):
    db, producer, workflow = work
    assert workflow["candidate"]["kind"] == "managed_artifacts"
    artifact = Path(producer.artifact_root) / "notebooks/result.md"
    artifact.write_text("Result: 9\n")
    changed = collect_candidate(db, session_state=producer, workflow_id=workflow["workflow_id"])
    assert changed["identity"] != workflow["candidate"]["identity"]
    artifact.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("Not managed\n")
    artifact.symlink_to(outside)
    with pytest.raises(WorkflowError, match="managed regular artifacts"):
        collect_candidate(db, session_state=producer, workflow_id=workflow["workflow_id"])
