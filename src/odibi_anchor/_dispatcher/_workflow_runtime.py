"""Public workflow operations collect evidence; callers cannot submit attestations."""

from __future__ import annotations

import json

from odibi_anchor._dispatcher._workflow_admission import bound_workflow, workflow_owner, workflow_packet
from odibi_anchor.codebase._workflow import (
    WorkflowError,
    create_workflow,
    digest,
    read_workflow,
    replay_public_request,
    transition_workflow,
)

DEFAULT_APPROVAL_TIMEOUT_MINUTES = 5
MAX_APPROVAL_TIMEOUT_MINUTES = 240
READ_COMMANDS = frozenset({"status", "prepare_delivery", "prepare_revocation"})
WRITE_COMMANDS = frozenset({"create", "accept_plan", "implemented", "review", "qualify", "check_artifact",
                            "request_delivery_approval", "revoke_delivery", "verify_delivery", "block", "resume", "cancel", "replan"})


def workflow_action(path, *, session_state, command="status", workflow_id=None,
                    request_id=None, expected_generation=None, plan=None, findings=None,
                    reason=None, blocker_kind=None, resolution=None, criterion_id=None,
                    timeout_minutes=None, output_format="dict"):
    """Operate on exact task-bound authority without performing destination mutations.

    Create returns a draft ID to bind at a fresh task acceptance via workflow_id.
    All transitions require a caller-observed generation and idempotency key.
    No actor, approval, measurement, candidate, or receipt payload is accepted.
    ``timeout_minutes`` (integer 1-240, default 5) bounds the owner's response window
    for request_delivery_approval and revoke_delivery only; it grants no authority.
    """
    from odibi_anchor._dispatcher._workflow_delivery import (
        observe_destination,
        prepare_delivery,
        prepare_revocation,
        request_delivery_approval,
        request_delivery_revocation,
    )
    from odibi_anchor._dispatcher._workflow_evidence import (
        _accepted_task,
        check_artifact_baseline,
        collect_artifact_baseline,
        collect_artifact_measurement,
        collect_candidate,
        collect_plan_baseline,
        collect_review,
        qualify_recorded,
        validate_producer_policy,
    )

    if command not in READ_COMMANDS | WRITE_COMMANDS:
        raise ValueError("unknown workflow command")
    if output_format not in {"dict", "markdown"}:
        raise ValueError("workflow output_format must be dict or markdown")
    if command in WRITE_COMMANDS and output_format != "dict":
        raise ValueError("workflow writes require dict output for durability checkpointing")
    if timeout_minutes is not None:
        if command not in {"request_delivery_approval", "revoke_delivery"}:
            raise ValueError("timeout_minutes applies only to request_delivery_approval and revoke_delivery")
        if type(timeout_minutes) is not int or not 1 <= timeout_minutes <= MAX_APPROVAL_TIMEOUT_MINUTES:
            raise ValueError(
                f"timeout_minutes must be an integer from 1 to {MAX_APPROVAL_TIMEOUT_MINUTES} "
                f"(default {DEFAULT_APPROVAL_TIMEOUT_MINUTES})"
            )
    public_request = {"command": command, "expected_generation": expected_generation,
                      "plan": plan, "findings": findings, "reason": reason,
                      "blocker_kind": blocker_kind, "resolution": resolution}
    if criterion_id is not None:
        public_request["criterion_id"] = criterion_id
    # The default stays out of the replay key so existing receipts and retries match.
    if timeout_minutes not in (None, DEFAULT_APPROVAL_TIMEOUT_MINUTES):
        public_request["timeout_minutes"] = timeout_minutes
    approval_timeout = timeout_minutes or DEFAULT_APPROVAL_TIMEOUT_MINUTES
    replayed = False
    if command == "status":
        if workflow_id is None:
            packet = workflow_packet(path, session_state=session_state)
        else:
            state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
            packet = {"kind": "workflow_packet", "schema_version": 1, "authority": "projection", "state": state}
    else:
        accepted = _accepted_task(path, session_state, session_state.task_window_id)
        if command == "create":
            if workflow_id is not None or expected_generation is not None:
                raise ValueError("create accepts plan and request_id, not existing workflow authority")
            state = create_workflow(
                path, owner=workflow_owner(session_state), request_id=request_id, plan=plan,
                artifact_baseline=collect_artifact_baseline(plan, session_state=session_state),
            )
        else:
            prior = None
            binding = accepted["task"].get("workflow_binding")
            if command in WRITE_COMMANDS:
                if type(expected_generation) is not int or expected_generation < 0:
                    raise ValueError("expected_generation must be a nonnegative integer")
                if not isinstance(request_id, str) or not request_id.strip():
                    raise ValueError("workflow transition requires request_id")
                if binding and binding == session_state.workflow_binding and (
                    workflow_id is None or workflow_id == binding["workflow_id"]
                ):
                    prior = replay_public_request(
                        path, owner=workflow_owner(session_state), workflow_id=binding["workflow_id"],
                        request_id=request_id, public_request=public_request,
                    )
            # A completed replan invalidates the old binding for *new* work, but
            # its exact receipt remains readable through the immutable old task.
            state = prior if prior is not None else bound_workflow(path, session_state=session_state)
            if state is None or (workflow_id is not None and workflow_id != state["workflow_id"]):
                raise WorkflowError("wrong_authority", "operation requires this exact accepted task workflow binding")
            workflow_id = state["workflow_id"]
            if command in {"prepare_delivery", "prepare_revocation"}:
                prepare = prepare_delivery if command == "prepare_delivery" else prepare_revocation
                packet = prepare(path, session_state=session_state, workflow_id=workflow_id)
                return packet if output_format == "dict" else "```json\n" + json.dumps(packet, indent=2) + "\n```"
            partial = None
            if prior is None and command == "verify_delivery":
                partial = replay_public_request(
                    path, owner=workflow_owner(session_state), workflow_id=workflow_id,
                    request_id=request_id + ":readback", public_request=public_request,
                )
                if partial is not None and partial["generation"] == state["generation"]:
                    expected_generation = state["generation"]
            if prior is None and expected_generation != state["generation"]:
                raise WorkflowError("conflict", "refresh workflow generation before acting")
            if prior is not None:
                state = prior
                replayed = True
            elif command == "qualify":
                state = qualify_recorded(path, session_state=session_state, workflow_id=workflow_id,
                                         expected_generation=expected_generation, request_id=request_id,
                                         public_request=public_request)
            elif command == "request_delivery_approval":
                state = request_delivery_approval(path, session_state=session_state,
                                                  workflow_id=workflow_id, request_id=request_id,
                                                  timeout_minutes=approval_timeout,
                                                  public_request=public_request)
            elif command == "revoke_delivery":
                state = request_delivery_revocation(path, session_state=session_state,
                                                    workflow_id=workflow_id, request_id=request_id,
                                                    timeout_minutes=approval_timeout,
                                                    public_request=public_request)
            else:
                operation = command
                if command == "accept_plan":
                    record = _accepted_task(path, session_state, session_state.task_window_id, require_open=True)
                    validate_producer_policy(state["plan"], session_state.active_task_profile)
                    payload = {"baseline": collect_plan_baseline(path, session_state=session_state, record=record),
                               "authority_ref": "accepted_task:" + session_state.task_window_id}
                elif command == "check_artifact":
                    operation = "record_check"
                    payload = collect_artifact_measurement(
                        path, session_state=session_state, workflow_id=workflow_id, criterion_id=criterion_id,
                    )
                elif command == "implemented":
                    _accepted_task(path, session_state, session_state.task_window_id, require_open=True)
                    candidate = collect_candidate(path, session_state=session_state, workflow_id=workflow_id)
                    if candidate["producer"] != session_state.task_window_id:
                        raise WorkflowError("wrong_authority", "only the producing task can establish its candidate")
                    payload = {"candidate": candidate}
                elif command == "review":
                    operation = "record_review"
                    payload = collect_review(path, session_state=session_state, workflow_id=workflow_id, findings=findings)
                elif command == "verify_delivery":
                    payload = observe_destination(path, session_state=session_state, workflow_id=workflow_id)
                    if state["progress"] == "approved_for_delivery" or state["status"] == "blocked":
                        state = transition_workflow(
                            path, owner=workflow_owner(session_state), workflow_id=workflow_id,
                            expected_generation=expected_generation, request_id=request_id + ":readback",
                            operation="reconcile_delivery", payload={**payload, "outcome": "delivered"},
                            public_request=public_request,
                        )
                        expected_generation = state["generation"]
                elif command == "replan":
                    if state["progress"] == "draft" and state["plan"]["execution_mode"] == "artifact_only":
                        check_artifact_baseline(state, session_state=session_state, path=path)
                    payload = {"plan": plan, "reason": reason,
                               "artifact_baseline": collect_artifact_baseline(plan, session_state=session_state)}
                elif command == "resume":
                    payload = {"resolution": resolution}
                elif command == "block":
                    payload = {"reason": reason, "kind": blocker_kind}
                else:
                    payload = {"reason": reason}
                state = transition_workflow(
                    path, owner=workflow_owner(session_state), workflow_id=workflow_id,
                    expected_generation=expected_generation, request_id=request_id,
                    operation=operation, payload=payload, public_request=public_request,
                )
        packet = {"kind": "workflow_packet", "schema_version": 1, "authority": "projection",
                  "state": state, "destination_mutation_performed": False,
                  "replayed": replayed,
                  "observation_semantics": "historical acknowledgement" if replayed else "current transition"}
    packet.pop("packet_sha256", None)
    packet = {**packet, "packet_sha256": digest(packet)}
    return packet if output_format == "dict" else "```json\n" + json.dumps(packet, indent=2) + "\n```"


