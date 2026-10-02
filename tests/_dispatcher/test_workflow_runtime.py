"""Public workflow operations cannot launder caller evidence into qualification."""

import hashlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init

    home = tmp_path / "home"
    target = tmp_path / "target"
    home.mkdir()
    target.mkdir()
    report = home / "workspace/projects/alpha/notebooks/report.md"
    (target / "test_report.py").write_text(
        "from pathlib import Path\ndef test_report():\n"
        f"    assert Path({str(report)!r}).read_bytes() == b'Result: 5\\n'\n"
    )
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    project_action(home, "create", name="alpha", target=target, output_format="dict")
    anchor, _, _ = init(root=str(target), project="alpha", output_format="dict")
    anchor("orient", output_format="dict")
    anchor("new_session", name="workflow_plan", inline=True, output_format="dict")
    with pytest.raises(RuntimeError, match="Fresh substantive artifact tasks require"):
        anchor("task", "Produce report without workflow", goal="Qualify report bytes", mode="planning",
               risk="medium", rigor="compact", trust_domain="personal",
               in_scope=["notebooks/report.md"], constraints=["Managed artifacts only"],
               acceptance_criteria=["Report matches contract"], output_format="dict")
    accepted = anchor("task", "Plan exact report output", goal="Qualify report bytes", mode="analysis",
                      risk="low", rigor="direct", trust_domain="personal",
                      in_scope=["notebooks/report.md"], constraints=["Managed artifacts only"],
                      acceptance_criteria=["Report matches its contract"], output_format="dict")
    assert accepted["workflow"]["status"] == "read_only_unphased"
    plan = {"schema_version": 1, "goal": "Qualify report bytes", "risk": "low",
            "execution_mode": "artifact_only", "scope": ["notebooks/report.md"],
            "artifact_paths": ["notebooks/report.md"], "exclusions": [], "constraints": [],
            "risks": [], "stop_conditions": [], "unresolved_decisions": [],
            "destination": {"kind": "managed_artifacts"},
            "reconciliation": {"requirements": [], "reason": "Isolated report has no linked external obligations"},
            "criteria": [{"id": "report", "expected": "Report contract holds", "method": "pytest",
                          "test_targets": ["test_report.py"]}]}
    parameter = getattr(request, "param", None)
    if parameter == "artifact" or isinstance(parameter, dict):
        plan["criteria"] = [{"id": "report", "expected": "Exact Result: 5 line",
                             "method": "artifact_sha256", "expected_sha256": {
                                 "notebooks/report.md": hashlib.sha256(b"Result: 5\n").hexdigest()}}]
    if isinstance(parameter, dict):
        if parameter["reconciliation"] is None:
            del plan["reconciliation"]
        else:
            plan["reconciliation"] = parameter["reconciliation"]
    if getattr(request, "param", None) == "preexisting":
        report.parent.mkdir(exist_ok=True)
        report.write_bytes(b"Original report\n")
    draft = anchor("workflow", "create", plan=plan, request_id="draft", output_format="dict")["state"]
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    anchor("new_session", name="workflow_implementation", inline=True, output_format="dict")
    anchor("task", "Produce exact report output", goal="Qualify report bytes", mode="planning",
           risk="low", rigor="direct", trust_domain="personal",
           in_scope=["notebooks/report.md"], constraints=["Managed artifacts only"],
           workflow_id=draft["workflow_id"], acceptance_criteria=["Report matches its contract"], output_format="dict")
    return anchor, home, target, draft


def advance(anchor, command, **kwargs):
    current = anchor("workflow", output_format="dict")["state"]
    return anchor("workflow", command, expected_generation=current["generation"],
                  request_id=f"{command}:{current['generation']}", output_format="dict", **kwargs)["state"]


def implement(runtime):
    anchor, home, _, _ = runtime
    advance(anchor, "accept_plan")
    artifact = home / "workspace/projects/alpha/notebooks/report.md"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("Result: 5\n")
    anchor("touched", str(artifact), output_format="dict")
    return advance(anchor, "implemented")


def finish_producer(anchor):
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    return anchor("learning", "assess", outcome="nothing_reusable_learned",
                  notes="Negative paths in this isolated regression fixture are deliberate.",
                  output_format="dict")["terminal_task_record"]


def test_public_lifecycle_requires_measurements_review_and_human_authority(runtime, monkeypatch):
    anchor, _, _, draft = runtime
    implemented = implement(runtime)
    assert implemented["progress"] == "implemented"
    with pytest.raises(RuntimeError, match="no retained review"):
        advance(anchor, "qualify")
    result = anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    assert result["metrics"]["passed"] == 1
    assert result["workflow_measurement"]["status"] == "satisfied"
    reviewed = advance(anchor, "review", findings=[])
    assert reviewed["review_result"]["kind"] == "self"
    assert reviewed["review_result"]["reviewer_authentication"] == "none"
    terminal = finish_producer(anchor)
    qualified = advance(anchor, "qualify")
    assert qualified["progress"] == "qualified" and qualified["completed"] is False
    assert qualified["qualification"]["producer_terminal_record_sha256"] == terminal["record_sha256"]
    prepared = anchor("workflow", "prepare_delivery", output_format="dict")
    assert prepared["authority_granted"] is False
    with pytest.raises(RuntimeError, match="retained approval"):
        advance(anchor, "verify_delivery")

    from odibi_anchor import human_input, human_input_owner

    provider = SimpleNamespace(expected_owner_id="test-owner", assurance="fixture",
                               transport=SimpleNamespace(name="fixture"))
    monkeypatch.setattr(human_input_owner, "select_owner_approval_provider", lambda: provider)
    monkeypatch.setattr(human_input, "request_human_input_record", lambda *a, **k: SimpleNamespace(
        response=prepared["approval_response"], response_user_id="test-owner", transport="fixture",
        request_id="fixture-request", response_message_id="fixture-response"))
    approved = advance(anchor, "request_delivery_approval")
    assert approved["progress"] == "approved_for_delivery"
    verified = advance(anchor, "verify_delivery")
    assert verified["progress"] == "delivery_verified" and verified["completed"] is True
    assert verified["workflow_id"] == draft["workflow_id"]
    assert verified["verification"]["observed"]["files"]["notebooks/report.md"]["size"] == 10


@pytest.mark.parametrize("forged", ["payload", "actor_kind", "candidate", "result", "receipt_ref", "session_state", "path"])
def test_public_workflow_rejects_forged_authority_arguments(runtime, forged):
    anchor, _, _, _ = runtime
    before = anchor("workflow", output_format="dict")["state"]
    with pytest.raises(TypeError):
        anchor("workflow", "implemented", expected_generation=0, request_id="forged",
               **{forged: {"status": "satisfied"}}, output_format="dict")
    assert anchor("workflow", output_format="dict")["state"] == before


