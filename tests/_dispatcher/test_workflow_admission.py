"""Phase admission supplements (never expands) accepted execution authority."""

import hashlib
import json

import pytest

from odibi_anchor._dispatcher._workflow_admission import (
    bind_workflow,
    bound_workflow,
    enforce_workflow_admission,
    workflow_owner,
    workflow_packet,
)
from odibi_anchor._utils._session_state import SessionState, reset_task_policy_state
from odibi_anchor.codebase._workflow import WorkflowError, create_workflow, transition_workflow
from odibi_anchor.planning._task_profile import normalize_task_profile


@pytest.fixture
def runtime(tmp_path):
    session = SessionState(
        active_project="project-a", trust_domain="personal", task_window_id="ltw_a",
        anchor_home=str(tmp_path / "home"), artifact_root=str(tmp_path / "artifacts"),
        target_root=str(tmp_path / "target"),
        active_task_profile=normalize_task_profile(
            work_type="change", execution_mode="source_change", risk="low", rigor="compact",
        ),
    )
    plan = {"schema_version": 1, "goal": "Reject duplicate keys", "risk": "low",
            "execution_mode": "source_change", "scope": ["src/parser.py"],
            "source_paths": ["src/parser.py"],
            "exclusions": ["storage"], "constraints": [], "risks": [],
            "stop_conditions": ["changed data semantics"], "unresolved_decisions": [],
            "criteria": [{"id": "duplicates", "expected": "Repeated keys fail", "method": "pytest"}],
            "destination": {"kind": "git", "ref": "main"}}
    db = tmp_path / "workflow.db"
    state = create_workflow(db, owner=workflow_owner(session), request_id="start", plan=plan)
    session.workflow_binding = bind_workflow(db, session_state=session, workflow_id=state["workflow_id"])
    return db, session


def advance(runtime, operation, payload):
    db, session = runtime
    state = bound_workflow(db, session_state=session)
    return transition_workflow(
        db, owner=workflow_owner(session), workflow_id=state["workflow_id"],
        expected_generation=state["generation"], request_id=f"{operation}:{state['generation']}",
        operation=operation, payload=payload,
    )


def admit(runtime):
    return advance(runtime, "accept_plan", {"baseline": {"head": "a" * 40}, "authority_ref": "owner:request"})


@pytest.mark.parametrize("effects", [("source_write",), ("read", "source_write")])
def test_draft_blocks_implementation_but_accepted_plan_admits(runtime, effects):
    db, session = runtime
    with pytest.raises(WorkflowError, match="accepted active plan"):
        enforce_workflow_admission(db, session_state=session, effects=effects)
    admit(runtime)
    enforce_workflow_admission(db, session_state=session, effects=effects,
                               source_targets=("src/parser.py",))


def test_plan_does_not_grant_external_delivery_authority(runtime):
    db, session = runtime
    admit(runtime)
    with pytest.raises(WorkflowError, match="never authorizes external"):
        enforce_workflow_admission(db, session_state=session, effects=("external_mutation",))


