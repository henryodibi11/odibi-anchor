"""Verified restoration moves Anchor storage, never source authority or history."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from tests.codebase.test_task_authority import fresh_state, result, state


def test_complete_chain_maps_only_home_paths(tmp_path):
    from odibi_anchor.codebase._authority_relocation import rebase_identity

    a, b, c = (str(tmp_path / part) for part in ("a", "b", "c"))
    original = {"anchor_home": a, "artifact_root": a + "/workspace/projects/x",
                "project_root": a + "/workspace/projects/x", "target_root": a + "/source",
                "project_id": "x", "trust_domain": "work", "repository_provider_id": "git"}
    chain = [{"source_home": a, "destination_home": b}, {"source_home": b, "destination_home": c}]
    assert rebase_identity(original, chain, c) == {
        **original, "anchor_home": c, "artifact_root": c + "/workspace/projects/x",
        "project_root": c + "/workspace/projects/x",
    }
    assert rebase_identity(original, chain[:1], c) == original
    assert rebase_identity(original, [], c) == original
    assert original["anchor_home"] == a


@pytest.mark.parametrize("edges", [[("a", "b"), ("a", "c")], [("a", "b"), ("b", "a")]])
def test_ambiguous_or_cyclic_chains_fail_closed(tmp_path, edges):
    from odibi_anchor.codebase._authority_relocation import rebase_identity

    with pytest.raises(RuntimeError, match=r"fork|cyclic"):
        rebase_identity({"anchor_home": str(tmp_path / "a")}, [
            {"source_home": str(tmp_path / a), "destination_home": str(tmp_path / b)}
            for a, b in edges
        ], str(tmp_path / "c"))


@pytest.fixture
def restored_work(tmp_path):
    from odibi_anchor import durability
    from odibi_anchor._dispatcher._project import RouteBinding
    from odibi_anchor._dispatcher._session import _save_continuity_state
    from odibi_anchor._dispatcher._workflow_admission import bind_workflow, workflow_owner
    from odibi_anchor.codebase._task_authority import persist_accepted_task
    from odibi_anchor.codebase._workflow import create_workflow

    original = state(tmp_path)
    home = tmp_path / "old-home"
    artifacts = home / "workspace/projects/project-a"
    artifacts.mkdir(parents=True)
    original.anchor_home = str(home)
    original.artifact_root = original.project_root = str(artifacts)
    original.trust_domain = "work"
    original.continuity_generation = 0
    original.continuity_record_sha256 = None
    original.continuity_status = "uninitialized"
    db = home / "memory.db"
    workflow = create_workflow(db, owner=workflow_owner(original), request_id="create",
                               plan={"schema_version": 1, "goal": "restore authority",
                                     "risk": "high", "execution_mode": "source_change"})
    original.workflow_binding = bind_workflow(db, session_state=original, workflow_id=workflow["workflow_id"])
    persist_accepted_task(db, session_state=original, task_stage={"trust_domain": "work"}, task_result=result())
    _save_continuity_state(RouteBinding(
        project_id="project-a", target_root=original.target_root, artifact_root=str(artifacts),
        anchor_home=str(home), binding_source="explicit", runtime_instance_id="fixture",
    ), original, {"workflow_id": workflow["workflow_id"]})
    durable = tmp_path / "durable"
    durable.mkdir()
    snapshot = durability.snapshot_state(source_db=db, source_artifacts=artifacts.parent,
                                          durable_root=durable, authority_id="work")
    new_home = tmp_path / "new-home"
    new_home.mkdir()
    restored = durability.restore_latest(durable_root=durable, destination_db=new_home / "memory.db",
                                         destination_artifacts=new_home / "workspace/projects", authority_id="work")
    resumed = fresh_state(original, anchor_home=str(new_home),
                          project_root=str(new_home / "workspace/projects/project-a"),
                          artifact_root=str(new_home / "workspace/projects/project-a"), workflow_binding=None)
    return original, resumed, workflow, snapshot, restored


def test_real_restore_rebinds_workflow_preserving_old_rows_and_snapshot(restored_work):
    from odibi_anchor._dispatcher._workflow_admission import bound_workflow
    from odibi_anchor._dispatcher._workflow_evidence import _accepted_task
    from odibi_anchor.codebase._task_authority import rebind_latest_open_task, task_recovery_context

    original, resumed, workflow, snapshot, restored = restored_work
    db = restored["destination_db"]
    assert task_recovery_context(db, session_state=resumed)["ownership_state"] == "interrupted_source_task"
    rebound = rebind_latest_open_task(db, session_state=resumed)
    assert rebound["owner_relocated"] is True
    assert resumed.target_root == original.target_root
    assert resumed.workflow_binding == original.workflow_binding
    assert bound_workflow(db, session_state=resumed) == workflow
    assert _accepted_task(db, resumed, resumed.task_window_id)["identity"]["anchor_home"] == original.anchor_home
    with sqlite3.connect(snapshot["snapshot_path"]) as old, sqlite3.connect(db) as new:
        for table in ("accepted_task_records", "workflow_events"):
            assert old.execute(f"SELECT * FROM {table}").fetchall() == new.execute(f"SELECT * FROM {table}").fetchall()
    assert hashlib.sha256(Path(snapshot["snapshot_path"]).read_bytes()).hexdigest() == restored["sha256"]
    assert restored["restored_sha256"] != restored["sha256"]


@pytest.mark.parametrize("change", ["target_root", "active_project", "trust_domain"])
def test_relocation_never_changes_target_project_or_trust(restored_work, change):
    from odibi_anchor.codebase._task_authority import TaskAuthorityUnavailable, rebind_latest_open_task

    _, resumed, _, _, restored = restored_work
    setattr(resumed, change, getattr(resumed, change) + "-other")
    with pytest.raises(TaskAuthorityUnavailable, match="no open accepted task"):
        rebind_latest_open_task(restored["destination_db"], session_state=resumed)


def test_unattested_database_copy_rejected(restored_work, tmp_path):
    from odibi_anchor.codebase._task_authority import TaskAuthorityUnavailable, rebind_latest_open_task

    _, resumed, _, snapshot, _ = restored_work
    copied = tmp_path / "copied.db"
    shutil.copyfile(snapshot["snapshot_path"], copied)
    with pytest.raises(TaskAuthorityUnavailable, match="no open accepted task"):
        rebind_latest_open_task(copied, session_state=resumed)


@pytest.mark.parametrize("damage", ["version", "missing_table", "bad_hash", "missing_genesis"])
def test_snapshot_inspection_rejects_corrupted_domains(restored_work, damage):
    from odibi_anchor.durability import _inspect_local_database

    *_, restored = restored_work
    db = restored["destination_db"]
    with sqlite3.connect(db) as connection:
        if damage == "version":
            connection.execute("UPDATE anchor_schema_versions SET version=99 WHERE domain='authority_relocation'")
        elif damage == "missing_table":
            connection.execute("DROP TABLE anchor_home_relocations")
        elif damage == "bad_hash":
            connection.execute("DROP TRIGGER anchor_home_relocations_no_update")
            connection.execute("UPDATE anchor_home_relocations SET event_sha256=?", ("0" * 64,))
            from odibi_anchor.codebase._authority_relocation import _DDL
            connection.execute(_DDL[1])
        else:
            connection.execute("DROP TRIGGER workflow_events_no_update")
            connection.execute("UPDATE workflow_events SET generation=1")
            from odibi_anchor.codebase._workflow import _DDL
            connection.execute(_DDL[1])
    with pytest.raises(RuntimeError, match=r"schema|integrity"):
        _inspect_local_database(Path(db))


def test_second_restore_follows_two_hops(restored_work, tmp_path):
    from odibi_anchor import durability
    from odibi_anchor._dispatcher._workflow_admission import bound_workflow
    from odibi_anchor.codebase._task_authority import rebind_latest_open_task

    original, resumed, workflow, _, restored = restored_work
    durable = tmp_path / "second-durable"
    durable.mkdir()
    durability.snapshot_state(source_db=restored["destination_db"],
                              source_artifacts=Path(resumed.artifact_root).parent,
                              durable_root=durable, authority_id="work")
    third = tmp_path / "third-home"
    third.mkdir()
    last = durability.restore_latest(durable_root=durable, destination_db=third / "memory.db",
                                     destination_artifacts=third / "workspace/projects", authority_id="work")
    final = fresh_state(original, anchor_home=str(third), project_root=str(third / "workspace/projects/project-a"),
                        artifact_root=str(third / "workspace/projects/project-a"), workflow_binding=None)
    rebind_latest_open_task(last["destination_db"], session_state=final)
    assert bound_workflow(last["destination_db"], session_state=final) == workflow
    with sqlite3.connect(last["destination_db"]) as connection:
        records = [json.loads(row[0]) for row in connection.execute("SELECT event_json FROM anchor_home_relocations")]
    assert {(r["source_home"], r["destination_home"]) for r in records} == {
        (original.anchor_home, resumed.anchor_home), (resumed.anchor_home, str(third)),
    }