def test_measurement_rejects_filtered_or_wrong_targets_before_running(runtime):
    anchor, _, _, _ = runtime
    implement(runtime)
    with pytest.raises(ValueError, match="without marker"):
        anchor("test", target=["test_report.py"], mark="fast", workflow_criterion="report", output_format="dict")
    with pytest.raises(RuntimeError, match="exactly match"):
        anchor("test", target=["other_test.py"], workflow_criterion="report", output_format="dict")
    assert anchor("workflow", output_format="dict")["state"]["measurements"] == {}


def test_stale_candidate_cannot_qualify(runtime):
    anchor, home, _, _ = runtime
    implement(runtime)
    anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    advance(anchor, "review", findings=[])
    (home / "workspace/projects/alpha/notebooks/report.md").write_text("different\n")
    with pytest.raises(RuntimeError, match="candidate changed"):
        advance(anchor, "qualify")


def test_status_markdown_is_rendered_and_help_discovers_action(runtime):
    anchor, _, _, _ = runtime
    assert "workflow" in str(anchor("help", output_format="dict"))
    help_text = str(anchor("help", "workflow", output_format="dict"))
    for contract in ("accept_plan", "expected_generation", "request_id", "test_targets",
                     "artifact_sha256", "source_paths", "human", "Common Workflows"):
        assert contract in help_text
    rendered = anchor("workflow", output_format="markdown")
    assert isinstance(rendered, str) and "workflow_packet" in rendered
    assert "draft" in rendered


def test_startup_guidance_does_not_offer_unbound_source_authority(runtime):
    from odibi_anchor._dispatcher._protocol import protocol_invocation
    from odibi_anchor._dispatcher._runtime_capabilities import collect_runtime_capabilities
    from odibi_anchor._utils._session_state import _SESSION_STATE

    anchor, _, _, _ = runtime
    invocation = protocol_invocation("task")
    assert 'mode="analysis"' in invocation
    assert 'mode="implementation"' not in invocation
    capability = collect_runtime_capabilities(_SESSION_STATE, None)["durable_workflow"]["value"]
    assert capability["enrollment"] == "explicit"
    assert capability["required_for_fresh_tasks"] == ["source_capable", "substantive_artifact"]
    assert capability["destination_mutation"] is False
    task_help = str(anchor("help", "task", output_format="dict"))
    assert "workflow_id" in task_help and "trust_domain" in task_help


def test_fresh_runtime_binds_explicit_trust_without_inheriting_old_task(runtime):
    _, _, target, draft = runtime
    from odibi_anchor.bootstrap import init

    restarted, _, _ = init(root=str(target), project="alpha", output_format="dict")
    restarted("orient", output_format="dict")
    restarted("new_session", name="resume_workflow", inline=True, output_format="dict")
    accepted = restarted("task", "Resume report workflow", goal="Qualify report bytes", mode="planning",
                         risk="low", rigor="direct", trust_domain="personal",
                         in_scope=["notebooks/report.md"], constraints=["Managed artifacts only"],
                         workflow_id=draft["workflow_id"], acceptance_criteria=["Report contract holds"],
                         output_format="dict")
    assert accepted["workflow"]["state"]["workflow_id"] == draft["workflow_id"]
    assert accepted["workflow"]["binding"]["task_window_id"] == accepted["task_window_id"]


def test_failed_binding_restores_runtime_and_does_not_accept_task(runtime):
    import sqlite3

    _, home, target, _ = runtime
    from odibi_anchor.bootstrap import init

    restarted, _, _ = init(root=str(target), project="alpha", output_format="dict")
    restarted("orient", output_format="dict")
    restarted("new_session", name="invalid_binding", inline=True, output_format="dict")
    from odibi_anchor._utils._session_state import _SESSION_STATE

    before = (_SESSION_STATE.task_window_id, _SESSION_STATE.trust_domain, _SESSION_STATE.active_task_profile)
    with sqlite3.connect(home / "memory.db") as connection:
        prior = connection.execute("SELECT count(*) FROM accepted_task_records").fetchone()[0]
    with pytest.raises(RuntimeError, match="workflow not found"):
        restarted("task", "Resume unknown workflow", goal="Check fail closed", mode="planning",
                  risk="low", rigor="direct", trust_domain="personal", workflow_id="wf_missing",
                  in_scope=["report"], constraints=["No target edits"], acceptance_criteria=["Reject missing owner"],
                  output_format="dict")
    assert (_SESSION_STATE.task_window_id, _SESSION_STATE.trust_domain, _SESSION_STATE.active_task_profile) == before
    with sqlite3.connect(home / "memory.db") as connection:
        assert connection.execute("SELECT count(*) FROM accepted_task_records").fetchone()[0] == prior


def test_failed_real_measurement_prevents_qualification(runtime):
    anchor, home, _, _ = runtime
    implement(runtime)
    (home / "workspace/projects/alpha/notebooks/report.md").write_text("Result: 6\n")
    advance(anchor, "implemented")
    result = anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    assert result["metrics"]["failed"] == 1
    assert result["workflow_measurement"]["status"] == "failed"
    advance(anchor, "review", findings=[])
    with pytest.raises(RuntimeError, match="completed gate-and-learning"):
        advance(anchor, "qualify")
    assert anchor("workflow", output_format="dict")["state"]["progress"] == "implemented"


