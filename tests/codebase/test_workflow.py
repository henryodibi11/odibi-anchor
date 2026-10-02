"""Workflow authority is not task closure, a test pass, or a publish receipt."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from odibi_anchor.codebase import _workflow as workflow


def checksum(value):
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


@pytest.fixture
def owner(tmp_path):
    return {"project_id": "alpha", "target_root": str(tmp_path / "target"),
            "artifact_root": str(tmp_path / "artifacts"), "anchor_home": str(tmp_path),
            "trust_domain": "personal"}


@pytest.fixture
def plan(request):
    return {"schema_version": 1, "goal": "Reject malformed orders", "risk": getattr(request, "param", "high"),
            "execution_mode": "source_change", "scope": ["parser"], "exclusions": ["UI"],
            "constraints": ["No data writes"], "risks": ["Compatibility"],
            "stop_conditions": ["Changed input contract"], "unresolved_decisions": [],
            "criteria": [{"id": "AC1", "expected": "invalid order is rejected", "method": "pytest"},
                         {"id": "AC2", "expected": "valid order stays accepted", "method": "pytest"}],
            "reconciliation": {"requirements": [], "reason": "No linked obligations in this isolated transition fixture"},
            "destination": {"kind": "git", "repository": "alpha", "ref": "main"}}


@pytest.fixture
def run(tmp_path, owner, plan):
    path = tmp_path / "authority.db"
    initial = workflow.create_workflow(path, owner=owner, request_id="create", plan=plan)

    def advance(operation, payload, *, state=None, request_id=None):
        current = state or workflow.read_workflow(path, owner=owner, workflow_id=initial["workflow_id"])
        return workflow.transition_workflow(
            path, owner=owner, workflow_id=initial["workflow_id"],
            expected_generation=current["generation"],
            request_id=request_id or f"{operation}:{current['generation']}",
            operation=operation, payload=payload,
        )
    advance.path = path
    advance.initial = initial
    return advance


def implemented(run):
    run("accept_plan", {"baseline": {"head": "a" * 40}, "authority_ref": "request:owner"})
    return run("implemented", {"candidate": {"kind": "git", "identity": "b" * 40,
                                             "producer": "agent:implementer"}})


def binding(state):
    return {"plan_sha256": checksum(state["plan"]), "candidate_sha256": checksum(state["candidate"])}


def qualification(state):
    bound = binding(state)
    return {**bound, "checks": [
        {**bound, "criterion_id": ac, "method": "pytest", "status": "satisfied",
         "evidence_ref": "test:retained", "collector": "pytest:isolated"}
        for ac in ("AC1", "AC2")
    ], "review": {**bound, "kind": "independent", "reviewer": "agent:reviewer",
                  "status": "satisfied", "evidence_ref": "review:retained"}}


def approval(state):
    return {**binding(state), "actor_kind": "human", "owner": "owner:henry",
            "authority_ref": "authenticated:challenge", "operation": "merge",
            "destination": state["plan"]["destination"]}


def delivered(run):
    state = implemented(run)
    state = run("qualify", qualification(state))
    state = run("approve_delivery", approval(state))
    return run("delivered", {**binding(state), "receipt_ref": "provider:merge:1",
                             "operation": "merge", "destination": state["plan"]["destination"]})


def readback(state):
    return {**binding(state), "observer": "github:read-api", "evidence_ref": "readback:1",
            "method": "readback", "status": "satisfied",
            "reconciliation": {**binding(state), "workflow_id": state["workflow_id"],
                               "contract_sha256": checksum(state["plan"]["reconciliation"]),
                               "status": "satisfied", "requirements": [],
                               "basis": "explicit_empty_plan_obligations",
                               "reason": state["plan"]["reconciliation"]["reason"]},
            "destination": state["plan"]["destination"], "observed_identity": "b" * 40}


def test_only_verified_destination_completes_workflow(run, owner):
    state = implemented(run)
    assert (state["phase"], state["progress"], state["completed"]) == (
        "implement_and_qualify", "implemented", False,
    )
    state = run("qualify", qualification(state))
    assert (state["phase"], state["progress"], state["completed"]) == ("deliver", "qualified", False)
    state = run("approve_delivery", approval(state))
    assert state["progress"] == "approved_for_delivery" and not state["completed"]
    state = run("delivered", {**binding(state), "receipt_ref": "merge:1", "operation": "merge",
                             "destination": state["plan"]["destination"]})
    assert state["progress"] == "delivered" and not state["completed"]
    final = run("verify_delivery", readback(state))
    assert (final["status"], final["progress"], final["completed"]) == (
        "completed", "delivery_verified", True,
    )
    assert workflow.read_workflow(run.path, owner=owner, workflow_id=final["workflow_id"]) == final
    with pytest.raises(workflow.WorkflowError, match="terminal"):
        run("replan", {"reason": "change", "plan": final["plan"]})


@pytest.mark.parametrize("field,value", [("project_id", "beta"), ("trust_domain", "work"),
                                         ("target_root", "/elsewhere"), ("artifact_root", "/other"),
                                         ("anchor_home", "/foreign")])
def test_exact_owner_is_required_for_reads_and_transitions(run, owner, field, value):
    wrong = {**owner, field: value}
    with pytest.raises(workflow.WorkflowError, match="exact authority"):
        workflow.read_workflow(run.path, owner=wrong, workflow_id=run.initial["workflow_id"])
    with pytest.raises(workflow.WorkflowError, match="exact authority"):
        workflow.transition_workflow(run.path, owner=wrong, workflow_id=run.initial["workflow_id"],
                                     expected_generation=0, request_id="foreign", operation="cancel",
                                     payload={"reason": "not mine"})


def test_read_and_transition_never_initialize_missing_authority(tmp_path, owner):
    path = tmp_path / "absent" / "state.db"
    with pytest.raises(workflow.WorkflowError, match="unavailable"):
        workflow.read_workflow(path, owner=owner, workflow_id="unknown")
    with pytest.raises(workflow.WorkflowError, match="unavailable"):
        workflow.transition_workflow(path, owner=owner, workflow_id="unknown", expected_generation=0,
                                     request_id="r", operation="cancel", payload={"reason": "stop"})
    assert not path.parent.exists()


def test_generation_and_idempotency_are_transactional(run, owner, plan):
    payload = {"baseline": {"head": "a" * 40}, "authority_ref": "request:owner"}
    accepted = run("accept_plan", payload, state=run.initial, request_id="accept")
    assert run("accept_plan", payload, state=run.initial, request_id="accept") == accepted
    with pytest.raises(workflow.WorkflowError, match="conflicting"):
        run("cancel", {"reason": "stop"}, state=run.initial, request_id="accept")
    with pytest.raises(workflow.WorkflowError, match="generation changed"):
        run("cancel", {"reason": "stop"}, state=run.initial)
    assert workflow.create_workflow(run.path, owner=owner, request_id="create", plan=plan) == run.initial
    with pytest.raises(workflow.WorkflowError, match="different workflow"):
        workflow.create_workflow(run.path, owner=owner, request_id="create", plan={**plan, "risk": "low"})


def test_concurrent_generation_has_one_winner(run):
    def cancel(index):
        try:
            return run("cancel", {"reason": "stop"}, state=run.initial, request_id=f"cancel-{index}")
        except workflow.WorkflowError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(cancel, [0, 1]))
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert outcomes.count("conflict") == 1


def test_failed_append_rolls_back_transition(run, owner, monkeypatch):
    original = workflow._append

    def interrupted(*args):
        original(*args)
        raise OSError("crash before commit")
    monkeypatch.setattr(workflow, "_append", interrupted)
    with pytest.raises(OSError, match="crash"):
        run("cancel", {"reason": "stop"})
    assert workflow.read_workflow(run.path, owner=owner, workflow_id=run.initial["workflow_id"]) == run.initial


@pytest.mark.parametrize("status", ["failed", "missing", "stale", "unavailable", "skipped", "unsafe", "not_applicable"])
def test_nonpassing_required_evidence_cannot_qualify(run, status):
    state = implemented(run)
    payload = qualification(state)
    payload["checks"][1]["status"] = status
    with pytest.raises(workflow.WorkflowError, match="not satisfied"):
        run("qualify", payload)


def test_evidence_requires_all_criteria_and_exact_candidate(run):
    state = implemented(run)
    payload = qualification(state)
    payload["checks"] = payload["checks"][:1]
    with pytest.raises(workflow.WorkflowError, match="each exact criterion"):
        run("qualify", payload)
    payload = qualification(state)
    payload["checks"][1]["candidate_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(workflow.WorkflowError, match="exact plan and candidate"):
        run("qualify", payload)


@pytest.mark.parametrize("kind,reviewer", [("self", "agent:implementer"), ("independent", "agent:implementer")])
def test_high_risk_cannot_self_qualify(run, kind, reviewer):
    state = implemented(run)
    payload = qualification(state)
    payload["review"].update(kind=kind, reviewer=reviewer)
    with pytest.raises(workflow.WorkflowError, match="independent review"):
        run("qualify", payload)


@pytest.mark.parametrize("plan", ["low"], indirect=True)
def test_low_risk_self_review_is_sufficient(run):
    state = implemented(run)
    payload = qualification(state)
    payload["review"].update(kind="self", reviewer="agent:implementer")
    assert run("qualify", payload)["progress"] == "qualified"


def test_qualification_does_not_authorize_delivery(run):
    state = implemented(run)
    state = run("qualify", qualification(state))
    with pytest.raises(workflow.WorkflowError, match="cannot delivered"):
        run("delivered", {"receipt_ref": "self-reported"})
    with pytest.raises(workflow.WorkflowError, match="human delivery authority"):
        run("approve_delivery", {**approval(state), "actor_kind": "agent"})
    with pytest.raises(workflow.WorkflowError, match="destination"):
        run("approve_delivery", {**approval(state), "destination": {"kind": "git", "ref": "production"}})


@pytest.mark.parametrize("field,value", [("observed_identity", "c" * 40), ("method", "receipt"),
                                         ("status", "unavailable"), ("reconciliation", "pending"),
                                         ("destination", {"kind": "package", "name": "wrong"})])
def test_destination_must_be_verified_not_asserted(run, field, value):
    state = delivered(run)
    payload = {**readback(state), field: value}
    with pytest.raises(workflow.WorkflowError):
        run("verify_delivery", payload)


def test_replan_invalidates_candidate_and_preserves_historical_packet(run, owner, plan):
    old = implemented(run)
    run("qualify", qualification(old))
    revised = run("replan", {"reason": "incompatible source assumption", "plan": {**plan, "goal": "New goal"}})
    assert revised["phase"] == "plan" and revised["progress"] == "draft"
    assert all(revised[field] is None for field in ("admission", "candidate", "qualification", "approval", "delivery"))
    with sqlite3.connect(run.path) as db:
        snapshots = [json.loads(row[0])["state"] for row in db.execute("SELECT event_json FROM workflow_events")]
    assert old in snapshots


def test_unresolved_plan_is_not_implementation_authority(run, plan):
    run("replan", {"reason": "new question", "plan": {**plan, "unresolved_decisions": ["owner decision"]}})
    with pytest.raises(workflow.WorkflowError, match="resolve consequential"):
        run("accept_plan", {"baseline": {"head": "a" * 40}, "authority_ref": "request:owner"})
    with pytest.raises(workflow.WorkflowError, match="cannot implemented"):
        run("implemented", {"candidate": {"identity": "b" * 40}})


def test_unknown_outcome_cannot_be_cancelled_replanned_or_relabelled(run, plan):
    state = implemented(run)
    state = run("qualify", qualification(state))
    state = run("approve_delivery", approval(state))
    blocked = run("block", {"kind": "outcome_unknown", "reason": "provider timeout"})
    assert blocked["status"] == "blocked" and not blocked["completed"]
    with pytest.raises(workflow.WorkflowError, match="reconcile"):
        run("resume", {"resolution": "try again"})
    with pytest.raises(workflow.WorkflowError, match="reconcile"):
        run("cancel", {"reason": "owner withdrew task"})
    with pytest.raises(workflow.WorkflowError, match="reconcile"):
        run("replan", {"reason": "new plan", "plan": plan})
    with pytest.raises(workflow.WorkflowError, match="unknown delivery outcome"):
        run("block", {"kind": "missing_evidence", "reason": "hide uncertainty"})
    with pytest.raises(workflow.WorkflowError, match="unknown delivery outcome"):
        run("revoke_delivery", {**binding(state), "actor_kind": "human", "authority_ref": "withdraw"})
    reconciled = run("reconcile_delivery", {**readback(state), "outcome": "not_delivered", "observed_identity": None})
    assert reconciled["progress"] == "qualified" and reconciled["approval"] is None
    cancelled = run("cancel", {"reason": "owner withdrew after verified absence"})
    assert cancelled["status"] == "cancelled" and not cancelled["completed"]


def test_unknown_outcome_readback_preserves_delivery_without_claiming_done(run):
    state = delivered(run)
    run("block", {"kind": "outcome_unknown", "reason": "response lost"})
    reconciled = run("reconcile_delivery", {**readback(state), "outcome": "delivered"})
    assert reconciled["progress"] == "delivered" and not reconciled["completed"]
    assert run("verify_delivery", readback(state))["completed"]


def test_block_after_approval_requires_revocation_not_generic_resume(run):
    state = implemented(run)
    state = run("qualify", qualification(state))
    state = run("approve_delivery", approval(state))
    run("block", {"kind": "recovery_required", "reason": "host restarted"})
    with pytest.raises(workflow.WorkflowError, match="revoke or reconcile"):
        run("resume", {"resolution": "restarted"})
    revoked = run("revoke_delivery", {**binding(state), "actor_kind": "human", "authority_ref": "withdraw"})
    assert revoked["progress"] == "qualified" and revoked["approval"] is None
    with pytest.raises(workflow.WorkflowError, match="cannot delivered"):
        run("delivered", {**binding(state), "receipt_ref": "old approval"})


def test_schema_and_records_are_fail_closed(run, owner):
    with sqlite3.connect(run.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM workflow_events")
        db.execute("DROP TRIGGER workflow_events_no_update")
    with pytest.raises(workflow.WorkflowError, match="schema was modified"):
        workflow.read_workflow(run.path, owner=owner, workflow_id=run.initial["workflow_id"])


def test_event_corruption_rejected_even_after_trigger_restored(run, owner):
    with sqlite3.connect(run.path) as db:
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='workflow_events_no_update'").fetchone()[0]
        db.execute("DROP TRIGGER workflow_events_no_update")
        db.execute("UPDATE workflow_events SET event_sha256='corrupt'")
        db.execute(trigger)
    with pytest.raises(workflow.WorkflowError, match="integrity check"):
        workflow.read_workflow(run.path, owner=owner, workflow_id=run.initial["workflow_id"])


@pytest.mark.parametrize("value", [{1: "wrong key"}, {"value": float("nan")}, {"value": "x" * 256_001}])
def test_noncanonical_or_unbounded_packets_rejected(value):
    with pytest.raises(ValueError):
        workflow.canonical(value)


@pytest.mark.parametrize("risk", ["low", "medium"])
def test_replan_cannot_erase_risk_and_review_requirement(run, owner, plan, risk):
    before = implemented(run)
    with pytest.raises(workflow.WorkflowError, match="risk downgrade"):
        run("replan", {"reason": "avoid review", "plan": {**plan, "risk": risk}})
    assert workflow.read_workflow(run.path, owner=owner, workflow_id=before["workflow_id"]) == before


@pytest.fixture
def family(tmp_path, owner, plan):
    """Several independently progressing workflows in one actual authority DB."""
    path = tmp_path / "family.db"

    def create(name, children=(), authority=None):
        identity = authority or owner
        initial = workflow.create_workflow(
            path, owner=identity, request_id=name,
            plan={**plan, "required_children": [
                {"workflow_id": child["workflow_id"], "plan_sha256": child["plan_sha256"]}
                for child in children
            ]},
        )

        def advance(operation, payload, *, state=None, request_id=None):
            current = state or workflow.read_workflow(path, owner=identity, workflow_id=initial["workflow_id"])
            return workflow.transition_workflow(
                path, owner=identity, workflow_id=initial["workflow_id"],
                expected_generation=current["generation"],
                request_id=request_id or f"{operation}:{current['generation']}",
                operation=operation, payload=payload,
            )

        advance.initial = initial
        advance.path = path
        return advance

    return create


def test_parent_requires_all_children_and_its_own_checks(family, owner):
    left, right = family("left"), family("right")
    parent = family("parent", [left.initial, right.initial])
    before = implemented(parent)
    left_done = left("verify_delivery", readback(delivered(left)))
    with pytest.raises(workflow.WorkflowError, match=r"required child.*verified"):
        parent("qualify", {**qualification(before), "required_children": [{"completed": True}]})
    assert workflow.read_workflow(parent.path, owner=owner, workflow_id=before["workflow_id"]) == before
    right_done = right("verify_delivery", readback(delivered(right)))
    with pytest.raises(workflow.WorkflowError, match="criterion evidence"):
        parent("qualify", {**qualification(before), "checks": []})
    qualified = parent("qualify", qualification(before))
    assert parent("qualify", qualification(before), state=before) == qualified
    assert qualified["qualification"]["required_children"] == [
        {"workflow_id": child["workflow_id"], "plan_sha256": child["plan_sha256"],
         "candidate_sha256": checksum(child["candidate"]),
         "verification_sha256": checksum(child["verification"])}
        for child in (left_done, right_done)
    ]
    approved = parent("approve_delivery", approval(qualified))
    observed = parent("reconcile_delivery", {**readback(approved), "outcome": "delivered"})
    assert parent("verify_delivery", readback(observed))["completed"] is True


def test_child_replan_invalidates_parent_without_rewriting_history(family, owner, plan):
    child = family("child")
    parent = family("parent", [child.initial])
    before = implemented(parent)
    child("replan", {"plan": {**plan, "goal": "Changed required outcome"}, "reason": "new fact"})
    with pytest.raises(workflow.WorkflowError, match="child plan changed"):
        parent("qualify", qualification(before))
    assert workflow.read_workflow(parent.path, owner=owner, workflow_id=before["workflow_id"]) == before


def test_dependencies_reject_foreign_missing_duplicate_and_cycle(family, owner):
    foreign = family("foreign", authority={**owner, "project_id": "other"})
    with pytest.raises(workflow.WorkflowError, match="exact authority"):
        family("parent-foreign", [foreign.initial])
    with pytest.raises(workflow.WorkflowError, match="not found"):
        family("parent-missing", [{"workflow_id": "wf_missing", "plan_sha256": "sha256:" + "0" * 64}])
    child = family("child")
    with pytest.raises(ValueError, match="unique"):
        family("duplicates", [child.initial, child.initial])
    parent = family("parent", [child.initial])
    cycle_plan = {**child.initial["plan"], "required_children": [
        {"workflow_id": parent.initial["workflow_id"], "plan_sha256": parent.initial["plan_sha256"]}
    ]}
    with pytest.raises(workflow.WorkflowError, match="cycle"):
        child("replan", {"plan": cycle_plan, "reason": "accidental cycle"})


@pytest.mark.parametrize("references", [None, {}, ["child"], [{"workflow_id": "wf_x"}],
                                        [{"workflow_id": "wf_x", "plan_sha256": "latest"}]])
def test_required_child_contract_is_not_freeform(run, plan, references):
    with pytest.raises(ValueError, match=r"required_children|child"):
        run("replan", {"plan": {**plan, "required_children": references}, "reason": "malformed"})


def test_dependency_graph_is_bounded_and_exact_retry_is_historical(family, owner):
    child = family("leaf")
    parent = family("parent", [child.initial])
    accepted = parent("accept_plan", {"baseline": {"head": "a" * 40}, "authority_ref": "owner"},
                      request_id="accept")
    child("replan", {"plan": {**child.initial["plan"], "goal": "New"}, "reason": "new fact"})
    assert parent("accept_plan", {"baseline": {"head": "a" * 40}, "authority_ref": "owner"},
                  state=parent.initial, request_id="accept") == accepted
    child = family("chain-leaf")
    with pytest.raises(workflow.WorkflowError, match="dependency graph exceeds"):
        for index in range(25):
            child = family(f"chain-{index}", [child.initial])


def test_dependency_graph_width_is_bounded(family):
    children = [family(f"leaf-{index}").initial for index in range(101)]
    with pytest.raises(workflow.WorkflowError, match="dependency graph exceeds"):
        family("too-wide", children)


@pytest.mark.parametrize("field", ["workflow_id", "plan_sha256", "candidate_sha256", "contract_sha256", "requirements"])
def test_reconciliation_proof_cannot_be_reused_or_forged(run, field):
    state = delivered(run)
    payload = readback(state)
    payload["reconciliation"][field] = [] if field != "requirements" else ["forged"]
    with pytest.raises(workflow.WorkflowError, match="reconciliation"):
        run("verify_delivery", payload)
    assert workflow.read_workflow(run.path, owner=state["owner"], workflow_id=state["workflow_id"]) == state
