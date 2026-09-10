"""Representative no-help agent journeys through prepared and lifecycle responses."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._action_preparation import prepare_action
from odibi_anchor._dispatcher._agent_context import build_agent_context
from odibi_anchor._dispatcher._operating_protocol import build_operating_protocol
from odibi_anchor._dispatcher._project import RouteBinding
from odibi_anchor.planning._task_profile import normalize_task_profile


@pytest.fixture
def authority():
    profile = normalize_task_profile(
        work_type="change", execution_mode="source_change", risk="high", rigor="full",
    )
    state = SimpleNamespace(
        active_task_profile=profile,
        active_project="project-a",
        artifact_root="/artifacts/a",
        target_root="/work/a",
        session_id="session-a",
        task_window_id="ltw-a",
        linked_problem="PRB-2026-0001",
        linked_spec="SPEC_A",
        linked_work_item="WI-2026-0001",
        task_goal="Deliver verified agent context",
        routing_stale=False,
        learning_obligation_id=None,
        latest_closed_obligation_id=None,
        prior_learn_debt=False,
        task_verification_epoch=0,
        terminal_status=None,
        session_name="campaign",
        skills_loaded={"writing-specs"},
        task_repository_baseline=None,
        evidence_ledger=[],
        managed_artifact_ledger=[],
        continuity_status="ready",
        runtime_current_revision="a" * 40,
        runtime_current_fingerprint="sha256:" + "b" * 64,
    )
    binding = RouteBinding(
        project_id="project-a",
        target_root="/work/a",
        artifact_root="/artifacts/a",
        anchor_home="/anchor",
        binding_source="explicit",
        runtime_instance_id="mcp:test",
    )
    return state, binding


def test_representative_operations_need_no_help_or_schema_discovery(authority):
    state, binding = authority
    requests = {
        "task.create": {
            "task": "Implement parser",
            "goal": "Reject malformed input",
            "mode": "implementation",
            "acceptance_criteria": ["Malformed input is rejected"],
        },
        "problem.create": {"title": "Parser accepts malformed input"},
        "problem.update": {"problem_id": "PRB-2026-0001", "uncertainty": "unknown"},
        "work_item.create": {"title": "Harden parser", "outcome": "Reject malformed input"},
        "work_item.update": {"work_item_id": "WI-2026-0001", "scope": "parser"},
        "gate.qualify": {},
        "handoff.prepare": {},
    }

    prepared = [
        prepare_action(state, operation=operation, inputs=inputs, route_binding=binding)
        for operation, inputs in requests.items()
    ]

    assert len(prepared) == 7
    assert all(result["status"] == "ready" for result in prepared)
    assert all(result["required_missing"] == [] for result in prepared)
    assert all(result["next_operation"]["copy_ready"].startswith("anchor(") for result in prepared)
    assert {result["next_operation"]["action"] for result in prepared}.isdisjoint(
        {"help", "prepare", "tools"}
    )


def test_lifecycle_context_supplies_one_operation_and_relevant_resource(authority):
    state, binding = authority
    protocol = build_operating_protocol(state, action="gate")
    protocol["required_now"] = [
        {"id": "learning_closure", "satisfy_with": {"route": "learning.assess"}},
    ]

    result = build_agent_context(state, protocol=protocol, route_binding=binding)

    assert result["next_operation"]["copy_ready"] == (
        'anchor("prepare", operation=\'learning.assess\', inputs={})'
    )
    assert result["suggested_next_actions"] == [
        'anchor("prepare", operation=\'learning.assess\', inputs={})'
    ]
    assert result["resource_pointers"] == [
        {
            "kind": "reference",
            "id": "odibi-anchor.capture-standards",
            "path": "references/odibi-anchor/capture-standards.md",
            "reason": "explains evidence-backed learning closure",
            "status": "derived",
            "provenance": {"source": "packaged_resource_contract:v1"},
        }
    ]


@pytest.mark.parametrize("action", ["context", "prepare"])
def test_learning_recovery_allows_generated_read_only_guidance(action, tmp_path, monkeypatch):
    from odibi_anchor._dispatcher._effects import build_static_action_contracts
    from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement
    from odibi_anchor._utils._session_state import SessionState

    state = SessionState(
        active_project="project-a",
        learning_owner_project_id="project-a",
        task_window_id="ltw-current",
        prior_learn_debt=True,
    )
    monkeypatch.setattr(
        "odibi_anchor.codebase.structured_learning_context.active_learning_obligation",
        lambda **_: {"task_window_id": "ltw-prior"},
    )

    decision = run_pre_dispatch_enforcement(
        action,
        (),
        {"operation": "learning.assess", "inputs": {}} if action == "prepare" else {},
        session_state=state,
        session_timings=[],
        session_files_changed=set(),
        session_boot_manifest={},
        planning_required_actions=frozenset(),
        root=str(tmp_path),
        action_contract=build_static_action_contracts()[action],
    )

    assert decision is None


def test_real_dispatch_executes_generated_session_and_semantic_preparation(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    repository = tmp_path / "repo"
    repository.mkdir()
    state_root.mkdir()
    monkeypatch.setenv("ANCHOR_HOME", str(state_root))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(state_root / "memory.db"))
    from odibi_anchor._dispatcher._project import project_action, resolve_route_binding
    from odibi_anchor.bootstrap import init

    project_action(
        state_root, "create", name="journey-project", target=repository,
        output_format="dict",
    )
    binding = resolve_route_binding(
        state_root, project="journey-project", runtime_instance_id="test:journey",
    )
    anchor, _, _ = init(route_binding=binding, output_format="dict")
    anchor("orient", output_format="dict")

    start = anchor("context", output_format="dict")["next_operation"]
    started = eval(start["copy_ready"], {"anchor": anchor})
    assert started["kind"] == "new_session"

    prepare = anchor("context", output_format="dict")["next_operation"]
    prepared = eval(prepare["copy_ready"], {"anchor": anchor})
    assert prepared["kind"] == "action_preparation"
    assert prepared["status"] == "blocked"
    assert [item["field"] for item in prepared["required_missing"]] == [
        "task", "goal", "in_scope",
    ]

    executable = anchor(
        "prepare",
        operation="task.create",
        inputs={
            "task": "Inspect the bounded journey",
            "goal": "Verify prepared task dispatch succeeds",
            "mode": "implementation",
            "execution_mode": "read_only",
            "acceptance_criteria": ["Prepared task is accepted"],
        },
        output_format="dict",
    )
    accepted = eval(executable["next_operation"]["copy_ready"], {"anchor": anchor})
    assert accepted["kind"] == "task_execution_context"
    assert accepted["readiness"]["score"] >= 40
