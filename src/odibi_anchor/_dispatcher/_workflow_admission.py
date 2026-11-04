"""Phase admission for runtime-owned workflow bindings.

This is additive to execution-mode/path/provider enforcement, never a replacement.
Fresh substantive writes require enrollment; historical task authority stays immutable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any, cast

from odibi_anchor.codebase._workflow import WorkflowError, canonical, digest, read_workflow


def data_only_legacy_exception(profile: Any) -> bool:
    """Temporary compatibility is determined by capability, never mode spelling."""
    from odibi_anchor._dispatcher._effects import task_profile_effect_compatible

    return (task_profile_effect_compatible("data_write", profile)
            and not task_profile_effect_compatible("source_write", profile))


def lightweight_artifact_task(profile: Any) -> bool:
    """Use existing immutable risk/rigor classification, never a bypass flag."""
    from odibi_anchor.assurance.evaluator import build_assurance_plan

    return (profile is not None and profile.execution_mode == "artifact_only"
            and profile.risk == "low" and profile.rigor == "direct"
            and build_assurance_plan(profile).tier not in {"T2", "T3"})


PLANNING_RECORD_DIRECTORIES = ("problems", "specs", "work_items", "decisions")


def planning_record_task(profile: Any) -> bool:
    """A design task that writes plans: governed by plan reviews and owner plan approval.

    Without a bound workflow it may write only the managed planning records; any other
    artifact output still needs a workflow.
    """
    return (profile is not None and profile.execution_mode == "artifact_only"
            and profile.work_type == "design")


def require_task_workflow(*, session_state: Any, profile: Any,
                          trust_domain: str | None, workflow_id: str | None) -> None:
    """Check fresh acceptance only; never reinterpret an immutable legacy task."""
    from odibi_anchor._dispatcher._effects import task_profile_effect_compatible

    source = task_profile_effect_compatible("source_write", profile)
    substantive_artifact = (profile is not None and profile.execution_mode == "artifact_only"
                            and not lightweight_artifact_task(profile)
                            and not planning_record_task(profile))
    if not source and not substantive_artifact:
        return
    missing = [name for name, value in (
        ("project", session_state.active_project), ("trust_domain", trust_domain),
        ("workflow_id", workflow_id),
    ) if not isinstance(value, str) or not value.strip()]
    if not missing:
        return
    error = WorkflowError(
        "workflow_required",
        f"Fresh {'source' if source else 'substantive artifact'} tasks require "
        "explicit managed project, trust_domain and workflow_id; "
        f"missing: {', '.join(missing)}. Bind an existing authorized project with init(project=...). "
        "Declare its authorized trust_domain on the task (or ANCHOR_TRUST_DOMAIN). "
        "In a read-only investigation task create a bounded plan with anchor('workflow', 'create', ...), "
        "then bind its workflow_id on a fresh producer task and accept_plan before edits. "
        "Project creation requires separate explicit approval. For interrupted historical work "
        "use anchor('task_rebind'), not replacement task creation.",
    )
    error.context = {"missing_authority": missing, "legacy_rebind_supported": True,
                     "automatic_project_creation": False}
    error.next_operations = [{"action": "help", "args": ["workflow"],
                              "kwargs": {"output_format": "dict"},
                              "copy_ready": "anchor('help', 'workflow', output_format='dict')"}]
    raise error


def workflow_owner(session_state: Any) -> dict[str, str]:
    """Derive exact ownership from runtime authority, never transport arguments."""
    result = {"project_id": session_state.active_project,
              "trust_domain": session_state.trust_domain}
    for key in ("anchor_home", "artifact_root", "target_root"):
        value = getattr(session_state, key, None)
        if not value:
            raise WorkflowError("unavailable", f"workflow requires runtime {key}")
        result[key] = str(Path(value).resolve())
    missing = [name for name, value in (
        ("project", result["project_id"]), ("trust_domain", result["trust_domain"]),
    ) if not isinstance(value, str) or not value.strip()]
    if missing:
        from odibi_anchor._recovery import attach_recovery, dispatcher_operation

        raise attach_recovery(WorkflowError(
            "unavailable", "workflow requires explicit project and trust domain; "
            f"missing: {', '.join(missing)}. Bind an existing authorized managed project at "
            "runtime startup with init(project=...). Close the current inquiry's obligations, "
            "then accept a fresh read-only task with its authorized trust_domain "
            "(typed anchor_task or generic task; ANCHOR_TRUST_DOMAIN is also supported). "
            "Retrying workflow creation or changing the environment does not repair an already "
            "accepted task. For interrupted work use task_rebind with its original context. "
            "Do not infer authority or create a project without separate approval.",
        ), error_code="workflow_context_required", context={
            "missing_authority": missing, "automatic_project_creation": False,
        }, next_operations=[dispatcher_operation(
            "help", "workflow", kwargs={"output_format": "dict"},
            reason="Inspect workflow enrollment before closing the inquiry and binding fresh context.",
        )])
    return result


def linked_plan(plan, *, linked_work_item=None, prior=None):
    """Keep a source workflow's declared work item across producers and replans."""
    if not isinstance(plan, dict):
        return plan
    prior = prior or {}
    retained = (prior.get("plan", {}).get("work_item")
                or (prior.get("admission") or {}).get("work_item_plan_approval", {}).get("work_item_id"))
    if plan.get("execution_mode") != "source_change":
        if retained:
            raise WorkflowError("wrong_mode", "linked source workflow cannot change execution mode; create a separate workflow")
        return plan
    links = {value for value in (retained, linked_work_item, plan.get("work_item")) if value is not None}
    if len(links) > 1:
        raise WorkflowError("wrong_authority", "source workflow cannot replace its linked work item; create a separate workflow")
    return {**plan, "work_item": links.pop()} if links else plan


