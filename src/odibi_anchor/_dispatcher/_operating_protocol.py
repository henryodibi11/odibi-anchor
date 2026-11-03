"""Deterministic, informational lifecycle projection for agent responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from odibi_anchor._dispatcher._effects import (
    dispatch_succeeded,
    task_profile_effect_compatible,
)

_PROFILE_EFFECTS = ("read", "governance_write", "artifact_write", "source_write", "data_write")
_MUTATING_SELECTORS = {
    "problem": {"create", "update", "link_spec", "close"},
    "spec": {"create", "update", "done", "persist", "execute", "review", "from_problem"},
    "work_item": {"create", "update", "review", "request_plan_approval", "approve", "record_publish", "close", "amend"},
}


def latest_delivery_gate_passed(
    session_timings: Sequence[Mapping[str, Any]], *, epoch: int | None,
) -> bool:
    """Return the latest observed delivery-gate state in the accepted task window."""
    window = session_timings[epoch or 0:] if epoch is not None else ()
    gates = [row for row in window if row.get("action") == "gate"]
    return bool(gates and gates[-1].get("passed") is True)


def should_emit_operating_protocol(
    action: str, args: Sequence[Any] = (), *, routing_changed: bool = False,
    checkpoint_nested: bool = False, kwargs: Mapping[str, Any] | None = None,
) -> bool:
    """Select lifecycle boundaries; ordinary inspection remains quiet."""
    selector = str(args[0]).strip().lower() if args else ""
    if action in {"orient", "status", "task", "new_session", "gate", "learn"}:
        return True
    if action == "snapshot":
        return bool(kwargs and kwargs.get("mode") == "handoff")
    if action == "checkpoint":
        return not checkpoint_nested
    if action == "learning":
        return selector in {"capture", "assess", "safe_stop"}
    if action in _MUTATING_SELECTORS:
        return selector in _MUTATING_SELECTORS[action]
    return action == "project" and routing_changed


def build_operating_protocol(
    session_state: Any, *, action: str, result: Mapping[str, Any] | None = None,
    session_timings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build the fixed v1 shape solely from committed runtime state."""
    profile = getattr(session_state, "active_task_profile", None)
    stale = bool(getattr(session_state, "routing_stale", False))
    active_learning = bool(getattr(session_state, "learning_obligation_id", None))
    closed_learning = bool(getattr(session_state, "latest_closed_obligation_id", None))
    epoch = getattr(session_state, "task_verification_epoch", None)
    window = list(session_timings[epoch or 0:]) if epoch is not None else []
    gates = [row for row in window if row.get("action") in {"gate", "checkpoint"}]
    transition_failed = bool(
        action in {"gate", "checkpoint"} and result is not None
        and not dispatch_succeeded(action, result)
    )
    gate_satisfied = bool(
        gates and gates[-1].get("passed") is True and not transition_failed
    )
    learning_due = active_learning and (
        profile is None
        or bool(getattr(session_state, "prior_learn_debt", False))
        or gate_satisfied
    )
    safe_terminal = getattr(session_state, "terminal_status", None)
    # A completed closure is recorded only for the task this process holds.
    closed_completed = profile is not None and safe_terminal == "completed"

    if stale:
        phase = "rebootstrap"
    elif safe_terminal in {"blocked", "failed"} or closed_completed:
        phase = "terminal"
    elif learning_due:
        phase = "learning"
    elif profile is None:
        phase = "bootstrap"
    elif transition_failed or (gates and not gate_satisfied):
        phase = "verification"
    elif gate_satisfied and closed_learning:
        phase = "terminal"
    else:
        phase = "execution"

    authority = {
        key: value for key, value in (
            ("session_id", getattr(session_state, "session_id", None)),
            ("project_id", getattr(session_state, "active_project", None)),
            ("artifact_root", getattr(session_state, "artifact_root", None)),
            ("target_root", getattr(session_state, "target_root", None)),
            ("problem_id", getattr(session_state, "linked_problem", None)),
            ("spec_name", getattr(session_state, "linked_spec", None)),
            ("task_window_id", getattr(session_state, "task_window_id", None) if profile else None),
        ) if value is not None
    }
    # Keep lifecycle authority explicit and shape-stable.  An inactive task has
    # no execution mode; its task-window identifier remains deliberately hidden.
    authority["task_active"] = profile is not None
    authority["execution_mode"] = profile.execution_mode if profile is not None else None
    required: list[dict[str, Any]] = []
    if stale:
        required.append({"id": "rebootstrap", "satisfy_with": {"route": "init"}})
    elif learning_due:
        required.append({"id": "learning_closure", "satisfy_with": {"route": "learning.assess"}})
    elif profile is None:
        startup_actions = {
            str(row.get("action")) for row in session_timings
            if row.get("error") is None and row.get("passed") is not False
        }
        if getattr(session_state, "session_name", None):
            required.append({"id": "accept_task", "satisfy_with": {"route": "task"}})
        elif not {"status", "audit_history"}.issubset(startup_actions):
            required.append({"id": "orient", "satisfy_with": {"route": "orient"}})
        else:
            required.append({"id": "start_session", "satisfy_with": {"route": "new_session"}})
    elif not closed_completed:
        skills, spec_disposition = _required_skills_and_spec(profile, session_state)
        loaded = set(getattr(session_state, "skills_loaded", set()))
        missing = sorted(set(skills) - loaded)[:20]
        for skill in missing:
            required.append({"id": "load_required_skill", "satisfy_with": {
                "route": "skill_loaded", "skill": skill,
            }})
        linked_spec = getattr(session_state, "linked_spec", None)
        spec_ready = bool(
            linked_spec
            and getattr(session_state, "persisted_spec_name", None) == linked_spec
            and getattr(session_state, "reviewed_spec_name", None) == linked_spec
            and getattr(session_state, "spec_review_rating", None) in {"good", "excellent"}
        )
        if spec_disposition == "required" and not spec_ready:
            # Surface the Spec requirement at acceptance, before any edit is blocked.
            required.append({"id": "required_spec", "satisfy_with": {
                "route": "spec",
                "sequence": ["spec create (or link an existing spec)", "spec execute", "spec review"],
                "minimum_review_rating": "good",
                "blocks": ["source_write", "artifact_write", "data_write"],
            }})

    compatible = [effect for effect in _PROFILE_EFFECTS
                  if task_profile_effect_compatible(effect, profile)]
    incompatible = [effect for effect in _PROFILE_EFFECTS
                    if effect != "read" and not task_profile_effect_compatible(effect, profile)]
    verification = [{
        "id": "task_window_gate", "status": (
            "satisfied" if gate_satisfied else "pending" if transition_failed or gates else "unknown"
        ),
    }]
    if profile is not None:
        # These are intentionally state/timing projections, not substitutes for
        # canonical gate predicates. Conditions requiring gate-only evidence stay
        # unknown rather than being inferred from suggestions or prose.
        linked_spec = getattr(session_state, "linked_spec", None)
        if linked_spec:
            verification.extend([
                {"id": "linked_spec", "status": "satisfied"},
                {"id": "persisted_spec", "status": "satisfied" if (
                getattr(session_state, "spec_persisted", False)
                and getattr(session_state, "persisted_spec_name", None) == linked_spec
                ) else "pending"},
                {"id": "reviewed_spec", "status": "satisfied" if (
                getattr(session_state, "spec_review_rating", None) in {"good", "excellent"}
                and getattr(session_state, "reviewed_spec_name", None) == linked_spec
                ) else "pending" if getattr(session_state, "spec_review_rating", None)
                else "unknown"},
            ])
        verification.extend(
            {"id": name, "status": _latest_action_status(window, name)}
            for name in ("preflight", "test", "review")
        )
    if learning_due or closed_learning:
        verification.append({
            "id": "learning_closure", "status": "pending" if learning_due else "satisfied",
        })
    if stale:
        terminal = {"status": "rebootstrap_required", "basis": "routing_invalidated"}
    elif safe_terminal in {"blocked", "failed"}:
        terminal = {"status": safe_terminal, "basis": "safe_stop"}
    elif closed_completed:
        terminal = {"status": "ready", "basis": getattr(session_state, "terminal_basis", None) or "task_closed"}
    elif phase == "terminal":
        terminal = {"status": "ready", "basis": "gate_and_learning_committed"}
    elif learning_due:
        terminal = {"status": "in_progress", "basis": "lifecycle_incomplete"}
    elif profile is None:
        terminal = {"status": "not_applicable", "basis": "no_accepted_task"}
    else:
        terminal = {"status": "in_progress", "basis": "lifecycle_incomplete"}
    return {
        "version": "1.0", "phase": phase, "authority": authority,
        "required_now": required[:20],
        "effect_authority": {
            "profile_compatible": compatible,
            "profile_incompatible": incompatible,
            "runtime_prohibited": ["external_mutation"],
        },
        "verification_obligations": verification[:20],
        "terminal_return": terminal, "rebootstrap_required": stale,
        "task_closure": _task_closure(
            session_state, profile, terminal=terminal, required=required,
            gate_satisfied=gate_satisfied, active_learning=active_learning,
            closed_learning=closed_learning,
        ),
    }


