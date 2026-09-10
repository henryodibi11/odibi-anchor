import json
from datetime import datetime

import pytest

from odibi_anchor.planning import quick_context, task_execution_context, render_task_execution_report
from odibi_anchor.planning.context_selection import project_context_generators
from odibi_anchor.planning.task_execution_context import Audience, TaskMode, TaskPriority, TASK_EXECUTION_CONTEXT_VERSION


REQUIRED_TOP_LEVEL_KEYS = {
    "kind",
    "version",
    "subject",
    "summary",
    "status",
    "mode",
    "audience",
    "ownership",
    "readiness",
    "intent",
    "background",
    "scope",
    "resources",
    "context",
    "constraints",
    "discovery",
    "plan",
    "critique_checks",
    "verification",
    "risks",
    "hints",
    "metrics",
    "findings",
    "samples",
    "suggested_next_actions",
    "metadata",
    "handoff",
}


def make_rich_context(**overrides):
    kwargs = {
        "task": "Test the generated artifact before starting Spark work.",
        "subject": "task_execution_context",
        "goal": "Verify the pure Python planning utility is ready for Databricks handoff.",
        "mode": "testing",
        "audience": "agent",
        "background": "A ChatGPT-generated artifact needs validation inside Databricks.",
        "current_state": "The artifact exists but has not been tested in the target workspace.",
        "desired_outcome": "A passing smoke test and clear next action.",
        "trigger": "New artifact handoff.",
        "artifacts": [
            {
                "name": "odibi_task_execution_context_mvp.zip",
                "type": "zip",
                "role": "candidate implementation",
                "location": "incoming_artifacts/",
                "status": "available",
            }
        ],
        "inputs": [
            {
                "name": "synthetic task examples",
                "type": "dict",
                "role": "test input",
                "status": "to_create",
            }
        ],
        "constraints": [
            "Do not modify production files before inspection.",
            "Use synthetic examples first.",
            "Do not move to Spark because this tool is pure Python.",
        ],
        "known_facts": [
            "This tool is standalone and has no pandas/Spark dependency.",
        ],
        "assumptions": [
            "The Databricks repo can import local source files.",
        ],
        "open_questions": [
            "Should this live under odibi.context or another standalone module?",
        ],
        "decisions_needed": [
            "Whether to promote the package path after tests pass.",
        ],
        "in_scope": [
            "Inspect artifact.",
            "Run pure Python tests.",
            "Validate prompt handoff output.",
        ],
        "out_of_scope": [
            "Spark implementation.",
            "Agent execution.",
            "Framework abstractions.",
        ],
        "dependencies": ["Python standard library", "pytest for tests"],
        "risks": ["The tool could drift into agent orchestration if scope is not controlled."],
        "acceptance_criteria": [
            "Imports pass.",
            "Output contract is stable.",
            "Readiness score is ready.",
        ],
        "stop_conditions": [
            "Imports fail.",
            "Output cannot be serialized to JSON.",
        ],
        "deliverables": [
            "Test summary.",
            "Databricks handoff notes.",
        ],
    }
    kwargs.update(overrides)
    return task_execution_context(**kwargs)


def test_returns_standard_context_keys():
    context = task_execution_context(task="Plan a small implementation task.")
    assert REQUIRED_TOP_LEVEL_KEYS <= set(context)
    assert context["kind"] == "task_execution_context"
    assert context["version"] == TASK_EXECUTION_CONTEXT_VERSION == "3.2"
    assert context["metadata"]["version"] == "3.2"
    assert context["samples"] == {}


def test_minimal_task_is_under_specified_but_useful():
    context = task_execution_context(task="Test the ZIP.")
    assert context["status"] == "under_specified"
    assert context["readiness"]["score"] < 50
    assert "Goal or desired outcome is missing." in context["readiness"]["missing_details"]
    assert context["plan"]
    assert context["handoff"]["prompt_brief"]
    assert context["suggested_next_actions"]


def test_rich_task_is_ready():
    context = make_rich_context()
    assert context["status"] == "ready"
    assert context["readiness"]["score"] >= 80
    assert context["readiness"]["missing_details"] == []
    assert context["findings"][0]["type"] == "readiness"


