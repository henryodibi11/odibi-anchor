from __future__ import annotations

from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._action_preparation import (
    CANONICAL_OPERATIONS,
    prepare_action,
    validate_mutation_submission,
)


@pytest.fixture
def owner():
    state = SimpleNamespace(
        task_window_id="ltw_exact",
        task_goal="Deliver a self-contained implementation handoff",
    )
    route = SimpleNamespace(
        project_id="project-a",
        target_root="/workspace/repo-a",
        artifact_root="/anchor/projects/project-a",
    )
    return state, route


@pytest.mark.parametrize(
    ("operation", "inputs", "expected_action"),
    [
        (
            "task.create",
            {
                "task": "Implement parser",
                "goal": "Reject malformed input",
                "mode": "implementation",
                "acceptance_criteria": ["Malformed input is rejected"],
            },
            "task",
        ),
        ("problem.create", {"title": "Parser accepts malformed input"}, "problem"),
        (
            "problem.update",
            {"problem_id": "PRB-2026-0001", "uncertainty": "Cause unverified"},
            "problem",
        ),
        (
            "work_item.create",
            {"title": "Harden parser", "outcome": "Malformed input is rejected"},
            "work_item",
        ),
        (
            "work_item.update",
            {"work_item_id": "WI-2026-0001", "scope": "Parser only"},
            "work_item",
        ),
        ("gate.qualify", {}, "gate"),
        ("handoff.prepare", {"summary": "Parser hardening is ready"}, "snapshot"),
        ("learning.assess", {"outcome": "nothing_reusable_learned"}, "learning"),
    ],
)
def test_all_canonical_operations_return_four_groups_and_executable_next_operation(
    owner, operation, inputs, expected_action
) -> None:
    state, route = owner
    result = prepare_action(state, operation=operation, inputs=inputs, route_binding=route)

    assert len(CANONICAL_OPERATIONS) == 8
    assert result["status"] == "ready"
    assert result["next_operation"]["action"] == expected_action
    assert result["next_operation"]["copy_ready"].startswith("anchor(")
    assert set(result) >= {
        "required_missing", "recommended_missing", "prefilled_verified", "not_applicable"
    }


def test_missing_semantic_input_is_not_fabricated(owner) -> None:
    state, route = owner
    result = prepare_action(state, operation="task.create", inputs={}, route_binding=route)

    assert result["status"] == "blocked"
    assert [item["field"] for item in result["required_missing"]] == [
        "task", "goal", "in_scope",
    ]
    assert result["next_operation"]["action"] == "supply_inputs"
    assert "goal" not in result["next_operation"]["invocation"]["kwargs"]["inputs"]


def test_task_preparation_reuses_dispatcher_minimum_semantic_length(owner) -> None:
    state, route = owner

    with pytest.raises(ValueError, match="meaningful description"):
        prepare_action(
            state,
            operation="task.create",
            inputs={"task": "x", "goal": "Reject malformed input"},
            route_binding=route,
        )
    with pytest.raises(ValueError, match="meaningful goal"):
        prepare_action(
            state,
            operation="task.create",
            inputs={"task": "Implement parser", "goal": "x"},
            route_binding=route,
        )


def test_task_preparation_surfaces_minimum_readiness_inputs(owner) -> None:
    state, route = owner
    blocked = prepare_action(
        state,
        operation="task.create",
        inputs={
            "task": "Implement parser",
            "goal": "Reject malformed input",
            "mode": "implementation",
        },
        route_binding=route,
    )
    assert [item["field"] for item in blocked["required_missing"]] == [
        "acceptance_criteria"
    ]
    assert blocked["status"] == "blocked"

    ready = prepare_action(
        state,
        operation="task.create",
        inputs={
            "task": "Implement parser",
            "goal": "Reject malformed input",
            "mode": "implementation",
            "acceptance_criteria": ["Malformed input is rejected"],
        },
        route_binding=route,
    )
    assert ready["status"] == "ready"

    with pytest.raises(ValueError, match="Unsupported legacy mode"):
        prepare_action(
            state,
            operation="task.create",
            inputs={
                "task": "Investigate parser",
                "goal": "Explain malformed input",
                "mode": "investigation",
                "in_scope": ["parser"],
                "constraints": ["Read only"],
            },
            route_binding=route,
        )
    with pytest.raises(ValueError, match="unknown task fields: invented"):
        prepare_action(
            state,
            operation="task.create",
            inputs={
                "task": "Implement parser",
                "goal": "Reject malformed input",
                "in_scope": ["parser"],
                "invented": True,
            },
            route_binding=route,
        )


def test_databricks_source_preparation_requires_explicit_scope_and_acknowledgement(owner) -> None:
    state, route = owner
    state.repository_provider = SimpleNamespace(provider_id="databricks:test")
    result = prepare_action(
        state,
        operation="task.create",
        inputs={
            "task": "Implement bounded notebook change",
            "goal": "Preserve exact Databricks source authority",
            "mode": "implementation",
            "execution_mode": "source_change",
            "acceptance_criteria": ["The scoped change is verified."],
        },
        route_binding=route,
    )

    assert result["status"] == "blocked"
    assert [item["field"] for item in result["required_missing"]] == [
        "repository_scope", "accept_unknown_git_state",
    ]


