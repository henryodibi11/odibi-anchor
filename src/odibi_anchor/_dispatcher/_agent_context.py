"""Bounded, read-only context derived from committed runtime authority."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any

_STATUSES = frozenset(
    {
        "verified",
        "attested",
        "derived",
        "missing",
        "unverified",
        "unavailable",
        "not_applicable",
        "stale",
        "conflicting",
    }
)
_VIEWS = ("compact", "summary", "full")
_VIEW_SECTIONS = {
    "compact": ("project", "task", "lifecycle"),
    "summary": ("project", "task", "lifecycle", "repository", "evidence", "policy"),
    "full": (
        "project",
        "task",
        "lifecycle",
        "repository",
        "evidence",
        "artifacts",
        "capabilities",
        "policy",
    ),
}
_MISSING = object()
_PHASE_RESOURCES = {
    "bootstrap": {
        "id": "odibi-anchor.workflow",
        "path": "references/odibi-anchor/workflow.md",
        "reason": "explains the current bootstrap and task-acceptance sequence",
    },
    "execution": {
        "id": "lifecycle.verify-every-edit",
        "path": "references/lifecycle/verify-every-edit.md",
        "reason": "explains the source-change verification lifecycle",
    },
    "verification": {
        "id": "lifecycle.compliance-gates",
        "path": "references/lifecycle/compliance-gates.md",
        "reason": "explains the current verification and gate obligations",
    },
    "learning": {
        "id": "odibi-anchor.capture-standards",
        "path": "references/odibi-anchor/capture-standards.md",
        "reason": "explains evidence-backed learning closure",
    },
    "terminal": {
        "id": "lifecycle.thread-discipline",
        "path": "references/lifecycle/thread-discipline.md",
        "reason": "explains terminal handoff and continuation boundaries",
    },
    "rebootstrap": {
        "id": "odibi-anchor.workflow",
        "path": "references/odibi-anchor/workflow.md",
        "reason": "explains safe runtime rebootstrap sequencing",
    },
}


def _fact(
    status: str,
    source: str,
    *,
    value: Any = _MISSING,
    reason: str | None = None,
    observed_at: str | None = None,
    inputs: Sequence[str] = (),
) -> dict[str, Any]:
    """Build one shape-stable fact and reject unsupported truth claims."""
    if status not in _STATUSES:
        raise ValueError(f"unsupported context status: {status!r}")
    fact: dict[str, Any] = {
        "status": status,
        "provenance": {"source": source},
    }
    if inputs:
        fact["provenance"]["inputs"] = list(inputs)
    if observed_at is not None:
        fact["observed_at"] = observed_at
    if value is not _MISSING:
        fact["value"] = value
    if reason is not None:
        fact["reason"] = reason
    if status in {"missing", "unavailable", "not_applicable", "conflicting"} and not reason:
        raise ValueError(f"{status} context facts require a reason")
    return fact


def _dataclass_projection(value: Any, names: Sequence[str]) -> dict[str, Any]:
    """Project only explicitly safe dataclass or mapping fields."""
    if is_dataclass(value) and not isinstance(value, type):
        available = {item.name for item in fields(value)}
        return {name: getattr(value, name) for name in names if name in available}
    if isinstance(value, Mapping):
        return {name: value[name] for name in names if name in value}
    return {}


def _project_fact(session_state: Any, route_binding: Any | None) -> dict[str, Any]:
    if route_binding is not None:
        value = {
            "project_id": route_binding.project_id,
            "target_root": route_binding.target_root,
            "artifact_root": route_binding.artifact_root,
            "binding_source": route_binding.binding_source,
            "route_fingerprint": route_binding.fingerprint(),
        }
        active = getattr(session_state, "active_project", None)
        if active is not None and active != route_binding.project_id:
            return _fact(
                "conflicting",
                "route_binding:v1",
                value=value,
                reason="session project conflicts with immutable RouteBinding",
            )
        if getattr(session_state, "routing_stale", False):
            return _fact(
                "stale",
                "route_binding:v1",
                value=value,
                reason="the bound route changed and requires rebootstrap",
            )
        return _fact("verified", "route_binding:v1", value=value)
    active = getattr(session_state, "active_project", None)
    if active:
        return _fact(
            "unverified",
            "session_state.active_project",
            value={"project_id": active},
            reason="mutable active_project is compatibility state, not routing authority",
        )
    return _fact("missing", "route_binding:v1", reason="no managed RouteBinding is available")


def _task_fact(session_state: Any) -> dict[str, Any]:
    profile = getattr(session_state, "active_task_profile", None)
    if profile is None:
        return _fact(
            "not_applicable",
            "accepted_task_authority:v1",
            reason="no task is active in this runtime",
        )
    value = {
        key: item
        for key, item in (
            ("task_window_id", getattr(session_state, "task_window_id", None)),
            ("execution_mode", getattr(profile, "execution_mode", None)),
            ("work_type", getattr(profile, "work_type", None)),
            ("risk", getattr(profile, "risk", None)),
            ("rigor", getattr(profile, "rigor", None)),
            ("problem_id", getattr(session_state, "linked_problem", None)),
            ("spec_name", getattr(session_state, "linked_spec", None)),
            ("work_item_id", getattr(session_state, "linked_work_item", None)),
            ("goal", getattr(session_state, "task_goal", None)),
        )
        if item is not None
    }
    if not value.get("task_window_id"):
        return _fact(
            "conflicting",
            "accepted_task_authority:v1",
            value=value,
            reason="active task profile has no exact task window",
        )
    return _fact("verified", "accepted_task_authority:v1", value=value)


def _lifecycle_fact(protocol: Mapping[str, Any]) -> dict[str, Any]:
    required = list(protocol.get("required_now", ()))
    value = {
        "phase": protocol.get("phase"),
        "required_now": required[:20],
        "terminal_return": protocol.get("terminal_return"),
        "verification_obligations": list(protocol.get("verification_obligations", ()))[:20],
    }
    return _fact(
        "derived",
        "operating_protocol:v1",
        value=value,
        inputs=("accepted_task_authority:v1", "session_action_timings"),
    )


def _repository_fact(session_state: Any) -> dict[str, Any]:
    baseline = getattr(session_state, "task_repository_baseline", None)
    if baseline is None:
        status = "not_applicable" if getattr(session_state, "active_task_profile", None) is None else "missing"
        reason = "no task is active" if status == "not_applicable" else "accepted task has no repository baseline"
        return _fact(status, "task_repository_baseline:v1", reason=reason)
    names = (
        "target_worktree",
        "branch",
        "configured_target_ref",
        "target_sha",
        "merge_base_sha",
        "task_start_head_sha",
        "captured_at",
        "authority_kind",
        "evidence_kind",
        "repository_id",
        "workspace_path",
        "head_sha",
    )
    value = _dataclass_projection(baseline, names)
    result = _fact(
        "verified",
        "task_repository_baseline:v1",
        value=value,
        observed_at=value.get("captured_at"),
    )
    from odibi_anchor._dispatcher._baseline_qualification import project_baseline_qualification
    result["qualification"] = project_baseline_qualification(
        getattr(session_state, "task_repository_baseline_qualification", None),
        baseline,
        task_window_id=getattr(session_state, "task_window_id", None),
    )
    return result


def _ledger_fact(session_state: Any, attribute: str, source: str) -> dict[str, Any]:
    rows = getattr(session_state, attribute, ())
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return _fact("unavailable", source, reason="runtime ledger has an unsupported shape")
    kinds = sorted({str(row.get("kind")) for row in rows if isinstance(row, Mapping) and row.get("kind") is not None})[
        :20
    ]
    return _fact("derived", source, value={"count": len(rows), "kinds": kinds})


def _capability_fact(session_state: Any, route_binding: Any | None) -> dict[str, Any]:
    from odibi_anchor._dispatcher._runtime_capabilities import collect_runtime_capabilities
    value = collect_runtime_capabilities(session_state, route_binding)
    value["runtime_source"] = {
        "state": "available",
        "status": "derived",
        "value": {
            "revision": getattr(session_state, "runtime_current_revision", None),
            "fingerprint": getattr(session_state, "runtime_current_fingerprint", None),
            "continuity_status": getattr(session_state, "continuity_status", None),
        },
        "provenance": {"source": "runtime_source_state"},
    }
    return _fact(
        "derived",
        "runtime_capabilities:v1",
        value=value,
        inputs=("route_binding:v1", "runtime_source_state", "continuity_state"),
    )


def _policy_fact(session_state: Any, protocol: Mapping[str, Any]) -> dict[str, Any]:
    profile = getattr(session_state, "active_task_profile", None)
    effects = protocol.get("effect_authority", {})
    value: dict[str, Any] = {
        "profile_compatible": list(effects.get("profile_compatible", ())),
        "profile_incompatible": list(effects.get("profile_incompatible", ())),
        "runtime_prohibited": list(effects.get("runtime_prohibited", ())),
    }
    if profile is not None:
        value["risk"] = getattr(profile, "risk", None)
        value["rigor"] = getattr(profile, "rigor", None)
    return _fact(
        "derived",
        "task_effect_policy:v1",
        value=value,
        inputs=("accepted_task_profile", "operating_protocol:v1"),
    )


def _prepared_operation(operation: str, reason: str) -> dict[str, Any]:
    kwargs = {"operation": operation, "inputs": {}}
    arguments = ", ".join(f"{key}={value!r}" for key, value in kwargs.items())
    return {
        "status": "ready",
        "kind": "semantic_input_preparation",
        "action": "prepare",
        "args": [],
        "kwargs": kwargs,
        "copy_ready": f'anchor("prepare", {arguments})',
        "reason": reason,
    }


def _next_operation(protocol: Mapping[str, Any]) -> dict[str, Any]:
    terminal = protocol.get("terminal_return", {})
    if terminal.get("status") in {"blocked", "failed", "ready"}:
        return {
            "status": "not_applicable",
            "kind": "terminal",
            "reason": f"terminal state is {terminal.get('status')}: {terminal.get('basis')}",
        }
    required = list(protocol.get("required_now", ()))
    if required:
        target = required[0].get("satisfy_with", {})
        route = str(target.get("route", "")).strip()
        action, separator, selector = route.partition(".")
        if route == "task":
            return _prepared_operation(
                "task.create",
                "task intent and goal are semantic inputs that cannot be fabricated",
            )
        if route == "learning.assess":
            return _prepared_operation(
                "learning.assess",
                "the learning outcome is an agent judgment that cannot be fabricated",
            )
        if route == "new_session":
            name = "odibi_anchor_session"
            return {
                "status": "ready",
                "kind": "dispatcher_call",
                "action": "new_session",
                "args": [],
                "kwargs": {"name": name, "inline": True},
                "copy_ready": f'anchor("new_session", name={name!r}, inline=True)',
                "reason": f"required lifecycle obligation: {required[0].get('id')}",
            }
        if route == "init":
            return {
                "status": "external_required",
                "kind": "runtime_bootstrap",
                "action": "rebootstrap_runtime",
                "copy_ready": "namespace = runpy.run_path(bootstrap_path)",
                "reason": "the immutable route is stale; no anchor() call can rebind this runtime",
                "required_context": ["bootstrap_path"],
            }
        if action:
            positional = [selector] if separator else []
            if target.get("skill"):
                positional.append(target["skill"])
            arguments = ", ".join(json.dumps(item) for item in positional)
            return {
                "status": "ready",
                "kind": "dispatcher_call",
                "action": action,
                "args": positional,
                "kwargs": {},
                "copy_ready": f"anchor({json.dumps(action)}{', ' if arguments else ''}{arguments})",
                "reason": f"required lifecycle obligation: {required[0].get('id')}",
            }
    verification = {str(row.get("id", "")): row.get("status") for row in protocol.get("verification_obligations", ())}
    for obligation, action in (
        ("preflight", "preflight"),
        ("test", "test"),
        ("review", "review"),
        ("task_window_gate", "gate"),
    ):
        if verification.get(obligation) != "satisfied":
            return {
                "status": "ready",
                "kind": "dispatcher_call",
                "action": action,
                "args": [],
                "kwargs": {},
                "copy_ready": f'anchor("{action}")',
                "reason": "next unsatisfied verification obligation",
            }
    return {
        "status": "not_applicable",
        "kind": "terminal",
        "reason": "no further lifecycle operation is required in the current terminal state",
    }


def _resource_pointers(protocol: Mapping[str, Any], next_operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select at most one packaged resource for the current exact operation."""
    if next_operation.get("action") == "skill_loaded":
        args = next_operation.get("args", ())
        skill = args[0] if isinstance(args, Sequence) and args else None
        if isinstance(skill, str) and skill:
            return [
                {
                    "kind": "skill",
                    "id": skill,
                    "path": f"skills/{skill}/SKILL.md",
                    "reason": "required by the current accepted-task policy",
                    "status": "derived",
                    "provenance": {"source": "operating_protocol.required_now"},
                }
            ]
    action = str(next_operation.get("action", ""))
    prepared = next_operation.get("kwargs", {}).get("operation")
    phase = "learning" if action == "learning" or prepared == "learning.assess" else str(protocol.get("phase", ""))
    resource = _PHASE_RESOURCES.get(phase)
    if resource is None:
        return []
    return [
        {
            "kind": "reference",
            **resource,
            "status": "derived",
            "provenance": {"source": "packaged_resource_contract:v1"},
        }
    ]


