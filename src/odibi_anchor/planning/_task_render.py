"""Rendering functions for task_execution_context."""

from __future__ import annotations

from typing import Any


def _format_context_generator(value: dict[str, str]) -> str:
    name = value.get("name", "unknown")
    status = value.get("status")
    if status:
        return f"{name} ({status})"
    return name


def _append_context_generators(lines: list[str], title: str, values: list[dict[str, str]]) -> None:
    if values:
        lines.extend(["", f"{title}:"])
        for value in values:
            label = _format_context_generator(value)
            reason = value.get("reason")
            suffix = f" — {reason}" if reason else ""
            lines.append(f"- {label}{suffix}")


def _append_context_plan(lines: list[str], context_plan: dict[str, Any]) -> None:
    """Render selected evidence questions without implying collection succeeded."""
    questions = context_plan.get("questions", [])
    if not questions:
        return
    lines.extend(["", "Selected context questions:"])
    for question in questions:
        actions = ", ".join(
            _format_candidate_action(action)
            for action in question.get("candidate_actions", [])
            if action.get("name")
        )
        suffix = f" [candidates: {actions}]" if actions else " [no candidate action exposed]"
        lines.append(
            f"- [{question.get('priority', 'recommended')}] "
            f"{question.get('question', '')}{suffix}"
        )


def _format_candidate_action(action: dict[str, Any]) -> str:
    """Keep recognition and call history visible without implying completion."""
    status = str(action.get("availability", "unknown"))
    if action.get("called_this_session"):
        status += ", called this session"
    return f"{action.get('name', 'unknown')} ({status})"


def _append_text(lines: list[str], title: str, value: str | None) -> None:
    if value:
        lines.extend(["", f"{title}:", value])


def _append_list(lines: list[str], title: str, values: list[Any]) -> None:
    if values:
        lines.extend(["", f"{title}:"])
        for value in values:
            lines.append(f"- {value}")


def _append_records(lines: list[str], title: str, values: list[dict[str, Any]]) -> None:
    if values:
        lines.extend(["", f"{title}:"])
        for value in values:
            name = value.get("name") or value.get("title") or value.get("option") or value.get("value") or str(value)
            role = value.get("role") or value.get("type") or value.get("criteria")
            detail = value.get("summary") or value.get("description") or value.get("status")
            suffix_parts = [str(part) for part in [role, detail] if part]
            suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {name}{suffix}")


def _append_discovery_steps(lines: list[str], steps: list[dict[str, Any]]) -> None:
    if steps:
        lines.extend(["", "Recommended discovery steps:"])
        for step in steps:
            tool = step.get("suggested_tool")
            suffix = f" [suggested tool: {tool}]" if tool else ""
            lines.append(f"{step.get('step')}. {step.get('action')}{suffix}")
            if step.get("why"):
                lines.append(f"   Why: {step['why']}")


def _append_plan(lines: list[str], plan: list[dict[str, Any]]) -> None:
    if plan:
        lines.extend(["", "Plan:"])
        for step in plan:
            lines.append(f"{step.get('step')}. [{step.get('phase')}] {step.get('action')}")
            if step.get("done_when"):
                lines.append(f"   Done when: {step['done_when']}")


def _append_resources(lines: list[str], resources: dict[str, Any]) -> None:
    artifacts = resources.get("artifacts", [])
    inputs = resources.get("inputs", [])
    dependencies = resources.get("dependencies", [])
    if not artifacts and not inputs and not dependencies:
        return
    lines.extend(["", "Resources:"])
    for label, rows in [("artifacts", artifacts), ("inputs", inputs)]:
        for row in rows:
            name = row.get("name") or row.get("value") or str(row)
            role = row.get("role")
            location = row.get("location")
            suffix_parts = [part for part in [role, location] if part]
            suffix = f" ({'; '.join(suffix_parts)})" if suffix_parts else ""
            lines.append(f"- {label}: {name}{suffix}")
    for dependency in dependencies:
        lines.append(f"- dependency: {dependency}")


