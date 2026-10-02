"""Verified restore transports evidence, never permission to absorb draft writes."""

import json
import os
import sqlite3

import pytest

from tests._dispatcher.test_workflow_runtime import advance
from tests._dispatcher.test_workflow_runtime import runtime as workflow_runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    return workflow_runtime.__wrapped__(tmp_path, monkeypatch, request)


def snapshot(home, durable):
    from odibi_anchor.durability import snapshot_state

    durable.mkdir(exist_ok=True)
    return snapshot_state(source_db=home / "memory.db",
                          source_artifacts=home / "workspace/projects",
                          durable_root=durable, authority_id="fixture")


def restore(home, target, durable, monkeypatch, task_id):
    from odibi_anchor.bootstrap import init
    from odibi_anchor.durability import restore_latest

    home.mkdir()
    result = restore_latest(durable_root=durable, destination_db=home / "memory.db",
                            destination_artifacts=home / "workspace/projects", authority_id="fixture")
    assert result["artifacts"]["status"] == "restored"
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "personal")
    anchor, _, _ = init(root=str(target), project="alpha", output_format="dict")
    anchor("orient", output_format="dict")
    rebound = anchor("task_rebind", task_window_id=task_id, output_format="dict")
    assert rebound["kind"] == "task_authority_rebind"
    return anchor


def output(home):
    return home / "workspace/projects/alpha/notebooks/report.md"


def mutate(path, kind):
    before = path.stat()
    original = path.read_bytes()
    path.write_bytes(b"Out-of-band content\n")
    if kind == "same_bytes":
        path.write_bytes(original)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_ctime_ns != before.st_ctime_ns