_ASSESS = 'anchor("learning", "assess", outcome="...")'
_SAFE_STOP = 'anchor("learning", "safe_stop", status="blocked", reason="...")'


def _task_closure(
    session_state: Any, profile: Any, *, terminal: Mapping[str, Any], required: Sequence[Mapping[str, Any]],
    gate_satisfied: bool, active_learning: bool, closed_learning: bool,
) -> dict[str, Any] | None:
    """Project whether this process's task is still open and its mode-specific closing calls.

    Informational: Anchor closes a task only through ``learning assess`` (completed) or
    ``learning safe_stop`` (blocked or failed); this projection never closes anything.
    """
    if profile is None:
        return None
    window = getattr(session_state, "task_window_id", None)
    binding = getattr(session_state, "workflow_binding", None)
    base: dict[str, Any] = {
        "task_window_id": window,
        "execution_mode": profile.execution_mode,
        "workflow_id": binding.get("workflow_id") if isinstance(binding, Mapping) else None,
    }
    if terminal.get("status") in {"ready", "blocked", "failed"}:
        return {**base, "state": "closed",
                "closed_as": "completed" if terminal["status"] == "ready" else terminal["status"]}
    read_only = profile.execution_mode == "read_only"
    remaining = [str(row.get("id")) for row in required]
    if read_only and _files_changed():
        # Assessment closes a read-only task as completed only when nothing changed
        # (_persist_terminal_task_if_ready), so recorded changes leave only a truthful stop.
        return {
            **base, "state": "open", "remaining": [*remaining, "read_only_changes_recorded"],
            "complete_with": None,
            "stop_with": [_ASSESS, _SAFE_STOP] if active_learning else [_SAFE_STOP],
            "resume_with": f"anchor('task_rebind', task_window_id={window!r})",
        }
    complete_with = []
    if not read_only and not gate_satisfied:
        remaining.append("task_window_gate")
        complete_with.append('anchor("gate")')
    if "learning_closure" not in remaining:
        remaining.append("learning_closure")
    complete_with.append(_ASSESS)
    if read_only:
        # Without an open learning obligation a read-only task stops directly; with one,
        # assessment closes it as completed, so no truthful stop call is offered.
        stop_with = None if active_learning else [_SAFE_STOP]
    else:
        stop_with = [_SAFE_STOP] if closed_learning and not active_learning else [_ASSESS, _SAFE_STOP]
    return {
        **base, "state": "open", "remaining": remaining,
        "complete_with": complete_with, "stop_with": stop_with,
        "resume_with": f"anchor('task_rebind', task_window_id={window!r})",
    }