def test_perfect_score_requires_grounding_beyond_checklist_completion():
    context = task_execution_context(
        task="Create a new anchor('lint') action that runs ruff on changed files.",
        goal="Add a lint action that fits the existing changed-file workflow.",
        mode="implementation",
        known_facts=["anchor() currently exposes 44 actions through agent_init.py."],
        in_scope=["Define lint action behavior."],
        out_of_scope=["Replacing preflight."],
        constraints=["Preserve the anchor output contract."],
        risks=["ruff may not be installed."],
        acceptance_criteria=["Plan identifies code locations to edit."],
        deliverables=["Implementation plan."],
    )
    # Implementation mode: all dims satisfied except background(5) = 95
    # known_facts with ".py" satisfies resources, but no background marker
    assert context["readiness"]["score"] == 95
    assert "Background or current state is missing." in context["readiness"]["missing_details"]
    assert not any("Grounding is incomplete" in item for item in context["readiness"]["strengths"])


def test_perfect_score_is_restored_when_background_and_resources_are_present():
    context = task_execution_context(
        task="Create a new anchor('lint') action that runs ruff on changed files.",
        goal="Add a lint action that fits the existing changed-file workflow.",
        mode="implementation",
        background="The repo already tracks changed files via anchor('touched'), but linting is still missing.",
        artifacts=[{"path": "agent_init.py"}],
        dependencies=["ruff"],
        in_scope=["Define lint action behavior."],
        out_of_scope=["Replacing preflight."],
        constraints=["Preserve the anchor output contract."],
        risks=["ruff may not be installed."],
        acceptance_criteria=["Plan identifies code locations to edit."],
        deliverables=["Implementation plan."],
    )
    assert context["readiness"]["score"] == 100


def test_evidence_can_ground_a_task_without_explicit_resources():
    context = task_execution_context(
        task="Review rollout readiness.",
        goal="Decide whether the rollout plan is grounded enough to proceed.",
        mode="review",
        background="A prior readiness audit found the score too optimistic.",
        evidence=[{"name": "audit report", "summary": "100 was achievable without evidence."}],
        in_scope=["Assess the current scoring approach."],
        out_of_scope=["Implementing unrelated planner changes."],
        constraints=["Keep the review deterministic."],
        risks=["Review could mistake checklist completion for evidence."],
        acceptance_criteria=["Recommendation is tied to cited evidence."],
        deliverables=["Review summary."],
    )
    assert context["readiness"]["score"] == 100


def test_subject_defaults_from_task_first_sentence():
    context = task_execution_context(task="Review generated files. Do not apply yet.")
    assert context["subject"] == "Review generated files"


def test_explicit_subject_is_preserved():
    context = task_execution_context(task="Review generated files.", subject="artifact review")
    assert context["subject"] == "artifact review"


def test_summary_uses_goal_when_available():
    context = task_execution_context(
        task="Review generated files.",
        subject="artifact review",
        goal="Decide whether to promote the artifact.",
    )
    assert "Decide whether to promote" in context["summary"]


def test_summary_uses_desired_outcome_when_goal_missing():
    context = task_execution_context(
        task="Review generated files.",
        subject="artifact review",
        desired_outcome="A clear promote or fix decision.",
    )
    assert "A clear promote or fix decision" in context["summary"]


def test_testing_mode_includes_artifact_inspection_and_synthetic_tests():
    context = task_execution_context(task="Test artifact.", mode="testing")
    actions = " ".join(step["action"].lower() for step in context["plan"])
    assert "inspect" in actions
    assert "synthetic" in actions
    assert any("output contract" in check.lower() for check in context["critique_checks"])


def test_implementation_mode_includes_api_tests_and_docs():
    context = task_execution_context(task="Build a tool.", mode="implementation")
    actions = " ".join(step["action"].lower() for step in context["plan"])
    assert "public api" in actions
    assert "tests" in actions
    assert "usage examples" in actions


def test_debugging_mode_includes_reproduce_before_fix():
    context = task_execution_context(task="Debug failed notebook.", mode="debugging")
    phases = [step["phase"] for step in context["plan"]]
    assert phases.index("reproduce") < phases.index("fix")
    assert any("guessing" in check.lower() for check in context["critique_checks"])


def test_review_mode_includes_approve_change_block_decision():
    context = task_execution_context(task="Review patch.", mode="review")
    actions = " ".join(step["action"].lower() for step in context["plan"])
    assert "approve" in actions
    assert "request changes" in actions
    assert "block" in actions


