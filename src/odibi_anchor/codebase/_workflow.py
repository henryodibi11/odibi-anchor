"""Durable workflow authority, independent of task-window closure.

This internal domain consumes observations from trusted runtime collectors. It
does not authenticate caller-supplied approvals or perform destination effects.
Public transports must not expose these persistence functions as attestation APIs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

DOMAIN = "workflow"
VERSION = 1
MAX_BYTES = 256_000
_DDL = (
    "CREATE TABLE workflow_events (workflow_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0), request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, previous_sha256 TEXT NOT NULL, event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL, PRIMARY KEY(workflow_id,generation), UNIQUE(workflow_id,request_id))",
    "CREATE TRIGGER workflow_events_no_update BEFORE UPDATE ON workflow_events BEGIN SELECT RAISE(ABORT,'workflow events are immutable'); END",
    "CREATE TRIGGER workflow_events_no_delete BEFORE DELETE ON workflow_events BEGIN SELECT RAISE(ABORT,'workflow events are immutable'); END",
)
SCHEMA_SHA256 = hashlib.sha256((";\n".join(_DDL) + ";\n").encode()).hexdigest()
_OWNER_KEYS = {"project_id", "target_root", "artifact_root", "anchor_home", "trust_domain"}
_BLOCK_REASONS = {
    "missing_evidence", "unavailable", "unsafe", "awaiting_authority",
    "replan_required", "recovery_required", "conflict", "outcome_unknown",
}


class WorkflowError(RuntimeError):
    """A workflow operation cannot safely advance authoritative state."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    """Encode bounded JSON without nonfinite numbers or non-string object keys."""
    def check(item: Any, depth: int = 0) -> None:
        if depth > 20:
            raise ValueError("workflow JSON nesting exceeds 20")
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("workflow object keys must be strings")
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("workflow values must be JSON values")

    check(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("workflow packet exceeds byte limit")
    return encoded


def digest(value: Any) -> str:
    """Identify exact packet/candidate bytes, not their correctness or authority."""
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() in {"...", "TBD"}:
        raise ValueError(f"{name} must be explicit nonempty text")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{name} must be a nonempty object")
    return json.loads(canonical(value))


def _owner(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _object(dict(value), "owner")
    if set(result) != _OWNER_KEYS:
        raise ValueError("workflow owner requires exact project, roots and trust domain")
    for key, item in result.items():
        _text(item, key)
        if key in {"target_root", "artifact_root", "anchor_home"} and (
            not Path(item).is_absolute() or str(Path(item).resolve()) != item
        ):
            raise ValueError(f"{key} must be a canonical absolute path")
    return result


def _plan(value: Any, *, ready: bool = False) -> dict[str, Any]:
    plan = _object(value, "plan")
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != VERSION:
        raise ValueError("unsupported plan schema_version")
    _text(plan.get("goal"), "plan.goal")
    if plan.get("risk") not in {"low", "medium", "high"}:
        raise ValueError("plan.risk must be low, medium or high")
    if plan.get("execution_mode") not in {"read_only", "artifact_only", "source_change", "data_change"}:
        raise ValueError("invalid plan execution_mode")
    # Drafts may omit fields, but a present list field must already have its accepted
    # shape; otherwise the error surfaces only later at accept_plan.
    for key in ("scope", "exclusions", "constraints", "risks", "stop_conditions",
                "unresolved_decisions", "source_paths", "artifact_paths", "criteria"):
        if key in plan and not isinstance(plan[key], list):
            raise ValueError(f"plan.{key} must be an explicit array")
    children = plan.get("required_children", [])
    if not isinstance(children, list):
        raise ValueError("plan.required_children must be an array")
    child_ids = []
    for child in children:
        if not isinstance(child, dict) or set(child) != {"workflow_id", "plan_sha256"}:
            raise ValueError("required child requires exact workflow_id and plan_sha256")
        child_ids.append(_text(child["workflow_id"], "child.workflow_id"))
        pin = child["plan_sha256"]
        if (not isinstance(pin, str) or not pin.startswith("sha256:") or len(pin) != 71
                or any(char not in "0123456789abcdef" for char in pin[7:])):
            raise ValueError("child.plan_sha256 must be a canonical SHA256 identity")
    if len(child_ids) != len(set(child_ids)):
        raise ValueError("required child workflow IDs must be unique")
    if ready:
        for key in ("scope", "exclusions", "constraints", "risks", "stop_conditions"):
            if not isinstance(plan.get(key), list):
                raise ValueError(f"plan.{key} must be an explicit array")
            for item in plan[key]:
                _text(item, key)
        if not plan["scope"]:
            raise ValueError("plan.scope must be bounded")
        if plan.get("unresolved_decisions") != []:
            raise WorkflowError("plan_unready", "resolve consequential decisions before implementation")
        _object(plan.get("destination"), "plan.destination")
        criteria = plan.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("plan requires acceptance criteria")
        ids = []
        for criterion in criteria:
            item = _object(criterion, "criterion")
            for key in ("id", "expected", "method"):
                _text(item.get(key), f"criterion.{key}")
            ids.append(item["id"])
        if len(ids) != len(set(ids)):
            raise ValueError("criterion IDs must be unique")
    return plan


def _matching(value: dict[str, Any], state: dict[str, Any]) -> None:
    if (value.get("plan_sha256") != state["plan_sha256"]
            or value.get("candidate_sha256") != digest(state["candidate"])):
        raise WorkflowError("stale_evidence", "evidence must match the exact plan and candidate")


def reconciliation_evidence(state: dict[str, Any]) -> dict[str, Any]:
    """Project only exact retained obligations and evidence, never byte-readback inference.

    Unsupported obligations stay unavailable. Existing criterion and producer
    closure evidence are the only supported sources; this is not a ticket writer.
    """
    contract = state["plan"].get("reconciliation")
    proof = {"workflow_id": state["workflow_id"], "plan_sha256": state["plan_sha256"],
             "candidate_sha256": digest(state["candidate"]), "contract_sha256": digest(contract),
             "status": "unavailable", "requirements": []}
    if not isinstance(contract, dict) or not isinstance(contract.get("requirements"), list):
        return {**proof, "reason": "Plan reconciliation contract is unavailable"}
    requirements = contract["requirements"]
    if not requirements:
        reason = contract.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return {**proof, "reason": "An empty obligation set requires an explicit plan rationale"}
        return {**proof, "status": "satisfied", "reason": reason, "basis": "explicit_empty_plan_obligations"}
    ids = [item.get("id") if isinstance(item, dict) else None for item in requirements]
    if (not all(isinstance(item, str) and item.strip() for item in ids)
            or len(ids) != len(set(ids))):
        return {**proof, "reason": "Reconciliation obligations require unique explicit IDs"}
    qualification = state.get("qualification") or {}
    bound = {key: proof[key] for key in ("plan_sha256", "candidate_sha256")}
    for obligation in requirements:
        entry = {"obligation": obligation, "status": "unavailable", "evidence_ref": None}
        method = obligation.get("method")
        if method == "qualification_criterion" and set(obligation) == {"id", "method", "criterion_id"}:
            matches = [check for check in qualification.get("checks", [])
                       if check.get("criterion_id") == obligation["criterion_id"]]
            entry["status"] = "unsatisfied"
            if (len(matches) == 1 and matches[0].get("status") == "satisfied"
                    and all(matches[0].get(key) == value for key, value in bound.items())):
                entry.update(status="satisfied", evidence_ref=digest(matches[0]))
        elif method == "producer_learning" and set(obligation) == {"id", "method"}:
            receipt = qualification.get("producer_terminal_record_sha256")
            entry["status"] = "unsatisfied"
            if receipt and all(qualification.get(key) == value for key, value in bound.items()):
                entry.update(status="satisfied", evidence_ref=receipt,
                             producer=state["candidate"]["producer"])
        proof["requirements"].append(entry)
    statuses = {item["status"] for item in proof["requirements"]}
    proof["status"] = "unavailable" if "unavailable" in statuses else (
        "unsatisfied" if "unsatisfied" in statuses else "satisfied"
    )
    return proof


def _apply(state: dict[str, Any], operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Pure transition predicate; retained snapshots never retroactively change."""
    result = json.loads(canonical(state))
    if state["status"] in {"completed", "cancelled"}:
        raise WorkflowError("terminal", "terminal workflows cannot be advanced")
    delivery_authorized = state["progress"] in {"approved_for_delivery", "delivered"}
    if delivery_authorized and operation in {"cancel", "replan", "resume"}:
        raise WorkflowError("recovery_required", "revoke or reconcile delivery before changing work")
    if operation == "revoke_delivery" and state["progress"] == "approved_for_delivery":
        if state["blocker"] and state["blocker"]["kind"] == "outcome_unknown":
            raise WorkflowError("recovery_required", "reconcile the unknown delivery outcome first")
        _matching(payload, state)
        _text(payload.get("authority_ref"), "revocation authority_ref")
        if payload.get("actor_kind") != "human":
            raise WorkflowError("authority_required", "delivery revocation requires human authority")
        result.update(status="active", progress="qualified", approval=None, blocker=None)
        return result
    if operation == "reconcile_delivery" and delivery_authorized:
        _matching(payload, state)
        for key in ("observer", "evidence_ref"):
            _text(payload.get(key), key)
        if (payload.get("destination") != state["plan"]["destination"]
                or payload.get("method") != "readback" or payload.get("status") != "satisfied"):
            raise WorkflowError("destination_unverified", "delivery recovery requires destination readback")
        if payload.get("outcome") == "not_delivered" and payload.get("observed_identity") is None:
            result.update(status="active", progress="qualified", approval=None,
                          delivery=None, blocker=None, recovery=payload)
        elif (payload.get("outcome") == "delivered"
              and payload.get("observed_identity") == state["candidate"]["identity"]):
            result.update(status="active", progress="delivered", blocker=None,
                          delivery=payload, recovery=payload)
        else:
            raise WorkflowError("destination_unverified", "delivery outcome remains unknown or conflicting")
        return result
    if operation in {"block", "cancel"}:
        _text(payload.get("reason"), "reason")
        if operation == "block" and payload.get("kind") not in _BLOCK_REASONS:
            raise ValueError("invalid workflow block kind")
        if (state["blocker"] and state["blocker"]["kind"] == "outcome_unknown"
                and operation == "block" and payload["kind"] != "outcome_unknown"):
            raise WorkflowError("recovery_required", "cannot replace an unknown delivery outcome")
        result.update(status="blocked" if operation == "block" else "cancelled", blocker=payload)
        if operation == "cancel":
            result["approval"] = None
        return result
    if operation == "replan":
        _text(payload.get("reason"), "reason")
        plan = _plan(payload.get("plan"))
        ranks = {"low": 0, "medium": 1, "high": 2}
        if ranks[plan["risk"]] < ranks[state["plan"]["risk"]]:
            raise WorkflowError("risk_downgrade", "replan cannot authorize a risk downgrade")
        result.update(phase="plan", status="active", progress="draft", plan=plan,
                      plan_sha256=digest(plan), admission=None, candidate=None, qualification=None,
                      approval=None, delivery=None, verification=None, blocker=None,
                      measurements={}, review_result=None)
        result["artifact_baseline"] = payload.get("artifact_baseline")
        return result
    if operation == "resume":
        if state["status"] != "blocked":
            raise WorkflowError("invalid_transition", "only blocked work can resume")
        _text(payload.get("resolution"), "resolution")
        if state["blocker"]["kind"] in {"unsafe", "replan_required", "outcome_unknown", "recovery_required"}:
            raise WorkflowError("recovery_required", "replan or reconcile the uncertain outcome first")
        result.update(status="active", blocker=None)
        return result
    if state["status"] != "active":
        raise WorkflowError("blocked", "resolve the workflow blocker before advancing")
    progress = state["progress"]
    if operation == "accept_plan" and progress == "draft":
        plan = _plan(state["plan"], ready=True)
        _object(payload.get("baseline"), "baseline")
        _text(payload.get("authority_ref"), "plan authority_ref")
        result.update(phase="implement_and_qualify", progress="planned", plan=plan,
                      admission=payload)
    elif operation == "implemented" and state["phase"] == "implement_and_qualify":
        candidate = _object(payload.get("candidate"), "candidate")
        for key in ("kind", "identity", "producer"):
            _text(candidate.get(key), f"candidate.{key}")
        result.update(progress="implemented", candidate=candidate, qualification=None,
                      approval=None, delivery=None, verification=None,
                      measurements={}, review_result=None)
    elif operation == "record_check" and progress == "implemented":
        _matching(payload, state)
        criterion = payload.get("criterion_id")
        if criterion not in {c["id"] for c in state["plan"]["criteria"]}:
            raise WorkflowError("missing_evidence", "measurement does not match a plan criterion")
        for key in ("method", "evidence_ref", "collector"):
            _text(payload.get(key), key)
        if payload.get("status") not in {"satisfied", "failed", "unavailable"}:
            raise ValueError("invalid measurement status")
        result.setdefault("measurements", {})[criterion] = payload
    elif operation == "record_review" and progress == "implemented":
        _matching(payload, state)
        for key in ("reviewer", "evidence_ref", "independence", "reviewer_authentication"):
            _text(payload.get(key), key)
        if payload.get("status") not in {"satisfied", "failed", "unavailable"}:
            raise ValueError("invalid review status")
        result["review_result"] = payload
    elif operation == "qualify" and progress == "implemented":
        _matching(payload, state)
        checks = payload.get("checks")
        if not isinstance(checks, list) or not checks:
            raise WorkflowError("missing_evidence", "qualification requires criterion evidence")
        ids = []
        for check in checks:
            check = _object(check, "check")
            _matching(check, state)
            for key in ("criterion_id", "method", "evidence_ref", "collector"):
                _text(check.get(key), f"check.{key}")
            if check.get("status") != "satisfied":
                raise WorkflowError("unsatisfied_evidence", "required evidence is not satisfied")
            ids.append(check["criterion_id"])
        if len(ids) != len(set(ids)) or set(ids) != {c["id"] for c in state["plan"]["criteria"]}:
            raise WorkflowError("missing_evidence", "evidence must cover each exact criterion once")
        review = _object(payload.get("review"), "review")
        _matching(review, state)
        _text(review.get("reviewer"), "reviewer")
        _text(review.get("evidence_ref"), "review evidence_ref")
        if review.get("status") != "satisfied":
            raise WorkflowError("review_required", "review has unresolved findings")
        if state["plan"]["risk"] == "high" and (
            review.get("kind") != "independent"
            or review["reviewer"] == state["candidate"]["producer"]
        ):
            raise WorkflowError("review_required", "high-risk work requires independent review")
        if review.get("kind") not in {"self", "independent"}:
            raise ValueError("unknown review kind")
        result.update(phase="deliver", progress="qualified", qualification=payload)
    elif operation == "approve_delivery" and progress == "qualified":
        _matching(payload, state)
        for key in ("owner", "authority_ref", "operation"):
            _text(payload.get(key), key)
        if payload.get("destination") != state["plan"]["destination"]:
            raise WorkflowError("wrong_destination", "approval destination does not match plan")
        if payload.get("actor_kind") != "human":
            raise WorkflowError("authority_required", "explicit human delivery authority required")
        result.update(progress="approved_for_delivery", approval=payload)
    elif operation == "delivered" and progress == "approved_for_delivery":
        _matching(payload, state)
        _text(payload.get("receipt_ref"), "receipt_ref")
        if (payload.get("destination") != state["approval"]["destination"]
                or payload.get("operation") != state["approval"]["operation"]):
            raise WorkflowError("authority_required", "delivery exceeds approved destination/operation")
        result.update(progress="delivered", delivery=payload)
    elif operation == "verify_delivery" and progress == "delivered":
        _matching(payload, state)
        for key in ("observer", "evidence_ref"):
            _text(payload.get(key), key)
        if (payload.get("destination") != state["plan"]["destination"]
                or payload.get("observed_identity") != state["candidate"]["identity"]
                or payload.get("method") != "readback" or payload.get("status") != "satisfied"):
            raise WorkflowError("destination_unverified", "independent matching destination readback required")
        reconciliation = reconciliation_evidence(state)
        if (reconciliation["status"] != "satisfied"
                or payload.get("reconciliation") != reconciliation):
            raise WorkflowError("missing_evidence", "required reconciliation remains owed")
        result.update(progress="delivery_verified", status="completed", verification=payload,
                      completed=True)
    else:
        raise WorkflowError("invalid_transition", f"cannot {operation} from {state['phase']}/{progress}")
    return result


