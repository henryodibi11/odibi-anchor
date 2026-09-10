"""Task intake and execution brief context generation.

This module provides a standalone utility for turning a rough task, idea,
request, or handoff into a structured execution context. It is useful for
humans first and AI/agent handoffs second.

The function is intentionally deterministic and side-effect free:
no LLM calls, no file access, no execution, no global state, and no framework
abstractions.

Version 3.1 adds evidence-aware planning, weighted readiness scoring, explicit
context-generator availability statuses, and a quick human brief helper. The tool still does not inspect data or
files directly. Instead, it helps identify evidence gaps and recommends focused
discovery steps that can be completed by humans or other standalone context
generators before execution.

Usage:
    from odibi_anchor.planning import task_execution_context

    context = task_execution_context(
        task="Test the generated validation_summary_context artifact.",
        goal="Verify pandas behavior before Spark work.",
        mode="testing",
        constraints=["Use synthetic data only."],
        acceptance_criteria=["Pandas smoke tests pass."],
    )
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section
from odibi_anchor.planning._task_profile import TaskProfile, normalize_task_profile
from odibi_anchor.planning.context_selection import select_context

TaskMode = Literal[
    "planning",
    "documentation",
    "implementation",
    "testing",
    "debugging",
    "review",
    "migration",
    "handoff",
    "decision",
    "analysis",
    "retrospective",
    "greenfield",
]

Audience = Literal["self", "teammate", "agent", "mixed"]
TaskPriority = Literal["low", "medium", "high", "critical"]

_VALID_MODES = {
    "planning",
    "documentation",
    "implementation",
    "testing",
    "debugging",
    "review",
    "migration",
    "handoff",
    "decision",
    "analysis",
    "retrospective",
    "greenfield",
}

_VALID_AUDIENCES = {"self", "teammate", "agent", "mixed"}
_VALID_PRIORITIES = {"low", "medium", "high", "critical"}

TASK_EXECUTION_CONTEXT_VERSION = "3.2"
__version__ = TASK_EXECUTION_CONTEXT_VERSION


# ── Static data (moved to _task_config) ──
from odibi_anchor.planning._task_config import (  # noqa: F401
    _MODE_DEFAULTS,
    _MODE_HINTS,
    _READINESS_GAP_QUESTIONS,
    _MODE_EVIDENCE_GAPS,
)

# ── Builder / helper functions (moved to _task_builders) ──
from odibi_anchor.planning._task_builders import (  # noqa: F401
    _build_readiness,
    _build_readiness_dimensions,
    _dimension_status,
    _build_discovery,
    _infer_evidence_gaps,
    _recommend_context_generators,
    _build_discovery_steps,
    _build_hints,
    _questions_from_gaps,
    _derive_risks_from_gaps,
    _build_findings,
    _build_summary,
)

# ── Rendering functions (moved to _task_render) ──
from odibi_anchor.planning._task_render import (  # noqa: F401
    render_task_execution_report,
    _render_prompt_brief,
    _render_teammate_brief,
    _format_context_generator,
    _append_context_generators,
    _append_text,
    _append_list,
    _append_records,
    _append_discovery_steps,
    _append_plan,
    _append_resources,
)

def task_execution_context(
    task: str,
    *,
    subject: str | None = None,
    goal: str | None = None,
    mode: TaskMode = "planning",
    audience: Audience = "mixed",
    requester: str | None = None,
    executor: str | None = None,
    priority: TaskPriority | None = None,
    work_type: str | None = None,
    execution_mode: str | None = None,
    risk: str | None = None,
    rigor: str | None = None,
    domains: list[str] | None = None,
    traits: list[str] | None = None,
    caller_required_evidence: list[dict[str, Any]] | None = None,
    due_date: str | None = None,
    stakeholders: list[str] | None = None,
    background: str | None = None,
    current_state: str | None = None,
    desired_outcome: str | None = None,
    trigger: str | None = None,
    expected_output_format: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    inputs: list[dict[str, Any]] | None = None,
    constraints: list[str] | None = None,
    known_facts: list[str] | None = None,
    assumptions: list[str] | None = None,
    open_questions: list[str] | None = None,
    decisions_needed: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    evidence_gaps: list[str] | None = None,
    recommended_discovery_steps: list[str] | None = None,
    options: list[dict[str, Any]] | None = None,
    in_scope: list[str] | None = None,
    out_of_scope: list[str] | None = None,
    dependencies: list[str] | None = None,
    risks: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    guardrails: dict[str, Any] | None = None,
    deliverables: list[str] | None = None,
    max_plan_steps: int = 8,
    max_items_per_section: int = 12,
    max_text_length: int = 500,
    include_handoff: bool = True,
    output_format: str = "dict",
    _already_called: "list[str] | None" = None,
    _available_actions: list[str] | None = None,
    _task_profile: TaskProfile | None = None,
    _specification_disposition: str | None = None,
    _assurance_plan: Any | None = None,
    _assurance_diagnostics: tuple[str, ...] = (),
) -> "dict[str, Any] | str":
    """Create a structured execution context from a task, request, or idea.

    The result helps a human organize work before execution and can also be
    pasted into an AI/agent prompt. The function does not call an LLM, execute
    work, read files, write files, or mutate external state.

    Args:
        task: Raw task, request, or idea to organize.
        subject: Short label for the task target.
        goal: Intended outcome.
        mode: Type of work being planned.
        audience: Primary consumer of the context.
        requester: Person or group requesting the work.
        executor: Person, team, or agent expected to execute the work.
        priority: Optional urgency level: low, medium, high, or critical.
        due_date: Optional due date or time expectation as plain text.
        stakeholders: People or groups affected by the task.
        background: Why the task exists.
        current_state: Current state before execution.
        desired_outcome: Concrete result expected after execution.
        trigger: Event or need that caused the task.
        expected_output_format: Preferred response, file, summary, or deliverable format.
        artifacts: Files, patches, tickets, notebooks, docs, or other artifacts.
        inputs: Runtime inputs, datasets, parameters, or values.
        constraints: Rules the executor must follow.
        known_facts: Facts already established.
        assumptions: Assumptions being made.
        open_questions: Questions that may need resolution.
        decisions_needed: Decisions that must be made.
        evidence: Evidence, observations, links, logs, or facts supporting the task.
        evidence_gaps: Evidence still needed before confident execution.
        recommended_discovery_steps: User-provided discovery steps to perform before execution.
        options: Options to compare for decision, planning, or review tasks.
        in_scope: Work explicitly included.
        out_of_scope: Work explicitly excluded.
        dependencies: Required tools, files, systems, people, or states.
        risks: Known risks or failure modes.
        acceptance_criteria: Conditions that define done.
        stop_conditions: Conditions that should pause execution.
        deliverables: Expected outputs from the task.
        max_plan_steps: Maximum generated plan steps.
        max_items_per_section: Maximum items retained per list section.
        max_text_length: Maximum characters retained per text field.
        include_handoff: Whether to include prompt/team-ready rendered briefs.

    Returns:
        Structured execution context dictionary for humans, teammates, or agents.

    Raises:
        ValueError: If task is empty, mode/audience is unsupported, or limits are invalid.
    """
    metadata = {
        "generated_by": "task_execution_context",
        "version": TASK_EXECUTION_CONTEXT_VERSION,
        "truncated": False,
        "max_plan_steps": max_plan_steps,
        "max_items_per_section": max_items_per_section,
        "max_text_length": max_text_length,
    }

    _validate_limits(
        max_plan_steps=max_plan_steps,
        max_items_per_section=max_items_per_section,
        max_text_length=max_text_length,
    )

    if mode not in _VALID_MODES:
        raise ValueError(f"Unsupported mode: {mode!r}. Expected one of: {sorted(_VALID_MODES)}")
    if audience not in _VALID_AUDIENCES:
        raise ValueError(f"Unsupported audience: {audience!r}. Expected one of: {sorted(_VALID_AUDIENCES)}")
    if priority is not None and priority not in _VALID_PRIORITIES:
        raise ValueError(f"Unsupported priority: {priority!r}. Expected one of: {sorted(_VALID_PRIORITIES)}")

    normalized_task = _clean_text(task, max_text_length=max_text_length, metadata=metadata)
    if not normalized_task:
        raise ValueError("task must be a non-empty string")

    normalized = _normalize_task_inputs(
        task=task,
        subject=subject,
        goal=goal,
        mode=mode,
        requester=requester,
        executor=executor,
        priority=priority,
        due_date=due_date,
        stakeholders=stakeholders,
        background=background,
        current_state=current_state,
        desired_outcome=desired_outcome,
        trigger=trigger,
        expected_output_format=expected_output_format,
        artifacts=artifacts,
        inputs=inputs,
        constraints=constraints,
        known_facts=known_facts,
        assumptions=assumptions,
        open_questions=open_questions,
        decisions_needed=decisions_needed,
        evidence=evidence,
        evidence_gaps=evidence_gaps,
        recommended_discovery_steps=recommended_discovery_steps,
        options=options,
        in_scope=in_scope,
        out_of_scope=out_of_scope,
        dependencies=dependencies,
        risks=risks,
        acceptance_criteria=acceptance_criteria,
        stop_conditions=stop_conditions,
        guardrails=guardrails,
        deliverables=deliverables,
        max_plan_steps=max_plan_steps,
        max_items_per_section=max_items_per_section,
        max_text_length=max_text_length,
        metadata=metadata,
    )

    profile = _task_profile or normalize_task_profile(
        legacy_mode=mode,
        work_type=work_type,
        execution_mode=execution_mode,
        risk=risk,
        rigor=rigor,
        domains=domains,
        traits=traits,
        caller_required_evidence=caller_required_evidence,
        task_text=" ".join(part for part in (task, goal or "", desired_outcome or "") if part),
        priority=priority,
    )

    context_plan = select_context(
        profile,
        task_text=" ".join(part for part in (task, subject or "", goal or "", desired_outcome or "") if part),
        out_of_scope=out_of_scope,
        available_actions=_available_actions,
        already_called=_already_called,
    )
    result = _assemble_context(
        normalized=normalized,
        mode=mode,
        audience=audience,
        include_handoff=False,
        max_plan_steps=max_plan_steps,
        max_items_per_section=max_items_per_section,
        max_text_length=max_text_length,
        metadata=metadata,
        context_plan=context_plan,
    )
    result["task_profile"] = profile.to_dict()
    if _assurance_plan is not None:
        from odibi_anchor.assurance import AssurancePlan, build_assurance_plan

        if not isinstance(_assurance_plan, AssurancePlan):
            raise TypeError("_assurance_plan must use the immutable AssurancePlan contract")
        if _assurance_plan != build_assurance_plan(profile):
            raise ValueError("_assurance_plan does not match the normalized TaskProfile")
        result["assurance"] = {"plan": _assurance_plan.to_dict()}
    elif _assurance_diagnostics:
        result["assurance"] = {
            "plan": None,
            "assessment": {
                "mode": "shadow",
                "status": "degraded",
                "diagnostics": list(_assurance_diagnostics),
            },
        }
    if _specification_disposition is None:
        from odibi_anchor.planning._task_policy import (
            BpsKernel,
            TaskPolicyContext,
            evaluate_task_policies,
        )
        policy_context = TaskPolicyContext(
            profile,
            BpsKernel(task, desired_outcome or goal or ""),
        )
        _specification_disposition = evaluate_task_policies(
            profile, policy_context,
        ).specification.disposition
    elif _specification_disposition not in {"required", "recommended", "not_required"}:
        raise ValueError("invalid internal specification disposition")
    result["required_skills"] = _build_required_skills(
        profile,
        specification_disposition=_specification_disposition,
    )
    result["context_plan"] = context_plan
    result["metrics"]["selected_eye_count"] = len(result["context_plan"]["questions"])
    result["metrics"]["required_eye_count"] = sum(
        question["priority"] == "required" for question in result["context_plan"]["questions"]
    )
    if include_handoff:
        result["handoff"] = {
            "prompt_brief": _render_prompt_brief(result),
            "teammate_brief": _render_teammate_brief(result),
        }
    else:
        result["handoff"] = {}

    if output_format == "markdown":
        return render_task_execution_report(result)
    return result


def _normalize_task_inputs(
    *,
    task: str,
    subject: str | None,
    goal: str | None,
    mode: str,
    requester: str | None,
    executor: str | None,
    priority: str | None,
    due_date: str | None,
    stakeholders: list[str] | None,
    background: str | None,
    current_state: str | None,
    desired_outcome: str | None,
    trigger: str | None,
    expected_output_format: str | None,
    artifacts: list[dict[str, Any]] | None,
    inputs: list[dict[str, Any]] | None,
    constraints: list[str] | None,
    known_facts: list[str] | None,
    assumptions: list[str] | None,
    open_questions: list[str] | None,
    decisions_needed: list[str] | None,
    evidence: list[dict[str, Any]] | None,
    evidence_gaps: list[str] | None,
    recommended_discovery_steps: list[str] | None,
    options: list[dict[str, Any]] | None,
    in_scope: list[str] | None,
    out_of_scope: list[str] | None,
    dependencies: list[str] | None,
    risks: list[str] | None,
    acceptance_criteria: list[str] | None,
    stop_conditions: list[str] | None,
    guardrails: dict[str, Any] | None,
    deliverables: list[str] | None,
    max_plan_steps: int,
    max_items_per_section: int,
    max_text_length: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Normalize and clean all task inputs into a flat dict."""
    ct = lambda v: _clean_text(v, max_text_length=max_text_length, metadata=metadata)  # noqa: E731
    ntl = lambda v: _normalize_text_list(v, max_items=max_items_per_section, max_text_length=max_text_length, metadata=metadata)  # noqa: E731
    nml = lambda v: _normalize_mapping_list(v, max_items=max_items_per_section, max_text_length=max_text_length, metadata=metadata)  # noqa: E731

    normalized_task = ct(task)
    if not normalized_task:
        raise ValueError("task must be a non-empty string")

    n_subject = ct(subject) or _subject_from_task(normalized_task, max_text_length=max_text_length, metadata=metadata)
    n_out_of_scope = ntl(out_of_scope)
    n_constraints = ntl(constraints)
    n_stop_conditions = ntl(stop_conditions)

    return {
        "task": normalized_task, "subject": n_subject,
        "goal": ct(goal), "requester": ct(requester), "executor": ct(executor),
        "priority": ct(priority), "due_date": ct(due_date),
        "expected_output_format": ct(expected_output_format),
        "background": ct(background), "current_state": ct(current_state),
        "desired_outcome": ct(desired_outcome), "trigger": ct(trigger),
        "artifacts": nml(artifacts), "inputs": nml(inputs),
        "dependencies": ntl(dependencies), "stakeholders": ntl(stakeholders),
        "constraints": n_constraints, "known_facts": ntl(known_facts),
        "assumptions": ntl(assumptions), "open_questions": ntl(open_questions),
        "decisions_needed": ntl(decisions_needed), "evidence": nml(evidence),
        "evidence_gaps": ntl(evidence_gaps),
        "recommended_discovery_steps": ntl(recommended_discovery_steps),
        "options": nml(options), "in_scope": ntl(in_scope),
        "out_of_scope": n_out_of_scope, "risks": ntl(risks),
        "acceptance_criteria": ntl(acceptance_criteria),
        "stop_conditions": n_stop_conditions, "deliverables": ntl(deliverables),
        "guardrails": _normalize_guardrails(
            guardrails, out_of_scope=n_out_of_scope, constraints=n_constraints,
            stop_conditions=n_stop_conditions, max_items=max_items_per_section,
            max_text_length=max_text_length, metadata=metadata,
        ),
    }