def test_core_status_packet_has_stable_independently_computed_digest(runtime):
    import hashlib
    import json

    from odibi_anchor._dispatcher._workflow_runtime import workflow_action
    from odibi_anchor._utils._session_state import _SESSION_STATE

    _, home, _, _ = runtime
    packet = workflow_action(home / "memory.db", session_state=_SESSION_STATE)
    checksum = packet.pop("packet_sha256")
    assert checksum == "sha256:" + hashlib.sha256(json.dumps(
        packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()


def test_public_separate_review_task_binds_exact_candidate_without_principal_claim(runtime):
    anchor, _, _, draft = runtime
    implemented = implement(runtime)
    anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    anchor("new_session", name="separate_review", inline=True, output_format="dict")
    task = anchor("task", "Review exact report candidate", goal="Verify report output", mode="review",
                  risk="low", rigor="direct", trust_domain="personal", workflow_id=draft["workflow_id"],
                  in_scope=["notebooks/report.md"], constraints=["Read only"],
                  acceptance_criteria=["Review the exact implemented bytes"], output_format="dict")
    assert task["workflow"]["binding"]["review_candidate_sha256"]
    anchor("skill_loaded", "code-comprehension", output_format="dict")
    reviewed = advance(anchor, "review", findings=[])
    review = reviewed["review_result"]
    assert review["reviewer"] != implemented["candidate"]["producer"]
    assert review["separate_read_only_accepted_task"] is True
    assert review["reviewer_authentication"] == "none"
    assert review["evidence_kind"] == "agent_judgment"
    assert advance(anchor, "qualify")["qualification"]["producer_terminal_record_sha256"]


def test_public_measurement_uses_the_durability_checkpoint_hook(runtime, monkeypatch):
    from odibi_anchor._dispatcher import _post_dispatch

    anchor, _, _, _ = runtime
    implement(runtime)
    checkpoints = []
    monkeypatch.setattr(_post_dispatch, "_snapshot_durable_state",
                        lambda result, **kwargs: checkpoints.append(result["workflow_measurement"].copy()))
    result = anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    assert checkpoints == [result["workflow_measurement"]]
    assert checkpoints[0]["status"] == "satisfied"


@pytest.mark.parametrize("action", ["workflow", "test"])
def test_workflow_writes_checkpoint_before_rendering_markdown(runtime, action, monkeypatch):
    from odibi_anchor._dispatcher import _post_dispatch

    anchor, _, _, _ = runtime
    if action == "test":
        implement(runtime)
    before = anchor("workflow", output_format="dict")["state"]
    checkpoints = []
    monkeypatch.setattr(_post_dispatch, "_snapshot_durable_state",
                        lambda result, **kwargs: checkpoints.append(result.copy()))
    if action == "workflow":
        rendered = anchor("workflow", "accept_plan", expected_generation=before["generation"],
                          request_id="markdown", output_format="markdown")
    else:
        rendered = anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="markdown")
    assert isinstance(rendered, str)
    assert len(checkpoints) == 1
    after = anchor("workflow", output_format="dict")["state"]
    assert after["generation"] == before["generation"] + 1
    if action == "workflow":
        assert checkpoints[0]["state"]["progress"] == after["progress"] == "planned"
        assert '"progress": "planned"' in rendered
    else:
        assert checkpoints[0]["workflow_measurement"]["status"] == "satisfied"
        assert after["measurements"]["report"]["counts"]["passed"] == 1
        assert "PASS" in rendered


def test_exact_retry_returns_historical_receipt_without_recollecting(runtime, monkeypatch):
    from odibi_anchor._dispatcher import _workflow_evidence

    anchor, home, _, _ = runtime
    implemented = implement(runtime)
    original_generation = implemented["generation"] - 1
    (home / "workspace/projects/alpha/notebooks/report.md").write_text("Later bytes\n")
    monkeypatch.setattr(_workflow_evidence, "collect_candidate",
                        lambda *a, **k: pytest.fail("retry must not recollect candidate"))
    replay = anchor("workflow", "implemented", expected_generation=original_generation,
                    request_id=f"implemented:{original_generation}", output_format="dict")
    assert replay["state"] == implemented
    assert replay["replayed"] is True
    assert replay["observation_semantics"] == "historical acknowledgement"
    with pytest.raises(RuntimeError, match="conflicting public request"):
        anchor("workflow", "implemented", expected_generation=original_generation,
               request_id=f"implemented:{original_generation}", reason="changed request", output_format="dict")


@pytest.fixture
def approved_runtime(runtime, monkeypatch):
    from odibi_anchor import human_input, human_input_owner

    anchor, _, _, _ = runtime
    implemented = implement(runtime)
    if implemented["plan"]["criteria"][0]["method"] == "artifact_sha256":
        advance(anchor, "check_artifact", criterion_id="report")
    else:
        anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    advance(anchor, "review", findings=[])
    finish_producer(anchor)
    advance(anchor, "qualify")
    prepared = anchor("workflow", "prepare_delivery", output_format="dict")
    provider = SimpleNamespace(expected_owner_id="fixture-owner", assurance="fixture",
                               transport=SimpleNamespace(name="fixture"))
    monkeypatch.setattr(human_input_owner, "select_owner_approval_provider", lambda: provider)
    monkeypatch.setattr(human_input, "request_human_input_record", lambda *a, **k: SimpleNamespace(
        response=prepared["approval_response"], response_user_id="fixture-owner", transport="fixture",
        request_id="fixture-request", response_message_id="fixture-response"))
    advance(anchor, "request_delivery_approval")
    return runtime


@pytest.mark.parametrize("runtime", [None, "artifact"], indirect=True)
def test_delivery_environment_freshness_applies_only_to_pytest(approved_runtime, monkeypatch):
    from odibi_anchor._dispatcher import _workflow_delivery

    anchor, _, _, _ = approved_runtime
    approved = anchor("workflow", output_format="dict")["state"]
    method = approved["plan"]["criteria"][0]["method"]
    monkeypatch.setattr(_workflow_delivery, "runtime_environment", lambda: {"python": "different-host"})
    if method == "pytest":
        with pytest.raises(RuntimeError, match="environment changed before delivery"):
            advance(anchor, "verify_delivery")
        assert anchor("workflow", output_format="dict")["state"] == approved
    else:
        assert approved["qualification"]["checks"][0]["collector"] == "anchor.managed_artifact_sha256"
        assert "environment_sha256" not in approved["qualification"]["checks"][0]
        verified = advance(anchor, "verify_delivery")
        assert verified["completed"] is True and verified["progress"] == "delivery_verified"
        assert verified["verification"]["observed"]["files"]["notebooks/report.md"]["size"] == 10


def test_approval_retry_does_not_prompt_again(approved_runtime, monkeypatch):
    from odibi_anchor import human_input

    anchor, _, _, _ = approved_runtime
    approved = anchor("workflow", output_format="dict")["state"]
    generation = approved["generation"] - 1
    monkeypatch.setattr(human_input, "request_human_input_record",
                        lambda *a, **k: pytest.fail("must not ask human again"))
    replay = anchor("workflow", "request_delivery_approval", expected_generation=generation,
                    request_id=f"request_delivery_approval:{generation}", output_format="dict")
    assert replay["replayed"] is True and replay["state"] == approved


def test_interrupted_readback_resumes_exact_request(approved_runtime, monkeypatch):
    from odibi_anchor._dispatcher import _workflow_runtime

    anchor, _, _, _ = approved_runtime
    generation = anchor("workflow", output_format="dict")["state"]["generation"]
    transition = _workflow_runtime.transition_workflow

    def interrupted(*args, **kwargs):
        if kwargs["operation"] == "verify_delivery":
            raise RuntimeError("injected interruption after reconciliation")
        return transition(*args, **kwargs)

    monkeypatch.setattr(_workflow_runtime, "transition_workflow", interrupted)
    with pytest.raises(RuntimeError, match="injected interruption"):
        anchor("workflow", "verify_delivery", expected_generation=generation,
               request_id="readback", output_format="dict")
    partial = anchor("workflow", output_format="dict")["state"]
    assert partial["progress"] == "delivered" and partial["completed"] is False
    monkeypatch.setattr(_workflow_runtime, "transition_workflow", transition)
    verified = anchor("workflow", "verify_delivery", expected_generation=generation,
                      request_id="readback", output_format="dict")
    assert verified["state"]["progress"] == "delivery_verified"
    replay = anchor("workflow", "verify_delivery", expected_generation=generation,
                    request_id="readback", output_format="dict")
    assert replay["state"] == verified["state"] and replay["replayed"] is True


def test_blocked_unknown_delivery_can_be_reconciled(approved_runtime):
    anchor, _, _, _ = approved_runtime
    blocked = advance(anchor, "block", reason="Receipt unavailable", blocker_kind="outcome_unknown")
    assert blocked["status"] == "blocked"
    verified = advance(anchor, "verify_delivery")
    assert verified["status"] == "completed" and verified["progress"] == "delivery_verified"
    assert verified["recovery"]["outcome"] == "delivered"


def test_replan_retry_acknowledges_receipt_without_reauthorizing_old_task(runtime):
    anchor, _, _, _ = runtime
    state = implement(runtime)
    plan = {**state["plan"], "goal": "Revised report contract"}
    kwargs = {"expected_generation": state["generation"], "request_id": "revised-plan",
              "plan": plan, "reason": "Owner changed outcome", "output_format": "dict"}
    result = anchor("workflow", "replan", **kwargs)
    assert result["state"]["progress"] == "draft"
    replay = anchor("workflow", "replan", **kwargs)
    assert replay["replayed"] is True and replay["state"] == result["state"]
    with pytest.raises(RuntimeError, match=r"plan changed|Plan changed"):
        anchor("workflow", "accept_plan", expected_generation=result["state"]["generation"],
               request_id="cannot-use-old-task", output_format="dict")


def test_replanned_context_and_handoff_explain_recovery_without_upgrading_binding(runtime):
    import json
    from pathlib import Path

    anchor, _, _, _ = runtime
    original = anchor("context", output_format="dict")["workflow"]["binding"]
    state = implement(runtime)
    revised = advance(anchor, "replan", plan={**state["plan"], "goal": "Revised report"}, reason="new fact")
    packet = anchor("context", output_format="dict")["workflow"]
    assert packet["binding"] == original
    assert packet["binding_status"] == "stale_plan"
    assert packet["status"] == "recovery_required"
    assert packet["next_step"] == "close_task_then_reenroll"
    assert packet["state"] == revised
    assert packet["completed"] is False and packet["delivery_verified"] is False
    assert packet["next_operation"]["args"] == ["status"]
    assert packet["recovery"]["workflow_id"] == revised["workflow_id"]
    assert packet["recovery"]["rebind_upgrades_plan"] is False
    handoff = anchor("snapshot", mode="handoff", output_format="dict")
    assert handoff["workflow"] == packet
    assert json.loads(Path(handoff["artifact_path"]).read_text())["workflow"] == packet
    with pytest.raises(RuntimeError, match=r"plan changed|Plan changed"):
        anchor("workflow", "implemented", expected_generation=revised["generation"],
               request_id="old-task", output_format="dict")


@pytest.mark.parametrize("change", ["missing", "different"])
def test_negative_readback_never_proves_unknown_delivery_terminated(approved_runtime, change):
    anchor, home, _, _ = approved_runtime
    blocked = advance(anchor, "block", reason="Lost remote response", blocker_kind="outcome_unknown")
    artifact = home / "workspace/projects/alpha/notebooks/report.md"
    if change == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"Other result\n")
    with pytest.raises(RuntimeError):
        advance(anchor, "verify_delivery")
    retained = anchor("workflow", output_format="dict")["state"]
    assert retained == blocked
    assert retained["completed"] is False
    assert retained["blocker"]["kind"] == "outcome_unknown"


