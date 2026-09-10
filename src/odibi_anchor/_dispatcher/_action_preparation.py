"""Bounded, read-only preparation for high-friction workflow actions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = "1.0"
CANONICAL_OPERATIONS = (
    "task.create",
    "problem.create",
    "problem.update",
    "work_item.create",
    "work_item.update",
    "gate.qualify",
    "handoff.prepare",
    "learning.assess",
)
_ALIASES = {
    "task": "task.create",
    "context": "task.create",
    "task_context.create": "task.create",
    "problem": "problem.create",
    "evidence": "problem.update",
    "evidence.capture": "problem.update",
    "work_item": "work_item.create",
    "gate": "gate.qualify",
    "qualification": "gate.qualify",
    "handoff": "handoff.prepare",
    "learning": "learning.assess",
}
_PLACEHOLDERS = frozenset({"...", "tbd", "todo", "placeholder", "lorem ipsum"})
_PLACEHOLDER_PATTERN = re.compile(r"^(?:<[^<>]+>|\{\{[^{}]+\}\}|\$\{[^{}]+\})$")
_VERIFIABLE_FIELDS = frozenset(
    {"project_id", "target_root", "artifact_root", "task_window_id"}
)
_SEMANTIC_FIELDS = {
    "task": frozenset(
        {
            "task", "goal", "subject", "background", "current_state", "desired_outcome",
            "constraints", "known_facts", "assumptions", "open_questions", "decisions_needed",
            "in_scope", "out_of_scope", "risks", "acceptance_criteria", "deliverables",
        }
    ),
    "problem": frozenset(
        {
            "title", "definition", "decision_needed", "scope", "constraints",
            "success_measures", "synthesis", "conflicting_evidence", "uncertainty",
            "recommendation", "alternatives", "risks", "reversal_conditions", "evidence",
        }
    ),
    "work_item": frozenset(
        {
            "title", "outcome", "context", "scope", "non_goals", "acceptance_criteria",
            "implementation_notes", "validation_notes", "risks", "dependencies", "evidence",
        }
    ),
    "snapshot": frozenset(
        {
            "summary", "subject", "goal", "decisions", "rejected_alternatives", "blockers",
            "next_steps", "context_needed", "open_questions",
        }
    ),
}


def _field(name: str, reason: str) -> dict[str, str]:
    return {"field": name, "reason": reason}


def _verified(name: str, value: Any, source: str) -> dict[str, Any]:
    return {
        "field": name,
        "value": value,
        "status": "verified",
        "provenance": {"source": source},
    }


def _authority(session_state: Any, route_binding: Any | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if route_binding is not None:
        for field in ("project_id", "target_root", "artifact_root"):
            value = getattr(route_binding, field, None)
            if value:
                result[field] = _verified(field, str(value), "route_binding:v1")
    task_window_id = getattr(session_state, "task_window_id", None)
    if task_window_id:
        result["task_window_id"] = _verified(
            "task_window_id", task_window_id, "accepted_task_authority:v1"
        )
    return result


def _nonempty(inputs: Mapping[str, Any], field: str) -> bool:
    value = inputs.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != [] and value != {}


def _values(inputs: Mapping[str, Any], field: str) -> list[Any]:
    value = inputs.get(field)
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _task_readiness_requirements(inputs: Mapping[str, Any]) -> list[dict[str, str]]:
    """Select the smallest deterministic fields needed by the runtime readiness floor."""
    from odibi_anchor.planning._task_builders import _MODE_DIMENSIONS, _build_readiness

    mode = str(inputs.get("mode") or "planning")
    readiness = _build_readiness(
        # Missing goal is already surfaced separately. Count its eventual presence
        # here so one preparation response can also disclose the remaining floor.
        goal=str(inputs.get("goal") or "pending semantic goal"),
        desired_outcome=inputs.get("desired_outcome"),
        background=inputs.get("background"),
        current_state=inputs.get("current_state"),
        trigger=inputs.get("trigger"),
        constraints=_values(inputs, "constraints"),
        in_scope=_values(inputs, "in_scope"),
        out_of_scope=_values(inputs, "out_of_scope"),
        artifacts=_values(inputs, "artifacts"),
        inputs=_values(inputs, "inputs"),
        dependencies=_values(inputs, "dependencies"),
        acceptance_criteria=_values(inputs, "acceptance_criteria"),
        risks=_values(inputs, "risks"),
        stop_conditions=_values(inputs, "stop_conditions"),
        deliverables=_values(inputs, "deliverables"),
        expected_output_format=inputs.get("expected_output_format"),
        requester=inputs.get("requester"),
        executor=inputs.get("executor"),
        evidence=_values(inputs, "evidence"),
        evidence_gaps=_values(inputs, "evidence_gaps"),
        known_facts=_values(inputs, "known_facts"),
        mode=mode,
    )
    score = int(readiness.get("score", 0))
    if score >= 40:
        return []
    field_for_evaluator = {
        "has_acceptance_criteria": "acceptance_criteria",
        "has_scope": "in_scope",
        "has_background": "background",
        "has_constraints": "constraints",
        "has_resources": "artifacts",
        "has_risks": "risks",
        "has_deliverables": "deliverables",
    }
    requirements: list[dict[str, str]] = []
    dimensions = readiness.get("dimensions", {})
    for dimension, weight, evaluator in _MODE_DIMENSIONS.get(mode, ()):
        if dimensions.get(dimension, {}).get("status") != "missing":
            continue
        field = field_for_evaluator.get(evaluator)
        if field and not any(item["field"] == field for item in requirements):
            requirements.append(_field(
                field,
                f"raises {mode} task readiness to the enforced 40% minimum",
            ))
        score += weight
        if score >= 40:
            return requirements
    for field in ("in_scope", "constraints"):
        if not _nonempty(inputs, field):
            requirements.append(_field(
                field,
                f"raises {mode} task readiness to the enforced 40% minimum",
            ))
    return requirements


def _operation(value: str) -> tuple[str, str | None]:
    requested = str(value or "").strip().lower()
    canonical = _ALIASES.get(requested, requested)
    if canonical not in CANONICAL_OPERATIONS:
        raise ValueError(
            "operation must be one of: " + ", ".join(CANONICAL_OPERATIONS)
            + "; evidence.capture is also supported as a problem.update specialization"
        )
    variant = "evidence" if requested in {"evidence", "evidence.capture"} else None
    return canonical, variant


def _required_and_recommended(
    operation: str,
    inputs: Mapping[str, Any],
    *,
    variant: str | None,
    session_state: Any,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    required: list[dict[str, str]] = []
    recommended: list[dict[str, str]] = []
    not_applicable: list[dict[str, str]] = []
    readiness_required: list[dict[str, str]] = []

    if operation == "task.create":
        required_fields = {"task": "describes the intended work", "goal": "states the intended outcome"}
        recommended_fields = {
            "acceptance_criteria": "defines independently verifiable completion",
            "in_scope": "bounds the implementation surface",
            "out_of_scope": "prevents unintended expansion",
            "constraints": "makes non-negotiable rules explicit",
            "background": "preserves the business reason",
            "deliverables": "makes the expected return concrete",
        }
        not_applicable.append(_field("priority", "policy and priority are not inferred by preparation"))
        readiness_required = _task_readiness_requirements(inputs)
        if (
            getattr(session_state, "repository_provider", None) is not None
            and str(inputs.get("execution_mode") or "") == "source_change"
        ):
            if not _nonempty(inputs, "repository_scope"):
                required.append(_field(
                    "repository_scope",
                    "Databricks source authority requires explicit relative-path scope",
                ))
            if inputs.get("accept_unknown_git_state") is not True:
                required.append(_field(
                    "accept_unknown_git_state",
                    "Databricks source authority requires explicit acknowledgement of unknown Git state",
                ))
    elif operation.startswith("problem."):
        required_fields = (
            {"title": "identifies the unresolved problem"}
            if operation == "problem.create"
            else {"problem_id": "selects the existing Problem Record"}
        )
        if variant == "evidence":
            required_fields["evidence"] = "carries source-provenance evidence to the Problem Record"
            recommended_fields = {
                "change_note": "explains why the evidence changed the record",
            }
        else:
            recommended_fields = {
                "definition": "states the observed gap without presupposing a solution",
                "decision_needed": "states what authority must decide",
                "uncertainty": "keeps unresolved facts explicit",
                "success_measures": "defines what resolution would demonstrate",
            }
        not_applicable.append(_field("outcome", "Problem Records investigate uncertainty; outcomes belong to Work Items"))
    elif operation.startswith("work_item."):
        required_fields = (
            {"title": "identifies the bounded work", "outcome": "states the required result"}
            if operation == "work_item.create"
            else {"work_item_id": "selects the existing Work Item"}
        )
        recommended_fields = {
            "scope": "bounds implementation ownership",
            "non_goals": "prevents unrelated work",
            "acceptance_criteria": "defines verifiable completion",
            "validation_notes": "states the required qualification",
            "dependencies": "preserves execution order",
        }
        not_applicable.append(_field("priority", "priority is an owner/policy decision, not a verified prefill"))
    elif operation == "gate.qualify":
        required_fields = {}
        recommended_fields = {}
        not_applicable.extend(
            [
                _field("changed_files", "gate derives changed files from the task ledger"),
                _field("test_results", "gate derives verification evidence from session state"),
            ]
        )
    elif operation == "learning.assess":
        required_fields = {"outcome": "records the agent's explicit learning judgment"}
        recommended_fields = {
            "observation_ids": "links genuine captured observations when the outcome records them",
            "notes": "preserves concise reasoning for the assessment",
        }
        not_applicable.append(
            _field("inferred_outcome", "preparation cannot invent an agent learning judgment")
        )
    else:
        required_fields = {"summary": "makes the handoff self-identifying"}
        recommended_fields = {
            "next_steps": "gives the receiver one concrete continuation",
            "decisions": "prevents repeated decision work",
            "open_questions": "preserves unresolved judgment",
        }
        not_applicable.append(_field("thread_heuristics", "handoff does not infer thread or token heuristics"))

    for name, reason in required_fields.items():
        if not _nonempty(inputs, name):
            required.append(_field(name, reason))
    required.extend(readiness_required)
    for name, reason in recommended_fields.items():
        if not _nonempty(inputs, name):
            recommended.append(_field(name, reason))
    required_names = {item["field"] for item in required}
    recommended = [item for item in recommended if item["field"] not in required_names]
    return required, recommended, not_applicable


def _invocation(operation: str, inputs: Mapping[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
    values = dict(inputs)
    if operation == "task.create":
        action, args = "task", [values.pop("task")]
    elif operation.startswith("problem."):
        command = operation.split(".", 1)[1]
        action, args = "problem", [command]
        identifier = values.pop("problem_id", None)
        if identifier is not None:
            args.append(identifier)
    elif operation.startswith("work_item."):
        command = operation.split(".", 1)[1]
        action, args = "work_item", [command]
        identifier = values.pop("work_item_id", None)
        if identifier is not None:
            args.append(identifier)
    elif operation == "gate.qualify":
        action, args, values = "gate", [], {}
    elif operation == "learning.assess":
        action, args = "learning", ["assess"]
    else:
        action, args = "snapshot", []
        values = {"mode": "handoff", **values}
    return action, args, values


def _copy_ready(action: str, args: list[Any], kwargs: Mapping[str, Any]) -> str:
    parts = [json.dumps(action, ensure_ascii=False)]
    parts.extend(repr(value) for value in args)
    parts.extend(f"{name}={value!r}" for name, value in sorted(kwargs.items()))
    return "anchor(" + ", ".join(parts) + ")"


def _render(result: Mapping[str, Any]) -> str:
    lines = [
        f"# Action preparation: {result['operation']}",
        "",
        f"**Status:** {result['status']}",
    ]
    for group in ("required_missing", "recommended_missing", "prefilled_verified", "not_applicable"):
        lines.extend(["", f"## {group}"])
        values = result[group]
        lines.extend(
            f"- `{item['field']}` — {item.get('reason', item.get('status', 'verified'))}"
            for item in values
        )
        if not values:
            lines.append("- None")
    lines.extend(["", "## Next operation", f"`{result['next_operation']['copy_ready']}`"])
    return "\n".join(lines)


def prepare_action(
    session_state: Any,
    *,
    operation: str,
    inputs: Mapping[str, Any] | None = None,
    verified_claims: list[Mapping[str, Any]] | None = None,
    route_binding: Any | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Prepare one bounded workflow invocation without performing its effects.

    Args:
        session_state: Current process-local lifecycle state.
        operation: Canonical operation or documented alias to prepare.
        inputs: Caller-supplied operation fields.
        verified_claims: Optional claims checked against current immutable authority.
        route_binding: Immutable runtime route authority.
        output_format: ``dict`` or ``markdown``.

    Returns:
        A four-group applicable-field contract and one deterministic next operation.

    Raises:
        TypeError: If inputs or claims have invalid shapes.
        ValueError: If the operation, placeholder, or verified claim is invalid.
    """
    if output_format not in {"dict", "markdown"}:
        raise ValueError("output_format must be 'dict' or 'markdown'")
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, Mapping):
        raise TypeError("inputs must be a JSON object")
    values = dict(inputs)
    canonical, variant = _operation(operation)
    validate_prepared_inputs(canonical, values)
    authority = _authority(session_state, route_binding)
    validate_verified_claims(verified_claims, authority)
    required, recommended, not_applicable = _required_and_recommended(
        canonical, values, variant=variant, session_state=session_state,
    )
    prefills = list(authority.values())
    if canonical == "handoff.prepare" and not _nonempty(values, "summary"):
        task_goal = getattr(session_state, "task_goal", None)
        if task_goal:
            values["summary"] = task_goal
            prefills.append(_verified("summary", task_goal, "accepted_task_authority:v1"))
            required = [item for item in required if item["field"] != "summary"]
    if canonical == "handoff.prepare" and "state" not in values:
        values["state"] = "in_progress"
        prefills.append(_verified("state", "in_progress", "handoff_contract:v1"))

    if required:
        next_operation = {
            "status": "blocked",
            "action": "supply_inputs",
            "required_fields": [item["field"] for item in required],
            "invocation": {"action": "prepare", "kwargs": {"operation": operation, "inputs": values}},
            "copy_ready": _copy_ready("prepare", [], {"operation": operation, "inputs": values}),
            "reason": "required semantic input is missing and cannot be fabricated",
        }
        status = "blocked"
    else:
        action, args, kwargs = _invocation(canonical, values)
        next_operation = {
            "status": "ready",
            "action": action,
            "args": args,
            "kwargs": kwargs,
            "copy_ready": _copy_ready(action, args, kwargs),
            "reason": "all runtime-required fields are present",
        }
        status = "ready"
    result: dict[str, Any] = {
        "kind": "action_preparation",
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "canonical_operation": canonical,
        "operation_family_count": len(CANONICAL_OPERATIONS),
        "status": status,
        "current_owner": {
            item["field"]: item["value"] for item in authority.values()
        },
        "required_missing": required,
        "recommended_missing": recommended,
        "prefilled_verified": prefills,
        "not_applicable": not_applicable,
        "next_operation": next_operation,
        "resource_pointers": [
            {
                "kind": "reference",
                "id": "odibi-anchor.workflow",
                "path": "references/odibi-anchor/workflow.md",
                "reason": "explains the governing lifecycle when deeper method guidance is needed",
            }
        ],
    }
    return _render(result) if output_format == "markdown" else result


