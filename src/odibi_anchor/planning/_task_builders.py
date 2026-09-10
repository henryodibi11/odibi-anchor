"""Builder and helper functions for task_execution_context."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from odibi_anchor.planning._task_policy import TaskPolicyContext
    from odibi_anchor.planning._task_profile import TaskProfile

from odibi_anchor.planning._task_config import (
    _MODE_EVIDENCE_GAPS,
    _MODE_HINTS,
    _READINESS_GAP_QUESTIONS,
)
from odibi_anchor.planning._task_helpers import (
    _cap_list,
    _clean_text,
    _dedupe_preserve_order,
)
from odibi_anchor.planning._task_render import _format_context_generator
from odibi_anchor.planning.context_selection import project_context_generators

# ─── Mode-Specific Readiness Dimensions ──────────────────────────────────────
# Each mode defines its own dimensions with weights summing to 100.
# Format: {mode: [(dimension_name, weight, evaluator_key), ...]}
# evaluator_key maps to a boolean check in _evaluate_dimension().

_MODE_DIMENSIONS: dict[str, list[tuple[str, int, str]]] = {
    "documentation": [
        ("goal", 50, "has_goal"),
        ("document_scope", 50, "has_scope"),
    ],
    "implementation": [
        ("goal", 20, "has_goal"),
        ("acceptance_criteria", 20, "has_acceptance_criteria"),
        ("scope", 15, "has_scope"),
        ("resources", 15, "has_resources"),
        ("constraints", 10, "has_constraints"),
        ("risks", 10, "has_risks"),
        ("background", 5, "has_background"),
        ("deliverables", 5, "has_deliverables"),
    ],
    "analysis": [
        ("goal", 30, "has_goal"),
        ("data_sources", 25, "has_resources"),
        ("questions", 20, "has_acceptance_criteria"),
        ("output_format", 15, "has_deliverables"),
        ("constraints", 10, "has_constraints"),
    ],
    "debugging": [
        ("symptom", 25, "has_goal"),
        ("reproduction", 20, "has_scope"),
        ("expected_vs_actual", 20, "has_acceptance_criteria"),
        ("error_context", 15, "has_background"),
        ("scope", 10, "has_constraints"),
        ("risks", 10, "has_risks"),
    ],
    "testing": [
        ("goal", 20, "has_goal"),
        ("test_targets", 25, "has_scope"),
        ("coverage_criteria", 20, "has_acceptance_criteria"),
        ("test_data", 15, "has_resources"),
        ("scope", 10, "has_constraints"),
        ("constraints", 10, "has_risks"),
    ],
    "planning": [
        ("goal", 30, "has_goal"),
        ("scope", 20, "has_scope"),
        ("constraints", 15, "has_constraints"),
        ("background", 15, "has_background"),
        ("risks", 10, "has_risks"),
        ("deliverables", 10, "has_deliverables"),
    ],
    "migration": [
        ("goal", 15, "has_goal"),
        ("source_target", 20, "has_scope"),
        ("breaking_changes", 20, "has_risks"),
        ("rollback_plan", 15, "has_acceptance_criteria"),
        ("scope", 10, "has_constraints"),
        ("acceptance_criteria", 10, "has_deliverables"),
        ("risks", 10, "has_background"),
    ],
    "review": [
        ("goal", 30, "has_goal"),
        ("review_targets", 25, "has_scope"),
        ("standards", 20, "has_constraints"),
        ("scope", 15, "has_acceptance_criteria"),
        ("output_format", 10, "has_deliverables"),
    ],
    "spec_creation": [
        ("goal", 25, "has_goal"),
        ("scope", 20, "has_scope"),
        ("standards", 15, "has_constraints"),
        ("acceptance_criteria", 15, "has_acceptance_criteria"),
        ("constraints", 10, "has_risks"),
        ("background", 10, "has_background"),
        ("deliverables", 5, "has_deliverables"),
    ],
    "greenfield": [
        ("goal", 40, "has_goal"),
        ("scope", 25, "has_scope"),
        ("acceptance_criteria", 20, "has_acceptance_criteria"),
        ("constraints", 15, "has_constraints"),
    ],
}

# Keep for backward compat — modes not in _MODE_DIMENSIONS use legacy scoring
_READ_ONLY_MODES = frozenset({"analysis", "review", "decision", "retrospective", "handoff"})

# Data modes whose Spec persistence requires observed source evidence. This is an
# evidence rule, not a second source of Spec policy: whether a task requires a
# Spec is decided by ``evaluate_task_policies`` from the accepted task profile.
_DATA_SPEC_MODES: frozenset[str] = frozenset({"data", "etl", "reconciliation"})

def required_skills_for_task(
    task: TaskProfile | TaskPolicyContext,
    *,
    specification_disposition: str = "not_required",
    explicit_formal_spec: bool | None = None,
) -> tuple[str, ...]:
    """Resolve direct native requirements from one accepted-task contract.

    Spec policy is an input rather than a pseudo-skill.  A policy context carries
    the production explicit-Spec signal; profile-only callers may supply that
    signal explicitly (principally deterministic render/review callers).
    """
    from odibi_anchor.planning._task_policy import TaskPolicyContext
    from odibi_anchor.planning._task_profile import TaskProfile

    if specification_disposition not in {"required", "recommended", "not_required"}:
        raise ValueError("invalid specification disposition")
    # ``bootstrap.init()`` deliberately flushes and reloads Odibi Anchor
    # modules. Objects created before that reload retain an equivalent immutable
    # contract but no longer satisfy identity-based ``isinstance`` checks.
    is_policy_context = (
        isinstance(task, TaskPolicyContext)
        or (task.__class__.__name__ == "TaskPolicyContext" and hasattr(task, "profile"))
    )
    is_task_profile = (
        isinstance(task, TaskProfile)
        or (task.__class__.__name__ == "TaskProfile" and hasattr(task, "to_dict"))
    )
    if is_policy_context:
        profile = task.profile
        if explicit_formal_spec is None:
            explicit_formal_spec = task.explicit_spec_requested
    elif is_task_profile:
        profile = task
    else:
        raise TypeError("task must be a TaskProfile or TaskPolicyContext")
    mode = profile.legacy_mode or ""
    domains, traits = set(profile.domains), set(profile.traits)
    selected: set[str] = set()
    if "memory-governance" in traits:
        selected.add("auditing-memory-governance")
    if "memory-authoring" in traits:
        selected.add("authoring-governed-memories")
    if "memory-packaging" in traits:
        selected.add("building-memory-packs")
    if mode == "documentation":
        selected.add("documentation")
    if mode == "debugging":
        selected.add("debugging")
    if mode in {"analysis", "review"} and ("code" in domains or "comprehension" in traits):
        selected.add("code-comprehension")
    if mode == "review" and traits & {"pr", "cross-functional-review"}:
        selected.add("cross-functional-pr")
    if mode == "testing" and traits & {"test-authoring", "test-repair", "test-strategy"}:
        selected.add("writing-tests")
    if specification_disposition == "required" or explicit_formal_spec or mode == "spec_creation":
        selected.add("writing-specs")
    dual_source_data_change = {"source-change", "data-change"}.issubset(traits)
    if profile.execution_mode == "data_change" or "mutation" in traits or dual_source_data_change:
        selected.add("data-operations")
    if traits & {"onboarding", "ingestion", "refresh", "new-source"}:
        selected.add("data-onboarding")
    if "reconciliation" in traits or mode == "reconciliation":
        selected.add("data-reconciliation")
    for trait, owner in {
        "dependency": "dependency-management",
        "incident": "incident-response",
        "performance": "performance-investigation",
        "schema": "schema-design",
        "work-item": "work-item-management",
    }.items():
        if trait in traits:
            selected.add(owner)
    from odibi_anchor._dispatcher._guidance import NATIVE_SKILLS
    return tuple(name for name in NATIVE_SKILLS if name in selected)


# Actions EXEMPT from the skill-load gate. The gate fires before any *substantive
# work* action once a task mode is active, so the pre-action skill layer has the
# same teeth as file edits — closing the read-only/analysis hole where planning /
# anti-rationalization / comprehension skills could be skipped entirely.
#
# Exempt = the orientation + planning + compliance + meta machinery (the actions
# you run to GET oriented, plan, satisfy gates, save memory, or inspect state).
# Everything NOT listed here is treated as "work" and requires the mode's skills.
# Fail-closed: a newly added data/analysis tool is gated by default.
_SKILL_GATE_EXEMPT: frozenset[str] = frozenset({
    # orientation / planning sequence
    "status", "memory", "audit_history", "new_session", "task", "orient",
    # skills + help
    "skill_loaded", "skills", "help", "context", "prepare",
    # compliance / delivery machinery
    "gate", "review", "preflight", "test", "checkpoint", "learn",
    # memory lifecycle
    "save", "confirm", "reject", "archive", "export_md", "import_md",
    "memory_stats", "memory_tags",
    # session io / snapshots
    "snapshot", "save_snap", "load_snap",
    "session_files", "session_diff", "session_delta", "session_log", "log",
    # diagnostics / lookups (orientation aids, not analysis deliverables)
    "known_error", "trace", "lookup", "import_resolve", "known_bad", "convention",
    # spec workflow
    "spec", "work_item",
    # meta / admin
    "config", "project", "problem", "manifest", "tools", "frame", "sync", "dogfood",
    "register_tool", "clear_cache", "db_migrate", "memory_hygiene", "quick", "echo",
})


def _build_readiness(
    *,
    goal: str | None,
    desired_outcome: str | None,
    background: str | None,
    current_state: str | None,
    trigger: str | None,
    constraints: list[str],
    in_scope: list[str],
    out_of_scope: list[str],
    artifacts: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    dependencies: list[str],
    acceptance_criteria: list[str],
    risks: list[str],
    stop_conditions: list[str],
    deliverables: list[str],
    expected_output_format: str | None,
    requester: str | None,
    executor: str | None,
    evidence: list[dict[str, Any]],
    evidence_gaps: list[str],
    known_facts: list[str] | None = None,
    mode: str = "planning",
) -> dict[str, Any]:
    """Score task readiness using mode-specific weighted dimensions.

    Each mode defines its own set of dimensions and weights in _MODE_DIMENSIONS.
    When mode is not in _MODE_DIMENSIONS, falls back to legacy fixed-weight scoring.
    All modes with defined dimensions use uniform thresholds (80/50) since
    dimensions are appropriately weighted per mode.
    """
    _known_facts = known_facts or []

    # ── Evaluate boolean conditions ──
    has_background = bool(background or current_state or trigger)
    normalized_known_facts = [fact.lower() for fact in _known_facts]
    background_fact_markers = (
        "current state:",
        "background:",
        "existing state:",
        "prior work:",
        "already done:",
        "trigger:",
    )
    has_background_fact = any(
        any(marker in fact for marker in background_fact_markers)
        for fact in normalized_known_facts
    )
    has_resources = bool(artifacts or inputs or dependencies)
    has_resource_fact = any(
        kw in fact
        for fact in normalized_known_facts
        for kw in ("/", ".py", ".sql", ".csv", "catalog.", "schema.", "table", "file:", "path:")
    )
    has_evidence = bool(evidence)
    has_grounded_background = has_background or has_background_fact
    has_grounded_resources = has_resources or has_resource_fact or has_evidence
    has_grounding = has_grounded_background and has_grounded_resources

    # ── Build evaluator map ──
    evaluators = {
        "has_goal": bool(goal or desired_outcome),
        "has_acceptance_criteria": bool(acceptance_criteria),
        "has_scope": bool(in_scope or out_of_scope),
        "has_background": has_grounded_background,
        "has_constraints": bool(constraints),
        "has_resources": has_grounded_resources,
        "has_risks": bool(risks or stop_conditions),
        "has_deliverables": bool(deliverables or expected_output_format),
    }

    # ── Score using mode-specific dimensions ──
    if mode in _MODE_DIMENSIONS:
        dimensions_def = _MODE_DIMENSIONS[mode]
        score = 0
        strengths = ["Task statement is present."]
        missing_details = []
        recommended_clarifications = []
        dimension_results = {}

        for dim_name, weight, evaluator_key in dimensions_def:
            is_satisfied = evaluators.get(evaluator_key, False)
            if is_satisfied:
                score += weight
                strengths.append(_DIMENSION_STRENGTHS.get(
                    evaluator_key, f"{dim_name.replace('_', ' ').title()} is satisfied ({weight}pts)."
                ))
            else:
                missing_details.append(_DIMENSION_MISSING.get(
                    evaluator_key, f"{dim_name.replace('_', ' ').title()} is missing."
                ))
                clarification = _DIMENSION_CLARIFICATIONS.get(
                    evaluator_key, f"Provide {dim_name.replace('_', ' ')} information."
                )
                recommended_clarifications.append(clarification)
            dimension_results[dim_name] = _dimension_status(
                is_satisfied,
                f"{dim_name.replace('_', ' ').title()} is present.",
                f"{dim_name.replace('_', ' ').title()} is missing.",
            )

        # Grounding cap: even at 100, cap to 90 if background+resources not both grounded
        capped_score = min(score, 100)
        if capped_score == 100 and not has_grounding:
            capped_score = 90
            strengths.append(
                "Grounding is incomplete, so perfect readiness is capped until "
                "background plus resources or evidence are explicit."
            )

        # Uniform thresholds — mode-specific dimensions already encode appropriate weight
        ready_threshold, clarify_threshold = 80, 50
        status = (
            "ready" if capped_score >= ready_threshold
            else "needs_clarification" if capped_score >= clarify_threshold
            else "under_specified"
        )

        return {
            "status": status,
            "score": capped_score,
            "strengths": strengths,
            "missing_details": missing_details,
            "recommended_clarifications": recommended_clarifications,
            "dimensions": dimension_results,
        }

    # ── Legacy fallback for unknown modes ──
    return _build_readiness_legacy(
        goal=goal,
        desired_outcome=desired_outcome,
        has_grounded_background=has_grounded_background,
        has_background=has_background,
        has_background_fact=has_background_fact,
        constraints=constraints,
        in_scope=in_scope,
        out_of_scope=out_of_scope,
        has_resources=has_resources,
        has_resource_fact=has_resource_fact,
        has_evidence=has_evidence,
        has_grounded_resources=has_grounded_resources,
        has_grounding=has_grounding,
        acceptance_criteria=acceptance_criteria,
        risks=risks,
        stop_conditions=stop_conditions,
        deliverables=deliverables,
        expected_output_format=expected_output_format,
        requester=requester,
        executor=executor,
        evidence=evidence,
        evidence_gaps=evidence_gaps,
        artifacts=artifacts,
        inputs=inputs,
        dependencies=dependencies,
        mode=mode,
    )


# ── Dimension messages (matching legacy format for backward compatibility) ──
_DIMENSION_STRENGTHS: dict[str, str] = {
    "has_goal": "Goal or desired outcome is explicit.",
    "has_acceptance_criteria": "Acceptance criteria are defined.",
    "has_scope": "Scope boundaries are described.",
    "has_background": "Background or current state is described.",
    "has_constraints": "Execution constraints are explicit.",
    "has_resources": "Resources or dependencies are identified.",
    "has_risks": "Risks or stop conditions are explicit.",
    "has_deliverables": "Expected deliverables or output format are listed.",
}

_DIMENSION_MISSING: dict[str, str] = {
    "has_goal": "Goal or desired outcome is missing.",
    "has_acceptance_criteria": "Acceptance criteria are missing.",
    "has_scope": "In-scope or out-of-scope boundaries are missing.",
    "has_background": "Background or current state is missing.",
    "has_constraints": "Constraints are missing.",
    "has_resources": "Artifacts, inputs, or dependencies are missing.",
    "has_risks": "Risks or stop conditions are missing.",
    "has_deliverables": "Expected deliverables are missing.",
}

_DIMENSION_CLARIFICATIONS: dict[str, str] = {
    "has_goal": "State the intended outcome in one sentence. [kwarg: goal='...']",
    "has_acceptance_criteria": (
        "Define what done means and how success will be checked. [kwarg: acceptance_criteria=[...]]"
    ),
    "has_scope": "Define what is included and excluded. [kwargs: in_scope=[...], out_of_scope=[...]]",
    "has_background": (
        "Explain why this task exists and what state it starts from. "
        "[kwargs: background='...' or current_state='...' or known_facts=['Current state: ...']]"
    ),
    "has_constraints": "List rules the executor must follow. [kwarg: constraints=[...]]",
    "has_resources": (
        "List available files, data, systems, or dependencies. "
        "[kwargs: artifacts=[{'path': '...'}], inputs=[{'name': '...'}], "
        "dependencies=[...], or known_facts=['File: path/to/file.py']]"
    ),
    "has_risks": (
        "List what could go wrong and when execution should pause. "
        "[kwargs: risks=[...], stop_conditions=[...]]"
    ),
    "has_deliverables": (
        "List what the executor should return or produce. "
        "[kwargs: deliverables=[...] or expected_output_format='...']"
    ),
}


def _build_readiness_legacy(
    *,
    goal: str | None,
    desired_outcome: str | None,
    has_grounded_background: bool,
    has_background: bool,
    has_background_fact: bool,
    constraints: list[str],
    in_scope: list[str],
    out_of_scope: list[str],
    has_resources: bool,
    has_resource_fact: bool,
    has_evidence: bool,
    has_grounded_resources: bool,
    has_grounding: bool,
    acceptance_criteria: list[str],
    risks: list[str],
    stop_conditions: list[str],
    deliverables: list[str],
    expected_output_format: str | None,
    requester: str | None,
    executor: str | None,
    evidence: list[dict[str, Any]],
    evidence_gaps: list[str],
    artifacts: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    dependencies: list[str],
    mode: str,
) -> dict[str, Any]:
    """Legacy fixed-weight scoring for modes not in _MODE_DIMENSIONS."""
    score = 0
    strengths = ["Task statement is present."]
    missing_details = []
    recommended_clarifications = []

    if goal or desired_outcome:
        score += 20
        strengths.append("Goal or desired outcome is explicit.")
    else:
        missing_details.append("Goal or desired outcome is missing.")
        recommended_clarifications.append(
            "State the intended outcome in one sentence. [kwarg: goal='...']"
        )

    if acceptance_criteria:
        score += 20
        strengths.append("Acceptance criteria are defined.")
    else:
        missing_details.append("Acceptance criteria are missing.")
        recommended_clarifications.append(
            "Define what done means and how success will be checked. [kwarg: acceptance_criteria=[...]]"
        )

    if in_scope or out_of_scope:
        score += 15
        strengths.append("Scope boundaries are described.")
    else:
        missing_details.append("In-scope or out-of-scope boundaries are missing.")
        recommended_clarifications.append(
            "Define what is included and excluded. [kwargs: in_scope=[...], out_of_scope=[...]]"
        )

    if has_background:
        score += 10
        strengths.append("Background or current state is described.")
    elif has_background_fact:
        score += 10
        strengths.append("Background inferred from known_facts.")
    else:
        missing_details.append("Background or current state is missing.")
        recommended_clarifications.append(
            "Explain why this task exists and what state it starts from. "
            "[kwargs: background='...' or current_state='...' or known_facts=['Current state: ...']]"
        )

    if constraints:
        score += 10
        strengths.append("Execution constraints are explicit.")
    else:
        missing_details.append("Constraints are missing.")
        recommended_clarifications.append(
            "List rules the executor must follow. [kwarg: constraints=[...]]"
        )

    if has_resources:
        score += 10
        strengths.append("Resources or dependencies are identified.")
    elif has_resource_fact:
        score += 10
        strengths.append("Resources inferred from known_facts (file paths or tables detected).")
    elif has_evidence:
        score += 10
        strengths.append("Supporting evidence is available even without explicit resources.")
    else:
        missing_details.append("Artifacts, inputs, or dependencies are missing.")
        recommended_clarifications.append(
            "List available files, data, systems, or dependencies. "
            "[kwargs: artifacts=[{'path': '...'}], inputs=[{'name': '...'}], "
            "dependencies=[...], or known_facts=['File: path/to/file.py']]"
        )

    if risks or stop_conditions:
        score += 10
        strengths.append("Risks or stop conditions are explicit.")
    else:
        missing_details.append("Risks or stop conditions are missing.")
        recommended_clarifications.append(
            "List what could go wrong and when execution should pause. "
            "[kwargs: risks=[...], stop_conditions=[...]]"
        )

    if deliverables or expected_output_format:
        score += 5
        strengths.append("Expected deliverables or output format are listed.")
    else:
        missing_details.append("Expected deliverables are missing.")
        recommended_clarifications.append(
            "List what the executor should return or produce. "
            "[kwargs: deliverables=[...] or expected_output_format='...']"
        )

    dimensions = _build_readiness_dimensions(
        goal=goal,
        desired_outcome=desired_outcome,
        background=None,
        current_state=None,
        trigger=None,
        constraints=constraints,
        in_scope=in_scope,
        out_of_scope=out_of_scope,
        artifacts=artifacts,
        inputs=inputs,
        dependencies=dependencies,
        acceptance_criteria=acceptance_criteria,
        risks=risks,
        stop_conditions=stop_conditions,
        deliverables=deliverables,
        expected_output_format=expected_output_format,
        requester=requester,
        executor=executor,
        evidence=evidence,
        evidence_gaps=evidence_gaps,
        has_grounded_background=has_grounded_background,
        has_grounded_resources=has_grounded_resources,
    )

    if expected_output_format:
        strengths.append("Expected output format is specified.")

    capped_score = min(score, 100)
    if capped_score == 100 and not has_grounding:
        capped_score = 90
        strengths.append(
            "Grounding is incomplete, so perfect readiness is capped until "
            "background plus resources or evidence are explicit."
        )

    # Legacy threshold hack for backward compat
    if mode in _READ_ONLY_MODES:
        ready_threshold, clarify_threshold = 40, 20
    else:
        ready_threshold, clarify_threshold = 80, 50

    status = (
        "ready" if capped_score >= ready_threshold
        else "needs_clarification" if capped_score >= clarify_threshold
        else "under_specified"
    )
    return {
        "status": status,
        "score": capped_score,
        "strengths": strengths,
        "missing_details": missing_details,
        "recommended_clarifications": recommended_clarifications,
        "dimensions": dimensions,
    }


def _build_readiness_dimensions(
    *,
    goal: str | None,
    desired_outcome: str | None,
    background: str | None,
    current_state: str | None,
    trigger: str | None,
    constraints: list[str],
    in_scope: list[str],
    out_of_scope: list[str],
    artifacts: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    dependencies: list[str],
    acceptance_criteria: list[str],
    risks: list[str],
    stop_conditions: list[str],
    deliverables: list[str],
    expected_output_format: str | None,
    requester: str | None,
    executor: str | None,
    evidence: list[dict[str, Any]],
    evidence_gaps: list[str],
    has_grounded_background: bool,
    has_grounded_resources: bool,
) -> dict[str, dict[str, Any]]:
    """Score readiness dimensions without changing the public API."""
    return {
        "intent": _dimension_status(
            bool(goal or desired_outcome),
            "Goal or desired outcome is present.",
            "Goal or desired outcome is missing.",
        ),
        "background": _dimension_status(
            has_grounded_background,
            "Background, current state, or trigger is present.",
            "Background or current state is missing.",
        ),
        "scope": _dimension_status(
            bool(in_scope or out_of_scope),
            "Scope boundaries are present.",
            "In-scope or out-of-scope boundaries are missing.",
        ),
        "resources": _dimension_status(
            has_grounded_resources,
            "Resources, dependencies, or supporting evidence are present.",
            "Artifacts, inputs, or dependencies are missing.",
        ),
        "constraints": _dimension_status(
            bool(constraints),
            "Constraints are present.",
            "Constraints are missing.",
        ),
        "verification": _dimension_status(
            bool(acceptance_criteria and (risks or stop_conditions)),
            "Acceptance criteria plus risks or stop conditions are present.",
            "Acceptance criteria and stop conditions are incomplete.",
        ),
        "evidence": _dimension_status(
            bool(evidence),
            "Evidence has been provided for this planning pass.",
            "No supporting evidence has been provided yet." if not evidence_gaps else "Evidence gaps are explicit but unresolved.",
        ),
        "handoff": _dimension_status(
            bool(deliverables or expected_output_format or requester or executor),
            "Handoff target or deliverable shape is present.",
            "Requester, executor, deliverables, or expected output format are missing.",
        ),
    }


def _dimension_status(is_ready: bool, ready_message: str, missing_message: str) -> dict[str, Any]:
    return {
        "status": "ready" if is_ready else "missing",
        "score": 100 if is_ready else 0,
        "message": ready_message if is_ready else missing_message,
    }


def _build_discovery(
    *,
    mode: str,
    readiness: dict[str, Any],
    artifacts: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    dependencies: list[str],
    evidence: list[dict[str, Any]],
    evidence_gaps: list[str],
    user_discovery_steps: list[str],
    known_facts: list[str],
    assumptions: list[str],
    open_questions: list[str],
    decisions_needed: list[str],
    constraints: list[str],
    acceptance_criteria: list[str],
    stop_conditions: list[str],
    max_items: int,
    metadata: dict[str, Any],
    context_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Build evidence-gap and discovery guidance without performing discovery."""
    generated_gaps = _infer_evidence_gaps(
        mode=mode,
        readiness=readiness,
        artifacts=artifacts,
        inputs=inputs,
        dependencies=dependencies,
        evidence=evidence,
        known_facts=known_facts,
        assumptions=assumptions,
        open_questions=open_questions,
        decisions_needed=decisions_needed,
        constraints=constraints,
        acceptance_criteria=acceptance_criteria,
        stop_conditions=stop_conditions,
    )
    combined_gaps = _cap_list(
        _dedupe_preserve_order([*evidence_gaps, *generated_gaps]),
        max_items=max_items,
        metadata=metadata,
    )
    recommended_generators = _recommend_context_generators(context_plan=context_plan)
    discovery_steps = _build_discovery_steps(
        evidence=evidence,
        evidence_gaps=combined_gaps,
        user_discovery_steps=user_discovery_steps,
        recommended_context_generators=recommended_generators,
        mode=mode,
        max_items=max_items,
        metadata=metadata,
    )
    return {
        "evidence_gaps": combined_gaps,
        "recommended_discovery_steps": discovery_steps,
        "recommended_context_generators": recommended_generators,
        "evidence_summary": {
            "provided_count": len(evidence),
            "gap_count": len(combined_gaps),
            "recommended_step_count": len(discovery_steps),
            "recommended_context_generator_count": len(recommended_generators),
            "has_enough_evidence_for_execution": bool(evidence) and not combined_gaps,
        },
    }