def test_migration_mode_includes_dependency_and_import_checks():
    context = task_execution_context(task="Move module into repo.", mode="migration")
    joined = " ".join(json.dumps(step).lower() for step in context["plan"])
    assert "dependencies" in joined
    assert "import" in joined


def test_handoff_mode_includes_artifacts_and_next_steps():
    context = task_execution_context(task="Hand off this thread.", mode="handoff")
    joined = " ".join(step["action"].lower() for step in context["plan"])
    assert "artifacts" in joined
    assert "next actions" in joined


def test_decision_mode_includes_options_criteria_and_tradeoffs():
    context = task_execution_context(task="Choose package path.", mode="decision")
    joined = " ".join(step["action"].lower() for step in context["plan"])
    assert "options" in joined
    assert "criteria" in joined
    assert "tradeoffs" in joined


def test_analysis_mode_includes_evidence_and_assumptions():
    context = task_execution_context(task="Analyze failure pattern.", mode="analysis")
    joined = " ".join(step["action"].lower() for step in context["plan"])
    assert "evidence" in joined
    assert "assumptions" in joined


def test_unknown_mode_raises_value_error():
    with pytest.raises(ValueError, match="Unsupported mode"):
        task_execution_context(task="Do work.", mode="unsupported")  # type: ignore[arg-type]


def test_unknown_audience_raises_value_error():
    with pytest.raises(ValueError, match="Unsupported audience"):
        task_execution_context(task="Do work.", audience="robot")  # type: ignore[arg-type]


def test_empty_task_raises_value_error():
    with pytest.raises(ValueError, match="task must be"):
        task_execution_context(task="   ")


def test_negative_limits_raise_value_error():
    with pytest.raises(ValueError, match="max_plan_steps"):
        task_execution_context(task="Do work.", max_plan_steps=-1)
    with pytest.raises(ValueError, match="max_items_per_section"):
        task_execution_context(task="Do work.", max_items_per_section=-1)
    with pytest.raises(ValueError, match="max_text_length"):
        task_execution_context(task="Do work.", max_text_length=-1)


def test_zero_text_length_raises_value_error():
    with pytest.raises(ValueError, match="max_text_length"):
        task_execution_context(task="Do work.", max_text_length=0)


def test_zero_plan_steps_returns_empty_plan_and_sets_truncated():
    context = task_execution_context(task="Plan work.", max_plan_steps=0)
    assert context["plan"] == []
    assert context["metadata"]["truncated"] is True


def test_zero_items_per_section_caps_lists():
    context = task_execution_context(
        task="Plan work.",
        constraints=["a", "b"],
        acceptance_criteria=["done"],
        max_items_per_section=0,
    )
    assert context["constraints"] == []
    assert context["verification"]["acceptance_criteria"] == []
    assert context["metadata"]["truncated"] is True


def test_long_text_is_truncated_everywhere():
    long_text = "x" * 80
    context = task_execution_context(
        task=long_text,
        goal=long_text,
        constraints=[long_text],
        max_text_length=20,
    )
    assert len(context["intent"]["task"]) <= 20
    assert context["intent"]["task"].endswith("...")
    assert len(context["intent"]["goal"]) <= 20
    assert len(context["constraints"][0]) <= 20
    assert context["metadata"]["truncated"] is True


def test_lists_are_capped_and_deduped():
    context = task_execution_context(
        task="Plan work.",
        constraints=["a", "a", "b", "c"],
        max_items_per_section=2,
    )
    assert context["constraints"] == ["a", "b"]
    assert context["metadata"]["truncated"] is True


def test_artifacts_accept_single_mapping_and_are_json_safe():
    context = task_execution_context(
        task="Test artifact.",
        artifacts={"name": "artifact.zip", "created_at": datetime(2026, 1, 1)},
    )
    assert context["resources"]["artifacts"] == [
        {"name": "artifact.zip", "created_at": "2026-01-01T00:00:00"}
    ]


def test_non_mapping_artifact_is_wrapped_as_value():
    context = task_execution_context(task="Test artifact.", artifacts=["artifact.zip"])  # type: ignore[list-item]
    assert context["resources"]["artifacts"] == [{"value": "artifact.zip"}]