def _walk_strings(value: Any, path: str = ""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _PLACEHOLDERS or bool(_PLACEHOLDER_PATTERN.fullmatch(value.strip()))


def validate_prepared_inputs(operation: str, inputs: Mapping[str, Any]) -> None:
    """Reject narrow scaffold placeholders in semantic fields."""
    action = operation.split(".", 1)[0]
    if action == "handoff":
        action = "snapshot"
    semantic = _SEMANTIC_FIELDS.get(action, frozenset())
    for field in semantic & inputs.keys():
        for path, value in _walk_strings(inputs[field], field):
            if _is_placeholder(value):
                raise ValueError(f"{path} contains a fabricated placeholder")
    if action == "problem" and "evidence" in inputs:
        from odibi_anchor._dispatcher._problem import _items

        for evidence in _items(inputs["evidence"]):
            source = evidence.get("source", evidence.get("Source", ""))
            if not isinstance(source, str) or not source.strip():
                raise ValueError("evidence requires source provenance")
    if action == "work_item":
        from odibi_anchor._dispatcher._work_item import _MATERIAL

        allowed = {*_MATERIAL, "work_item_id", "change_note"}
        unknown = set(inputs) - allowed
        if unknown:
            raise ValueError("unknown work-item fields: " + ", ".join(sorted(unknown)))
    if operation == "task.create":
        import inspect

        from odibi_anchor.planning.task_execution_context import task_execution_context

        planner_fields = {
            name for name in inspect.signature(task_execution_context).parameters
            if not name.startswith("_") and name not in {"task", "output_format"}
        }
        dispatcher_fields = {
            "task", "continuation", "repository_scope", "accept_unknown_git_state",
            "baseline_qualification", "memory_limit", "adoption_approval_id", "trust_domain",
            "problem", "create_problem", "work_item", "spec",
        }
        unknown = set(inputs) - planner_fields - dispatcher_fields
        if unknown:
            raise ValueError("unknown task fields: " + ", ".join(sorted(unknown)))
    if operation == "task.create" and _nonempty(inputs, "task") and _nonempty(inputs, "goal"):
        from odibi_anchor._dispatcher._enforcement import should_block_task_inputs
        from odibi_anchor.planning._task_profile import normalize_task_profile

        normalize_task_profile(
            legacy_mode=str(inputs.get("mode") or "planning"),
            work_type=inputs.get("work_type"),
            execution_mode=inputs.get("execution_mode"),
            risk=inputs.get("risk"),
            rigor=inputs.get("rigor"),
            domains=inputs.get("domains"),
            traits=inputs.get("traits"),
        )

        blocked, reason = should_block_task_inputs(
            str(inputs.get("task", "")), str(inputs.get("goal", "")),
        )
        if blocked:
            raise ValueError(reason)
    if operation == "learning.assess":
        allowed = {"outcome", "observation_ids", "notes", "actor_kind", "actor_ref"}
        unknown = set(inputs) - allowed
        if unknown:
            raise ValueError("unknown learning assessment fields: " + ", ".join(sorted(unknown)))
    if operation == "learning.assess" and _nonempty(inputs, "outcome"):
        outcome = inputs.get("outcome")
        observation_ids = inputs.get("observation_ids") or []
        if outcome not in {"observations_recorded", "nothing_reusable_learned"}:
            raise ValueError(
                "invalid assessment outcome: use observations_recorded or "
                "nothing_reusable_learned"
            )
        if outcome == "observations_recorded" and not observation_ids:
            raise ValueError("observations_recorded requires observation_ids")
        if outcome == "nothing_reusable_learned" and observation_ids:
            raise ValueError("nothing_reusable_learned does not accept observation_ids")


def validate_verified_claims(
    claims: list[Mapping[str, Any]] | None,
    authority: Mapping[str, Mapping[str, Any]],
) -> None:
    """Require caller-supplied verified claims to match current authority exactly."""
    if claims is None:
        return
    if not isinstance(claims, list):
        raise TypeError("verified_claims must be a list of JSON objects")
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            raise TypeError(f"verified_claims[{index}] must be a JSON object")
        field = str(claim.get("field") or "")
        if field not in _VERIFIABLE_FIELDS:
            raise ValueError(f"unsupported verified claim field: {field!r}")
        expected = authority.get(field)
        if expected is None:
            raise ValueError(f"verified claim {field!r} has no current authoritative source")
        if claim.get("value") != expected["value"]:
            raise ValueError(f"verified claim {field!r} does not match current authority")
        if claim.get("provenance") != expected["provenance"]:
            raise ValueError(f"verified claim {field!r} has unsupported provenance")


def validate_mutation_submission(
    action: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    session_state: Any,
    route_binding: Any | None,
) -> None:
    """Validate covered mutating submissions before their handlers can write."""
    command = str(args[0]).strip().lower() if args else ""
    covered = (
        action == "task"
        or (action in {"problem", "work_item"} and command in {"create", "update"})
        or (action == "snapshot" and kwargs.get("mode", "full") == "handoff")
    )
    if not covered:
        return
    operation = (
        "task.create" if action == "task"
        else "handoff.prepare" if action == "snapshot"
        else f"{action}.{command}"
    )
    values = dict(kwargs)
    values.pop("output_format", None)
    claims = values.pop("verified_claims", None)
    if action == "task" and args:
        values["task"] = args[0]
    validate_prepared_inputs(operation, values)
    validate_verified_claims(claims, _authority(session_state, route_binding))
    kwargs.pop("verified_claims", None)