@pytest.mark.parametrize("runtime", ["preexisting", None], indirect=True)
@pytest.mark.parametrize("command", ["accept_plan", "replan"])
@pytest.mark.parametrize("hops", [1, 2])
def test_verified_restore_preserves_draft_admission(runtime, tmp_path, monkeypatch, command, hops):
    anchor, home, target, draft = runtime
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    original = output(home).read_bytes() if output(home).exists() else None
    with sqlite3.connect(home / "memory.db") as connection:
        history = connection.execute("SELECT * FROM workflow_events").fetchall()
    for hop in range(hops):
        durable = tmp_path / f"durable-{hop}"
        snapshot(home, durable)
        home = tmp_path / f"restored-{hop}"
        anchor = restore(home, target, durable, monkeypatch, task_id)
        assert (output(home).read_bytes() if output(home).exists() else None) == original
        with sqlite3.connect(home / "memory.db") as connection:
            assert connection.execute("SELECT * FROM workflow_events").fetchall() == history
    kwargs = {"plan": draft["plan"], "reason": "Verified restore"} if command == "replan" else {}
    result = advance(anchor, command, **kwargs)
    assert result["progress"] == ("draft" if command == "replan" else "planned")
    assert result["completed"] is False
    if command == "replan":
        assert advance(anchor, "accept_plan")["progress"] == "planned"


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
@pytest.mark.parametrize("when", ["before_snapshot", "after_restore", "before_second_snapshot"])
@pytest.mark.parametrize("kind", ["changed_bytes", "same_bytes"])
def test_restore_never_launders_draft_mutations(runtime, tmp_path, monkeypatch, when, kind):
    anchor, home, target, draft = runtime
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    durable = tmp_path / "durable"
    if when == "before_snapshot":
        # An older valid checkpoint must not be reused for a same-byte rewrite.
        first = snapshot(home, durable)
        mutate(output(home), kind)
    result = snapshot(home, durable)
    if when == "before_snapshot":
        assert result["manifest"]["snapshot_id"] != first["manifest"]["snapshot_id"]
    home = tmp_path / "restored"
    anchor = restore(home, target, durable, monkeypatch, task_id)
    if when != "before_snapshot":
        mutate(output(home), kind)
    if when == "before_second_snapshot":
        snapshot(home, tmp_path / "second-durable")
        home = tmp_path / "second-restored"
        anchor = restore(home, target, tmp_path / "second-durable", monkeypatch, task_id)
    for command, kwargs in [("accept_plan", {}),
                            ("replan", {"plan": draft["plan"], "reason": "Must not absorb drift"})]:
        with pytest.raises(RuntimeError, match="pre-plan artifact changed"):
            advance(anchor, command, **kwargs)
    assert anchor("workflow", output_format="dict")["state"]["progress"] == "draft"


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
@pytest.mark.parametrize("damage", ["bundle", "manifest"])
def test_tampered_snapshot_cannot_restore_admission_provenance(runtime, tmp_path, damage):
    from pathlib import Path

    from odibi_anchor.durability import restore_latest

    _, home, _, _ = runtime
    result = snapshot(home, tmp_path / "durable")
    path = Path(result["artifacts_path"] if damage == "bundle" else result["manifest_path"])
    if damage == "bundle":
        with path.open("ab") as stream:
            stream.write(b"tamper")
    else:
        value = json.loads(path.read_text())
        value["artifacts"]["workflow_baselines"] = []
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    restored = tmp_path / "restored"
    restored.mkdir()
    with pytest.raises(RuntimeError, match=r"hash|checksum|size"):
        restore_latest(durable_root=tmp_path / "durable", destination_db=restored / "memory.db",
                       destination_artifacts=restored / "workspace/projects", authority_id="fixture")
    assert not (restored / "memory.db").exists()


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
@pytest.mark.parametrize("field", ["target_root", "active_project", "trust_domain"])
def test_restore_provenance_does_not_change_workflow_authority(runtime, tmp_path, monkeypatch, field):
    from odibi_anchor._dispatcher._workflow_admission import workflow_owner
    from odibi_anchor.codebase._workflow import read_workflow

    anchor, home, target, draft = runtime
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    snapshot(home, tmp_path / "durable")
    restored = tmp_path / "restored"
    restore(restored, target, tmp_path / "durable", monkeypatch, task_id)
    from odibi_anchor._utils._session_state import _SESSION_STATE

    monkeypatch.setattr(_SESSION_STATE, field, getattr(_SESSION_STATE, field) + "-other")
    with pytest.raises(RuntimeError, match="exact authority"):
        read_workflow(restored / "memory.db", owner=workflow_owner(_SESSION_STATE),
                      workflow_id=draft["workflow_id"])


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
def test_snapshot_refuses_mutation_during_artifact_packing(runtime, tmp_path, monkeypatch):
    from odibi_anchor import durability

    _, home, _, _ = runtime
    original = durability._stage_artifact_bundle

    def rewrite_after_packing(*args):
        result = original(*args)
        mutate(output(home), "same_bytes")
        return result

    monkeypatch.setattr(durability, "_stage_artifact_bundle", rewrite_after_packing)
    with pytest.raises(RuntimeError, match="baseline changed during snapshot"):
        snapshot(home, tmp_path / "durable")
    assert not list((tmp_path / "durable").rglob("*.manifest.json"))


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
@pytest.mark.parametrize("change", ["legacy", "wrong_owner", "wrong_baseline", "wrong_state", "wrong_content"])
def test_restore_requires_exact_portable_proof(runtime, tmp_path, monkeypatch, change):
    from pathlib import Path

    from odibi_anchor.durability import _canonical_bytes, _stage_artifact_bundle

    anchor, home, target, _ = runtime
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    result = snapshot(home, tmp_path / "durable")
    manifest = result["manifest"]
    if change == "legacy":
        del manifest["artifacts"]["workflow_baselines"]
    elif change == "wrong_content":
        output(home).write_bytes(b"Replacement bytes\n")
        archive = tmp_path / "replacement.tar"
        inventory = _stage_artifact_bundle(home / "workspace/projects", archive)
        archive.rename(Path(result["artifacts_path"]).parent / inventory["file"])
        manifest["artifacts"].update(inventory)
    else:
        proof = manifest["artifacts"]["workflow_baselines"][0]
        if change == "wrong_owner":
            proof["owner"]["trust_domain"] = "unrelated"
        else:
            proof["baseline_sha256" if change == "wrong_baseline" else "state_sha256"] = "sha256:" + "0" * 64
    # Recompute the envelope to exercise semantic verification, not only checksums.
    import hashlib

    del manifest["manifest_sha256"]
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    Path(result["manifest_path"]).write_bytes(_canonical_bytes(manifest))
    if change == "legacy":
        restarted = restore(tmp_path / "restored", target, tmp_path / "durable", monkeypatch, task_id)
        with pytest.raises(RuntimeError, match="pre-plan artifact changed"):
            advance(restarted, "accept_plan")
    else:
        with pytest.raises(RuntimeError, match="content differs" if change == "wrong_content" else "draft authority"):
            restore(tmp_path / "restored", target, tmp_path / "durable", monkeypatch, task_id)
        assert not (tmp_path / "restored/memory.db").exists()
        assert not (tmp_path / "restored/workspace/projects").exists()


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
@pytest.mark.parametrize("damage", ["version", "schema", "checksum"])
def test_restore_receipt_corruption_fails_admission_and_snapshot(runtime, tmp_path, monkeypatch, damage):
    anchor, home, target, _ = runtime
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    snapshot(home, tmp_path / "durable")
    home = tmp_path / "restored"
    restarted = restore(home, target, tmp_path / "durable", monkeypatch, task_id)
    with sqlite3.connect(home / "memory.db") as connection:
        if damage == "version":
            connection.execute("UPDATE anchor_schema_versions SET version=99 WHERE domain='workflow_artifact_restore'")
        else:
            connection.execute("DROP TRIGGER workflow_artifact_restores_no_update")
            if damage == "checksum":
                connection.execute("UPDATE workflow_artifact_restores SET receipt_sha256='bad'")
                from odibi_anchor.codebase._workflow_artifact_restore import _DDL

                connection.execute(_DDL[1])
    with pytest.raises(RuntimeError, match=r"artifact restore.*(schema|integrity)"):
        advance(restarted, "accept_plan")
    with pytest.raises(RuntimeError, match=r"artifact restore.*(schema|integrity)"):
        snapshot(home, tmp_path / "second-durable")


