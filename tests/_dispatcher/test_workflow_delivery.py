"""Delivery authority is an exact human response, not a passing local gate."""

import copy
import importlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._dispatcher.test_workflow_evidence import retain_evidence
from tests.codebase.test_task_authority import result, state


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    d = importlib.import_module("odibi_anchor._dispatcher._workflow_delivery")
    e = importlib.import_module("odibi_anchor._dispatcher._workflow_evidence")
    w = importlib.import_module("odibi_anchor.codebase._workflow")
    a = importlib.import_module("odibi_anchor._dispatcher._workflow_admission")
    from odibi_anchor.codebase._task_authority import persist_accepted_task
    from odibi_anchor.planning._task_profile import normalize_task_profile

    producer = state(tmp_path)
    producer.trust_domain = "personal"
    producer.active_task_profile = normalize_task_profile(legacy_mode="planning", risk="high")
    plan = {"schema_version": 1, "goal": "Deliver exact report", "risk": "high",
            "execution_mode": "artifact_only", "scope": ["report"], "artifact_paths": ["notebooks/result.md"],
            "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
            "unresolved_decisions": [], "destination": {"kind": "managed_artifacts"},
            "criteria": [{"id": "constant", "expected": "result is 2", "method": "pytest",
                          "test_targets": ["tests/test_constant.py"]}]}
    db = tmp_path / "authority.db"
    workflow = w.create_workflow(db, owner=a.workflow_owner(producer), request_id="create", plan=plan)
    producer.workflow_binding = a.bind_workflow(db, session_state=producer, workflow_id=workflow["workflow_id"])
    persist_accepted_task(db, session_state=producer, task_stage={"trust_domain": "personal"}, task_result=result())
    w.transition_workflow(db, owner=a.workflow_owner(producer), workflow_id=workflow["workflow_id"],
                          expected_generation=0, request_id="plan", operation="accept_plan",
                          payload={"baseline": {"task_window_id": producer.task_window_id}, "authority_ref": "fixture"})
    artifact = Path(producer.artifact_root) / "notebooks/result.md"
    artifact.parent.mkdir()
    artifact.write_bytes(b"Result: 2\n")
    candidate = e.collect_candidate(db, session_state=producer, workflow_id=workflow["workflow_id"])
    workflow = w.transition_workflow(db, owner=a.workflow_owner(producer), workflow_id=workflow["workflow_id"],
                                     expected_generation=1, request_id="implemented", operation="implemented",
                                     payload={"candidate": candidate})
    retain_evidence((db, producer, workflow))
    workflow = e.qualify_recorded(db, session_state=producer, workflow_id=workflow["workflow_id"],
                                  expected_generation=4, request_id="qualify")
    provider = SimpleNamespace(expected_owner_id="henry", assurance="test_authenticated_owner",
                               transport=SimpleNamespace(name="fixture"))
    owner_module = importlib.import_module("odibi_anchor.human_input_owner")
    monkeypatch.setattr(owner_module, "select_owner_approval_provider", lambda: provider)
    return SimpleNamespace(d=d, w=w, db=db, producer=producer, workflow=workflow, artifact=artifact,
                           args={"session_state": producer, "workflow_id": workflow["workflow_id"]})


def approve(delivery, monkeypatch, **overrides):
    human = importlib.import_module("odibi_anchor.human_input")
    prepared = delivery.d.prepare_delivery(delivery.db, **delivery.args)
    reply = {"response": prepared["approval_response"], "response_user_id": "henry",
             "transport": "fixture", "request_id": "request:1", "response_message_id": "response:1"}
    reply.update(overrides)
    monkeypatch.setattr(human, "request_human_input_record", lambda *a, **k: SimpleNamespace(**reply))
    return delivery.d.request_delivery_approval(delivery.db, **delivery.args, request_id="approve")


def test_prepare_is_not_authority_and_exposes_exact_review_limit(delivery):
    prepared = delivery.d.prepare_delivery(delivery.db, **delivery.args)
    subject = prepared["subject"]
    assert prepared["authority_granted"] is False
    assert subject["candidate_sha256"] == delivery.w.digest(delivery.workflow["candidate"])
    assert subject["qualification_sha256"] == delivery.w.digest(delivery.workflow["qualification"])
    assert subject["destination"] == {"kind": "managed_artifacts"}
    assert subject["review_assurance"] == "separate_read_only_accepted_task"
    assert subject["reviewer_authentication"] == "none"
    with pytest.raises(delivery.d.WorkflowError, match="retained approval"):
        delivery.d.observe_destination(delivery.db, **delivery.args)


def test_historical_qualification_without_producer_closure_cannot_authorize(delivery, monkeypatch):
    historical = copy.deepcopy(delivery.workflow)
    del historical["qualification"]["producer_terminal_record_sha256"]
    monkeypatch.setattr(delivery.d, "read_workflow", lambda *a, **k: historical)
    with pytest.raises(delivery.d.WorkflowError, match="exact producer terminal proof"):
        delivery.d.prepare_delivery(delivery.db, **delivery.args)