def test_mapping_passed_to_text_list_raises_value_error():
    with pytest.raises(ValueError, match="Expected a list of text values"):
        task_execution_context(task="Plan work.", constraints={"bad": "value"})  # type: ignore[arg-type]


def test_known_facts_assumptions_questions_and_decisions_are_separate():
    context = task_execution_context(
        task="Plan work.",
        known_facts=["Fact."],
        assumptions=["Assumption."],
        open_questions=["Question?"],
        decisions_needed=["Decision."],
    )
    assert context["context"]["known_facts"] == ["Fact."]
    assert context["context"]["assumptions"] == ["Assumption."]
    assert context["context"]["open_questions"] == ["Question?"]
    assert context["context"]["decisions_needed"] == ["Decision."]


def test_scope_is_separated_from_constraints():
    context = task_execution_context(
        task="Plan work.",
        in_scope=["Pandas test."],
        out_of_scope=["Spark work."],
        constraints=["Use synthetic data."],
    )
    assert context["scope"]["in_scope"] == ["Pandas test."]
    assert context["scope"]["out_of_scope"] == ["Spark work."]
    assert context["constraints"] == ["Use synthetic data."]


def test_readiness_score_increases_with_more_details():
    minimal = task_execution_context(task="Plan work.")
    richer = make_rich_context()
    assert richer["readiness"]["score"] > minimal["readiness"]["score"]


def test_missing_acceptance_criteria_creates_readiness_gap_and_risk():
    # Use implementation mode where acceptance_criteria is a dimension
    context = task_execution_context(task="Plan work.", goal="Get it done.", mode="implementation")
    assert "Acceptance criteria are missing." in context["readiness"]["missing_details"]
    assert any("definition of done" in risk.lower() or "acceptance" in risk.lower() for risk in context["risks"])
    assert any("Acceptance criteria" in finding["message"] for finding in context["findings"])


def test_user_risks_are_preserved_before_generated_risks():
    context = task_execution_context(task="Plan work.", risks=["Custom risk."])
    assert context["risks"][0] == "Custom risk."


def test_metrics_count_core_sections():
    context = make_rich_context()
    assert context["metrics"]["readiness_score"] == context["readiness"]["score"]
    assert context["metrics"]["constraint_count"] == 3
    assert context["metrics"]["artifact_count"] == 1
    assert context["metrics"]["input_count"] == 1
    assert context["metrics"]["acceptance_criteria_count"] == 3


def test_prompt_brief_contains_key_sections_for_agent():
    context = make_rich_context()
    prompt = context["handoff"]["prompt_brief"]
    assert "You are helping with: task_execution_context" in prompt
    assert "Goal:" in prompt
    assert "Constraints:" in prompt
    assert "Plan:" in prompt
    assert "Stop if:" in prompt
    assert "Acceptance criteria:" in prompt
    assert "Execution instruction:" in prompt


def test_prompt_brief_omits_agent_instruction_for_self_audience():
    context = task_execution_context(task="Plan work.", audience="self")
    assert "Execution instruction:" not in context["handoff"]["prompt_brief"]


def test_include_handoff_false_returns_empty_handoff():
    context = task_execution_context(task="Plan work.", include_handoff=False)
    assert context["handoff"] == {}


def test_teammate_brief_contains_summary_and_next_actions():
    context = make_rich_context(audience="teammate")
    brief = context["handoff"]["teammate_brief"]
    assert "Task: task_execution_context" in brief
    assert "Summary:" in brief
    assert "Recommended next actions:" in brief


def test_output_is_json_serializable():
    context = make_rich_context()
    encoded = json.dumps(context, sort_keys=True)
    assert "task_execution_context" in encoded


def test_nested_sets_and_dates_in_resources_are_json_safe():
    context = task_execution_context(
        task="Plan work.",
        artifacts=[{"name": "artifact", "tags": {"b", "a"}, "created": datetime(2026, 5, 9)}],
    )
    artifact = context["resources"]["artifacts"][0]
    assert artifact["tags"] == ["a", "b"]
    assert artifact["created"] == "2026-05-09T00:00:00"
    json.dumps(context)


def test_single_string_constraints_are_accepted_as_one_item():
    context = task_execution_context(task="Plan work.", constraints="Use synthetic data only.")
    assert context["constraints"] == ["Use synthetic data only."]


