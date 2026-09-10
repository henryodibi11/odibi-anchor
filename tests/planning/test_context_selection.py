"""Provider-neutral context-eye selection and compatibility projection tests."""

from __future__ import annotations

from odibi_anchor.planning import eye_catalog, select_context, task_execution_context
from odibi_anchor.planning._task_profile import TaskProfile, normalize_task_profile
from odibi_anchor.planning.context_selection import project_context_generators


def _profile(mode: str = "planning", **overrides) -> TaskProfile:
    return normalize_task_profile(legacy_mode=mode, **overrides)


def test_catalog_has_eight_stable_unique_eye_families() -> None:
    catalog = eye_catalog()
    assert len(catalog) == 8
    assert len({item["id"] for item in catalog}) == 8
    assert all(item["question"] and item["expected_evidence"] for item in catalog)


def test_source_change_requires_intent_code_and_verification() -> None:
    plan = select_context(_profile("implementation"), task_text="Implement a small feature")
    priorities = {item["id"]: item["priority"] for item in plan["questions"]}
    assert priorities["work.intent"] == "required"
    assert priorities["code.change"] == "required"
    assert priorities["verification.delivery"] == "required"
    assert "dependency.lineage-impact" not in priorities


def test_project_setup_does_not_select_data_or_lineage_without_specific_signals() -> None:
    plan = select_context(
        _profile("implementation"),
        task_text="Create a referenced project and validate the MCP package setup.",
    )
    selected = {item["id"] for item in plan["questions"]}
    assert selected == {"work.intent", "code.change", "verification.delivery"}


def test_package_validation_and_dependencies_do_not_select_data_or_lineage() -> None:
    plan = select_context(
        _profile("analysis"),
        task_text="Run validation checks for package configuration and upgrade package dependencies.",
    )
    selected = {item["id"] for item in plan["questions"]}
    assert "data.state-quality" not in selected
    assert "dependency.lineage-impact" not in selected


def test_validation_checks_for_dataset_select_data_quality_actions() -> None:
    plan = select_context(
        _profile("testing"),
        task_text="Plan validation checks before promoting a dataset.",
    )
    data_question = next(
        question for question in plan["questions"]
        if question["id"] == "data.state-quality"
    )
    assert {"validate", "quality"} <= {
        action["name"] for action in data_question["candidate_actions"]
    }


def test_text_inference_only_adds_recommended_questions() -> None:
    plan = select_context(_profile(), task_text="Maybe optimize a slow and expensive query")
    performance = next(item for item in plan["questions"] if item["id"] == "performance.cost")
    assert performance["priority"] == "recommended"
    assert plan["selection_basis"]["text_inference_can_require"] is False


def test_signal_matching_avoids_databricks_and_runtime_substring_collisions() -> None:
    plan = select_context(
        _profile("analysis"),
        task_text="Verify a fresh Databricks runtime and its permissions.",
    )
    selected = {item["id"] for item in plan["questions"]}
    assert "environment.capability" in selected
    assert "data.state-quality" not in selected
    assert "execution.incident" not in selected


def test_signal_matching_preserves_plural_terms() -> None:
    plan = select_context(
        _profile("analysis"),
        task_text="Inspect tables, rows, dependencies, and affected consumers.",
    )
    selected = {item["id"] for item in plan["questions"]}
    assert "data.state-quality" in selected
    assert "dependency.lineage-impact" in selected


def test_negated_task_text_does_not_select_context_eye() -> None:
    plan = select_context(
        _profile("analysis"),
        task_text="Review the implementation. Do not modify data or inspect tables.",
    )
    selected = {item["id"] for item in plan["questions"]}
    assert "data.state-quality" not in selected
    assert "code.change" in selected


def test_mixed_polarity_preserves_positive_prefix_signals() -> None:
    plan = select_context(
        _profile("analysis"),
        task_text="Inspect upstream dependencies without modifying tables.",
    )
    selected = {item["id"] for item in plan["questions"]}
    assert "dependency.lineage-impact" in selected
    assert "data.state-quality" not in selected


def test_out_of_scope_suppresses_fuzzy_recommendation() -> None:
    plan = select_context(
        _profile("analysis"),
        task_text="Review code and data quality before delivery.",
        out_of_scope=["Data and table changes are out of scope."],
    )
    selected = {item["id"] for item in plan["questions"]}
    assert "data.state-quality" not in selected
    assert "code.change" in selected
    assert "verification.delivery" in selected


def test_out_of_scope_suppresses_action_only_signal() -> None:
    plan = select_context(
        _profile("analysis"),
        task_text="Review merge and upsert behavior.",
        out_of_scope=["Merges and upserts"],
    )
    selected = {item["id"] for item in plan["questions"]}
    assert "data.state-quality" not in selected