def _assemble_context(
    *,
    normalized: dict[str, Any],
    mode: str,
    audience: str,
    include_handoff: bool,
    max_plan_steps: int,
    max_items_per_section: int,
    max_text_length: int,
    metadata: dict[str, Any],
    context_plan: dict[str, Any],
) -> dict[str, Any]:
    """Build the full task execution context dict from normalized inputs."""
    n = normalized  # short alias for readability
    defaults = _MODE_DEFAULTS[mode]
    default_plan = _cap_list(
        _number_plan_steps(defaults["plan"]),
        max_items=max_plan_steps,
        metadata=metadata,
    )
    default_critique_checks = _cap_list(defaults["critique_checks"], max_items=max_items_per_section, metadata=metadata)
    default_verification_steps = _cap_list(
        defaults["verification_steps"],
        max_items=max_items_per_section,
        metadata=metadata,
    )

    readiness = _build_readiness(
        goal=n["goal"], desired_outcome=n["desired_outcome"],
        background=n["background"], current_state=n["current_state"],
        trigger=n["trigger"], constraints=n["constraints"],
        in_scope=n["in_scope"], out_of_scope=n["out_of_scope"],
        artifacts=n["artifacts"], inputs=n["inputs"],
        dependencies=n["dependencies"], acceptance_criteria=n["acceptance_criteria"],
        risks=n["risks"], stop_conditions=n["stop_conditions"],
        deliverables=n["deliverables"],
        expected_output_format=n["expected_output_format"],
        requester=n["requester"], executor=n["executor"],
        evidence=n["evidence"], evidence_gaps=n["evidence_gaps"],
        known_facts=n["known_facts"],
        mode=mode,
    )

    discovery = _build_discovery(
        mode=mode,
        readiness=readiness, artifacts=n["artifacts"], inputs=n["inputs"],
        dependencies=n["dependencies"], evidence=n["evidence"],
        evidence_gaps=n["evidence_gaps"],
        user_discovery_steps=n["recommended_discovery_steps"],
        known_facts=n["known_facts"], assumptions=n["assumptions"],
        open_questions=n["open_questions"],
        decisions_needed=n["decisions_needed"],
        constraints=n["constraints"],
        acceptance_criteria=n["acceptance_criteria"],
        stop_conditions=n["stop_conditions"],
        max_items=max_items_per_section, metadata=metadata,
        context_plan=context_plan,
    )

    combined_risks = _build_combined_risks(
        n["risks"], readiness, defaults, max_items_per_section, metadata,
    )
    hints = _build_hints(
        mode=mode, audience=audience, readiness=readiness,
        open_questions=n["open_questions"],
        decisions_needed=n["decisions_needed"],
        artifacts=n["artifacts"], inputs=n["inputs"],
        constraints=n["constraints"],
        acceptance_criteria=n["acceptance_criteria"],
        expected_output_format=n["expected_output_format"],
        evidence=n["evidence"], discovery=discovery,
        max_items=max_items_per_section, metadata=metadata,
    )
    summary = _build_summary(
        subject=n["subject"], mode=mode, goal=n["goal"],
        desired_outcome=n["desired_outcome"], task=n["task"],
        max_text_length=max_text_length, metadata=metadata,
    )

    result = _build_result_dict(
        n, mode, audience, readiness, discovery, combined_risks,
        hints, summary, default_plan, default_critique_checks,
        default_verification_steps, metadata,
    )

    if include_handoff:
        result["handoff"] = {
            "prompt_brief": _render_prompt_brief(result),
            "teammate_brief": _render_teammate_brief(result),
        }
    else:
        result["handoff"] = {}

    return result