def _render_prompt_brief(context: dict[str, Any]) -> str:
    lines: list[str] = [f"You are helping with: {context['subject']}", ""]
    lines.append(f"Mode: {context['mode']}")
    lines.append(f"Readiness: {context['readiness']['status']} ({context['readiness']['score']}/100)")
    ownership = context.get("ownership", {})
    _append_text(lines, "Requester", ownership.get("requester"))
    _append_text(lines, "Executor", ownership.get("executor"))
    _append_text(lines, "Priority", ownership.get("priority"))
    _append_text(lines, "Due date", ownership.get("due_date"))
    _append_list(lines, "Stakeholders", ownership.get("stakeholders", []))
    _append_text(lines, "Task", context["intent"].get("task"))
    _append_text(lines, "Goal", context["intent"].get("goal"))
    _append_text(lines, "Desired outcome", context["intent"].get("desired_outcome"))
    _append_text(lines, "Current state", context["background"].get("current_state"))
    _append_text(lines, "Background", context["background"].get("summary"))
    _append_list(lines, "In scope", context["scope"].get("in_scope", []))
    _append_list(lines, "Out of scope", context["scope"].get("out_of_scope", []))
    _append_resources(lines, context["resources"])
    _append_list(lines, "Constraints", context.get("constraints", []))
    _append_list(lines, "Open questions", context["context"].get("open_questions", []))
    _append_list(lines, "Decisions needed", context["context"].get("decisions_needed", []))
    _append_records(lines, "Evidence", context["context"].get("evidence", []))
    _append_context_plan(lines, context.get("context_plan", {}))
    _append_list(lines, "Evidence gaps", context.get("discovery", {}).get("evidence_gaps", []))
    _append_discovery_steps(lines, context.get("discovery", {}).get("recommended_discovery_steps", []))
    _append_context_generators(lines, "Recommended context generators", context.get("discovery", {}).get("recommended_context_generators", []))
    _append_records(lines, "Options", context["context"].get("options", []))
    _append_plan(lines, context.get("plan", []))
    _append_list(lines, "Critique checks", context.get("critique_checks", []))
    _append_list(lines, "Stop if", context["verification"].get("stop_conditions", []))
    _append_list(lines, "Acceptance criteria", context["verification"].get("acceptance_criteria", []))
    _append_list(lines, "Deliverables", context["verification"].get("deliverables", []))
    _append_text(lines, "Expected output format", context["verification"].get("expected_output_format"))
    _append_list(lines, "Clarifying questions to resolve", context.get("hints", {}).get("clarifying_questions", []))
    _append_list(lines, "Prompting hints", context.get("hints", {}).get("prompting_hints", []))

    if context["audience"] == "agent":
        lines.extend(
            [
                "",
                "Execution instruction:",
                "Follow the plan. Check critique checks before changing files or expanding scope. Stop if any stop condition is met.",
            ]
        )
    return "\n".join(lines).strip()


def _render_teammate_brief(context: dict[str, Any]) -> str:
    lines = [f"Task: {context['subject']}", f"Summary: {context['summary']}"]
    lines.append(f"Readiness: {context['readiness']['status']} ({context['readiness']['score']}/100)")
    _append_list(lines, "Recommended next actions", context.get("suggested_next_actions", []))
    _append_list(lines, "Evidence gaps", context.get("discovery", {}).get("evidence_gaps", []))
    _append_list(lines, "Clarifying questions", context.get("hints", {}).get("clarifying_questions", []))
    _append_list(lines, "Acceptance criteria", context["verification"].get("acceptance_criteria", []))
    _append_list(lines, "Risks", context.get("risks", []))
    return "\n".join(lines).strip()