def test_out_of_scope_does_not_override_required_structured_signal() -> None:
    plan = select_context(
        _profile("data"),
        task_text="Do not modify data.",
        out_of_scope=["Data changes"],
    )
    priorities = {item["id"]: item["priority"] for item in plan["questions"]}
    assert priorities["data.state-quality"] == "required"


def test_blast_radius_text_selects_focused_dependency_context() -> None:
    plan = select_context(
        _profile("debugging"),
        task_text="Investigate zero rows and determine the blast radius.",
    )
    dependency = next(
        item for item in plan["questions"]
        if item["id"] == "dependency.lineage-impact"
    )
    assert dependency["priority"] == "recommended"
    impact = next(
        action for action in dependency["candidate_actions"]
        if action["name"] == "impact"
    )
    assert impact["focused"] is True


def test_required_questions_are_never_silently_truncated() -> None:
    profile = _profile("data", risk="high")
    plan = select_context(profile, task_text="pipeline failure", max_questions=1)
    required = [item for item in plan["questions"] if item["priority"] == "required"]
    assert len(required) > 1
    assert plan["omitted_required_count"] == 0


def test_available_actions_are_filtered_and_only_called_is_reported() -> None:
    plan = select_context(
        _profile("implementation"),
        available_actions={"memory", "map", "test"},
        already_called={"map"},
    )
    actions = {
        action["name"]: action
        for question in plan["questions"]
        for action in question["candidate_actions"]
    }
    assert set(actions) == {"memory", "map", "test"}
    assert actions["map"]["availability"] == "registered"
    assert actions["map"]["called_this_session"] is True
    assert "satisfied" not in actions["map"]


def test_unknown_dispatch_availability_is_explicit() -> None:
    plan = select_context(_profile("implementation"))
    actions = [action for question in plan["questions"] for action in question["candidate_actions"]]
    assert actions
    assert {action["availability"] for action in actions} == {"unknown"}


def test_caller_evidence_request_makes_matching_eye_required() -> None:
    profile = _profile(
        caller_required_evidence=[{
            "id": "run-log",
            "kind": "execution-log",
            "description": "failed pipeline run evidence",
            "required_before": "work",
        }],
    )
    plan = select_context(profile)
    execution = next(item for item in plan["questions"] if item["id"] == "execution.incident")
    assert execution["priority"] == "required"
    assert any("run-log" in reason for reason in execution["reasons"])


def test_selection_and_projection_are_deterministic() -> None:
    profile = _profile("debugging")
    kwargs = {
        "task_text": "debug failed code",
        "available_actions": {"memory", "trace", "map", "status"},
        "already_called": {"trace"},
    }
    first = select_context(profile, **kwargs)
    second = select_context(profile, **kwargs)
    assert first == second
    projected = project_context_generators(first)
    trace = next(item for item in projected if item["name"] == "trace")
    assert trace["status"] == "already_called"
    assert all(item["name"] in kwargs["available_actions"] for item in projected)


def test_task_result_and_handoff_share_the_context_plan() -> None:
    result = task_execution_context(
        "Fix a failing data pipeline and prepare it for review.",
        mode="debugging",
        audience="agent",
        _available_actions=["memory", "trace", "run_diff", "review"],
    )
    assert result["version"] == "3.2"
    assert result["context_plan"]["questions"]
    assert result["metrics"]["selected_eye_count"] == len(result["context_plan"]["questions"])
    assert "Selected context questions:" in result["handoff"]["prompt_brief"]
    projected = result["discovery"]["recommended_context_generators"]
    assert projected == project_context_generators(result["context_plan"])
    assert {item["name"] for item in projected} <= {"memory", "trace", "run_diff", "review"}
    assert {item["status"] for item in projected} == {"registered"}


def test_goal_only_signal_uses_one_context_plan_for_legacy_projection() -> None:
    result = task_execution_context(
        "Plan work.",
        goal="Validate data quality before delivery.",
        mode="planning",
    )
    assert result["discovery"]["recommended_context_generators"] == (
        project_context_generators(result["context_plan"])
    )
    assert "validate" in {
        item["name"] for item in result["discovery"]["recommended_context_generators"]
    }


def test_task_out_of_scope_is_used_by_context_selection() -> None:
    result = task_execution_context(
        "Review this code and data quality.",
        mode="analysis",
        out_of_scope=["Data and tables"],
    )
    selected = {item["id"] for item in result["context_plan"]["questions"]}
    assert "code.change" in selected
    assert "data.state-quality" not in selected


def test_legacy_projection_is_exact_even_with_small_section_limit() -> None:
    result = task_execution_context(
        "Implement and verify a data pipeline change.",
        mode="implementation",
        max_items_per_section=1,
    )
    assert result["discovery"]["recommended_context_generators"] == (
        project_context_generators(result["context_plan"])
    )


def test_markdown_renders_selected_questions() -> None:
    report = task_execution_context(
        "Review this source change.",
        mode="review",
        output_format="markdown",
    )
    assert "## Selected Context Questions" in report
    assert "verification-delivery" in report