@contextmanager
def _connection(path: str | Path, *, write: bool = False):
    path = Path(path).resolve()
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise WorkflowError("unavailable", "workflow authority is unavailable")
    connection = connect_shared_memory(
        str(path) if write else path.as_uri() + "?mode=ro", uri=not write,
        owner_key_kind="workflow_id", isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        if write:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if write:
            connection.commit()
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def _schema(connection: sqlite3.Connection, *, create: bool = False) -> None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='anchor_schema_versions' AND type='table'"
    ).fetchone()
    if not exists and not create:
        raise WorkflowError("unavailable", "workflow schema is unavailable")
    if create:
        connection.execute("CREATE TABLE IF NOT EXISTS anchor_schema_versions (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64), applied_at TEXT NOT NULL)")
    row = connection.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
    ).fetchone()
    if row is None:
        if not create:
            raise WorkflowError("unavailable", "workflow domain is unavailable")
        for statement in _DDL:
            connection.execute(statement)
        connection.execute("INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                           (DOMAIN, VERSION, SCHEMA_SHA256, datetime.now(UTC).isoformat()))
    elif tuple(row) != (VERSION, SCHEMA_SHA256):
        raise WorkflowError("integrity", "workflow schema version/checksum mismatch")
    with sqlite3.connect(":memory:") as expected:
        for statement in _DDL:
            expected.execute(statement)
        query = "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='workflow_events' AND sql IS NOT NULL ORDER BY type,name"
        if [tuple(r) for r in connection.execute(query)] != expected.execute(query).fetchall():
            raise WorkflowError("integrity", "workflow schema was modified")


def _events(connection: sqlite3.Connection, workflow_id: str, owner: dict[str, Any]) -> list[dict[str, Any]]:
    from odibi_anchor.codebase._authority_relocation import load_relocations, rebase_identity

    relocations = load_relocations(connection)
    rows = connection.execute("SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY generation",
                              (workflow_id,)).fetchall()
    events = []
    previous = ""
    for index, row in enumerate(rows):
        event = json.loads(row["event_json"])
        state = event["state"]
        if rebase_identity(state["owner"], relocations, owner["anchor_home"]) != owner:
            raise WorkflowError("unavailable", "workflow does not match this exact authority")
        if (row["generation"] != index or state["generation"] != index
                or state["workflow_id"] != workflow_id or state["schema_version"] != VERSION
                or row["previous_sha256"] != previous or event["previous_sha256"] != previous
                or digest(event) != row["event_sha256"]
                or event["request_id"] != row["request_id"]
                or digest(event["request"]) != row["request_sha256"]):
            raise WorkflowError("integrity", "workflow event integrity check failed")
        previous = row["event_sha256"]
        events.append(event)
    if not events:
        raise WorkflowError("unavailable", "workflow not found")
    return events


def _append(connection, state, request_id, request, previous):
    event = {"state": state, "request_id": request_id, "request": request,
             "previous_sha256": previous, "observed_at": datetime.now(UTC).isoformat()}
    connection.execute("INSERT INTO workflow_events VALUES(?,?,?,?,?,?,?)", (
        state["workflow_id"], state["generation"], request_id, digest(request), previous,
        canonical(event), digest(event),
    ))
    return state


def create_workflow(path: str | Path, *, owner: Mapping[str, Any], request_id: str,
                    plan: dict[str, Any], artifact_baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create draft authority; request replay never invents historical evidence."""
    identity = _owner(owner)
    request_id = _text(request_id, "request_id")
    plan = _plan(plan)
    identifier = "wf_" + digest({"owner": identity, "request_id": request_id})[7:]
    request = {"operation": "create", "plan": plan}
    with _connection(path, write=True) as connection:
        _schema(connection, create=True)
        exists = connection.execute("SELECT 1 FROM workflow_events WHERE workflow_id=?", (identifier,)).fetchone()
        if exists:
            event = _events(connection, identifier, identity)[0]
            if event["request"] != request:
                raise WorkflowError("conflict", "request_id reused for different workflow creation")
            return event["state"]
        state = {"schema_version": VERSION, "workflow_id": identifier, "generation": 0,
                 "owner": identity, "phase": "plan", "status": "active", "progress": "draft",
                 "plan": plan, "plan_sha256": digest(plan), "admission": None,
                 "candidate": None, "qualification": None, "approval": None,
                 "delivery": None, "verification": None, "blocker": None, "completed": False,
                 "measurements": {}, "review_result": None}
        if artifact_baseline is not None:
            state["artifact_baseline"] = _object(artifact_baseline, "artifact_baseline")
        _required_child_receipts(connection, state, identity, complete=False)
        return _append(connection, state, request_id, request, "")


def read_workflow(path: str | Path, *, owner: Mapping[str, Any], workflow_id: str) -> dict[str, Any]:
    """Read exact-owner current state without creating or repairing storage."""
    identity = _owner(owner)
    _text(workflow_id, "workflow_id")
    with _connection(path) as connection:
        _schema(connection)
        return _events(connection, workflow_id, identity)[-1]["state"]


def replay_public_request(path: str | Path, *, owner: Mapping[str, Any], workflow_id: str,
                          request_id: str, public_request: dict[str, Any]) -> dict[str, Any] | None:
    """Read a verified prior receipt before repeating collectors or human prompts.

    This is historical acknowledgement, not evidence that the destination remains
    unchanged. Ownership and the full hash chain are verified on every replay.
    """
    identity = _owner(owner)
    _text(request_id, "request_id")
    with _connection(path) as connection:
        _schema(connection)
        for event in _events(connection, workflow_id, identity):
            if event["request_id"] == request_id:
                if event["request"].get("public_request") != public_request:
                    raise WorkflowError("conflict", "request_id reused with conflicting public request")
                return event["state"]
    return None


def _required_child_receipts(connection, state, owner, *, complete):
    """Read bounded exact-owner dependencies under the caller's write transaction."""
    loaded = {}
    checked = set()

    def visit(node, ancestors):
        identifier = node["workflow_id"]
        if identifier in ancestors:
            raise WorkflowError("dependency_cycle", "required child dependency cycle")
        if len(ancestors) > 20 or len(loaded) > 100:
            raise WorkflowError("unavailable", "dependency graph exceeds 100 nodes or 20 levels")
        if identifier in checked:
            return
        for reference in node["plan"].get("required_children", []):
            child_id = reference["workflow_id"]
            if child_id in ancestors or child_id == identifier:
                raise WorkflowError("dependency_cycle", "required child dependency cycle")
            if child_id not in loaded:
                loaded[child_id] = _events(connection, child_id, owner)[-1]["state"]
            child = loaded[child_id]
            if child["plan_sha256"] != reference["plan_sha256"]:
                raise WorkflowError("stale_plan", "required child plan changed; replan the parent")
            if complete and (child["status"] != "completed"
                             or child["progress"] != "delivery_verified"
                             or child["completed"] is not True
                             or not child["candidate"] or not child["verification"]):
                raise WorkflowError("missing_evidence", "required child is not delivery verified")
            visit(child, ancestors | {identifier})
        checked.add(identifier)

    visit(state, set())
    if not complete:
        return []
    return [{"workflow_id": child["workflow_id"], "plan_sha256": child["plan_sha256"],
             "candidate_sha256": digest(child["candidate"]),
             "verification_sha256": digest(child["verification"])}
            for reference in state["plan"].get("required_children", [])
            for child in [loaded[reference["workflow_id"]]]]


def transition_workflow(path: str | Path, *, owner: Mapping[str, Any], workflow_id: str,
                        expected_generation: int, request_id: str, operation: str,
                        payload: dict[str, Any], public_request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Atomically compare generation, validate evidence, and append a transition."""
    identity = _owner(owner)
    _text(workflow_id, "workflow_id")
    _text(request_id, "request_id")
    if type(expected_generation) is not int or expected_generation < 0:
        raise ValueError("expected_generation must be a nonnegative integer")
    request = {"operation": _text(operation, "operation"), "payload": _object(payload, "payload"),
               "expected_generation": expected_generation}
    if public_request is not None:
        request["public_request"] = _object(public_request, "public_request")
    if not Path(path).is_file():
        raise WorkflowError("unavailable", "workflow authority is unavailable")
    with _connection(path, write=True) as connection:
        _schema(connection)
        events = _events(connection, workflow_id, identity)
        for event in events:
            if event["request_id"] == request_id:
                if event["request"] != request:
                    raise WorkflowError("conflict", "request_id reused with conflicting transition")
                return event["state"]
        current = events[-1]["state"]
        if current["generation"] != expected_generation:
            raise WorkflowError("conflict", "workflow generation changed; refresh before acting")
        state = _apply(current, operation, request["payload"])
        if operation in {"accept_plan", "replan", "qualify", "approve_delivery", "verify_delivery"}:
            complete = operation in {"qualify", "approve_delivery", "verify_delivery"}
            receipts = _required_child_receipts(connection, state, identity, complete=complete)
            if operation == "qualify" and state["plan"].get("required_children"):
                # Do not mutate the canonical request: exact retries compare it.
                state["qualification"] = {**state["qualification"], "required_children": receipts}
            elif receipts and current["qualification"].get("required_children") != receipts:
                raise WorkflowError("stale_evidence", "required child completion changed since qualification")
        state["generation"] += 1
        return _append(connection, state, request_id, request, digest(events[-1]))