@pytest.mark.parametrize("overrides", [{"response": "approved"}, {"response_user_id": "other"},
                                      {"transport": "forged"}, {"response_message_id": None}])
def test_wrong_human_response_cannot_authorize(delivery, monkeypatch, overrides):
    with pytest.raises(delivery.d.WorkflowError, match="exact challenge and owner"):
        approve(delivery, monkeypatch, **overrides)
    retained = delivery.w.read_workflow(delivery.db, owner=delivery.workflow["owner"],
                                        workflow_id=delivery.workflow["workflow_id"])
    assert retained["progress"] == "qualified"
    assert retained["approval"] is None


def test_changed_bytes_during_approval_cannot_authorize(delivery, monkeypatch):
    human = importlib.import_module("odibi_anchor.human_input")
    prepared = delivery.d.prepare_delivery(delivery.db, **delivery.args)

    def reply(*args, **kwargs):
        delivery.artifact.write_bytes(b"Different result\n")
        return SimpleNamespace(response=prepared["approval_response"], response_user_id="henry",
                               transport="fixture", request_id="request:1", response_message_id="response:1")

    monkeypatch.setattr(human, "request_human_input_record", reply)
    with pytest.raises(delivery.d.WorkflowError, match="changed after qualification"):
        delivery.d.request_delivery_approval(delivery.db, **delivery.args, request_id="approve")


def test_exact_artifact_readback_is_measurement_not_automatic_completion(delivery, monkeypatch):
    approved = approve(delivery, monkeypatch)
    observation = delivery.d.observe_destination(delivery.db, **delivery.args)
    assert observation["status"] == "satisfied"
    assert observation["method"] == "readback"
    assert observation["observed_identity"] == delivery.workflow["candidate"]["identity"]
    assert observation["observed"]["files"]["notebooks/result.md"]["size"] == 10
    retained = delivery.w.read_workflow(delivery.db, owner=delivery.workflow["owner"],
                                        workflow_id=delivery.workflow["workflow_id"])
    assert retained == approved
    assert retained["completed"] is False
    delivery.artifact.write_bytes(b"Result: 9\n")
    with pytest.raises(delivery.d.WorkflowError, match="changed after qualification"):
        delivery.d.observe_destination(delivery.db, **delivery.args)


def test_unavailable_owner_transport_does_not_grant(delivery, monkeypatch):
    owner = importlib.import_module("odibi_anchor.human_input_owner")

    def unavailable():
        raise RuntimeError("owner transport unavailable")

    monkeypatch.setattr(owner, "select_owner_approval_provider", unavailable)
    with pytest.raises(RuntimeError, match="owner transport unavailable"):
        delivery.d.request_delivery_approval(delivery.db, **delivery.args, request_id="approve")


def test_unknown_outcome_can_be_read_without_reauthorizing_or_retrying_effect(delivery, monkeypatch):
    approved = approve(delivery, monkeypatch)
    blocked = delivery.w.transition_workflow(
        delivery.db, owner=approved["owner"], workflow_id=approved["workflow_id"],
        expected_generation=approved["generation"], request_id="uncertain", operation="block",
        payload={"kind": "outcome_unknown", "reason": "host receipt lost"},
    )
    with pytest.raises(delivery.d.WorkflowError, match="active retained qualification"):
        delivery.d.prepare_delivery(delivery.db, **delivery.args)
    observation = delivery.d.observe_destination(delivery.db, **delivery.args)
    assert observation["status"] == "satisfied"
    retained = delivery.w.read_workflow(delivery.db, owner=approved["owner"], workflow_id=approved["workflow_id"])
    assert retained == blocked
    assert retained["status"] == "blocked"


def test_json_redirects_are_not_followed(delivery):
    assert delivery.d._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://foreign") is None


@pytest.mark.parametrize("destination", [{"kind": "unknown"}, {"kind": "managed_artifacts", "root": "/other"},
                                         {"kind": "pypi_release", "name": "../other", "version": "1"}])
def test_destination_contract_rejects_unknown_authority(delivery, destination):
    with pytest.raises(delivery.d.WorkflowError):
        delivery.d._destination({"plan": {"destination": destination}}, delivery.producer)


def remote_state(delivery, monkeypatch, destination, candidate):
    """Isolate remote readers from already-covered SQLite/qualification mechanics."""
    retained = copy.deepcopy(delivery.workflow)
    retained.update(progress="approved_for_delivery", candidate=candidate)
    retained["plan"]["destination"] = destination
    retained["approval"] = {"destination": destination,
                            "operation": delivery.d._OPERATIONS[destination["kind"]],
                            "qualification_sha256": delivery.w.digest(retained["qualification"])}
    monkeypatch.setattr(delivery.d, "_fresh", lambda *a, **k: copy.deepcopy(retained))
    return retained