def _files_changed() -> bool:
    """Read the dispatcher's recorded file changes; the same set gates read-only completion."""
    from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED

    return bool(_SESSION_FILES_CHANGED)


def _required_skills_and_spec(profile: Any, session_state: Any) -> tuple[tuple[str, ...], str | None]:
    """Resolve required skills and the Spec disposition from the accepted-task policy."""
    try:
        from odibi_anchor.planning._task_builders import required_skills_for_task
        from odibi_anchor.planning._task_policy import evaluate_fresh_task_policies

        context, policies = evaluate_fresh_task_policies(
            profile, session_state=session_state,
            current_action="operating_protocol", current_effect="orient",
        )
        disposition = policies.specification.disposition
        return required_skills_for_task(context, specification_disposition=disposition), disposition
    except (AttributeError, TypeError, ValueError):
        # A projection must never manufacture requirements from incomplete state.
        return (), None


def _latest_action_status(window: Sequence[Mapping[str, Any]], action: str) -> str:
    """Project only the latest factual action result; applicability remains unknown."""
    rows = [row for row in window if row.get("action") == action]
    if not rows:
        return "unknown"
    latest = rows[-1]
    return "satisfied" if (
        latest.get("error") is None and latest.get("passed") is True
    ) else "pending"


def attach_operating_protocol(result: Any, protocol: dict[str, Any]) -> Any:
    """Add the projection and matching hard suggestions without replacing keys."""
    if not isinstance(result, dict):
        return result
    result["operating_protocol"] = protocol
    suggestions = result.get("suggested_next_actions")
    if not isinstance(suggestions, list):
        suggestions = []
        result["suggested_next_actions"] = suggestions
    for obligation in protocol["required_now"]:
        route = obligation["satisfy_with"]["route"]
        suggestion = f"MUST: satisfy {obligation['id']} via {route}."
        if suggestion not in suggestions:
            suggestions.append(suggestion)
    return result