def build_agent_context(
    session_state: Any,
    *,
    protocol: Mapping[str, Any],
    route_binding: Any | None = None,
    view: str = "compact",
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Build a deterministic context envelope from existing runtime authority.

    Args:
        session_state: Current dispatcher state; it is read but never mutated.
        protocol: Current operating-protocol projection.
        route_binding: Optional immutable managed-project route authority.
        view: ``compact``, ``summary``, or ``full``.
        output_format: ``dict`` or ``markdown``.

    Returns:
        A JSON-safe context dictionary or its bounded Markdown rendering.

    Raises:
        ValueError: If ``view`` or ``output_format`` is unsupported.
    """
    if view not in _VIEWS:
        raise ValueError("view must be compact, summary, or full")
    if output_format not in {"dict", "markdown"}:
        raise ValueError("output_format must be dict or markdown")
    all_facts = {
        "project": _project_fact(session_state, route_binding),
        "task": _task_fact(session_state),
        "lifecycle": _lifecycle_fact(protocol),
        "repository": _repository_fact(session_state),
        "evidence": _ledger_fact(session_state, "evidence_ledger", "task_evidence_ledger:v1"),
        "artifacts": _ledger_fact(
            session_state,
            "managed_artifact_ledger",
            "managed_artifact_ledger:v1",
        ),
        "capabilities": _capability_fact(session_state, route_binding),
        "policy": _policy_fact(session_state, protocol),
    }
    sections = _VIEW_SECTIONS[view]
    facts = {name: all_facts[name] for name in sections}
    statuses = {fact["status"] for fact in facts.values()}
    status = "conflicting" if "conflicting" in statuses else "stale" if "stale" in statuses else "ready"
    required = list(protocol.get("required_now", ()))
    terminal_status = protocol.get("terminal_return", {}).get("status")
    blocker = (
        required[0].get("id") if required
        else terminal_status if terminal_status in {"blocked", "failed"}
        else None
    )
    next_operation = _next_operation(protocol)
    result: dict[str, Any] = {
        "kind": "agent_context",
        "version": "1.0",
        "subject": "runtime",
        "summary": f"{view} derived context for phase {protocol.get('phase', 'unknown')}",
        "status": status,
        "view": view,
        "current_state": protocol.get("phase", "unknown"),
        "blocker": blocker,
        "facts": facts,
        "omitted_sections": [name for name in all_facts if name not in sections],
        "next_operation": next_operation,
        "resource_pointers": _resource_pointers(protocol, next_operation),
        "metrics": {"fact_count": len(facts), "omitted_section_count": len(all_facts) - len(facts)},
        "findings": [],
        "risks": [],
        "samples": {},
        "suggested_next_actions": (
            [next_operation["copy_ready"]] if next_operation.get("copy_ready") else []
        ),
    }
    return render_agent_context(result) if output_format == "markdown" else result


def attach_agent_context(
    result: Any,
    session_state: Any,
    *,
    protocol: Mapping[str, Any],
    route_binding: Any | None = None,
) -> Any:
    """Attach one authoritative compact projection without replacing result keys."""
    if isinstance(result, dict):
        result["agent_context"] = build_agent_context(
            session_state,
            protocol=protocol,
            route_binding=route_binding,
            view="compact",
        )
    return result


def render_agent_context(context: Mapping[str, Any], *, concise: bool = False) -> str:
    """Render a bounded agent-context envelope as Markdown."""
    operation = context["next_operation"]
    if concise:
        lines = [
            "## Agent Context v1",
            "",
            f"**Status:** `{context['status']}`",
            f"**State:** `{context['current_state']}`",
            f"**Blocker:** `{context['blocker'] or 'none'}`",
            "**Next:** "
            + (f"`{operation['copy_ready']}`" if operation.get("copy_ready") else str(operation["reason"])),
        ]
        resources = context.get("resource_pointers", ())
        if resources:
            lines.append(f"**Resource:** `{resources[0]['path']}` — {resources[0]['reason']}")
        return "\n".join(lines)
    lines = [
        "# Agent Context",
        "",
        str(context["summary"]),
        "",
        f"**Status:** `{context['status']}`",
        f"**State:** `{context['current_state']}`",
        f"**Blocker:** `{context['blocker'] or 'none'}`",
        "",
    ]
    for name, fact in context["facts"].items():
        lines.append(f"## {name.replace('_', ' ').title()} — `{fact['status']}`")
        if "reason" in fact:
            lines.append(str(fact["reason"]))
        if "value" in fact:
            lines.append("```json")
            lines.append(json.dumps(fact["value"], sort_keys=True, indent=2, default=str))
            lines.append("```")
        lines.append(f"Source: `{fact['provenance']['source']}`")
        lines.append("")
    lines.append("## Next operation")
    lines.append(f"`{operation['copy_ready']}`" if operation.get("status") == "ready" else str(operation["reason"]))
    resources = context.get("resource_pointers", ())
    if resources:
        lines.extend(["", "## Relevant packaged resource"])
        lines.extend(f"- `{row['path']}` — {row['reason']}" for row in resources)
    return "\n".join(lines)