@pytest.mark.parametrize("runtime", ["preexisting"], indirect=True)
def test_restore_at_original_location_keeps_recovery_authority(runtime, tmp_path, monkeypatch):
    import shutil

    anchor, home, target, _ = runtime
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    snapshot(home, tmp_path / "durable")
    shutil.rmtree(home)  # Disposable fixture: simulate local disk loss at the same path.
    restarted = restore(home, target, tmp_path / "durable", monkeypatch, task_id)
    advance(restarted, "block", reason="Pause for review", blocker_kind="awaiting_authority")
    advance(restarted, "resume", resolution="Owner confirmed continuation")
    assert advance(restarted, "accept_plan")["progress"] == "planned"


def test_large_valid_draft_retains_bounded_restore_receipts(runtime, tmp_path, monkeypatch):
    from odibi_anchor._dispatcher._workflow_admission import workflow_owner
    from odibi_anchor._dispatcher._workflow_evidence import collect_artifact_baseline
    from odibi_anchor._utils._session_state import _SESSION_STATE
    from odibi_anchor.codebase._workflow import create_workflow

    anchor, home, target, draft = runtime
    paths = [f"notebooks/report-{i:04d}.md" for i in range(800)]
    for name in paths:
        (home / "workspace/projects/alpha" / name).write_bytes(b"x")
    plan = {**draft["plan"], "scope": paths, "artifact_paths": paths}
    large = create_workflow(home / "memory.db", owner=workflow_owner(_SESSION_STATE),
                            request_id="large-draft", plan=plan,
                            artifact_baseline=collect_artifact_baseline(plan, session_state=_SESSION_STATE))
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    for hop in range(2):
        durable = tmp_path / f"large-durable-{hop}"
        snapshot(home, durable)
        home = tmp_path / f"large-restored-{hop}"
        restore(home, target, durable, monkeypatch, task_id)
    # Import the current runtime collectors after rebootstrap invalidates modules.
    from odibi_anchor._dispatcher._workflow_admission import workflow_owner
    from odibi_anchor._dispatcher._workflow_evidence import check_artifact_baseline
    from odibi_anchor._utils._session_state import _SESSION_STATE
    from odibi_anchor.codebase._workflow import read_workflow

    state = read_workflow(home / "memory.db", owner=workflow_owner(_SESSION_STATE),
                          workflow_id=large["workflow_id"])
    observed = check_artifact_baseline(state, session_state=_SESSION_STATE, path=home / "memory.db")
    assert len(observed["files"]) == 800
    with sqlite3.connect(home / "memory.db") as connection:
        assert connection.execute("SELECT max(length(receipt_json)) FROM workflow_artifact_restores").fetchone()[0] <= 256000
    mutate(home / "workspace/projects/alpha" / paths[-1], "same_bytes")
    with pytest.raises(RuntimeError, match="pre-plan artifact changed"):
        check_artifact_baseline(state, session_state=_SESSION_STATE, path=home / "memory.db")