def _build_combined_risks(
    normalized_risks: list[str],
    readiness: dict[str, Any],
    defaults: dict[str, Any],
    max_items_per_section: int,
    metadata: dict[str, Any],
) -> list[str]:
    """Merge user-provided risks with inferred risks from readiness gaps."""
    generated_risks = _derive_risks_from_gaps(readiness["missing_details"])
    return _cap_list(
        _dedupe_preserve_order([*normalized_risks, *generated_risks]),
        max_items=max_items_per_section,
        metadata=metadata,
    )


def _build_required_skills(
    profile,
    *,
    specification_disposition: str = "not_required",
    explicit_formal_spec: bool = False,
) -> list[dict[str, str]]:
    """Build required skills list for the given mode.

    Returns list of dicts with skill name, path, and enforcement status.
    """
    from odibi_anchor.planning._task_builders import required_skills_for_task
    skills = required_skills_for_task(
        profile,
        specification_disposition=specification_disposition,
        explicit_formal_spec=explicit_formal_spec,
    )
    return [
        {
            "skill": name,
            "path": f"skills/{name}/SKILL.md",
            "enforcement": "MUST load before each non-exempt substantive action — RuntimeError if skipped",
            "register": f'anchor("skill_loaded", "{name}")',
        }
        for name in skills
    ]