def test_plan_steps_have_professional_fields():
    context = task_execution_context(task="Plan work.", mode="planning")
    first = context["plan"][0]
    assert {"step", "phase", "action", "why", "done_when"} <= set(first)


def test_acceptance_stop_and_deliverables_are_preserved():
    context = task_execution_context(
        task="Test artifact.",
        acceptance_criteria=["Passes."],
        stop_conditions=["Fails import."],
        deliverables=["Summary."],
    )
    assert context["verification"]["acceptance_criteria"] == ["Passes."]
    assert context["verification"]["stop_conditions"] == ["Fails import."]
    assert context["verification"]["deliverables"] == ["Summary."]


def test_task_mode_priority_and_audience_type_aliases_are_importable():
    mode: TaskMode = "planning"
    priority: TaskPriority = "high"
    audience: Audience = "mixed"
    assert mode == "planning"
    assert priority == "high"
    assert audience == "mixed"


def test_no_pandas_or_spark_dependency_is_required():
    # The function should run using only the Python standard library.
    context = task_execution_context(task="Create a planning brief.")
    assert context["kind"] == "task_execution_context"


def test_context_can_drive_validation_artifact_testing_prompt():
    context = task_execution_context(
        task="Extract and test the validation_summary_context ZIP in Databricks.",
        subject="validation_summary_context artifact intake",
        goal="Verify pandas behavior before Spark work.",
        mode="testing",
        audience="agent",
        artifacts=[
            {
                "name": "odibi_validation_summary_context_pandas_mvp.zip",
                "type": "zip",
                "location": "features/validation_summary_context/incoming_artifacts/",
                "role": "candidate implementation",
            }
        ],
        constraints=[
            "Do not move files into production odibi package yet.",
            "Use synthetic pandas data.",
            "Do not begin Spark work.",
        ],
        out_of_scope=["Spark implementation"],
        acceptance_criteria=[
            "ZIP extracted.",
            "Expected files found.",
            "Pandas smoke tests pass.",
            "Output contract verified.",
        ],
        stop_conditions=[
            "Expected files are missing.",
            "Imports fail.",
            "Pandas tests fail.",
        ],
        deliverables=["Test summary", "Spark follow-up checklist"],
    )
    prompt = context["handoff"]["prompt_brief"]
    assert "validation_summary_context artifact intake" in prompt
    assert "Do not begin Spark work." in prompt
    assert "Pandas smoke tests pass." in prompt
    assert "Spark follow-up checklist" in prompt


def test_ownership_fields_are_preserved_and_prompted():
    context = task_execution_context(
        task="Plan artifact test.",
        subject="artifact test",
        audience="agent",
        requester="Henry",
        executor="Genie Code",
        priority="high",
        due_date="before Spark follow-up",
        stakeholders=["data engineering", "analytics engineering"],
    )
    assert context["ownership"] == {
        "requester": "Henry",
        "executor": "Genie Code",
        "stakeholders": ["data engineering", "analytics engineering"],
        "priority": "high",
        "due_date": "before Spark follow-up",
    }
    prompt = context["handoff"]["prompt_brief"]
    assert "Requester:" in prompt
    assert "Henry" in prompt
    assert "Executor:" in prompt
    assert "Genie Code" in prompt
    assert "Priority:" in prompt
    assert "high" in prompt


def test_unknown_priority_raises_value_error():
    with pytest.raises(ValueError, match="Unsupported priority"):
        task_execution_context(task="Plan work.", priority="urgent")  # type: ignore[arg-type]


def test_evidence_and_options_are_preserved_and_prompted():
    context = task_execution_context(
        task="Choose whether to promote artifact.",
        mode="decision",
        audience="agent",
        evidence=[{"name": "pytest report", "summary": "45 tests passed"}],
        options=[{"name": "promote", "description": "copy files into odibi/context"}],
    )
    assert context["context"]["evidence"] == [{"name": "pytest report", "summary": "45 tests passed"}]
    assert context["context"]["options"] == [{"name": "promote", "description": "copy files into odibi/context"}]
    prompt = context["handoff"]["prompt_brief"]
    assert "Evidence:" in prompt
    assert "pytest report" in prompt
    assert "Options:" in prompt
    assert "promote" in prompt
    assert context["metrics"]["evidence_count"] == 1
    assert context["metrics"]["option_count"] == 1