def test_context_exposes_exact_workflow_separately_from_task_completion(runtime):
    anchor, _, _, draft = runtime
    context = anchor("context", output_format="dict")
    assert context["workflow"]["state"]["workflow_id"] == draft["workflow_id"]
    assert context["workflow"]["state"]["phase"] == "plan"
    assert context["workflow"]["next_operation"]["kwargs"]["expected_generation"] == 0
    assert context["task_window_completion_is_workflow_completion"] is False
    implement(runtime)
    context = anchor("context", output_format="dict")
    assert context["workflow"]["pending_criteria"] == ["report"]
    assert context["workflow"]["review_required"] is True
    assert "`implemented`" in anchor("context", output_format="markdown")
    assert anchor._agent_context_snapshot()["workflow"] == context["workflow"]


def test_canonical_workflow_handoff_retains_packet_and_rejects_drift(runtime):
    import json
    from pathlib import Path

    from odibi_anchor._dispatcher._canonical_handoff import validate_canonical_handoff
    from odibi_anchor._utils._session_state import _SESSION_STATE

    anchor, home, _, draft = runtime
    packet = anchor("snapshot", mode="handoff", summary="Resume exact report", output_format="dict")
    assert packet["schema_version"] == "3.0"
    assert packet["workflow"]["state"]["workflow_id"] == draft["workflow_id"]
    assert packet["workflow"]["state"]["plan"] == draft["plan"]
    assert packet["first_action"]["action"] == "task_rebind"
    assert "source_change" not in packet["first_action"]["copy_ready"]
    retained = json.loads(Path(packet["artifact_path"]).read_text())
    assert retained["workflow"] == packet["workflow"]
    def check():
        return validate_canonical_handoff(
            packet, _SESSION_STATE, anchor._route_binding, memory_db=str(home / "memory.db"),
        )
    assert check()["status"] == "verified"
    advance(anchor, "accept_plan")
    stale = check()
    assert stale["status"] == "stale" and "workflow" in stale["conflicts"]
    assert stale["first_action"] is None


@pytest.mark.parametrize("action", ["context", "snapshot"])
def test_caller_cannot_redirect_context_authority_database(runtime, action):
    anchor, _, target, _ = runtime
    kwargs = {"mode": "handoff"} if action == "snapshot" else {}
    with pytest.raises(TypeError, match="memory_db"):
        anchor(action, memory_db=str(target / "forged.db"), output_format="dict", **kwargs)
    assert not (target / "forged.db").exists()


@pytest.mark.parametrize("runtime", ["artifact"], indirect=True)
@pytest.mark.parametrize("content", [b"Result: 5\n", b"Result: 6\n"])
def test_artifact_byte_check_qualifies_only_planned_content_without_pytest(runtime, monkeypatch, content):
    from odibi_anchor._dispatcher import _workflow_evidence

    anchor, home, _, _ = runtime
    implement(runtime)
    (home / "workspace/projects/alpha/notebooks/report.md").write_bytes(content)
    advance(anchor, "implemented")
    checked = advance(anchor, "check_artifact", criterion_id="report")
    measurement = checked["measurements"]["report"]
    assert measurement["status"] == ("satisfied" if content == b"Result: 5\n" else "failed")
    assert measurement["collector"] == "anchor.managed_artifact_sha256"
    assert measurement["result"]["expected_sha256"]["notebooks/report.md"] == hashlib.sha256(b"Result: 5\n").hexdigest()
    assert "environment" not in measurement
    advance(anchor, "review", findings=[])
    finish_producer(anchor)
    monkeypatch.setattr(_workflow_evidence, "runtime_environment", lambda: {"python": "different-host"})
    if content == b"Result: 5\n":
        assert advance(anchor, "qualify")["progress"] == "qualified"
    else:
        with pytest.raises(RuntimeError, match="not satisfied"):
            advance(anchor, "qualify")


