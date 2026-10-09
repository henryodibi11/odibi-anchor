"""Contract tests for the incident replay harness itself."""
from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest

from tests.fixtures.fake_databricks import DEFAULT_VOLUME, apis
from tests.fixtures.fake_databricks.crash import (
    CrashInjector,
    InjectedCrash,
    crash_matrix,
    record_mutations,
)


def test_crash_injector_numbers_scoped_points_and_crashes_before_applying(tmp_path):
    original_replace = os.replace

    def operation(scope: Path) -> None:
        (tmp_path / f"outside-{scope.name}.txt").write_text("not in scope")
        (scope / "dir").mkdir()
        (scope / "dir" / "staged.txt").write_text("payload")
        os.replace(scope / "dir" / "staged.txt", scope / "dir" / "final.txt")

    recorded, crashed = tmp_path / "recorded", tmp_path / "crashed"
    recorded.mkdir()
    crashed.mkdir()
    _, points = record_mutations(lambda: operation(recorded), scope=[recorded])
    assert [(point.index, point.operation) for point in points] == [
        (1, "os.mkdir"), (2, "open:w"), (3, "os.replace"),
    ]
    assert os.replace is original_replace

    with CrashInjector(scope=[crashed], crash_at=3) as injector:
        try:
            operation(crashed)
        except Exception:  # an abrupt crash must bypass ordinary recovery handlers
            pytest.fail("InjectedCrash was handled as an ordinary exception")
        except InjectedCrash as crash:
            assert crash.point == injector.crashed
            assert (crash.point.index, crash.point.operation) == (3, "os.replace")
    assert (crashed / "dir" / "staged.txt").read_text() == "payload"
    assert not (crashed / "dir" / "final.txt").exists()
    assert os.replace is original_replace


def test_fake_files_api_reports_sdk_errors_faults_and_remote_mutations(databricks, tmp_path):
    files = importlib.import_module("databricks.sdk").WorkspaceClient().files
    errors = importlib.import_module("databricks.sdk.errors")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"durable bytes")
    remote = f"{DEFAULT_VOLUME}/lineage/payload.bin"

    with pytest.raises(errors.NotFound):
        files.get_directory_metadata("/Volumes/main/anchor/unprovisioned")
    with CrashInjector(scope=[tmp_path]) as injector:
        files.create_directory(f"{DEFAULT_VOLUME}/lineage")
        files.upload_from(remote, str(source), overwrite=False)
        files.download_to(remote, str(tmp_path / "copy.bin"), overwrite=False)
    assert [(point.kind, point.operation) for point in injector.points] == [
        ("remote", "files.create_directory"), ("remote", "files.upload_from"),
    ]
    assert (tmp_path / "copy.bin").read_bytes() == b"durable bytes"
    with pytest.raises(errors.AlreadyExists):
        files.upload_from(remote, str(source), overwrite=False)

    databricks.inject_fault("files", "delete", when="after", match="payload.bin")
    with pytest.raises(apis.DatabricksError):
        files.delete(remote)
    assert not databricks.volume_path(remote).exists(), "an 'after' fault applies, then fails"
    assert ("files", "delete", remote) in databricks.calls


def _source_state(root: Path) -> tuple[Path, Path]:
    durability = importlib.import_module("odibi_anchor.durability")
    database = root / "state" / ".agent_memory.db"
    projects = root / "state" / "workspace" / "projects"
    (projects / "alpha").mkdir(parents=True)
    (projects / "alpha" / "PROJECT.md").write_text("---\nid: alpha\n---\n")
    durability.ensure_database_authority(
        database, authority_id="henry", trust_domain="work", initialize=True,
    )
    return database, projects


def test_snapshot_publication_crash_never_exposes_a_partial_snapshot(databricks, tmp_path):
    durability = importlib.import_module("odibi_anchor.durability")
    runs = iter(range(1000))

    def prepare():
        run = tmp_path / f"run-{next(runs)}"
        database, projects = _source_state(run)
        durable_root = f"{DEFAULT_VOLUME}/{run.name}"
        prior = durability.snapshot_state(
            source_db=database, source_artifacts=projects, durable_root=durable_root,
            authority_id="henry", databricks=True,
        )
        (projects / "alpha" / "notes.md").write_text("new evidence\n")
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE replay_marker (value TEXT)")
        return {"run": run, "database": database, "projects": projects,
                "durable_root": durable_root, "prior": prior["manifest"]["snapshot_id"]}

    def operation(state):
        state["published"] = durability.snapshot_state(
            source_db=state["database"], source_artifacts=state["projects"],
            durable_root=state["durable_root"], authority_id="henry", databricks=True,
        )["manifest"]["snapshot_id"]

    def verify(state, point):
        listed = durability.list_snapshots(
            durable_root=state["durable_root"], authority_id="henry", databricks=True,
        )["snapshots"]
        destination = state["run"] / "restored"
        destination.mkdir()
        restored = durability.restore_latest(
            durable_root=state["durable_root"], destination_db=destination / ".agent_memory.db",
            destination_artifacts=destination / "workspace" / "projects",
            authority_id="henry", databricks=True,
        )
        expected = state["prior"] if point is not None else state["published"]
        assert restored["snapshot_id"] == expected
        assert [item["snapshot_id"] for item in listed][-1] == expected
        notes = destination / "workspace" / "projects" / "alpha" / "notes.md"
        assert notes.exists() is (point is None)

    points = crash_matrix(
        prepare=prepare, operation=operation, verify=verify,
        scope=lambda state: [state["run"] / "state"],
    )
    assert [point.operation for point in points if point.kind == "remote"] == [
        "files.create_directory", "files.upload_from", "files.upload_from", "files.upload_from",
    ]