@pytest.mark.parametrize("field,value", [
    ("task_window_id", "ltw_other"), ("active_project", "other"),
    ("trust_domain", "employer"), ("target_root", "/different-target"),
    ("artifact_root", "/different-artifacts"), ("anchor_home", "/different-home"),
])
def test_binding_rejects_authority_drift(runtime, field, value):
    db, session = runtime
    admit(runtime)
    setattr(session, field, value)
    with pytest.raises(WorkflowError, match="does not match"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",))


def test_mode_cannot_be_broadened_by_binding(runtime):
    db, session = runtime
    state = bound_workflow(db, session_state=session)
    session.active_task_profile = normalize_task_profile(legacy_mode="analysis")
    with pytest.raises(WorkflowError, match="execution modes differ"):
        bind_workflow(db, session_state=session, workflow_id=state["workflow_id"])
    with pytest.raises(WorkflowError, match="does not match"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",))


def test_replan_invalidates_old_window_but_keeps_recovery_reads_available(runtime):
    db, session = runtime
    state = admit(runtime)
    advance(runtime, "replan", {"reason": "new requirement", "plan": {**state["plan"], "goal": "Allow duplicate keys"}})
    with pytest.raises(WorkflowError, match="plan changed"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",))
    enforce_workflow_admission(db, session_state=session, effects=("read",))
    enforce_workflow_admission(db, session_state=session, effects=("governance_write",))


@pytest.mark.parametrize("operation,payload", [
    ("block", {"kind": "unsafe", "reason": "Unbounded effect"}),
    ("cancel", {"reason": "Owner cancelled"}),
])
def test_nonactive_state_stops_implementation(runtime, operation, payload):
    db, session = runtime
    admit(runtime)
    advance(runtime, operation, payload)
    with pytest.raises(WorkflowError, match="accepted active plan"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",))


def test_missing_legacy_binding_does_not_create_workflow_or_prior_evidence(tmp_path):
    session = SessionState()
    db = tmp_path / "absent.db"
    packet = workflow_packet(db, session_state=session)
    assert packet["status"] == "legacy_unphased"
    assert packet["next_step"] == "enroll_at_safe_boundary"
    assert not db.exists()


def test_canonical_packet_preserves_content_and_is_not_authority(runtime):
    db, session = runtime
    state = bound_workflow(db, session_state=session)
    long_goal = "Meaningful complete requirement. " * 100
    updated = advance(runtime, "replan", {"reason": "More detail", "plan": {**state["plan"], "goal": long_goal}})
    session.workflow_binding = bind_workflow(db, session_state=session, workflow_id=updated["workflow_id"])
    packet = workflow_packet(db, session_state=session)
    assert packet["state"]["plan"]["goal"] == long_goal
    assert packet["next_step"] == "accept_plan"
    fingerprint = packet.pop("packet_sha256")
    assert fingerprint == "sha256:" + hashlib.sha256(json.dumps(
        packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    packet["binding"]["task_window_id"] = "forged"
    assert session.workflow_binding["task_window_id"] == "ltw_a"
    admit(runtime)
    assert workflow_packet(db, session_state=session)["next_step"] == "implement"


def test_session_reset_cannot_inherit_another_tasks_binding(runtime):
    _, session = runtime
    reset_task_policy_state(session)
    assert session.workflow_binding is None


def test_pre_dispatch_enforces_resolved_effect_before_handler(runtime, monkeypatch):
    from odibi_anchor._dispatcher._boot import _ENV
    from odibi_anchor._dispatcher._effects import ActionContract, InvocationSemantics
    from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement
    from odibi_anchor._dispatcher._workflow_admission import WorkflowError as DispatchWorkflowError

    db, session = runtime
    monkeypatch.setitem(_ENV, "memory_db", str(db))
    contract = ActionContract(
        frozenset({"source_write"}), frozenset({"task_required"}),
        lambda _args, _kwargs: InvocationSemantics("source_write", "task_required"),
    )
    with pytest.raises(DispatchWorkflowError, match="accepted active plan"):
        run_pre_dispatch_enforcement(
            "map", (), {}, session_state=session, session_timings=[],
            session_files_changed=set(), session_boot_manifest={},
            planning_required_actions=frozenset(), root=session.target_root,
            action_contract=contract,
        )


@pytest.mark.parametrize("binding", [{}, {"schema_version": True}, {"schema_version": 99}])
def test_corrupt_or_unknown_binding_fails_closed(runtime, binding):
    db, session = runtime
    session.workflow_binding = binding
    with pytest.raises(WorkflowError, match="unsupported or incomplete"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",))


@pytest.mark.parametrize("target", ["src/other.py", "src/parser.py/other", "../outside.py", "/outside.py"])
def test_plan_never_expands_to_another_source_path(runtime, target):
    db, session = runtime
    admit(runtime)
    with pytest.raises(WorkflowError, match=r"accepted plan|escapes"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",),
                                   source_targets=(target,))


def test_source_symlink_cannot_escape_target_root(runtime, tmp_path):
    db, session = runtime
    from pathlib import Path

    root = Path(session.target_root)
    root.mkdir()
    (root / "src").symlink_to(tmp_path, target_is_directory=True)
    admit(runtime)
    with pytest.raises(WorkflowError, match="escapes"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",),
                                   source_targets=("src/parser.py",))


def test_unknown_source_target_and_data_collector_fail_closed(runtime):
    db, session = runtime
    admit(runtime)
    with pytest.raises(WorkflowError, match="resolved targets"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",))
    with pytest.raises(WorkflowError, match="exact-resource collector"):
        enforce_workflow_admission(db, session_state=session, effects=("data_write",))


def test_qualified_candidate_is_frozen_but_readback_remains_available(runtime):
    from odibi_anchor.codebase._workflow import digest

    db, session = runtime
    admit(runtime)
    state = advance(runtime, "implemented", {
        "candidate": {"kind": "git", "identity": "b" * 40, "producer": "agent:implementer"},
    })
    binding = {"plan_sha256": state["plan_sha256"], "candidate_sha256": digest(state["candidate"])}
    advance(runtime, "qualify", {
        **binding, "checks": [{**binding, "criterion_id": "duplicates", "method": "pytest",
                               "status": "satisfied", "evidence_ref": "test:1", "collector": "pytest"}],
        "review": {**binding, "kind": "self", "reviewer": "agent:implementer",
                   "status": "satisfied", "evidence_ref": "review:1"},
    })
    with pytest.raises(WorkflowError, match="delivery freezes"):
        enforce_workflow_admission(db, session_state=session, effects=("source_write",),
                                   source_targets=("src/parser.py",))
    enforce_workflow_admission(db, session_state=session, effects=("read",))
    assert workflow_packet(db, session_state=session)["next_step"] == "request_delivery_authority"


@pytest.mark.parametrize("mode,traits,eligible", [
    ("data_change", [], True), ("data_change", ["source-change"], True),
    ("data_change", ["source-change", "data-change"], False),
    ("source_change", ["source-change", "data-change"], False),
    ("source_change", [], False), ("artifact_only", [], False), ("read_only", [], False),
])
def test_data_only_exception_is_effect_based_and_never_delivery_evidence(tmp_path, mode, traits, eligible):
    from odibi_anchor._dispatcher._workflow_admission import data_only_legacy_exception

    profile = normalize_task_profile(work_type="change", execution_mode=mode, traits=traits)
    session = SessionState(active_task_profile=profile)
    assert data_only_legacy_exception(profile) is eligible
    packet = workflow_packet(tmp_path / "absent.db", session_state=session)
    if eligible:
        assert packet["status"] == "unphased_unsupported_collector"
        assert packet["completed"] is False and packet["delivery_verified"] is False
        assert packet["compatibility_exception"]["id"] == "temporary_data_only_legacy"
        assert packet["compatibility_exception"]["removal_criteria"]
        assert "state" not in packet and "workflow_id" not in packet
        enforce_workflow_admission(tmp_path / "absent.db", session_state=session, effects=("data_write",))
        with pytest.raises(RuntimeError, match="never permits source"):
            enforce_workflow_admission(tmp_path / "absent.db", session_state=session,
                                       effects=("data_write", "source_write"))
    else:
        assert "compatibility_exception" not in packet
    assert not (tmp_path / "absent.db").exists()


def test_unbound_data_exception_rejects_resolved_source_effect_before_handler(tmp_path, monkeypatch):
    from odibi_anchor._dispatcher._boot import _ENV
    from odibi_anchor._dispatcher._effects import ActionContract, InvocationSemantics
    from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement

    monkeypatch.setitem(_ENV, "memory_db", str(tmp_path / "absent.db"))
    session = SessionState(active_task_profile=normalize_task_profile(legacy_mode="data"))
    contract = ActionContract(frozenset({"source_write"}), frozenset({"task_required"}),
                              lambda _a, _k: InvocationSemantics("source_write", "task_required"))
    with pytest.raises(RuntimeError, match="never permits source"):
        run_pre_dispatch_enforcement(
            "map", (), {}, session_state=session, session_timings=[], session_files_changed=set(),
            session_boot_manifest={}, planning_required_actions=frozenset(), root=str(tmp_path),
            action_contract=contract,
        )