def _infer_evidence_gaps(
    *,
    mode: str,
    readiness: dict[str, Any],
    artifacts: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    dependencies: list[str],
    evidence: list[dict[str, Any]],
    known_facts: list[str],
    assumptions: list[str],
    open_questions: list[str],
    decisions_needed: list[str],
    constraints: list[str],
    acceptance_criteria: list[str],
    stop_conditions: list[str],
) -> list[str]:
    gaps: list[str] = []
    if not evidence and mode in {"implementation", "testing", "debugging", "review", "decision", "analysis"}:
        gaps.extend(_MODE_EVIDENCE_GAPS[mode])
    if not artifacts and mode in {"testing", "migration", "review"}:
        gaps.append("Artifact, patch, changed files, or target paths to inspect before execution.")
    if not inputs and mode in {"testing", "analysis", "debugging"}:
        gaps.append("Safe synthetic inputs, reproduction inputs, or evidence sources needed to validate behavior.")
    if not dependencies and mode in {"implementation", "migration", "testing"}:
        gaps.append("Environment or package dependencies needed before execution can be trusted.")
    if assumptions and not known_facts:
        gaps.append("Evidence needed to convert key assumptions into known facts.")
    if open_questions:
        gaps.append("Answers to open questions that affect scope, implementation, or verification.")
    if decisions_needed:
        gaps.append("Decision criteria or evidence needed to resolve pending decisions.")
    if not constraints:
        gaps.append("Guardrails and non-goals needed to prevent scope creep or unsafe execution.")
    if not acceptance_criteria:
        gaps.append("Acceptance criteria or expected outputs needed to define done.")
    if not stop_conditions and mode in {"testing", "debugging", "migration", "implementation"}:
        gaps.append("Stop conditions that tell the executor when to pause instead of continuing.")
    for missing in readiness.get("missing_details", []):
        if missing == "Background or current state is missing.":
            gaps.append("Current state or prior work needed to avoid repeating effort.")
        elif missing == "Artifacts, inputs, or dependencies are missing.":
            gaps.append("Concrete resources needed for execution: files, tables, notebooks, tickets, data, or dependencies.")
    return gaps