@pytest.mark.parametrize("runtime", ["artifact", "preexisting_artifact"], indirect=True)
@pytest.mark.parametrize("second_recovery", [None, "planned", "implemented", "reviewed", "replan", "qualification"])
def test_complete_recovered_producer_lifecycle(runtime, tmp_path, monkeypatch, second_recovery):
    from tests._dispatcher.test_workflow_runtime import finish_producer

    anchor, home, target, draft = runtime
    task_id = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    with sqlite3.connect(home / "memory.db") as connection:
        original = connection.execute("SELECT record_json FROM accepted_task_records WHERE task_window_id=?",
                                      (task_id,)).fetchone()[0]
    snapshot(home, tmp_path / "first")
    home = tmp_path / "restored-first"
    anchor = restore(home, target, tmp_path / "first", monkeypatch, task_id)
    if second_recovery == "replan":
        advance(anchor, "replan", plan=draft["plan"], reason="Same contract after verified restore")
    admitted = advance(anchor, "accept_plan")
    assert admitted["progress"] == "planned"
    for phase in ("planned", "implemented", "reviewed"):
        if second_recovery == phase:
            snapshot(home, tmp_path / "second")
            home = tmp_path / "restored-second"
            anchor = restore(home, target, tmp_path / "second", monkeypatch, task_id)
        if phase == "planned":
            output(home).write_bytes(b"Result: 5\n")
            anchor("touched", str(output(home)), output_format="dict")
            assert advance(anchor, "implemented")["progress"] == "implemented"
        elif phase == "implemented":
            checked = advance(anchor, "check_artifact", criterion_id="report")
            assert checked["measurements"]["report"]["status"] == "satisfied"
            advance(anchor, "review", findings=[])
    terminal = finish_producer(anchor)
    assert terminal["record"]["terminal"]["status"] == "completed"
    assert terminal["record"]["identities"]["task_window_id"] == task_id
    assert terminal["record"]["identities"]["session_id"] != json.loads(original)["identity"]["session_id"]
    if second_recovery == "qualification":
        # Closed producers are never rebound. Recover a separately accepted
        # exact-candidate review task, then consume the retained producer proof.
        anchor("new_session", name="separate_qualification", inline=True, output_format="dict")
        review = anchor("task", "Review recovered exact candidate", goal="Verify report output", mode="review",
                        risk="low", rigor="direct", trust_domain="personal", workflow_id=draft["workflow_id"],
                        acceptance_criteria=["Exact bytes and producer closure"], output_format="dict")
        anchor("skill_loaded", "code-comprehension", output_format="dict")
        advance(anchor, "review", findings=[])
        snapshot(home, tmp_path / "second")
        home = tmp_path / "restored-second"
        anchor = restore(home, target, tmp_path / "second", monkeypatch, review["task_window_id"])
        anchor("skill_loaded", "code-comprehension", output_format="dict")
    qualified = advance(anchor, "qualify")
    assert qualified["progress"] == "qualified" and qualified["completed"] is False
    assert qualified["qualification"]["producer_terminal_record_sha256"] == terminal["record_sha256"]
    with sqlite3.connect(home / "memory.db") as connection:
        assert connection.execute("SELECT record_json FROM accepted_task_records WHERE task_window_id=?",
                                  (task_id,)).fetchone()[0] == original
    assert anchor("workflow", "prepare_delivery", output_format="dict")["authority_granted"] is False


@pytest.mark.parametrize("runtime", ["preexisting_artifact"], indirect=True)
@pytest.mark.parametrize("damage", ["missing", "cross_workflow", "cross_project"])
def test_implementation_requires_exact_retained_restore_proof(runtime, tmp_path, monkeypatch, damage):
    import hashlib

    anchor, home, target, _ = runtime
    task = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    snapshot(home, tmp_path / "durable")
    home = tmp_path / "restored"
    anchor = restore(home, target, tmp_path / "durable", monkeypatch, task)
    admitted = advance(anchor, "accept_plan")
    output(home).write_bytes(b"Result: 5\n")
    anchor("touched", str(output(home)), output_format="dict")
    from odibi_anchor.codebase._workflow import canonical, create_workflow
    from odibi_anchor.codebase._workflow_artifact_restore import _DDL

    other = create_workflow(home / "memory.db", owner=admitted["owner"], request_id="other",
                            plan=admitted["plan"], artifact_baseline=admitted["artifact_baseline"])
    with sqlite3.connect(home / "memory.db") as connection:
        if damage == "missing":
            connection.execute("DROP TABLE workflow_artifact_restores")
            connection.execute("DELETE FROM anchor_schema_versions WHERE domain='workflow_artifact_restore'")
        else:
            receipt = json.loads(connection.execute("SELECT receipt_json FROM workflow_artifact_restores").fetchone()[0])
            if damage == "cross_workflow":
                receipt["proof"]["workflow_id"] = other["workflow_id"]
            else:
                receipt["owner"]["project_id"] = "beta"
            # Recompute the envelope to test semantic binding, not just checksum rejection.
            raw = canonical(receipt)
            connection.execute("DROP TRIGGER workflow_artifact_restores_no_update")
            connection.execute("UPDATE workflow_artifact_restores SET receipt_json=?,receipt_sha256=?",
                               (raw, "sha256:" + hashlib.sha256(raw.encode()).hexdigest()))
            connection.execute(_DDL[1])
    with pytest.raises(RuntimeError, match="exact pre-plan artifact admission"):
        advance(anchor, "implemented")