@pytest.mark.parametrize("runtime", ["artifact"], indirect=True)
def test_artifact_check_rejects_changed_candidate_and_does_not_record_pass(runtime):
    anchor, home, _, _ = runtime
    implemented = implement(runtime)
    (home / "workspace/projects/alpha/notebooks/report.md").write_text("changed\n")
    with pytest.raises(RuntimeError, match="changed before measurement"):
        advance(anchor, "check_artifact", criterion_id="report")
    current = anchor("workflow", output_format="dict")["state"]
    assert current["generation"] == implemented["generation"] and current["measurements"] == {}


@pytest.mark.parametrize("runtime", ["artifact"], indirect=True)
def test_artifact_check_retry_is_historical_and_criterion_is_part_of_request(runtime):
    anchor, home, _, _ = runtime
    implemented = implement(runtime)
    kwargs = {"expected_generation": implemented["generation"], "request_id": "measure",
              "criterion_id": "report", "output_format": "dict"}
    first = anchor("workflow", "check_artifact", **kwargs)
    (home / "workspace/projects/alpha/notebooks/report.md").write_text("changed\n")
    repeated = anchor("workflow", "check_artifact", **kwargs)
    assert repeated["state"] == first["state"] and repeated["replayed"] is True
    assert repeated["observation_semantics"] == "historical acknowledgement"
    with pytest.raises(RuntimeError, match="request_id reused"):
        anchor("workflow", "check_artifact", **{**kwargs, "criterion_id": "different"})


def test_qualification_cannot_bypass_producer_gate_and_learning(runtime):
    anchor, _, _, _ = runtime
    implement(runtime)
    anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    before = advance(anchor, "review", findings=[])
    with pytest.raises(RuntimeError, match="completed gate-and-learning"):
        advance(anchor, "qualify")
    assert anchor("workflow", output_format="dict")["state"] == before


def test_checkpoint_closure_is_usable_without_inventing_another_gate(runtime):
    anchor, _, _, _ = runtime
    implement(runtime)
    anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    advance(anchor, "review", findings=[])
    anchor("review", output_format="dict")
    checkpoint = anchor("checkpoint", label="report_candidate", test_target="test_report.py",
                        generate_pr_draft=False,
                        learning_assessment={"outcome": "nothing_reusable_learned"},
                        output_format="dict")
    assert checkpoint["metrics"]["failed_at"] is None
    assert checkpoint["metrics"]["overall_pass"] is True
    qualified = advance(anchor, "qualify")
    assert qualified["progress"] == "qualified" and qualified["completed"] is False
    assert len(qualified["qualification"]["producer_terminal_record_sha256"]) == 64


@pytest.mark.parametrize("assess", [False, True])
def test_candidate_cannot_be_created_after_gate_or_closure_but_receipt_replays(runtime, assess):
    anchor, _, _, _ = runtime
    implemented = implement(runtime)
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    if assess:
        anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    with pytest.raises(RuntimeError, match="gated or closed"):
        advance(anchor, "implemented")
    generation = implemented["generation"] - 1
    replay = anchor("workflow", "implemented", expected_generation=generation,
                    request_id=f"implemented:{generation}", output_format="dict")
    assert replay["replayed"] and replay["state"] == implemented


def test_out_of_band_source_drift_blocks_artifact_gate_and_workflow_qualification(runtime):
    anchor, _, target, _ = runtime
    implement(runtime)
    anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    advance(anchor, "review", findings=[])
    (target / "unapproved.py").write_text("VALUE = 99\n")
    with pytest.raises(RuntimeError, match="Non-artifact files"):
        anchor("gate", output_format="dict")
    with pytest.raises(RuntimeError, match="completed gate-and-learning"):
        advance(anchor, "qualify")


@pytest.fixture
def source_runtime(tmp_path, monkeypatch, request):
    import subprocess

    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init
    from tests._dispatcher.test_databricks_repository_integration import SimulatedDatabricksProvider

    kind = getattr(request, "param", "git")
    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    target.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=target, check=True, capture_output=True,
                              text=True).stdout.strip()

    if kind != "unborn":
        (target / "source.py").write_text("VALUE = 1\n")
        (target / "test_source.py").write_text("import source\ndef test_value():\n    assert source.VALUE == 2\n")
    if kind != "databricks":
        git("init", "-b", "main")
        if kind != "unborn":
            git("add", ".")
            git("commit", "-m", "base")
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    project_action(home, "create", name="alpha", target=target, output_format="dict")
    provider = SimulatedDatabricksProvider() if kind == "databricks" else None
    anchor, _, _ = init(root=str(target), project="alpha", repository_provider=provider, output_format="dict")
    anchor("orient", output_format="dict")
    anchor("new_session", name="plan_source", inline=True, output_format="dict")
    anchor("task", "Plan constant correction", goal="Make VALUE equal 2", mode="analysis",
           risk="low", rigor="direct", trust_domain="personal", in_scope=["source.py"],
           constraints=["Do not edit source before plan acceptance"],
           acceptance_criteria=["A bounded plan exists"], output_format="dict")
    anchor("skill_loaded", "code-comprehension", output_format="dict")
    plan = {"schema_version": 1, "goal": "Make VALUE equal 2", "risk": "low",
            "execution_mode": "source_change", "scope": ["source.py"], "source_paths": ["source.py"],
            "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
            "unresolved_decisions": [], "destination": {"kind": "github_ref", "repository": "acme/app", "ref": "refs/heads/main"},
            "reconciliation": {"requirements": [], "reason": "Disposable source fixture has no linked obligations"},
            "criteria": [{"id": "constant", "expected": "VALUE equals 2", "method": "pytest",
                          "test_targets": ["test_source.py"]}]}
    draft = anchor("workflow", "create", plan=plan, request_id="draft", output_format="dict")["state"]
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    anchor("new_session", name="produce_source", inline=True, output_format="dict")
    kwargs = {"repository_scope": ["source.py"], "accept_unknown_git_state": True} if provider else {}
    anchor("task", "Implement constant correction", goal="Make VALUE equal 2", mode="implementation",
           risk="low", rigor="direct", trust_domain="personal", in_scope=["source.py"],
           constraints=["Only source.py may change"], acceptance_criteria=["VALUE equals 2"],
           workflow_id=draft["workflow_id"], output_format="dict", **kwargs)
    return anchor, target, git