def test_expected_output_format_is_preserved_and_improves_hints():
    context = task_execution_context(
        task="Test artifact.",
        mode="testing",
        expected_output_format="Return a pass/fail table plus recommended next action.",
    )
    assert context["verification"]["expected_output_format"] == "Return a pass/fail table plus recommended next action."
    assert "Expected output format:" in context["handoff"]["prompt_brief"]
    assert not any("What format should the executor return" in q for q in context["hints"]["clarifying_questions"])


def test_hints_include_human_questions_and_prompt_quality_checks():
    context = task_execution_context(task="Test the ZIP.", mode="testing", audience="agent")
    hints = context["hints"]
    assert hints["ready_to_ask_ai"] is False
    assert any("What outcome" in q for q in hints["clarifying_questions"])
    assert any("inspect artifacts" in h.lower() for h in hints["prompting_hints"])
    assert any("production data" in h.lower() for h in hints["anti_patterns"])
    assert "Goal" in hints["recommended_prompt_sections"]
    assert any("expected output format" in h.lower() for h in hints["prompt_quality_checks"])


def test_ready_to_ask_ai_when_context_is_strong_enough():
    context = make_rich_context(
        expected_output_format="Return a concise test summary.",
        requester="Henry",
        executor="Genie Code",
    )
    assert context["hints"]["ready_to_ask_ai"] is True
    # Testing mode dimensions: goal, test_targets, coverage_criteria, test_data, scope, constraints
    assert context["readiness"]["dimensions"]["goal"]["status"] == "ready"
    assert context["readiness"]["dimensions"]["test_targets"]["status"] == "ready"


def test_readiness_dimensions_surface_missing_sections():
    context = task_execution_context(task="Do the thing.")
    dimensions = context["readiness"]["dimensions"]
    # Planning mode dimensions: goal, scope, constraints, background, risks, deliverables
    assert dimensions["goal"]["status"] == "missing"
    assert dimensions["scope"]["status"] == "missing"
    assert dimensions["constraints"]["status"] == "missing"
    assert dimensions["deliverables"]["status"] == "missing"


def test_hints_are_capped_by_max_items_per_section():
    context = task_execution_context(
        task="Plan work.",
        open_questions=["q1", "q2", "q3"],
        max_items_per_section=2,
    )
    assert len(context["hints"]["clarifying_questions"]) <= 2
    assert context["metadata"]["truncated"] is True


def test_v3_output_includes_discovery_section_and_metrics():
    context = task_execution_context(task="Plan a merge into silver.", mode="implementation")
    assert "discovery" in context
    assert {"evidence_gaps", "recommended_discovery_steps", "recommended_context_generators", "evidence_summary"} <= set(context["discovery"])
    assert context["metrics"]["evidence_gap_count"] == len(context["discovery"]["evidence_gaps"])
    assert context["metrics"]["discovery_step_count"] == len(context["discovery"]["recommended_discovery_steps"])
    assert context["metrics"]["recommended_context_generator_count"] == len(context["discovery"]["recommended_context_generators"])


def test_v3_explicit_evidence_gaps_and_discovery_steps_are_preserved_first():
    context = task_execution_context(
        task="Plan artifact testing.",
        mode="testing",
        evidence_gaps=["Need expected output examples."],
        recommended_discovery_steps=["Inspect the ZIP manifest before applying files."],
    )
    assert context["discovery"]["evidence_gaps"][0] == "Need expected output examples."
    assert context["discovery"]["recommended_discovery_steps"][0]["action"] == "Inspect the ZIP manifest before applying files."
    assert context["discovery"]["recommended_discovery_steps"][0]["why"] == "User-provided discovery step for this task."


def test_v3_merge_task_recommends_focused_context_generators_without_inspecting_data():
    context = task_execution_context(
        task="Plan a safe merge of bronze readings into silver target table.",
        subject="bronze to silver readings merge",
        mode="planning",
    )
    generators = context["discovery"]["recommended_context_generators"]
    names = [item["name"] for item in generators]
    statuses = {item["name"]: item["status"] for item in generators}
    assert "pre_merge" in names
    assert "quality" in names
    assert "schema_diff" in names
    assert statuses["pre_merge"] == "unknown"
    assert statuses["quality"] == "unknown"
    assert context["kind"] == "task_execution_context"