def work_item_plan_approval(state, *, session_state, linked_work_item=None, accepting=False):
    """Reuse an existing owner decision; never authenticate review prose or invent approval."""
    from odibi_anchor._dispatcher._spec import spec_path
    from odibi_anchor._dispatcher._work_item import _plan_readiness_problems, work_item_action
    from odibi_anchor._recovery import attach_recovery, dispatcher_operation

    if state["plan"]["execution_mode"] != "source_change":
        return None
    admission = state.get("admission") or {}
    retained = admission.get("work_item_plan_approval")
    plan = linked_plan(state["plan"], linked_work_item=linked_work_item, prior=state)
    item_id = plan.get("work_item")
    if item_id is None:
        return None  # Legitimate standalone workflows keep their existing contract.
    root = Path(session_state.artifact_root)
    shown = cast(dict[str, Any], work_item_action(root, "show", item_id, output_format="dict"))
    item = shown["record"]
    current = [approval for approval in shown["plan_approvals"] if not approval["stale"]]
    if retained and not accepting:
        current = [approval for approval in current if approval["approval_id"] == retained["approval_id"]]
    problems = []
    chosen = None
    for approval in reversed(current):
        # Dependencies may be in the approved group, so this checks review freshness only;
        # dependency ordering was checked when the owner approved the group.
        problems = _plan_readiness_problems(root, [(item_id, {**item, "depends_on": []})], approval["rigor"],
                                            allow_completed=not accepting)
        ranks = {"direct": 0, "compact": 1, "full": 2}
        if ranks[approval["rigor"]] < ranks[state["plan"].get("rigor", "direct")]:
            problems.append("plan approval rigor is lower than workflow rigor")
        spec = approval.get("spec")
        if item.get("specification") and (
            not spec or spec_path(root / "specs", spec["name"]) != spec_path(root / "specs", item["specification"])
        ):
            problems.append("linked specification has no exact approved digest; request plan approval with spec")
        if spec:
            file = spec_path(root / "specs", spec["name"])
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != spec["sha256"]:
                problems.append("approved specification changed or is unavailable")
        if not problems:
            chosen = approval
            break
    if chosen is None:
        raise attach_recovery(WorkflowError(
            "plan_approval_required", f"{item_id} requires current plan approval: "
            + ("; ".join(problems) if problems else "approval missing or stale")
            + ". Preserve work; update/review the exact work item, request_plan_approval, then workflow replan "
            "and accept_plan. Approval never comes from agent prose.",
        ), error_code="workflow_plan_approval_required", context={
            "work_item_id": item_id, "fingerprint": shown["fingerprint"],
            "retained_approval": retained, "problems": problems,
            "plan_approval": shown["plan_approval"],
        }, next_operations=[dispatcher_operation(
            "work_item", "show", item_id, kwargs={"output_format": "dict"},
            reason="inspect the current item, reviews and approvals before recovery",
        )])
    proof = {"work_item_id": item_id, **{key: chosen.get(key) for key in (
        "approval_id", "fingerprint", "depends_on", "plan_sha256", "authority_ref", "assurance", "spec",
    )}}
    if not accepting and retained != proof:
        raise WorkflowError("stale_plan", "linked work item approval changed; workflow replan and accept_plan are required")
    return proof


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
        raise WorkflowError(
            "stale_plan",
            "plan changed; establish a fresh task at a safe boundary. Rework order: close this task "
            "(review, gate, learning assess, or learning safe_stop), then with a clean worktree open a "
            "fresh producer with the same workflow_id and risk, accept_plan, and only then edit; "
            "see help('workflow').",
        )
    return state