def test_public_git_candidate_requires_qualification_approval_and_exact_remote_readback(source_runtime, monkeypatch):
    from odibi_anchor import human_input, human_input_owner
    from odibi_anchor._dispatcher import _workflow_delivery

    anchor, target, git = source_runtime
    git("remote", "add", "origin", "https://github.com/acme/app.git")
    advance(anchor, "accept_plan")
    anchor("known_bad", changed_files=["source.py"], output_format="dict")
    (target / "source.py").write_text("VALUE = 2\n")
    anchor("touched", "source.py", output_format="dict")
    git("add", "source.py")
    git("commit", "-m", "qualified fixture candidate")
    head = git("rev-parse", "HEAD")
    implemented = advance(anchor, "implemented")
    assert implemented["progress"] == "implemented" and not implemented["completed"]
    result = anchor("test", target=["test_source.py"], workflow_criterion="constant", output_format="dict")
    assert result["metrics"]["passed"] == 1 and result["workflow_measurement"]["status"] == "satisfied"
    advance(anchor, "review", findings=[])
    anchor("preflight", output_format="dict")
    terminal = finish_producer(anchor)
    qualified = advance(anchor, "qualify")
    assert qualified["qualification"]["producer_terminal_record_sha256"] == terminal["record_sha256"]
    assert qualified["progress"] == "qualified" and not qualified["completed"]
    prepared = anchor("workflow", "prepare_delivery", output_format="dict")
    assert prepared["authority_granted"] is False
    with pytest.raises(RuntimeError, match="retained approval"):
        advance(anchor, "verify_delivery")
    provider = SimpleNamespace(expected_owner_id="fixture-owner", assurance="fixture",
                               transport=SimpleNamespace(name="fixture"))
    monkeypatch.setattr(human_input_owner, "select_owner_approval_provider", lambda: provider)
    monkeypatch.setattr(human_input, "request_human_input_record", lambda *a, **k: SimpleNamespace(
        response=prepared["approval_response"], response_user_id="fixture-owner", transport="fixture",
        request_id="fixture-approval", response_message_id="fixture-response"))
    approved = advance(anchor, "request_delivery_approval")
    assert approved["progress"] == "approved_for_delivery" and not approved["completed"]
    reads = []
    observed_head = "0" * 40

    def read_json(service, path):
        reads.append((service, path))
        return {"sha": observed_head}

    monkeypatch.setattr(_workflow_delivery, "_get_json", read_json)
    with pytest.raises(RuntimeError, match="differs from qualified head"):
        advance(anchor, "verify_delivery")
    assert anchor("workflow", output_format="dict")["state"] == approved
    observed_head = head
    verified = advance(anchor, "verify_delivery")
    assert verified["progress"] == "delivery_verified" and verified["completed"] is True
    assert verified["verification"]["observed"] == {
        "repository": "acme/app", "ref": "refs/heads/main", "sha": head}
    assert reads == [("github", "/repos/acme/app/commits/refs%2Fheads%2Fmain")] * 2
    assert git("status", "--porcelain") == ""


@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked", "committed", "empty_commit"])
def test_source_changed_before_plan_acceptance_cannot_be_laundered(source_runtime, change):
    anchor, target, git = source_runtime
    before = anchor("workflow", output_format="dict")["state"]
    if change != "empty_commit":
        (target / ("extra.py" if change == "untracked" else "source.py")).write_text("VALUE = 2\n")
    if change in {"staged", "committed"}:
        git("add", "source.py")
    if change in {"committed", "empty_commit"}:
        git("commit", "--allow-empty", "-m", "premature change")
    with pytest.raises(RuntimeError, match="before plan acceptance"):
        advance(anchor, "accept_plan")
    assert anchor("workflow", output_format="dict")["state"] == before


@pytest.mark.parametrize("source_runtime", ["git", "unborn", "databricks"], indirect=True)
def test_unchanged_source_can_enter_plan_and_exact_retry_is_historical(source_runtime):
    anchor, target, _ = source_runtime
    planned = advance(anchor, "accept_plan")
    assert planned["progress"] == "planned"
    assert planned["admission"]["baseline"]["source_observation"]["basis"] == "unchanged_task_source"
    (target / "source.py").write_text("VALUE = 2\n")
    repeated = anchor("workflow", "accept_plan", expected_generation=0,
                      request_id="accept_plan:0", output_format="dict")
    assert repeated["replayed"] is True and repeated["state"] == planned


@pytest.mark.parametrize("source_runtime", ["unborn", "databricks"], indirect=True)
def test_recording_a_premature_source_edit_does_not_grant_plan_admission(source_runtime):
    anchor, target, _ = source_runtime
    before = anchor("workflow", output_format="dict")["state"]
    anchor("known_bad", changed_files=["source.py"], output_format="dict")
    (target / "source.py").write_text("VALUE = 2\n")
    anchor("touched", "source.py", output_format="dict")
    with pytest.raises(RuntimeError, match="before plan acceptance"):
        advance(anchor, "accept_plan")
    assert anchor("workflow", output_format="dict")["state"] == before


def test_closed_producer_cannot_accept_new_plan(runtime):
    anchor, _, _, _ = runtime
    before = anchor("workflow", output_format="dict")["state"]
    finish_producer(anchor)
    with pytest.raises(RuntimeError, match="gated or closed"):
        advance(anchor, "accept_plan")
    assert anchor("workflow", output_format="dict")["state"] == before