def _recommend_context_generators(
    *,
    context_plan: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Compatibility projection of one already-selected canonical context plan."""
    return project_context_generators(context_plan)


def _build_discovery_steps(
    *,
    evidence: list[dict[str, Any]],
    evidence_gaps: list[str],
    user_discovery_steps: list[str],
    recommended_context_generators: list[dict[str, str]],
    mode: str,
    max_items: int,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step in user_discovery_steps:
        steps.append(
            {
                "action": step,
                "why": "User-provided discovery step for this task.",
                "suggested_tool": None,
            }
        )
    if evidence:
        steps.append(
            {
                "action": "Review provided evidence before changing the execution plan.",
                "why": "Evidence should influence scope, risks, stop conditions, and acceptance criteria.",
                "suggested_tool": None,
            }
        )
    if evidence_gaps:
        steps.append(
            {
                "action": "Resolve or explicitly defer the highest-impact evidence gaps.",
                "why": "Unresolved evidence gaps are the main reason plans fail during execution.",
                "suggested_tool": None,
            }
        )
    if recommended_context_generators:
        steps.append(
            {
                "action": (
                    "Evaluate candidate context actions and invoke one only after "
                    "confirming required inputs and runtime support."
                ),
                "why": (
                    "Candidate names identify possible evidence channels; dispatcher "
                    "recognition and call history do not establish runnable capability or evidence."
                ),
                "suggested_tool": ", ".join(_format_context_generator(item) for item in recommended_context_generators[:5]),
            }
        )
    if not steps:
        steps.append(
            {
                "action": "Proceed with the current plan, then re-check acceptance criteria before execution.",
                "why": "No obvious discovery gap was detected from the supplied context.",
                "suggested_tool": None,
            }
        )
    numbered = [{"step": idx, **step} for idx, step in enumerate(steps, start=1)]
    return _cap_list(numbered, max_items=max_items, metadata=metadata)

def _build_hints(
    *,
    mode: str,
    audience: str,
    readiness: dict[str, Any],
    open_questions: list[str],
    decisions_needed: list[str],
    artifacts: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    constraints: list[str],
    acceptance_criteria: list[str],
    expected_output_format: str | None,
    evidence: list[dict[str, Any]],
    discovery: dict[str, Any],
    max_items: int,
    metadata: dict[str, Any],
) -> dict[str, list[str] | bool]:
    """Build human-first planning and prompt engineering hints."""
    mode_hints = _MODE_HINTS[mode]
    clarifying_questions = _questions_from_gaps(readiness.get("missing_details", []))
    clarifying_questions.extend(open_questions)

    if decisions_needed:
        clarifying_questions.append("Which decisions must be made before execution can continue?")
    if artifacts and not inputs and mode in {"testing", "implementation", "migration"}:
        clarifying_questions.append("What synthetic or safe runtime inputs should be used to exercise the artifacts?")
    if not expected_output_format:
        clarifying_questions.append("What format should the executor return: summary, table, patch, notebook, test log, or decision memo?")
    if not evidence and mode in {"implementation", "testing", "debugging", "review", "decision", "analysis"}:
        clarifying_questions.insert(0, "What evidence should be gathered before trusting this plan?")
    if discovery.get("evidence_gaps"):
        clarifying_questions.insert(1, "Which evidence gaps must be resolved before execution, and which can be deferred?")
    if audience == "agent" and not constraints:
        clarifying_questions.append("What guardrails should the agent follow before changing files or expanding scope?")
    if audience == "teammate" and not acceptance_criteria:
        clarifying_questions.append("What should the teammate check before considering this done?")

    prompt_sections = [
        "Role or audience",
        "Goal",
        "Background/current state",
        "Inputs and artifacts",
        "Constraints",
        "Scope boundaries",
        "Plan or requested approach",
        "Stop conditions",
        "Acceptance criteria",
        "Expected output format",
    ]

    prompt_quality_checks = [
        "Can the executor identify the desired outcome in one sentence?",
        "Are constraints and non-goals visible before the plan?",
        "Are facts, assumptions, and open questions separated?",
        "Is the expected output format explicit?",
        "Would the executor know when to stop or ask for help?",
    ]

    return {
        "ready_to_ask_ai": readiness["status"] == "ready" or readiness["score"] >= 70,
        "clarifying_questions": _cap_list(
            _dedupe_preserve_order(clarifying_questions),
            max_items=max_items,
            metadata=metadata,
        ),
        "thinking_prompts": _cap_list(mode_hints["thinking_prompts"], max_items=max_items, metadata=metadata),
        "prompting_hints": _cap_list(mode_hints["prompting_hints"], max_items=max_items, metadata=metadata),
        "execution_hints": _cap_list(mode_hints["execution_hints"], max_items=max_items, metadata=metadata),
        "anti_patterns": _cap_list(mode_hints["anti_patterns"], max_items=max_items, metadata=metadata),
        "recommended_prompt_sections": _cap_list(prompt_sections, max_items=max_items, metadata=metadata),
        "prompt_quality_checks": _cap_list(prompt_quality_checks, max_items=max_items, metadata=metadata),
    }


def _questions_from_gaps(missing_details: list[str]) -> list[str]:
    questions: list[str] = []
    for gap in missing_details:
        questions.extend(_READINESS_GAP_QUESTIONS.get(gap, [f"How should this gap be resolved: {gap}"]))
    return questions


def _derive_risks_from_gaps(missing_details: list[str]) -> list[str]:
    risk_by_gap = {
        "Goal or desired outcome is missing.": "The executor may optimize for the wrong outcome because the goal is unclear.",
        "Background or current state is missing.": "The executor may repeat prior work or miss important context.",
        "Constraints are missing.": "Important rules may remain implicit and be violated during execution.",
        "In-scope or out-of-scope boundaries are missing.": "The task may expand beyond the intended scope.",
        "Artifacts, inputs, or dependencies are missing.": "Execution may stall because required resources are not identified.",
        "Acceptance criteria are missing.": "The task may not have an objective definition of done.",
        "Risks or stop conditions are missing.": "The executor may continue through a blocker instead of pausing.",
        "Expected deliverables are missing.": "The final output may not match what the requester needs.",
    }
    return [risk_by_gap[gap] for gap in missing_details if gap in risk_by_gap]


def _build_findings(readiness: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    severity = "low" if readiness["status"] == "ready" else "medium" if readiness["status"] == "needs_clarification" else "high"
    for detail in readiness["missing_details"]:
        findings.append({"type": "readiness_gap", "severity": severity, "message": detail})
    if not findings:
        findings.append({"type": "readiness", "severity": "low", "message": "Task has enough context to begin execution."})
    return findings


def _build_summary(
    *,
    subject: str,
    mode: str,
    goal: str | None,
    desired_outcome: str | None,
    task: str,
    max_text_length: int,
    metadata: dict[str, Any],
) -> str:
    outcome = goal or desired_outcome
    if outcome:
        summary = f"Prepare {mode} work for {subject}: {outcome}"
    else:
        summary = f"Prepare {mode} work for {subject}: {task}"
    return _clean_text(summary, max_text_length=max_text_length, metadata=metadata) or "Prepare execution context."
