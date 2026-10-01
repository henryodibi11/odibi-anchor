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

READ_COMMANDS = frozenset({"status", "prepare_delivery"})
WRITE_COMMANDS = frozenset({"create", "accept_plan", "implemented", "review", "qualify",
                            "request_delivery_approval", "verify_delivery", "block", "resume", "cancel", "replan"})


def workflow_action(path, *, session_state, command="status", workflow_id=None,
                    request_id=None, expected_generation=None, plan=None, findings=None,
                    reason=None, blocker_kind=None, resolution=None, output_format="dict"):
    """Operate on exact task-bound authority without performing destination mutations.

    Create returns a draft ID to bind at a fresh task acceptance via workflow_id.
    All transitions require a caller-observed generation and idempotency key.
    No actor, approval, measurement, candidate, or receipt payload is accepted.
    """
    from odibi_anchor._dispatcher._workflow_delivery import (
        observe_destination,
        prepare_delivery,
        request_delivery_approval,
    )
    from odibi_anchor._dispatcher._workflow_evidence import (
        _accepted_task,
        collect_candidate,
        collect_review,
        qualify_recorded,
    )

    if command not in READ_COMMANDS | WRITE_COMMANDS:
        raise ValueError("unknown workflow command")
    if output_format not in {"dict", "markdown"}:
        raise ValueError("workflow output_format must be dict or markdown")
    if command in WRITE_COMMANDS and output_format != "dict":
        raise ValueError("workflow writes require dict output for durability checkpointing")
    public_request = {"command": command, "expected_generation": expected_generation,
                      "plan": plan, "findings": findings, "reason": reason,
                      "blocker_kind": blocker_kind, "resolution": resolution}
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
            state = create_workflow(path, owner=workflow_owner(session_state), request_id=request_id, plan=plan)
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
            if command == "prepare_delivery":
                packet = prepare_delivery(path, session_state=session_state, workflow_id=workflow_id)
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
                                                  public_request=public_request)
            else:
                operation = command
                if command == "accept_plan":
                    record = _accepted_task(path, session_state, session_state.task_window_id)
                    payload = {"baseline": {"accepted_task_record": record["record_id"]},
                               "authority_ref": "accepted_task:" + session_state.task_window_id}
                elif command == "implemented":
                    payload = {"candidate": collect_candidate(path, session_state=session_state, workflow_id=workflow_id)}
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
                    payload = {"plan": plan, "reason": reason}
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


def bind_task_workflow(path, *, session_state, workflow_id):
    """Prepare binding before immutable task persistence; never alter an old record."""
    from odibi_anchor._dispatcher._workflow_admission import bind_workflow
    from odibi_anchor._dispatcher._workflow_evidence import bind_review

    profile = session_state.active_task_profile
    if profile.execution_mode == "read_only" and profile.work_type == "verify":
        return bind_review(path, session_state=session_state, workflow_id=workflow_id)
    state = read_workflow(path, owner=workflow_owner(session_state), workflow_id=workflow_id)
    ranks = {"low": 0, "medium": 1, "high": 2}
    if ranks[profile.risk] < ranks[state["plan"]["risk"]]:
        raise WorkflowError("wrong_authority", "accepted task cannot downgrade workflow risk")
    return bind_workflow(path, session_state=session_state, workflow_id=workflow_id)


def measured_test(path, *, session_state, runner, criterion_id, args, kwargs):
    """Retain actual runner output with exact before/after candidate and environment."""
    from odibi_anchor._dispatcher._session_tools import _format_test_result
    from odibi_anchor._dispatcher._workflow_evidence import (
        collect_candidate,
        collect_test_measurement,
        runtime_environment,
    )

    state = bound_workflow(path, session_state=session_state)
    if state is None or state["progress"] != "implemented" or state["status"] != "active":
        raise WorkflowError("missing_evidence", "measurement requires a bound implemented candidate")
    if args or kwargs.get("mark"):
        raise ValueError("workflow measurement requires exact keyword target list without marker filters")
    criteria = [c for c in state["plan"]["criteria"] if c["id"] == criterion_id]
    targets = kwargs.get("target")
    if (len(criteria) != 1 or criteria[0]["method"] != "pytest"
            or not isinstance(targets, list) or targets != criteria[0].get("test_targets")):
        raise WorkflowError("missing_evidence", "test target must exactly match the workflow criterion")
    before = collect_candidate(path, session_state=session_state, workflow_id=state["workflow_id"])
    environment = runtime_environment()
    output_format = kwargs.get("output_format", "dict")
    if output_format != "dict":
        raise ValueError("workflow measurements require dict output for durability checkpointing")
    result = runner(**{**kwargs, "output_format": "dict"})
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
    return _format_test_result(result, output_format)