@pytest.mark.parametrize("change", [None, "candidate", "wrong_owner", "generation", "unknown", "completed"])
def test_public_exact_owner_revocation_and_historical_retry(runtime, monkeypatch, change):
    from odibi_anchor import human_input, human_input_owner

    anchor, home, _, _ = runtime
    implement(runtime)
    anchor("test", target=["test_report.py"], workflow_criterion="report", output_format="dict")
    advance(anchor, "review", findings=[])
    finish_producer(anchor)
    advance(anchor, "qualify")
    prepared = anchor("workflow", "prepare_delivery", output_format="dict")
    provider = SimpleNamespace(expected_owner_id="test-owner", assurance="fixture",
                               transport=SimpleNamespace(name="fixture"))
    monkeypatch.setattr(human_input_owner, "select_owner_approval_provider", lambda: provider)
    monkeypatch.setattr(human_input, "request_human_input_record", lambda *a, **k: SimpleNamespace(
        response=prepared["approval_response"], response_user_id="test-owner", transport="fixture",
        request_id="approval-request", response_message_id="approval-response"))
    approved = advance(anchor, "request_delivery_approval")
    if change == "candidate":
        (home / "workspace/projects/alpha/notebooks/report.md").write_text("Changed after approval\n")
    if change == "unknown":
        advance(anchor, "block", reason="Host outcome uncertain", blocker_kind="outcome_unknown")
    if change == "completed":
        advance(anchor, "verify_delivery")
    if change in {"unknown", "completed"}:
        with pytest.raises(RuntimeError, match=r"unknown delivery|unused delivery approval"):
            anchor("workflow", "prepare_revocation", output_format="dict")
        return
    revocation = anchor("workflow", "prepare_revocation", output_format="dict")
    assert revocation["authority_granted"] is False
    assert revocation["subject"]["destination"] == {"kind": "managed_artifacts"}
    assert revocation["subject"]["generation"] == approved["generation"]
    calls = []

    def reply(*args, **kwargs):
        calls.append(args)
        if change == "generation":
            advance(anchor, "block", reason="New blocker while waiting", blocker_kind="unavailable")
        return SimpleNamespace(response=revocation["approval_response"],
                               response_user_id="other" if change == "wrong_owner" else "test-owner",
                               transport="fixture", request_id="revoke-request", response_message_id="revoke-response")

    monkeypatch.setattr(human_input, "request_human_input_record", reply)
    kwargs = {"expected_generation": approved["generation"], "request_id": "revoke-once", "output_format": "dict"}
    if change in {"wrong_owner", "generation"}:
        with pytest.raises(RuntimeError, match=r"exact challenge and owner|changed during"):
            anchor("workflow", "revoke_delivery", **kwargs)
        assert anchor("workflow", output_format="dict")["state"]["approval"] == approved["approval"]
    else:
        revoked = anchor("workflow", "revoke_delivery", **kwargs)
        assert revoked["state"]["approval"] is None
        assert revoked["state"]["progress"] == "qualified"
        assert revoked["state"]["completed"] is False
        repeated = anchor("workflow", "revoke_delivery", **kwargs)
        assert repeated["replayed"] is True and repeated["state"] == revoked["state"]
        assert len(calls) == 1
        if change == "candidate":
            with pytest.raises(RuntimeError, match="changed after qualification"):
                anchor("workflow", "prepare_delivery", output_format="dict")


def test_public_data_only_exception_stays_unphased_through_task_closure(runtime):
    anchor, _, _, _ = runtime
    finish_producer(anchor)
    anchor("new_session", name="legacy_data_only", inline=True, output_format="dict")
    accepted = anchor("task", "Update bounded test data", goal="Retain existing data-only policy",
                      mode="implementation", execution_mode="data_change", risk="low", rigor="direct", trust_domain="personal",
                      in_scope=["scratch fixture data"], constraints=["No source edits"],
                      acceptance_criteria=["Data-only compatibility stays explicit"], output_format="dict")
    packet = accepted["workflow"]
    assert packet["status"] == "unphased_unsupported_collector"
    assert packet["completed"] is False and packet["delivery_verified"] is False
    for skill in ("data-operations", "writing-specs"):
        anchor("skill_loaded", skill, output_format="dict")
    for command in ("implemented", "qualify", "verify_delivery"):
        with pytest.raises(RuntimeError, match="exact accepted task workflow binding"):
            anchor("workflow", command, expected_generation=0, request_id=command, output_format="dict")
    finish_producer(anchor)
    from odibi_anchor.codebase._workflow import digest

    assert anchor("workflow", output_format="dict") == {**packet, "packet_sha256": digest(packet)}


def test_fresh_standalone_source_rejection_preserves_authority_and_files(tmp_path, monkeypatch):
    import sqlite3
    import subprocess

    from odibi_anchor.bootstrap import init

    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    target.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(target)], check=True, capture_output=True)
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    monkeypatch.delenv("ANCHOR_TRUST_DOMAIN", raising=False)
    anchor, _, _ = init(root=str(target), output_format="dict")
    anchor("orient", output_format="dict")
    anchor("new_session", name="standalone", inline=True, output_format="dict")
    from odibi_anchor._utils._session_state import _SESSION_STATE

    prior_window = _SESSION_STATE.task_window_id
    with pytest.raises(RuntimeError, match="Fresh source tasks require") as caught:
        anchor("task", "Change one source file", goal="Test fresh source admission", mode="implementation",
               risk="low", rigor="direct", acceptance_criteria=["An explicit workflow owns the change"],
               output_format="dict")
    assert caught.value.context["missing_authority"] == ["project", "trust_domain", "workflow_id"]
    assert _SESSION_STATE.task_window_id == prior_window
    assert _SESSION_STATE.workflow_binding is None
    assert _SESSION_STATE.active_task_profile is None
    with sqlite3.connect(home / "memory.db") as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accepted_task_records" in tables:
            assert connection.execute("SELECT count(*) FROM accepted_task_records").fetchone()[0] == 0
    assert sorted(p.name for p in target.iterdir()) == [".git"]


@pytest.mark.parametrize("runtime,mutation", [
    (None, "write"), ("preexisting", "write"), ("preexisting", "delete"),
    ("preexisting", "restore"), (None, "symlink"), ("preexisting", "symlink"),
], indirect=["runtime"])
def test_draft_artifact_mutation_cannot_be_laundered_at_admission(runtime, mutation):
    anchor, home, _, _ = runtime
    output = home / "workspace/projects/alpha/notebooks/report.md"
    output.parent.mkdir(exist_ok=True)
    old = output.read_bytes() if output.exists() else None
    if mutation == "symlink":
        output.unlink(missing_ok=True)
        output.symlink_to(home / "other.md")
    elif mutation == "delete":
        output.unlink()
    else:
        output.write_bytes(b"Result: 5\n")
        if mutation == "restore" and old is not None:
            output.write_bytes(old)
    with pytest.raises(RuntimeError, match="pre-plan artifact"):
        advance(anchor, "accept_plan")
    assert anchor("workflow", output_format="dict")["state"]["progress"] == "draft"


def test_draft_output_registration_requires_plan_but_notes_remain_writable(runtime):
    anchor, home, _, _ = runtime
    directory = home / "workspace/projects/alpha/notebooks"
    directory.mkdir(exist_ok=True)
    note = directory / "plan-notes.md"
    note.write_text("Planning evidence, not the report output.\n")
    assert anchor("touched", str(note), output_format="dict")
    with pytest.raises(RuntimeError, match="accepted active plan"):
        anchor("touched", str(directory / "report.md"), output_format="dict")
    assert advance(anchor, "accept_plan")["progress"] == "planned"


def test_draft_creation_replay_does_not_refresh_artifact_baseline(runtime):
    anchor, home, _, draft = runtime
    output = home / "workspace/projects/alpha/notebooks/report.md"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"Result: 5\n")
    replay = anchor("workflow", "create", plan=draft["plan"], request_id="draft", output_format="dict")
    assert replay["state"] == draft
    with pytest.raises(RuntimeError, match="pre-plan artifact"):
        advance(anchor, "accept_plan")


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
def test_unchanged_preexisting_artifact_has_explicit_admission_baseline(runtime):
    anchor, _, _, _ = runtime
    admitted = advance(anchor, "accept_plan")
    baseline = admitted["admission"]["baseline"].get("artifact_observation")
    assert baseline is not None
    assert baseline["files"]["notebooks/report.md"]["sha256"] == hashlib.sha256(b"Original report\n").hexdigest()