def render_operating_protocol(protocol: Mapping[str, Any], *, concise: bool = False) -> str:
    """Render the shared provider-neutral projection without permission wording."""
    lines = ["## Operating Protocol v1", "", f"**Phase:** `{protocol['phase']}`"]
    if concise:
        routes = [row["satisfy_with"]["route"] for row in protocol["required_now"]]
        lines.append("**Required now:** " + (", ".join(f"`{x}`" for x in routes) or "none"))
        return "\n".join(lines)
    lines.extend(["", "### Current authority"])
    lines.extend(f"- `{key}`: `{value}`" for key, value in protocol["authority"].items())
    lines.extend(["", "### Hard obligations"])
    lines.extend(f"- **{row['id']}** via `{row['satisfy_with']['route']}`"
                 for row in protocol["required_now"])
    effects = protocol["effect_authority"]
    lines.extend(["", "### Task-profile effect compatibility",
                  "Compatibility is informational and is not permission to invoke an action."])
    for key in ("profile_compatible", "profile_incompatible", "runtime_prohibited"):
        lines.append(f"- **{key}:** {', '.join(effects[key]) or 'none'}")
    lines.extend(["", "### Verification obligations"])
    lines.extend(
        f"- **{row['id']}:** `{row['status']}`"
        for row in protocol["verification_obligations"]
    )
    lines.extend(["", f"**Terminal:** `{protocol['terminal_return']['status']}` "
                  f"(`{protocol['terminal_return']['basis']}`)"])
    closure = protocol.get("task_closure")
    if closure and closure["state"] == "closed":
        lines.append(f"**Task:** `{closure['task_window_id']}` closed as `{closure['closed_as']}`")
    elif closure:
        lines.append(
            f"**Task:** `{closure['task_window_id']}` is open; remaining: "
            + ", ".join(f"`{x}`" for x in closure["remaining"])
            + "; complete with " + " then ".join(f"`{x}`" for x in closure["complete_with"])
            + (" or stop with " + " then ".join(f"`{x}`" for x in closure["stop_with"])
               if closure["stop_with"] else "")
            + f"; resume later with `{closure['resume_with']}`"
        )
    return "\n".join(lines)