@pytest.mark.parametrize("runtime", ["artifact"], indirect=True)
@pytest.mark.parametrize("damage", ["unrelated_session", "missing", "cross_project", "other_task_record",
                                    "rebound_event", "late", "schema", "missing_closure", "changed_candidate"])
def test_recovered_qualification_requires_exact_completion_lineage(runtime, tmp_path, monkeypatch, damage):
    import hashlib

    from tests._dispatcher.test_workflow_runtime import finish_producer

    anchor, home, target, _ = runtime
    task = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    snapshot(home, tmp_path / "durable")
    home = tmp_path / "restored"
    anchor = restore(home, target, tmp_path / "durable", monkeypatch, task)
    advance(anchor, "accept_plan")
    output(home).write_bytes(b"Result: 5\n")
    anchor("touched", str(output(home)), output_format="dict")
    advance(anchor, "implemented")
    advance(anchor, "check_artifact", criterion_id="report")
    advance(anchor, "review", findings=[])
    finish_producer(anchor)
    from odibi_anchor.codebase._task_authority import _DDL, _SESSION_DDL, _canonical

    with sqlite3.connect(home / "memory.db") as connection:
        if damage == "missing":
            connection.execute("DROP TABLE task_rebind_sessions")
            connection.execute("DELETE FROM anchor_schema_versions WHERE domain='task_rebind_sessions'")
        elif damage == "schema":
            connection.execute("DROP TRIGGER task_rebind_sessions_no_update")
        elif damage == "missing_closure":
            connection.execute("DROP TRIGGER accepted_task_events_no_delete")
            connection.execute("DELETE FROM accepted_task_events WHERE task_window_id=? AND event_type='closed'", (task,))
            connection.execute(_DDL[-1])
        elif damage in {"unrelated_session", "cross_project", "other_task_record", "rebound_event", "late"}:
            receipt = json.loads(connection.execute("SELECT receipt_json FROM task_rebind_sessions").fetchone()[0])
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute("UPDATE task_rebind_sessions SET receipt_sha256='bad'")
            if damage == "unrelated_session":
                receipt["session_id"] = "unrelated-runtime"
            elif damage == "cross_project":
                receipt["owner"]["project_id"] = "beta"
            elif damage == "other_task_record":
                receipt["accepted_record_sha256"] = connection.execute(
                    "SELECT record_sha256 FROM accepted_task_records WHERE task_window_id!=?", (task,)).fetchone()[0]
            elif damage == "rebound_event":
                receipt["rebind_event_sha256"] = "0" * 64
            else:
                receipt["created_at"] = "2099-01-01T00:00:00.000000Z"
            raw = _canonical(receipt)
            connection.execute("DROP TRIGGER task_rebind_sessions_no_update")
            connection.execute("UPDATE task_rebind_sessions SET session_id=?,receipt_json=?,receipt_sha256=?",
                               (receipt["session_id"], raw, hashlib.sha256(raw.encode()).hexdigest()))
            connection.execute(_SESSION_DDL[1])
    if damage == "changed_candidate":
        output(home).write_bytes(b"Different bytes after completed producer\n")
    with pytest.raises(RuntimeError, match=r"lineage|rebind session schema|task closure|candidate changed"):
        advance(anchor, "qualify")


@pytest.mark.parametrize("runtime", ["preexisting_artifact"], indirect=True)
def test_recovered_producer_cannot_use_a_stale_plan(runtime, tmp_path, monkeypatch):
    anchor, home, target, draft = runtime
    task = anchor("context", output_format="dict")["facts"]["task"]["value"]["task_window_id"]
    snapshot(home, tmp_path / "durable")
    home = tmp_path / "restored"
    anchor = restore(home, target, tmp_path / "durable", monkeypatch, task)
    advance(anchor, "accept_plan")
    advance(anchor, "replan", plan={**draft["plan"], "goal": "A different plan"}, reason="Changed requirements")
    with pytest.raises(RuntimeError, match="plan"):
        advance(anchor, "implemented")