@pytest.mark.parametrize("runtime,expected", [
    ({"reconciliation": None}, "unavailable"),
    ({"reconciliation": {"requirements": [], "reason": ""}}, "unavailable"),
    ({"reconciliation": {"requirements": [{"id": "ticket", "method": "github_issue"}]}}, "unavailable"),
    ({"reconciliation": {"requirements": [{"id": "docs", "method": "qualification_criterion",
                                          "criterion_id": "missing"}]}}, "unsatisfied"),
    ({"reconciliation": {"requirements": [{"id": "docs", "method": "qualification_criterion",
                                          "criterion_id": "report"},
                                         {"id": "learning", "method": "producer_learning"}]}}, "satisfied"),
], indirect=["runtime"])
def test_destination_bytes_do_not_pay_unrelated_reconciliation(approved_runtime, expected):
    anchor, _, _, _ = approved_runtime
    if expected != "satisfied":
        with pytest.raises(RuntimeError, match="reconciliation"):
            advance(anchor, "verify_delivery")
        state = anchor("workflow", output_format="dict")["state"]
        assert state["progress"] == "delivered" and state["completed"] is False
        proof = state["delivery"]["reconciliation"]
    else:
        state = advance(anchor, "verify_delivery")
        assert state["progress"] == "delivery_verified" and state["completed"] is True
        proof = state["verification"]["reconciliation"]
    assert proof["status"] == expected
    assert proof["plan_sha256"] == state["plan_sha256"]
    assert proof["candidate_sha256"] == state["qualification"]["candidate_sha256"]


def test_draft_replan_cannot_adopt_unadmitted_output(runtime):
    anchor, home, _, draft = runtime
    output = home / "workspace/projects/alpha/notebooks/report.md"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"Result: 5\n")
    with pytest.raises(RuntimeError, match="pre-plan artifact"):
        advance(anchor, "replan", plan={**draft["plan"], "goal": "Launder changed bytes"}, reason="Retry")


@pytest.mark.parametrize("later_change", [None, "after_gate", "after_close", "current_task"])
def test_warm_artifact_task_does_not_inherit_closed_source_drift(source_runtime, later_change):
    from pathlib import Path

    from odibi_anchor._utils._session_state import _SESSION_STATE, check_filesystem_drift

    anchor, target, git = source_runtime
    check_filesystem_drift(str(target))
    advance(anchor, "accept_plan")
    anchor("known_bad", changed_files=["source.py"], output_format="dict")
    (target / "source.py").write_text("VALUE = 2\n")
    anchor("touched", "source.py", output_format="dict")
    git("add", "source.py")
    git("commit", "-m", "Completed fixture source task")
    anchor("test", target=["test_source.py"], output_format="dict")
    anchor("preflight", output_format="dict")
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    if later_change == "after_gate":
        (target / "source.py").write_text("VALUE = 3\n")
        git("add", "source.py")
        git("commit", "-m", "Out-of-band mutation after gate")
    terminal = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    assert terminal["terminal_task_record"]["record"]["terminal"]["status"] == "completed"
    if later_change == "after_close":
        (target / "source.py").write_text("VALUE = 4\n")
        git("add", "source.py")
        git("commit", "-m", "Out-of-band mutation between tasks")
    artifact_root = Path(_SESSION_STATE.artifact_root)
    anchor("new_session", name="markdown_only", inline=True, output_format="dict")
    anchor("task", "Retain Markdown handoff", goal="Archive observed candidate", mode="documentation",
           risk="low", rigor="direct", trust_domain="personal",
           acceptance_criteria=["No source edits are included"], output_format="dict")
    anchor("skill_loaded", "documentation", output_format="dict")
    artifact = artifact_root / "notebooks/handoff.md"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text("Retained fixture candidate.\n")
    anchor("touched", str(artifact), output_format="dict")
    if later_change == "current_task":
        (target / "source.py").write_text("VALUE = 5\n")
    anchor("review", output_format="dict")
    if later_change:
        with pytest.raises(RuntimeError, match=r"Non-artifact|Documentation mode"):
            anchor("gate", output_format="dict")
    else:
        result = anchor("gate", output_format="dict")
        assert result["metrics"]["must_unpaid"] == 0
        assert result["metrics"].get("auto_touched_count", 0) == 0
        assert check_filesystem_drift(str(target))["modified"] == []


def test_data_only_exception_survives_context_canonical_handoff_and_exact_rebind(runtime, monkeypatch):
    import json
    from pathlib import Path

    from odibi_anchor.bootstrap import init
    from odibi_anchor.planning import task_execution_context

    anchor, _, target, _ = runtime
    spec_input = task_execution_context(
        "Retain data-only compatibility status", goal="Preserve unsupported data authority", mode="implementation",
        execution_mode="data_change", risk="low", rigor="direct",
        acceptance_criteria=["No completion or source authority is inferred"], output_format="dict",
    )
    spec = anchor("spec", "persist", spec_input, output_format="dict")
    spec_path = Path(spec["path"])
    spec_path.write_text(spec_path.read_text().replace("status: draft", "status: ready"))
    finish_producer(anchor)
    anchor("new_session", name="data_only_projection", inline=True, output_format="dict")
    task = anchor("task", "Retain data-only compatibility", goal="Preserve unsupported data authority",
                  mode="implementation", execution_mode="data_change", risk="low", rigor="direct",
                  spec=spec["name"], trust_domain="personal",
                  acceptance_criteria=["No completion or source authority is inferred"], output_format="dict")
    for skill in ("data-operations", "writing-specs"):
        anchor("skill_loaded", skill, output_format="dict")
    reviewed = anchor("spec", "review", spec["name"], output_format="dict")
    assert reviewed["rating"] in {"good", "excellent"}
    packet = task["workflow"]
    context = anchor("context", output_format="dict")
    assert context.get("workflow") == packet
    handoff = anchor("snapshot", mode="handoff", output_format="dict")
    assert handoff.get("workflow") == packet
    assert json.loads(Path(handoff["artifact_path"]).read_text())["workflow"] == packet
    assert handoff["first_action"]["kwargs"] == {"task_window_id": task["task_window_id"]}
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "personal")
    restarted, _, _ = init(root=str(target), project="alpha", output_format="dict")
    restarted("orient", output_format="dict")
    rebound = restarted("task_rebind", task_window_id=task["task_window_id"], output_format="dict")
    assert rebound.get("workflow") == packet
    assert restarted("context", output_format="dict").get("workflow") == packet
    assert "unphased_unsupported_collector" in restarted("context", output_format="markdown")
    assert packet["status"] == "unphased_unsupported_collector"
    assert packet["completed"] is False and packet["delivery_verified"] is False
    assert packet["compatibility_exception"]["source_effects_permitted"] is False
