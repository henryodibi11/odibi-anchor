"""Public workflow operations cannot launder caller evidence into qualification."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
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
    accepted = anchor("task", "Plan exact report output", goal="Qualify report bytes", mode="planning",
                      risk="low", rigor="direct", trust_domain="personal",
                      in_scope=["notebooks/report.md"], constraints=["Managed artifacts only"],
                      acceptance_criteria=["Report matches its contract"], output_format="dict")
    assert accepted["workflow"]["status"] == "legacy_unphased"
    plan = {"schema_version": 1, "goal": "Qualify report bytes", "risk": "low",
            "execution_mode": "artifact_only", "scope": ["notebooks/report.md"],
            "artifact_paths": ["notebooks/report.md"], "exclusions": [], "constraints": [],
            "risks": [], "stop_conditions": [], "unresolved_decisions": [],
            "destination": {"kind": "managed_artifacts"},
            "criteria": [{"id": "report", "expected": "Report contract holds", "method": "pytest",
                          "test_targets": ["test_report.py"]}]}
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
    qualified = advance(anchor, "qualify")
    assert qualified["progress"] == "qualified" and qualified["completed"] is False
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
    rendered = anchor("workflow", output_format="markdown")
    assert isinstance(rendered, str) and "workflow_packet" in rendered
    assert "draft" in rendered


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
    with pytest.raises(RuntimeError, match="not satisfied"):
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