def test_learning_preparation_requires_explicit_nonfabricated_outcome(owner) -> None:
    state, route = owner
    result = prepare_action(state, operation="learning.assess", route_binding=route)

    assert result["status"] == "blocked"
    assert result["required_missing"] == [
        {"field": "outcome", "reason": "records the agent's explicit learning judgment"}
    ]
    assert result["next_operation"]["action"] == "supply_inputs"
    assert "outcome" not in result["next_operation"]["invocation"]["kwargs"]["inputs"]
    with pytest.raises(ValueError, match="unknown learning assessment fields"):
        prepare_action(
            state,
            operation="learning.assess",
            inputs={"outcome": "nothing_reusable_learned", "invented": True},
            route_binding=route,
        )


def test_verified_prefills_use_immutable_route_and_task_authority(owner) -> None:
    state, route = owner
    result = prepare_action(state, operation="gate", route_binding=route)
    prefills = {item["field"]: item for item in result["prefilled_verified"]}

    assert prefills["project_id"]["value"] == "project-a"
    assert prefills["project_id"]["provenance"] == {"source": "route_binding:v1"}
    assert prefills["task_window_id"]["provenance"] == {
        "source": "accepted_task_authority:v1"
    }
    assert result["next_operation"]["kwargs"] == {}


def test_evidence_alias_requires_problem_and_provenance_bearing_evidence(owner) -> None:
    state, route = owner
    result = prepare_action(
        state,
        operation="evidence.capture",
        inputs={
            "problem_id": "PRB-2026-0001",
            "evidence": [{"source": "pytest", "observation": "3 assertions passed"}],
        },
        route_binding=route,
    )

    assert result["canonical_operation"] == "problem.update"
    assert result["status"] == "ready"
    assert result["next_operation"]["args"] == ["update", "PRB-2026-0001"]


def test_evidence_preparation_reuses_runtime_source_provenance_requirement(owner) -> None:
    state, route = owner
    with pytest.raises(ValueError, match="evidence requires source provenance"):
        prepare_action(
            state,
            operation="evidence.capture",
            inputs={"problem_id": "PRB-2026-0001", "evidence": [{"observation": "passed"}]},
            route_binding=route,
        )


def test_work_item_preparation_reuses_runtime_unknown_field_boundary(owner) -> None:
    state, route = owner
    with pytest.raises(ValueError, match="unknown work-item fields: priority"):
        prepare_action(
            state,
            operation="work_item.create",
            inputs={"title": "Harden parser", "outcome": "Safe", "priority": "high"},
            route_binding=route,
        )


@pytest.mark.parametrize("placeholder", ["...", "TBD", "<insert goal>", "${goal}"])
def test_narrow_placeholders_fail_before_mutation(owner, placeholder) -> None:
    state, route = owner
    kwargs = {"goal": placeholder, "output_format": "dict"}

    with pytest.raises(ValueError, match="fabricated placeholder"):
        validate_mutation_submission(
            "task", ("Implement parser",), kwargs,
            session_state=state, route_binding=route,
        )

    assert kwargs["goal"] == placeholder


def test_unknown_is_truthful_input_not_a_placeholder(owner) -> None:
    state, route = owner
    result = prepare_action(
        state,
        operation="problem.update",
        inputs={"problem_id": "PRB-2026-0001", "uncertainty": "unknown"},
        route_binding=route,
    )
    assert result["status"] == "ready"


def test_unsupported_verified_claim_fails_before_mutation(owner) -> None:
    state, route = owner
    kwargs = {
        "title": "Observed failure",
        "verified_claims": [
            {
                "field": "project_id",
                "value": "project-b",
                "provenance": {"source": "route_binding:v1"},
            }
        ],
    }

    with pytest.raises(ValueError, match="does not match current authority"):
        validate_mutation_submission(
            "problem", ("create",), kwargs,
            session_state=state, route_binding=route,
        )

    assert "verified_claims" in kwargs


def test_matching_verified_claim_is_consumed_before_handler(owner) -> None:
    state, route = owner
    kwargs = {
        "title": "Observed failure",
        "verified_claims": [
            {
                "field": "project_id",
                "value": "project-a",
                "provenance": {"source": "route_binding:v1"},
            }
        ],
    }

    validate_mutation_submission(
        "problem", ("create",), kwargs,
        session_state=state, route_binding=route,
    )

    assert kwargs == {"title": "Observed failure"}


def test_handoff_prefills_verified_task_goal_without_guessing(owner) -> None:
    state, route = owner
    result = prepare_action(state, operation="handoff", route_binding=route)

    assert result["status"] == "ready"
    assert result["next_operation"]["kwargs"]["summary"] == state.task_goal
    assert result["next_operation"]["kwargs"]["mode"] == "handoff"


def test_markdown_view_is_compact_and_actionable(owner) -> None:
    state, route = owner
    result = prepare_action(
        state,
        operation="gate.qualify",
        route_binding=route,
        output_format="markdown",
    )

    assert "# Action preparation: gate.qualify" in result
    assert "## Next operation" in result
    assert '`anchor("gate")`' in result