def render_task_execution_report(ctx: dict[str, Any]) -> str:
    """Render a task execution context dict as markdown.

    Produces a structured report suitable for LLM prompts or human review.

    Args:
        ctx: Dictionary from ``task_execution_context()``.

    Returns:
        Markdown-formatted string.

    Raises:
        ValueError: If ctx is missing required keys.
    """
    required = {"kind", "subject", "summary", "status"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    lines.append(f"# Task Execution Context: {ctx['subject']}")
    lines.append("")
    lines.append(f"**Status:** {ctx['status'].upper()}")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Mode and readiness
    readiness = ctx.get("readiness", {})
    if readiness:
        score = readiness.get("score", 0)
        lines.append(f"**Readiness:** {score}/100")
        lines.append("")

    canonical_guidance = ctx.get("canonical_guidance", [])
    if canonical_guidance:
        lines.append("## Canonical Guidance")
        lines.append("")
        for target in canonical_guidance:
            lines.append(f"* `{target.get('skill', 'unknown')}`")
        lines.append("")

    reference_guidance = ctx.get("reference_guidance", [])
    if reference_guidance:
        lines.append("## Engineering References")
        lines.append("")
        for reference in reference_guidance:
            lines.append(
                f"* **{reference.get('title', 'unknown')}** (`{reference.get('id', 'unknown')}`, "
                f"score {reference.get('score', 0)}): {reference.get('summary', '')} "
                f"Load with `{reference.get('load', '')}`; search source with "
                f"`{reference.get('search', '')}`."
            )
        lines.append("")

    artifact_contract = ctx.get("artifact_contract", {})
    if artifact_contract.get("version"):
        lines.append(f"## Managed Artifact Contract v{artifact_contract['version']}")
        lines.append("")
        lines.append(str(artifact_contract.get("root_rule", "")))
        lines.append("")
        for artifact in artifact_contract.get("artifacts", []):
            lines.append(
                f"* `{artifact.get('path', 'unknown')}` — {artifact.get('use_when', '')} "
                f"Do not use for: {artifact.get('do_not_use_for', '')}"
            )
        lines.append("")
        lines.append(str(artifact_contract.get("activation", "")))
        lines.append("")

    capture_guidance = ctx.get("capture_guidance", {})
    if capture_guidance.get("version"):
        lines.append(f"## Capture Guidance v{capture_guidance['version']}")
        lines.append("")
        lines.append(str(capture_guidance.get("truth_rule", "")))
        lines.append("")
        lines.append(str(capture_guidance.get("authority_rule", "")))
        lines.append("")
        for route in capture_guidance.get("routes", []):
            required = ", ".join(route.get("required", []))
            lines.append(
                f"* `{route.get('record', 'unknown')}` — {route.get('use_when', '')} "
                f"Record in {route.get('destination', 'the governing record')}. "
                f"Required: {required}."
            )
        lines.append("")

    context_plan = ctx.get("context_plan", {})
    if context_plan.get("questions"):
        lines.append("## Selected Context Questions")
        lines.append("")
        for question in context_plan["questions"]:
            lines.append(
                f"* **{question.get('priority', 'recommended')} — "
                f"{question.get('family', 'context')}**: {question.get('question', '')}"
            )
            actions = [
                _format_candidate_action(action)
                for action in question.get("candidate_actions", [])
                if action.get("name")
            ]
            if actions:
                lines.append(f"  Candidate actions: {', '.join(actions)}")
        lines.append("")

    guidance_obligations = ctx.get("guidance_obligations", [])
    if guidance_obligations:
        lines.append("## Guidance Obligations")
        lines.append("")
        for obligation in guidance_obligations:
            evidence_id = obligation.get("evidence_id") or "none"
            lines.append(
                f"* `{obligation.get('id', 'unknown')}` — **{obligation.get('status', 'unknown')}** "
                f"(evidence: `{evidence_id}`): {obligation.get('reason', '')}"
            )
        lines.append("")

    # Plan
    plan = ctx.get("plan", [])
    if plan:
        lines.append("## Plan")
        lines.append("")
        for step in plan:
            phase = step.get("phase", "")
            action = step.get("action", "")
            done_when = step.get("done_when", "")
            lines.append(f"{step.get('step', '?')}. **[{phase}]** {action}")
            if done_when:
                lines.append(f"   *Done when:* {done_when}")
        lines.append("")

    # Evidence gaps
    discovery = ctx.get("discovery", {})
    gaps = discovery.get("evidence_gaps", [])
    if gaps:
        lines.append("## Evidence Gaps")
        lines.append("")
        for gap in gaps:
            lines.append(f"* {gap}")
        lines.append("")

    # Risks
    risks = ctx.get("risks", [])
    if risks:
        lines.append("## Risks")
        lines.append("")
        for r in risks:
            lines.append(f"* {r}")
        lines.append("")

    # Acceptance criteria
    criteria = ctx.get("acceptance_criteria", [])
    if criteria:
        lines.append("## Acceptance Criteria")
        lines.append("")
        for c in criteria:
            lines.append(f"* {c}")
        lines.append("")

    # Mode hints
    mode_context = ctx.get("mode_context", {})
    thinking = mode_context.get("thinking_prompts", [])
    if thinking:
        lines.append("## Thinking Prompts")
        lines.append("")
        for t in thinking:
            lines.append(f"* {t}")
        lines.append("")

    # Guardrails
    guardrails = ctx.get("guardrails", {})
    if guardrails.get("has_boundaries"):
        lines.append("## Guardrails")
        lines.append("")
        if guardrails.get("do_not_modify"):
            lines.append("**Do NOT modify:**")
            for f in guardrails["do_not_modify"]:
                lines.append(f"- `{f}`")
        if guardrails.get("do_not_create"):
            lines.append("**Do NOT create:**")
            for f in guardrails["do_not_create"]:
                lines.append(f"- `{f}`")
        if guardrails.get("max_files_changed"):
            lines.append(f"**Max files to change:** {guardrails['max_files_changed']}")
        if guardrails.get("require_verification"):
            lines.append("**Required verification:**")
            for v in guardrails["require_verification"]:
                lines.append(f"- {v}")
        lines.append("")

    # Suggested next actions
    actions = ctx.get("suggested_next_actions", [])
    if actions:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for a in actions:
            lines.append(f"* {a}")
        lines.append("")

    rendered = "\n".join(lines)
    protocol = ctx.get("operating_protocol")
    if protocol:
        from odibi_anchor._dispatcher._operating_protocol import render_operating_protocol
        rendered += "\n\n" + render_operating_protocol(protocol)
    return rendered