def test_v3_validation_task_recommends_validation_and_quality_context():
    context = task_execution_context(
        task="Plan validation and quality checks before promoting a dataset.",
        mode="planning",
    )
    generators = context["discovery"]["recommended_context_generators"]
    names = [item["name"] for item in generators]
    statuses = {item["name"]: item["status"] for item in generators}
    assert "validate" in names
    assert "quality" in names
    assert statuses["validate"] == "unknown"
    assert statuses["quality"] == "unknown"


def test_v3_testing_mode_without_evidence_surfaces_evidence_gap():
    context = task_execution_context(task="Test the generated ZIP.", mode="testing")
    gaps = " ".join(context["discovery"]["evidence_gaps"])
    assert "Artifact under test" in gaps
    assert "expected import path" in gaps
    assert any("What evidence should be gathered" in question for question in context["hints"]["clarifying_questions"])


def test_v3_evidence_is_used_as_planning_input_not_executed():
    context = task_execution_context(
        task="Plan whether to promote the artifact.",
        mode="decision",
        evidence=[{"name": "pytest report", "summary": "53 tests passed"}],
        acceptance_criteria=["All tests pass."],
        stop_conditions=["Any import error occurs."],
    )
    assert context["context"]["evidence"] == [{"name": "pytest report", "summary": "53 tests passed"}]
    assert context["discovery"]["evidence_summary"]["provided_count"] == 1
    assert context["readiness"]["dimensions"]["evidence"]["status"] == "ready"
    assert any("Review provided evidence" in step["action"] for step in context["discovery"]["recommended_discovery_steps"])


def test_v3_prompt_brief_includes_discovery_guidance():
    context = task_execution_context(
        task="Plan a safe merge into silver.",
        mode="planning",
        audience="agent",
        evidence_gaps=["Need duplicate key evidence."],
        recommended_discovery_steps=["Run duplicate key checks on proposed merge keys."],
    )
    prompt = context["handoff"]["prompt_brief"]
    assert "Evidence gaps:" in prompt
    assert "Need duplicate key evidence." in prompt
    assert "Recommended discovery steps:" in prompt
    assert "Run duplicate key checks" in prompt
    assert "Recommended context generators:" in prompt


def test_v32_discovery_lists_are_capped_except_exact_context_projection():
    context = task_execution_context(
        task="Plan a merge with validation, schema diff, duplicate keys, and pipeline checks.",
        mode="testing",
        evidence_gaps=["gap1", "gap2", "gap3"],
        recommended_discovery_steps=["step1", "step2", "step3"],
        max_items_per_section=2,
    )
    assert len(context["discovery"]["evidence_gaps"]) <= 2
    assert len(context["discovery"]["recommended_discovery_steps"]) <= 2
    assert context["discovery"]["recommended_context_generators"] == (
        project_context_generators(context["context_plan"])
    )
    assert context["metadata"]["truncated"] is True



def test_v31_discovery_recommendations_include_status_and_reason():
    context = task_execution_context(
        task="Plan validation checks before promoting a dataset.",
        mode="testing",
    )
    recommendation = context["discovery"]["recommended_context_generators"][0]
    assert {"name", "status", "reason"} <= set(recommendation)
    assert any(item["name"] == "validate" and item["status"] == "unknown" for item in context["discovery"]["recommended_context_generators"])


def test_v31_quick_context_returns_teammate_brief_string():
    brief = quick_context(
        "Plan the artifact test before Spark work.",
        goal="Get a clear pandas-first test plan.",
        mode="planning",
        constraints=["Do not start Spark yet."],
    )
    assert isinstance(brief, str)
    assert "Task:" in brief
    assert "Readiness:" in brief
    assert "Recommended next actions:" in brief
    assert "Spark" in brief or "spark" in brief


def test_v31_weighted_readiness_privileges_goal_and_acceptance_criteria():
    no_goal = task_execution_context(
        task="Plan work.",
        acceptance_criteria=["Done criteria exists."],
        in_scope=["One thing."],
        constraints=["No scope creep."],
        artifacts=[{"name": "artifact"}],
        risks=["Risk."],
        deliverables=["Summary."],
    )
    with_goal = task_execution_context(
        task="Plan work.",
        goal="Produce a clear execution brief.",
        acceptance_criteria=["Done criteria exists."],
        in_scope=["One thing."],
        constraints=["No scope creep."],
        artifacts=[{"name": "artifact"}],
        risks=["Risk."],
        deliverables=["Summary."],
    )
    # Planning mode: goal weighs 30 points
    assert with_goal["readiness"]["score"] - no_goal["readiness"]["score"] == 30