@pytest.mark.parametrize("change", [None, "head", "repository", "dirty", "draft", "moved_tag"])
def test_github_release_requires_exact_repo_clean_head_and_published_tag(delivery, monkeypatch, change):
    destination = {"kind": "github_release", "repository": "acme/app", "tag": "v1.2"}
    candidate = {"kind": "git", "identity": "qualified-content", "snapshot": {"head_sha": "a" * 40}}
    remote_state(delivery, monkeypatch, destination, candidate)

    def git(root, *args):
        if args[0] == "remote":
            return "https://github.com/" + ("other/app" if change == "repository" else "acme/app") + ".git"
        if args[0] == "status":
            return " M source.py" if change == "dirty" else ""
        assert args[0] == "check-ref-format"
        return ""

    calls = []

    def read(service, path):
        calls.append((service, path))
        if "/commits/" in path:
            wrong = change == "head" or (change == "moved_tag" and len(calls) == 3)
            return {"sha": ("b" if wrong else "a") * 40}
        return {"tag_name": "v1.2", "draft": change == "draft", "published_at": "2026-01-01", "id": 42}

    monkeypatch.setattr(delivery.d, "_git", git)
    monkeypatch.setattr(delivery.d, "_get_json", read)
    if change:
        with pytest.raises(delivery.d.WorkflowError):
            delivery.d.observe_destination(delivery.db, **delivery.args)
        if change in {"repository", "dirty"}:
            assert calls == []
    else:
        observed = delivery.d.observe_destination(delivery.db, **delivery.args)
        assert observed["observed"] == {"repository": "acme/app", "ref": "refs/tags/v1.2",
                                        "sha": "a" * 40, "release_id": 42}
        assert calls == [("github", "/repos/acme/app/commits/refs%2Ftags%2Fv1.2"),
                         ("github", "/repos/acme/app/releases/tags/v1.2"),
                         ("github", "/repos/acme/app/commits/refs%2Ftags%2Fv1.2")]


@pytest.mark.parametrize("change", [None, "hash", "size", "yanked", "missing", "duplicate", "version", "name"])
def test_package_readback_requires_exact_release_and_each_file(delivery, monkeypatch, change):
    destination = {"kind": "pypi_release", "name": "acme-app", "version": "1.2"}
    candidate = {"kind": "managed_artifacts", "identity": "qualified-wheel", "snapshot": {
        "notebooks/acme_app-1.2.whl": {"sha256": "a" * 64, "size": 731},
    }}
    remote_state(delivery, monkeypatch, destination, candidate)
    entry = {"filename": "acme_app-1.2.whl", "yanked": change == "yanked",
             "digests": {"sha256": ("b" if change == "hash" else "a") * 64},
             "size": 732 if change == "size" else 731}
    response = {"info": {"name": "other" if change == "name" else "acme_app",
                         "version": "1.3" if change == "version" else "1.2"},
                "urls": [] if change == "missing" else [entry, entry] if change == "duplicate" else [entry]}
    calls = []

    def read(service, path):
        calls.append((service, path))
        return response

    monkeypatch.setattr(delivery.d, "_get_json", read)
    if change:
        with pytest.raises(delivery.d.WorkflowError):
            delivery.d.observe_destination(delivery.db, **delivery.args)
    else:
        observed = delivery.d.observe_destination(delivery.db, **delivery.args)
        assert observed["observed"] == candidate["snapshot"]
    assert calls == [("pypi", "/pypi/acme-app/1.2/json")]


@pytest.mark.parametrize("change", [None, "host", "bytes", "notebook", "absent"])
@pytest.mark.parametrize("configured", [False, True])
def test_workspace_readback_uses_exact_host_file_path_and_bytes(delivery, monkeypatch, change, configured):
    import hashlib

    destination = {"kind": "databricks_workspace_files", "host": "https://workspace.example"}
    candidate = {"kind": "databricks_git_folder", "identity": "qualified-file", "snapshot": {
        "workspace_path": "/Repos/user/project", "content_changes": {"source.py": {
            "final_sha256": None if change == "absent" else hashlib.sha256(b"VALUE = 2\n").hexdigest(),
            "final_size": 10,
        }},
    }}
    remote_state(delivery, monkeypatch, destination, candidate)
    calls = []

    def status(path):
        calls.append(("status", path))
        return SimpleNamespace(object_type="NOTEBOOK" if change == "notebook" else "FILE")

    def download(path):
        calls.append(("download", path))
        return io.BytesIO(b"VALUE = 9\n" if change == "bytes" else b"VALUE = 2\n")

    client = SimpleNamespace(config=SimpleNamespace(host="https://other" if change == "host" else destination["host"]),
                             workspace=SimpleNamespace(get_status=status, download=download))
    options = {"workspace_client": client}
    if configured:
        delivery.args["session_state"].repository_provider = SimpleNamespace(
            api_executor=SimpleNamespace(workspace_client=client),
        )
        options = {}
    if change:
        with pytest.raises(delivery.d.WorkflowError):
            delivery.d.observe_destination(delivery.db, **delivery.args, **options)
        if change == "host":
            assert calls == []
    else:
        observed = delivery.d.observe_destination(delivery.db, **delivery.args, **options)
        assert observed["observed"]["source.py"]["size"] == 10
        assert calls == [("status", "/Repos/user/project/source.py"), ("download", "/Repos/user/project/source.py")]