def bind_task_workflow(path, *, session_state, workflow_id, task_call=None):
    """Prepare binding before immutable task persistence; never alter an old record.

    ``task_call`` is the caller's ``(args, kwargs)`` and only shapes the copy-ready
    correction in a rejection; it never contributes authority.
    """
    from odibi_anchor._dispatcher._workflow_admission import bind_workflow
    from odibi_anchor._dispatcher._workflow_evidence import bind_review, validate_producer_policy

    profile = session_state.active_task_profile
    if profile.execution_mode == "read_only" and profile.work_type == "verify":
        return bind_review(path, session_state=session_state, workflow_id=workflow_id)
    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    validate_producer_policy(state["plan"], profile)
    ranks = {"low": 0, "medium": 1, "high": 2}
    required = state["plan"]["risk"]
    if ranks[profile.risk] < ranks[required]:
        from odibi_anchor._recovery import attach_recovery, dispatcher_operation

        call_args, call_kwargs = task_call if task_call is not None else ((), {})
        corrected = {key: value for key, value in dict(call_kwargs).items()
                     if not str(key).startswith("_")}
        corrected.update(risk=required, workflow_id=workflow_id)
        raise attach_recovery(WorkflowError(
            "wrong_authority",
            f"accepted task cannot downgrade workflow risk: workflow {workflow_id} requires "
            f"risk={required!r}, but the task was accepted with risk={profile.risk!r}. "
            f"Restate risk={required!r} explicitly on the producer task.",
        ), error_code="workflow_risk_downgrade", context={
            "workflow_id": workflow_id, "required_risk": required, "requested_risk": profile.risk,
            "execution_mode": state["plan"]["execution_mode"],
        }, next_operations=[dispatcher_operation(
            "task", *call_args, kwargs=corrected,
            reason=f"accept the producer at the workflow's exact risk ({required})",
            retry_safety="not_idempotent",
        )])
    return bind_workflow(path, session_state=session_state, workflow_id=workflow_id)