def enforce_workflow_admission(path: str | Path, *, session_state: Any,
                               effects: Collection[str], source_targets: Collection[str] = (),
                               artifact_targets: Collection[str] = ()) -> None:
    """Reject implementation outside an accepted, active, exact workflow plan.

    Managed planning/evidence writes retain their existing mode/path checks. Reads
    and recovery remain possible after qualification and terminal task closure.
    """
    implementation_effects = set(effects).intersection({"source_write", "data_write", "external_mutation"})
    if not implementation_effects and not artifact_targets:
        return
    if data_only_legacy_exception(session_state.active_task_profile) and "source_write" in effects:
        raise WorkflowError("wrong_mode", "data-only compatibility never permits source-write effects")
    state = bound_workflow(path, session_state=session_state)
    if state is None:
        if artifact_targets and planning_record_task(session_state.active_task_profile):
            outside = sorted(str(target) for target in artifact_targets
                             if PurePosixPath(str(target).replace("\\", "/")).parts[:1]
                             not in {(name,) for name in PLANNING_RECORD_DIRECTORIES})
            if outside:
                raise WorkflowError(
                    "workflow_required",
                    "a planning task without a workflow writes only planning records ("
                    + ", ".join(PLANNING_RECORD_DIRECTORIES) + "); bind a workflow for "
                    + ", ".join(outside),
                )
        return
    if not implementation_effects:
        root = Path(session_state.artifact_root).resolve()
        outputs = {root / name for name in state["plan"].get("artifact_paths", [])}
        if not any((root / target).absolute() in outputs for target in artifact_targets):
            return  # Ancillary planning/evidence is not implementation of an output.
    if "external_mutation" in effects:
        raise WorkflowError("authority_required", "workflow phase never authorizes external mutation")
    if (state["status"] != "active" or state["phase"] != "implement_and_qualify"
            or state["progress"] not in {"planned", "implemented"}):
        raise WorkflowError("plan_required", "implementation requires an accepted active plan; delivery freezes the candidate")
    work_item_plan_approval(state, session_state=session_state,
                            linked_work_item=getattr(session_state, "linked_work_item", None))
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
    stale_plan = False
    try:
        state = bound_workflow(path, session_state=session_state)
    except WorkflowError as exc:
        if exc.code != "stale_plan":
            raise
        # bound_workflow has already checked the exact owner and task binding.
        # A diagnostic projection must not upgrade that immutable old authority.
        state = read_workflow(path, owner=workflow_owner(session_state),
                              workflow_id=session_state.workflow_binding["workflow_id"])
        stale_plan = True
    if state is None:
        profile = session_state.active_task_profile
        if profile is not None and profile.execution_mode == "read_only":
            return {"kind": "workflow_packet", "schema_version": 1, "authority": "projection",
                    "status": "read_only_unphased", "completed": False,
                    "delivery_verified": False, "next_step": "report_observed_result"}
        if planning_record_task(profile) and not lightweight_artifact_task(profile):
            return {"kind": "workflow_packet", "schema_version": 1, "authority": "projection",
                    "status": "planning_records_unphased", "completed": False,
                    "delivery_verified": False, "next_step": "request_plan_approval",
                    "limitation": "Writes only planning records; the owner approves the plan with "
                                  "work_item request_plan_approval."}
        if lightweight_artifact_task(profile):
            return {"kind": "workflow_packet", "schema_version": 1, "authority": "projection",
                    "status": "lightweight_unphased", "completed": False,
                    "delivery_verified": False, "next_step": "verify_bounded_result",
                    "limitation": "Low-risk direct work only; enroll for substantive or delivery-verified outcomes."}
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
    if stale_plan:
        next_step = "close_task_then_reenroll"
    elif state["status"] == "blocked":
        next_step = "reconcile_delivery" if state["blocker"]["kind"] == "outcome_unknown" else "resolve_blocker"
    elif state["status"] in {"cancelled", "completed"}:
        next_step = "none"
    else:
        next_step = {"draft": "accept_plan", "planned": "implement",
                     "implemented": "qualify", "qualified": "request_delivery_authority",
                     "approved_for_delivery": "deliver", "delivered": "verify_delivery"}[state["progress"]]
    recapture_required = False
    recovery_reason = None
    if next_step == "qualify":
        from odibi_anchor._dispatcher._workflow_evidence import collect_candidate, task_closure_state

        producer = state["candidate"]["producer"]
        reviewer = "review_candidate_sha256" in session_state.workflow_binding
        current_closure = task_closure_state(path, session_state.task_window_id)
        try:
            recapture_required = not reviewer and producer != session_state.task_window_id and current_closure == "open"
            if not recapture_required:
                recapture_required = collect_candidate(path, session_state=session_state,
                    workflow_id=state["workflow_id"]) != state["candidate"]
            if recapture_required:
                if (not reviewer and current_closure == "open"
                        and (producer == session_state.task_window_id
                             or task_closure_state(path, producer) == "closed")):
                    next_step = "implement"
                else:
                    next_step = "recover_candidate"
                    recovery_reason = "Candidate changed; preserve bytes and close/rebind the exact producer before recapture. Reviewer authority never permits producing a replacement."
        except (WorkflowError, OSError) as exc:
            next_step = "recover_candidate"
            recovery_reason = f"Candidate freshness unavailable; resolve before further work: {exc}"
    packet = {"kind": "workflow_packet", "schema_version": 1, "authority": "projection",
              "binding": session_state.workflow_binding, "state": state, "next_step": next_step,
              "recapture_required": recapture_required}
    if stale_plan:
        packet.update(status="recovery_required", binding_status="stale_plan",
                      completed=False, delivery_verified=False,
                      recovery={
                          "workflow_id": state["workflow_id"], "rebind_upgrades_plan": False,
                          "steps": [
                              "Inspect the current plan; do not implement through the old binding.",
                              "Close the old task with its existing review, gate and learning obligations.",
                              "At a safe boundary accept a fresh task with this exact workflow_id.",
                              "Dirty source requires existing exact ownership recovery, never a fresh baseline bypass.",
                          ],
                      })
    from odibi_anchor._recovery import dispatcher_operation

    command = {"accept_plan": "accept_plan", "request_delivery_authority": "prepare_delivery",
               "reconcile_delivery": "verify_delivery", "verify_delivery": "verify_delivery"}.get(next_step)
    kwargs: dict[str, Any] = {"workflow_id": state["workflow_id"], "output_format": "dict"}
    packet["pending_criteria"] = [
        criterion["id"] for criterion in state["plan"].get("criteria", [])
        if criterion["method"] != "review"
        and state.get("measurements", {}).get(criterion["id"], {}).get("status") != "satisfied"
    ]
    packet["review_required"] = state["progress"] == "implemented" and (
        state.get("review_result") or {}
    ).get("status") != "satisfied"
    operation: dict[str, Any] = {"status": "external_required", "kind": "workflow_work",
                                 "reason": next_step, "destination_mutation_authorized": False}
    if recovery_reason:
        operation.update(kind="candidate_recovery", reason=recovery_reason)
    elif next_step == "implement":
        operation["reason"] = (
            "Perform the accepted scoped implementation and applicable focused checks, then "
            "capture workflow implemented before gate or learning; suggestions grant no authority."
        )
        packet["capture_operation"] = dispatcher_operation(
            "workflow", "implemented", kwargs={**kwargs, "expected_generation": state["generation"],
                "request_id": f"{state['workflow_id']}:{state['generation']}:implemented"},
            reason="capture the completed implementation before gate or learning", retry_safety="state_checked",
        )
    elif next_step == "qualify":
        if packet["pending_criteria"]:
            criterion = next(c for c in state["plan"]["criteria"] if c["id"] == packet["pending_criteria"][0])
            if criterion["method"] in {"pytest", "command"}:
                options: dict[str, Any] = {"workflow_criterion": criterion["id"], "output_format": "dict"}
                if criterion["method"] == "pytest":
                    options["target"] = criterion["test_targets"]
                operation = dispatcher_operation("test", kwargs=options,
                    reason="measure the accepted criterion against this candidate", retry_safety="not_idempotent")
            else:
                command = "check_artifact"
                kwargs["criterion_id"] = criterion["id"]
        elif packet["review_required"]:
            operation.update(kind="workflow_review", required_context=["findings"], reason=(
                "Supply truthful findings to workflow review at the current generation; all open "
                "workflow findings block qualification, including minor findings. Full rigor needs "
                "a separate accepted reviewer task. Do not fabricate an empty findings list."
            ))
            if (state["plan"].get("risk") == "high" or state["plan"].get("rigor") == "full"):
                from odibi_anchor._dispatcher._workflow_evidence import task_closure_state

                if task_closure_state(path, state["candidate"]["producer"]) != "closed":
                    operation.update(kind="producer_closure", required_context=[], reason=(
                        "Close the producer with applicable review, gate and learning first; then "
                        "accept a separate read-only reviewer bound to this exact candidate."
                    ))
        else:
            from odibi_anchor._dispatcher._workflow_evidence import task_closure_state

            if task_closure_state(path, state["candidate"]["producer"]) == "closed":
                command = "qualify"
            else:
                operation.update(kind="producer_closure", reason=(
                    "Complete the producer's applicable review, gate and learning closure before qualify; "
                    "task closure is not workflow or delivery completion."
                ))
    if command is not None:
        if command != "prepare_delivery":
            kwargs.update(expected_generation=state["generation"],
                          request_id=f"{state['workflow_id']}:{state['generation']}:{command}")
        operation = dispatcher_operation("workflow", command, kwargs=kwargs,
                                         reason=next_step, retry_safety="state_checked")
    packet["next_operation"] = {**operation, "destination_mutation_authorized": False}
    # Canonical round-trip both bounds the packet and detaches mutable runtime state.
    packet = json.loads(canonical(packet))
    return {**packet, "packet_sha256": digest(packet)}
