"""Phase admission for runtime-owned workflow bindings.

This is additive to execution-mode/path/provider enforcement, never a replacement.
Public enrollment is deliberately not enabled until recovery and collectors exist.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any

from odibi_anchor.codebase._workflow import WorkflowError, canonical, digest, read_workflow


def data_only_legacy_exception(profile: Any) -> bool:
    """Temporary compatibility is determined by capability, never mode spelling."""
    from odibi_anchor._dispatcher._effects import task_profile_effect_compatible

    return (task_profile_effect_compatible("data_write", profile)
            and not task_profile_effect_compatible("source_write", profile))


def workflow_owner(session_state: Any) -> dict[str, str]:
    """Derive exact ownership from runtime authority, never transport arguments."""
    result = {"project_id": session_state.active_project,
              "trust_domain": session_state.trust_domain}
    for key in ("anchor_home", "artifact_root", "target_root"):
        value = getattr(session_state, key, None)
        if not value:
            raise WorkflowError("unavailable", f"workflow requires runtime {key}")
        result[key] = str(Path(value).resolve())
    if not all(isinstance(value, str) and value.strip() for value in result.values()):
        raise WorkflowError("unavailable", "workflow requires explicit project and trust domain")
    return result


def bind_workflow(path: str | Path, *, session_state: Any,
                  workflow_id: str) -> dict[str, Any]:
    """Prepare a binding for persistence with one accepted task, not an approval."""
    owner = workflow_owner(session_state)
    state = read_workflow(path, owner=owner, workflow_id=workflow_id)
    if not session_state.task_window_id or session_state.active_task_profile is None:
        raise WorkflowError("unavailable", "workflow requires materialized task authority")
    mode = session_state.active_task_profile.execution_mode
    if mode != state["plan"]["execution_mode"]:
        raise WorkflowError("wrong_mode", "workflow plan and accepted task execution modes differ")
    if state["status"] in {"cancelled", "completed"}:
        raise WorkflowError("terminal", "cannot bind a terminal workflow to new work")
    return {"schema_version": 1, "workflow_id": workflow_id,
            "task_window_id": session_state.task_window_id,
            "owner_sha256": digest(state["owner"]), "plan_sha256": state["plan_sha256"],
            "execution_mode": mode}


def bound_workflow(path: str | Path, *, session_state: Any) -> dict[str, Any] | None:
    """Read current authority; a missing legacy binding never fabricates history."""
    binding = getattr(session_state, "workflow_binding", None)
    if binding is None:
        return None
    keys = {"schema_version", "workflow_id", "task_window_id", "owner_sha256",
            "plan_sha256", "execution_mode"}
    is_review = isinstance(binding, dict) and "review_candidate_sha256" in binding
    if is_review:
        keys.add("review_candidate_sha256")
    if (not isinstance(binding, dict) or set(binding) != keys
            or type(binding["schema_version"]) is not int or binding["schema_version"] != 1):
        raise WorkflowError("integrity", "unsupported or incomplete workflow binding")
    owner = workflow_owner(session_state)
    profile = session_state.active_task_profile
    state = read_workflow(path, owner=owner, workflow_id=binding["workflow_id"])
    if (binding["owner_sha256"] != digest(state["owner"])
            or binding["task_window_id"] != session_state.task_window_id
            or profile is None or binding["execution_mode"] != profile.execution_mode):
        raise WorkflowError("wrong_authority", "workflow binding does not match accepted task authority")
    if is_review:
        if (profile.execution_mode != "read_only" or profile.work_type != "verify"
                or binding["plan_sha256"] != state["plan_sha256"]
                or state["candidate"] is None
                or binding["review_candidate_sha256"] != digest(state["candidate"])):
            raise WorkflowError("stale_evidence", "review task requires exact plan and candidate with read-only authority")
        return state
    if (binding["plan_sha256"] != state["plan_sha256"]
            or binding["execution_mode"] != state["plan"]["execution_mode"]):
        raise WorkflowError("stale_plan", "plan changed; establish a fresh task at a safe boundary")
    return state


def enforce_workflow_admission(path: str | Path, *, session_state: Any,
                               effects: Collection[str], source_targets: Collection[str] = ()) -> None:
    """Reject implementation outside an accepted, active, exact workflow plan.

    Managed planning/evidence writes retain their existing mode/path checks. Reads
    and recovery remain possible after qualification and terminal task closure.
    """
    if not set(effects).intersection({"source_write", "data_write", "external_mutation"}):
        return
    if data_only_legacy_exception(session_state.active_task_profile) and "source_write" in effects:
        raise WorkflowError("wrong_mode", "data-only compatibility never permits source-write effects")
    state = bound_workflow(path, session_state=session_state)
    if state is None:
        return
    if "external_mutation" in effects:
        raise WorkflowError("authority_required", "workflow phase never authorizes external mutation")
    if (state["status"] != "active" or state["phase"] != "implement_and_qualify"
            or state["progress"] not in {"planned", "implemented"}):
        raise WorkflowError("plan_required", "implementation requires an accepted active plan; delivery freezes the candidate")
    if "data_write" in effects:
        raise WorkflowError("unavailable", "data effects require a supported exact-resource collector; arbitrary SQL is not a bounded target")
    if "source_write" in effects:
        allowed = state["plan"].get("source_paths")
        if not isinstance(allowed, list) or not allowed or not source_targets:
            raise WorkflowError("scope_required", "source writes require explicit source_paths and resolved targets")
        for item in allowed:
            if (not isinstance(item, str) or not item or "\\" in item
                    or any(char in item for char in "*?[") or PurePosixPath(item).is_absolute()
                    or ".." in PurePosixPath(item).parts or PurePosixPath(item).as_posix() != item
                    or item == "."):
                raise WorkflowError("invalid_scope", "source_paths must be exact canonical relative file paths")
        root = Path(session_state.target_root).resolve()
        for target in source_targets:
            if not isinstance(target, str) or not target:
                raise WorkflowError("scope_required", "source target is unavailable")
            try:
                relative = (root / target).resolve().relative_to(root).as_posix()
            except ValueError as exc:
                raise WorkflowError("out_of_scope", "source target escapes the accepted root") from exc
            if relative not in allowed:
                raise WorkflowError("out_of_scope", "source target is outside the accepted plan")


def workflow_packet(path: str | Path, *, session_state: Any) -> dict[str, Any]:
    """Return a complete content-addressed projection, not executable authority."""
    state = bound_workflow(path, session_state=session_state)
    if state is None:
        if data_only_legacy_exception(session_state.active_task_profile):
            return {"kind": "workflow_packet", "schema_version": 1, "authority": "projection",
                    "status": "unphased_unsupported_collector", "completed": False,
                    "delivery_verified": False, "next_step": "legacy_data_checks",
                    "compatibility_exception": {
                        "id": "temporary_data_only_legacy", "source_effects_permitted": False,
                        "reason": "No bounded data candidate collector; existing data policies still apply.",
                        "removal_criteria": [
                            "Bounded data collector with exact resource identities and independent readback",
                            "Scope, authority, stale evidence and negative-path qualification on supported hosts",
                            "Safe-boundary enrollment of fresh tasks without rewriting legacy evidence",
                        ],
                    }}
        return {"kind": "workflow_packet", "schema_version": 1, "status": "legacy_unphased",
                "next_step": "enroll_at_safe_boundary", "authority": "projection"}
    if state["status"] == "blocked":
        next_step = "reconcile_delivery" if state["blocker"]["kind"] == "outcome_unknown" else "resolve_blocker"
    elif state["status"] in {"cancelled", "completed"}:
        next_step = "none"
    else:
        next_step = {"draft": "accept_plan", "planned": "implement",
                     "implemented": "qualify", "qualified": "request_delivery_authority",
                     "approved_for_delivery": "deliver", "delivered": "verify_delivery"}[state["progress"]]
    packet = {"kind": "workflow_packet", "schema_version": 1, "authority": "projection",
              "binding": session_state.workflow_binding, "state": state, "next_step": next_step}
    command = {"accept_plan": "accept_plan", "request_delivery_authority": "prepare_delivery",
               "reconcile_delivery": "verify_delivery", "verify_delivery": "verify_delivery"}.get(next_step)
    if command is None:
        command = "status"
    kwargs = {"workflow_id": state["workflow_id"], "output_format": "dict"}
    if command not in {"status", "prepare_delivery"}:
        kwargs.update(expected_generation=state["generation"],
                      request_id=f"{state['workflow_id']}:{state['generation']}:{command}")
    packet["next_operation"] = {
        "action": "workflow", "args": [command], "kwargs": kwargs,
        "copy_ready": f"anchor('workflow', {command!r}, **{kwargs!r})",
        "reason": "Inspect retained obligations before acting" if command == "status" else next_step,
        "destination_mutation_authorized": False,
    }
    packet["pending_criteria"] = [
        criterion["id"] for criterion in state["plan"].get("criteria", [])
        if state.get("measurements", {}).get(criterion["id"], {}).get("status") != "satisfied"
    ]
    packet["review_required"] = state["progress"] == "implemented" and not state.get("review_result")
    # Canonical round-trip both bounds the packet and detaches mutable runtime state.
    packet = json.loads(canonical(packet))
    return {**packet, "packet_sha256": digest(packet)}