def measured_test(path, *, session_state, runner, criterion_id, args, kwargs):
    """Retain actual runner output with exact before/after candidate and environment."""
    from odibi_anchor._dispatcher._session_tools import finish_test_steps

    steps = measured_test_steps(path, session_state=session_state, criterion_id=criterion_id,
                                args=args, kwargs=kwargs)
    options = next(steps)
    try:
        return finish_test_steps(steps, runner(**options))
    finally:
        steps.close()


def measured_test_steps(path, *, session_state, criterion_id, args, kwargs):
    """Suspend after capturing admission, candidate and environment; resume in the caller."""
    from odibi_anchor._dispatcher._session_tools import _format_test_result
    from odibi_anchor._dispatcher._workflow_evidence import (
        collect_candidate,
        collect_test_measurement,
        runtime_environment,
    )
    from odibi_anchor._recovery import attach_recovery, dispatcher_operation

    def rejected(exc, *operations):
        # Nothing ran, so the call is neither passing nor failing evidence.
        return attach_recovery(exc, error_code="workflow_measurement_rejected", context={
            "executed": False, "criterion_id": criterion_id,
            "reason_code": getattr(exc, "code", "invalid_request"),
        }, next_operations=operations)

    state = bound_workflow(path, session_state=session_state)
    if state is None or state["progress"] != "implemented" or state["status"] != "active":
        raise rejected(WorkflowError("missing_evidence", "measurement requires a bound implemented candidate"))
    criteria = [c for c in state["plan"]["criteria"] if c["id"] == criterion_id]
    exact = []
    if len(criteria) == 1 and criteria[0]["method"] == "pytest":
        exact = [dispatcher_operation(
            "test", kwargs={"target": criteria[0].get("test_targets"), "workflow_criterion": criterion_id,
                            "timeout": kwargs.get("timeout", 600), "output_format": "dict"},
            reason="measure with the criterion's exact test_targets", retry_safety="not_idempotent",
        )]
    if args or kwargs.get("mark"):
        raise rejected(ValueError(
            "workflow measurement requires exact keyword target list without marker filters"), *exact)
    targets = kwargs.get("target")
    if (len(criteria) != 1 or criteria[0]["method"] != "pytest"
            or not isinstance(targets, list) or targets != criteria[0].get("test_targets")):
        raise rejected(WorkflowError(
            "missing_evidence", "test target must exactly match the workflow criterion"), *exact)
    output_format = kwargs.get("output_format", "dict")
    if output_format != "dict":
        raise rejected(ValueError("workflow measurements require dict output for durability checkpointing"),
                       *exact)
    before = collect_candidate(path, session_state=session_state, workflow_id=state["workflow_id"])
    environment = runtime_environment()
    result = yield {**kwargs, "output_format": "dict"}
    after = collect_candidate(path, session_state=session_state, workflow_id=state["workflow_id"])
    measurement = collect_test_measurement(state=state, before=before, after=after,
                                          criterion_id=criterion_id, targets=targets,
                                          environment_before=environment, result=result)
    updated = transition_workflow(
        path, owner=workflow_owner(session_state), workflow_id=state["workflow_id"],
        expected_generation=state["generation"], request_id="test:" + digest(measurement),
        operation="record_check", payload=measurement,
    )
    result["workflow_measurement"] = {"workflow_id": state["workflow_id"], "criterion_id": criterion_id,
                                      "status": measurement["status"], "generation": updated["generation"]}
    if measurement["status"] == "failed" and measurement["counts"]["skipped"]:
        from odibi_anchor._dispatcher._workflow_evidence import missing_optional_dependencies

        missing = missing_optional_dependencies((result.get("samples") or {}).get("skip_reasons"))
        result["workflow_measurement"]["skipped"] = measurement["counts"]["skipped"]
        result["workflow_measurement"]["missing_dependencies"] = missing
        result.setdefault("findings", []).append(
            f"Criterion {criterion_id!r} requires zero skipped tests; "
            f"{measurement['counts']['skipped']} skipped."
            + (f" Install the missing optional dependencies in the qualification environment and "
               f"re-measure: {', '.join(missing)}." if missing
               else " Inspect samples.skip_reasons for the skip causes.")
        )
    return _format_test_result(result, output_format)