def test_v31_prompt_brief_formats_context_generator_status():
    context = task_execution_context(
        task="Plan validation and quality checks before promoting a dataset.",
        mode="testing",
        audience="agent",
    )
    prompt = context["handoff"]["prompt_brief"]
    assert "validate (unknown)" in prompt
    assert "quality (unknown)" in prompt



# ---------------------------------------------------------------------------
# Guardrails Tests (v0.3.1)
# ---------------------------------------------------------------------------


class TestGuardrails:
    """Tests for the guardrails parameter."""

    def test_guardrails_none_by_default(self):
        """Guardrails block exists with defaults when not provided."""
        ctx = task_execution_context(task="Do something", mode="implementation")
        assert "guardrails" in ctx
        assert ctx["guardrails"]["do_not_modify"] == []
        assert ctx["guardrails"]["do_not_create"] == []
        assert ctx["guardrails"]["require_verification"] == []
        assert ctx["guardrails"]["max_files_changed"] is None
        assert ctx["guardrails"]["has_boundaries"] is False

    def test_guardrails_populated(self):
        """Guardrails block reflects provided values."""
        ctx = task_execution_context(
            task="Add feature X",
            mode="implementation",
            guardrails={
                "do_not_modify": ["src/existing.py", "README.md"],
                "do_not_create": ["src/framework.py"],
                "max_files_changed": 3,
                "require_verification": ["pytest passes", "no new imports"],
            },
        )
        g = ctx["guardrails"]
        assert g["do_not_modify"] == ["src/existing.py", "README.md"]
        assert g["do_not_create"] == ["src/framework.py"]
        assert g["max_files_changed"] == 3
        assert g["require_verification"] == ["pytest passes", "no new imports"]
        assert g["has_boundaries"] is True

    def test_guardrails_merges_with_existing_params(self):
        """Guardrails out_of_scope merges with the out_of_scope parameter."""
        ctx = task_execution_context(
            task="Fix bug",
            mode="debugging",
            out_of_scope=["Refactoring"],
            guardrails={"out_of_scope": ["Adding new features"]},
        )
        assert "Refactoring" in ctx["guardrails"]["out_of_scope"]
        assert "Adding new features" in ctx["guardrails"]["out_of_scope"]

    def test_guardrails_in_metrics(self):
        """Guardrail count appears in metrics."""
        ctx = task_execution_context(
            task="Migrate module",
            mode="migration",
            guardrails={
                "do_not_modify": ["src/a.py"],
                "require_verification": ["tests pass"],
            },
        )
        assert ctx["metrics"]["guardrail_count"] == 2

    def test_guardrails_markdown_render(self):
        """Guardrails section appears in markdown output."""
        ctx = task_execution_context(
            task="Build feature",
            mode="implementation",
            guardrails={"do_not_modify": ["README.md"]},
        )
        md = render_task_execution_report(ctx)
        assert "## Guardrails" in md
        assert "`README.md`" in md

    def test_guardrails_invalid_max_files_ignored(self):
        """Invalid max_files_changed values are ignored."""
        ctx = task_execution_context(
            task="Test",
            mode="testing",
            guardrails={"max_files_changed": -1},
        )
        assert ctx["guardrails"]["max_files_changed"] is None

    def test_guardrails_constraints_merge(self):
        """Guardrails constraints merge with constraints parameter."""
        ctx = task_execution_context(
            task="Build",
            mode="implementation",
            constraints=["No frameworks"],
            guardrails={"constraints": ["No global state"]},
        )
        assert "No frameworks" in ctx["guardrails"]["constraints"]
        assert "No global state" in ctx["guardrails"]["constraints"]

    def test_guardrails_json_serializable(self):
        """Output with guardrails passes json.dumps."""
        import json
        ctx = task_execution_context(
            task="Build",
            mode="implementation",
            guardrails={
                "do_not_modify": ["a.py"],
                "max_files_changed": 5,
                "require_verification": ["tests"],
            },
        )
        json.dumps(ctx)  # Should not raise