def _build_result_dict(
    n: dict[str, Any],
    mode: str,
    audience: str,
    readiness: dict[str, Any],
    discovery: dict[str, Any],
    combined_risks: list[str],
    hints: dict[str, Any],
    summary: str,
    default_plan: list[dict[str, Any]],
    default_critique_checks: list[str],
    default_verification_steps: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the final result dictionary from computed sections."""
    defaults = _MODE_DEFAULTS[mode]
    findings = _build_findings(readiness)
    suggested_next_actions = _cap_list(
        _dedupe_preserve_order([*readiness["recommended_clarifications"], *defaults["suggested_next_actions"]]),
        max_items=len(default_plan) + 4,
        metadata=metadata,
    )
    guardrails = n["guardrails"]
    return {
        "kind": "task_execution_context",
        "version": TASK_EXECUTION_CONTEXT_VERSION,
        "subject": n["subject"],
        "summary": summary,
        "status": readiness["status"],
        "mode": mode,
        "audience": audience,
        "ownership": {
            "requester": n["requester"], "executor": n["executor"],
            "stakeholders": n["stakeholders"],
            "priority": n["priority"], "due_date": n["due_date"],
        },
        "readiness": readiness,
        "intent": {
            "task": n["task"], "goal": n["goal"],
            "desired_outcome": n["desired_outcome"],
        },
        "background": {
            "summary": n["background"], "current_state": n["current_state"],
            "trigger": n["trigger"],
        },
        "scope": {"in_scope": n["in_scope"], "out_of_scope": n["out_of_scope"]},
        "resources": {
            "artifacts": n["artifacts"], "inputs": n["inputs"],
            "dependencies": n["dependencies"],
        },
        "context": {
            "known_facts": n["known_facts"], "assumptions": n["assumptions"],
            "open_questions": n["open_questions"],
            "decisions_needed": n["decisions_needed"],
            "evidence": n["evidence"], "options": n["options"],
        },
        "constraints": n["constraints"],
        "discovery": discovery,
        "plan": default_plan,
        "critique_checks": default_critique_checks,
        "verification": {
            "acceptance_criteria": n["acceptance_criteria"],
            "stop_conditions": n["stop_conditions"],
            "deliverables": n["deliverables"],
            "expected_output_format": n["expected_output_format"],
            "verification_steps": default_verification_steps,
        },
        "guardrails": guardrails,
        "risks": combined_risks,
        "hints": hints,
        "metrics": {
            "readiness_score": readiness["score"],
            "plan_step_count": len(default_plan),
            "constraint_count": len(n["constraints"]),
            "risk_count": len(combined_risks),
            "acceptance_criteria_count": len(n["acceptance_criteria"]),
            "open_question_count": len(n["open_questions"]),
            "artifact_count": len(n["artifacts"]),
            "input_count": len(n["inputs"]),
            "dependency_count": len(n["dependencies"]),
            "stakeholder_count": len(n["stakeholders"]),
            "evidence_count": len(n["evidence"]),
            "evidence_gap_count": len(discovery["evidence_gaps"]),
            "discovery_step_count": len(discovery["recommended_discovery_steps"]),
            "recommended_context_generator_count": len(discovery["recommended_context_generators"]),
            "option_count": len(n["options"]),
            "clarifying_question_count": len(hints["clarifying_questions"]),
            "guardrail_count": sum(
                len(guardrails[k]) for k in
                ("do_not_modify", "do_not_create", "require_verification")
            ) + (1 if guardrails["max_files_changed"] else 0),
        },
        "findings": findings,
        "samples": {},
        "required_skills": [],
        "suggested_next_actions": suggested_next_actions,
        "metadata": metadata,
    }


def quick_context(
    task: str,
    *,
    subject: str | None = None,
    goal: str | None = None,
    mode: TaskMode = "planning",
    audience: Audience = "mixed",
    output_format: str = "markdown",
    **kwargs: Any,
) -> dict | str:
    """Return a concise execution brief, optionally as a contract-compliant dict.

    This convenience wrapper lowers the barrier for the common case where a
    user wants the planning benefit without navigating the full structured
    dictionary. It still uses :func:`task_execution_context` internally and
    remains deterministic and side-effect free.

    Args:
        task: Raw task, request, or idea to organize.
        subject: Optional short label for the task target.
        goal: Optional intended outcome.
        mode: Type of work being planned.
        audience: Primary consumer of the context.
        output_format: "dict" returns a standard contract dict;
            "markdown" (default) returns a concise teammate brief string.
        **kwargs: Additional keyword-only details accepted by
            ``task_execution_context``. ``include_handoff`` is ignored because
            this wrapper always needs the teammate brief.

    Returns:
        A contract-compliant dict (output_format="dict") or teammate brief string.
    """
    from odibi_anchor._utils.contract import build_base_context

    kwargs.pop("include_handoff", None)
    kwargs.pop("output_format", None)  # always need dict internally
    context = task_execution_context(
        task=task,
        subject=subject,
        goal=goal,
        mode=mode,
        audience=audience,
        include_handoff=True,
        output_format="dict",
        **kwargs,
    )

    brief = context["handoff"]["teammate_brief"]

    if output_format == "dict":
        return build_base_context(
            kind="quick_context",
            subject=subject or task[:50],
            summary=f"Quick plan: {task[:80]}",
            metrics=context.get("metrics", {}),
            findings=[brief],
            risks=context.get("risk_factors", []),
            samples={},
            suggested_next_actions=context.get("suggested_next_actions", []),
            brief=brief,
            readiness=context.get("metrics", {}).get("readiness_score", 0),
        )
    return brief


def render_quick_report(
    task: str,
    *,
    subject: str | None = None,
    goal: str | None = None,
    mode: TaskMode = "planning",
    audience: Audience = "mixed",
    **kwargs: Any,
) -> str:
    """Render a concise human-readable execution brief (alias for quick_context)."""
    return quick_context(task, subject=subject, goal=goal, mode=mode, audience=audience, **kwargs)


# ── Utility helpers (moved to _task_helpers) ──
from odibi_anchor.planning._task_helpers import (  # noqa: F401
    _validate_limits,
    _clean_text,
    _subject_from_task,
    _normalize_text_list,
    _normalize_mapping_list,
    _json_safe,
    _cap_list,
    _cap_mapping_list,
    _dedupe_preserve_order,
    _normalize_guardrails,
    _number_plan_steps,
)
