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
            "owner_sha256": digest(owner), "plan_sha256": state["plan_sha256"],
            "execution_mode": mode}


def bound_workflow(path: str | Path, *, session_state: Any) -> dict[str, Any] | None:
    """Read current authority; a missing legacy binding never fabricates history."""
    binding = getattr(session_state, "workflow_binding", None)
    if binding is None:
        return None
    keys = {"schema_version", "workflow_id", "task_window_id", "owner_sha256",
            "plan_sha256", "execution_mode"}
    if (not isinstance(binding, dict) or set(binding) != keys
            or type(binding["schema_version"]) is not int or binding["schema_version"] != 1):
        raise WorkflowError("integrity", "unsupported or incomplete workflow binding")
    owner = workflow_owner(session_state)
    profile = session_state.active_task_profile
    if (binding["owner_sha256"] != digest(owner)
            or binding["task_window_id"] != session_state.task_window_id
            or profile is None or binding["execution_mode"] != profile.execution_mode):
        raise WorkflowError("wrong_authority", "workflow binding does not match accepted task authority")
    state = read_workflow(path, owner=owner, workflow_id=binding["workflow_id"])
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
    # Canonical round-trip both bounds the packet and detaches mutable runtime state.
    packet = json.loads(canonical(packet))
    return {**packet, "packet_sha256": digest(packet)}
