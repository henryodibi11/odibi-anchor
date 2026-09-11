"""Phase-2 BPS, policy-context, and ledger acceptance tests."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._effects import (
    EFFECT_PERMISSIONS,
    dispatch_succeeded,
)
from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch
from odibi_anchor._utils._session_state import (
    SessionState,
    is_managed_artifact_path,
    record_evidence,
)
from odibi_anchor.planning._task_policy import (
    BpsKernel,
    EvidenceEntry,
    ManagedArtifactEntry,
    TaskPolicyContext,
    TaskPolicySet,
    build_task_policy_context,
    effective_required_evidence,
    evaluate_task_policies,
)
from odibi_anchor.planning._task_profile import EvidenceRequest, normalize_task_profile


@pytest.mark.parametrize("rigor,problem", [("direct", "not_required"), ("compact", "not_required"), ("full", "required")])
@pytest.mark.parametrize("execution,trait,spec", [
    ("read_only", (), "not_required"), ("source_change", (), "recommended"),
    ("source_change", ("schema-change",), "required"),
])
def test_problem_and_spec_are_cartesian(rigor, problem, execution, trait, spec):
    profile = normalize_task_profile(rigor=rigor, execution_mode=execution, traits=trait)
    context = TaskPolicyContext(profile, BpsKernel("p", "o"))
    policy = evaluate_task_policies(profile, context)
    assert policy.problem_record.disposition == problem
    assert policy.specification.disposition == spec
    assert policy.pr_readiness is None


@pytest.mark.parametrize("profile_kwargs,phase_count", [
    ({"execution_mode": "source_change", "risk": "high"}, 1),
    ({"execution_mode": "source_change", "risk": "medium"}, 2),
    ({"execution_mode": "data_change", "risk": "medium"}, 1),
])
def test_material_mutations_require_specification(profile_kwargs, phase_count):
    profile = normalize_task_profile(**profile_kwargs)
    context = TaskPolicyContext(
        profile, BpsKernel("p", "o"), phase_count=phase_count,
    )

    assert evaluate_task_policies(profile, context).specification.disposition == "required"


def test_work_items_require_team_visible_intent():
    profile = normalize_task_profile(legacy_mode="implementation")
    policy = evaluate_task_policies(profile, TaskPolicyContext(profile, BpsKernel("p", "o")))
    assert policy.work_items.disposition == "not_required"
    assert policy.work_items.depth == "none"


def test_ordinary_team_visible_work_recommends_one_draft():
    profile = normalize_task_profile(legacy_mode="implementation", traits=["team-visible"])
    policy = evaluate_task_policies(profile, TaskPolicyContext(profile, BpsKernel("p", "o")))
    assert policy.work_items.disposition == "recommended"
    assert policy.work_items.depth == "single"


def test_complex_team_visible_work_uses_outcome_set_without_step_ticketing():
    profile = normalize_task_profile(legacy_mode="implementation", traits=["team-visible"])
    context = TaskPolicyContext(profile, BpsKernel("complex problem", "team outcome"), phase_count=3)
    policy = evaluate_task_policies(profile, context)
    assert policy.work_items.disposition == "recommended"
    assert policy.work_items.depth == "set"


def test_high_risk_team_visible_work_requires_one_draft():
    profile = normalize_task_profile(legacy_mode="implementation", risk="high", traits=["team-visible"])
    policy = evaluate_task_policies(profile, TaskPolicyContext(profile, BpsKernel("p", "o")))
    assert policy.work_items.disposition == "required"
    assert policy.work_items.depth == "single"


def test_bps_text_alone_does_not_force_work_item_decomposition():
    profile = normalize_task_profile(legacy_mode="implementation")
    context = TaskPolicyContext(profile, BpsKernel("complex multi-stage problem", "team outcome"))
    policy = evaluate_task_policies(profile, context)
    assert policy.work_items.depth == "none"


def test_caller_required_work_item_uses_complex_task_depth():
    evidence = EvidenceRequest("ticket", "work-item", "Link team work", "final")
    profile = normalize_task_profile(caller_required_evidence=[evidence])
    context = TaskPolicyContext(profile, BpsKernel("p", "o"), phase_count=2)
    policy = evaluate_task_policies(profile, context)
    assert policy.work_items.disposition == "required"
    assert policy.work_items.depth == "set"


def test_accepted_task_exposes_stable_work_item_policy():
    profile = normalize_task_profile(execution_mode="read_only", traits=["team-visible"])
    kernel = BpsKernel("p", "o")
    context = TaskPolicyContext(profile, kernel)
    state = SessionState()
    result = {"readiness": {"score": 100}, "task_profile": profile.to_dict()}
    run_post_dispatch(
        "task", result, None, ("team ticket",), {"mode": "implementation"},
        session_timings=[{"action": "task", "error": None}],
        session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(),
        task_stage={"context": context, "profile": profile, "bps_kernel": kernel,
                    "referenced_facts": (), "phase_count": 1, "current_phase": 1},
    )
    assert result["work_item_policy"] == {
        "disposition": "recommended",
        "depth": "single",
        "rule_ids": ["work-item.team-visible"],
        "reasons": ["Team-visible ordinary work recommends one durable work-item draft."],
    }


def test_task_policy_set_preserves_former_fourth_positional_argument():
    decision = evaluate_task_policies(
        normalize_task_profile(),
        TaskPolicyContext(normalize_task_profile(), BpsKernel("p", "o")),
    ).problem_record
    policies = TaskPolicySet(decision, decision, decision, decision)
    assert policies.pr_readiness is decision
    assert policies.work_items.depth == "none"


def test_context_is_a_deep_immutable_snapshot():
    provenance = {"nested": ["old"]}
    entry = EvidenceEntry("e", "test", "pass", "caller", "2026-01-01T00:00:00+00:00", provenance)
    profile = normalize_task_profile()
    context = TaskPolicyContext(profile, BpsKernel("p", "o"), current_evidence_entries=(entry,),
                                active_runtime_identifiers={"nested": {"value": [1]}})
    provenance["nested"].append("new")
    assert context.current_evidence_entries[0].provenance["nested"] == ("old",)
    with pytest.raises(TypeError):
        context.active_runtime_identifiers["x"] = "y"


def test_fresh_builder_has_no_policy_cache_and_snapshots_all_ledgers():
    profile = normalize_task_profile()
    state = SimpleNamespace(bps_kernel=BpsKernel("p", "o"), artifact_root="a", target_root="t",
                            active_problem=None, active_spec_name=None, managed_artifact_ledger=[],
                            referenced_facts=(), evidence_ledger=[], guidance_attestations=[],
                            observed_effects=[], intended_pr_paths=(), session_id="s", active_project="p",
                            repository_snapshot=None, repository_pr_config=None)
    first = build_task_policy_context(profile, session_state=state, current_action="task")
    state.evidence_ledger.append(EvidenceEntry("e", "test", "pass", "tool", "now", {}))
    second = build_task_policy_context(profile, session_state=state, current_action="gate", checkpoint_final=True)
    assert first.current_evidence_entries == ()
    assert len(second.current_evidence_entries) == 1
    assert second.current_action == "gate" and second.checkpoint_final
    assert not hasattr(state, "task_policy_set")


def test_caller_evidence_remains_distinct_from_effective_evidence():
    request = EvidenceRequest("caller", "test", "run it", "gate")
    profile = normalize_task_profile(caller_required_evidence=[request])
    policies = evaluate_task_policies(profile, TaskPolicyContext(profile, BpsKernel("p", "o")))
    assert profile.caller_required_evidence == (request,)
    assert effective_required_evidence(profile, policies) == (request,)


def test_exact_colocated_managed_paths_do_not_broaden_root(tmp_path):
    root = str(tmp_path)
    for path in (
        "PROJECT.md", "source/input.md", "notebooks/EVD-1.md", "problems/PRB-1.md",
        "specs/X_SPEC.md", "work_items/WI-1.md", "decisions/DEC-1.md", "archive/old.md",
    ):
        assert is_managed_artifact_path(path, artifact_root=root, target_root=root)
    assert not is_managed_artifact_path("src/problems.py", artifact_root=root, target_root=root)
    assert not is_managed_artifact_path("user_specs/X.md", artifact_root=root, target_root=root)


def test_only_persisted_linked_spec_suppresses_ordinary_full_rigor_problem():
    profile = normalize_task_profile(
        work_type="change", rigor="full", execution_mode="source_change",
    )
    base = TaskPolicyContext(profile, BpsKernel("p", "o"))
    linked = replace(base, linked_problem="PRB-1", linked_spec="X")
    assert evaluate_task_policies(profile, base) == evaluate_task_policies(profile, linked)
    persisted = replace(base, linked_spec="X", persisted_spec=True)
    assert evaluate_task_policies(profile, persisted).problem_record.disposition == "not_required"
    explicit = replace(persisted, explicit_problem_requested=True)
    assert evaluate_task_policies(profile, explicit).problem_record.disposition == "required"
    investigation = normalize_task_profile(
        work_type="investigate", rigor="full", execution_mode="source_change",
    )
    assert evaluate_task_policies(
        investigation, replace(base, profile=investigation, linked_spec="X", persisted_spec=True),
    ).problem_record.disposition == "required"


def test_ledgers_have_distinct_typed_entries():
    evidence = EvidenceEntry("e", "test", "unknown", "caller", "now", {})
    artifact = ManagedArtifactEntry("specs/X.md", "spec", "now", {})
    assert not isinstance(evidence, ManagedArtifactEntry)
    assert not isinstance(artifact, EvidenceEntry)


@pytest.mark.parametrize(("action", "result", "expected"), [
    ("status", {"kind": "status"}, True),
    ("status", {"error": "boom"}, False),
    ("gate", {"passed": True}, True),
    ("gate", {"passed": False}, False),
    ("checkpoint", {"metrics": {"overall_pass": True}}, True),
    ("checkpoint", {"metrics": {"overall_pass": False}}, False),
    ("test", {"metrics": {"exit_code": 0}}, True),
    ("test", {"metrics": {"exit_code": 1}}, False),
    ("preflight", {"metrics": {"errors": 0}}, True),
    ("preflight", {"metrics": {"errors": 1}}, False),
    ("task", {"readiness": {"score": 39}}, False),
    ("task", {"readiness": {"score": 40}}, True),
    ("problem", {"status": "blocked"}, False),
    ("reject", {"status": "rejected", "action": "rejected"}, True),
    ("status", {"status": "rejected"}, False),
])
def test_dispatch_succeeded_action_matrix(action, result, expected):
    assert dispatch_succeeded(action, result) is expected
    assert dispatch_succeeded(action, result, RuntimeError("dispatch failed")) is False


@pytest.mark.parametrize("field,bad", [
    ("linked_managed_artifacts", ({"path": "specs/X.md"},)),
    ("current_evidence_entries", ({"id": "e"},)),
    ("guidance_attestations", ({"id": "a"},)),
    ("referenced_facts", (object(),)),
    ("source_files_changed", (1,)),
])
def test_policy_context_rejects_wrong_typed_deep_entries(field, bad):
    profile = normalize_task_profile()
    with pytest.raises(TypeError):
        TaskPolicyContext(profile, BpsKernel("p", "o"), **{field: bad})


def test_policy_context_rejects_permission_and_phase_mismatches():
    profile = normalize_task_profile()
    with pytest.raises(ValueError, match="permissions"):
        TaskPolicyContext(profile, BpsKernel("p", "o"), current_effect="read")
    with pytest.raises(ValueError, match="current_phase"):
        TaskPolicyContext(profile, BpsKernel("p", "o"), current_phase=2, phase_count=1)
    with pytest.raises(TypeError, match="integers"):
        TaskPolicyContext(profile, BpsKernel("p", "o"), current_phase=True)


def test_fresh_context_exposes_each_changed_policy_fact():
    profile = normalize_task_profile()
    state = SessionState()
    state.bps_kernel = BpsKernel("old", "old outcome")
    state.referenced_facts = ("new fact",)
    state.evidence_ledger = [EvidenceEntry("e", "test", "pass", "runner", "now", {})]
    state.guidance_attestations = [EvidenceEntry("a", "guidance", "pass", "caller", "now", {})]
    state.observed_effects = ["read"]
    state.linked_problem = "PRB-7"
    state.current_phase, state.phase_count = 2, 3
    context = build_task_policy_context(
        profile, session_state=state, current_action="checkpoint",
        current_effect="source_write",
        current_effect_permissions=EFFECT_PERMISSIONS["source_write"],
        bps_kernel=BpsKernel("new", "new outcome"), checkpoint_final=True,
    )
    assert context.current_action == "checkpoint"
    assert context.current_effect == "source_write" and context.checkpoint_final
    assert context.linked_problem == "PRB-7"
    assert (context.current_phase, context.phase_count) == (2, 3)
    assert context.referenced_facts == ("new fact",)
    assert context.current_evidence_entries[0].id == "e"
    assert context.guidance_attestations[0].id == "a"
    assert context.observed_effects == ("read",)
    assert context.bps_kernel.problem == "new"


def test_evidence_upsert_keeps_exactly_one_current_entry():
    state = SessionState()
    record_evidence(state, id="tests", kind="test", status="fail", source="pytest")
    record_evidence(state, id="tests", kind="test", status="pass", source="pytest")
    assert len(state.evidence_ledger) == 1
    assert state.evidence_ledger[0].status == "pass"
