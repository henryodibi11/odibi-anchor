from __future__ import annotations

import errno
import hashlib
import json
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from odibi_anchor import durability


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO example(value) VALUES (?)", [("alpha",), ("beta",)])


class FakeDatabricksFiles:
    def __init__(self) -> None:
        self.directories: set[str] = set()
        self.files: dict[str, bytes] = {}
        self.uploads: list[tuple[str, bool, bool]] = []
        self.downloads: list[tuple[str, bool, bool]] = []
        self.fail_manifest_upload = False
        self.fail_index_upload = False
        self.missing_roots: set[str] = set()
        self.root_error: Exception | None = None
        self.strict_listing = False

    def create_directory(self, path: str) -> None:
        self.directories.add(path)

    def list_directory_contents(self, path: str):
        prefix = path.rstrip("/") + "/"
        if (
            self.strict_listing
            and path.rstrip("/") not in self.directories
            and not any(name.startswith(prefix) for name in self.files)
        ):
            raise FakeDatabricksNotFound(path)
        return [
            SimpleNamespace(path=name, file_size=len(self.files[name]))
            for name in sorted(self.files)
            if name.startswith(prefix) and "/" not in name.removeprefix(prefix)
        ]

    def upload_from(
        self,
        path: str,
        source: str,
        *,
        overwrite: bool,
        use_parallel: bool,
    ) -> None:
        self.uploads.append((path, overwrite, use_parallel))
        if self.fail_manifest_upload and path.endswith(".manifest.json"):
            raise OSError("simulated remote manifest publication failure")
        if self.fail_index_upload and path.endswith(".index.json"):
            raise OSError("simulated remote index publication failure")
        if path in self.files and not overwrite:
            raise FileExistsError(path)
        self.files[path] = Path(source).read_bytes()

    def download_to(
        self,
        path: str,
        destination: str,
        *,
        overwrite: bool,
        use_parallel: bool,
    ) -> None:
        self.downloads.append((path, overwrite, use_parallel))
        target = Path(destination)
        if target.exists() and not overwrite:
            raise FileExistsError(destination)
        target.write_bytes(self.files[path])

    def delete(self, path: str) -> None:
        del self.files[path]

    def get_directory_metadata(self, path: str) -> None:
        if path in self.missing_roots:
            raise FakeDatabricksNotFound(path)
        if self.root_error is not None:
            raise self.root_error


class FakeDatabricksNotFound(Exception):
    pass


def test_snapshot_restore_round_trip_and_idempotence(tmp_path: Path) -> None:
    source, durable, restored = tmp_path / "live.db", tmp_path / "durable", tmp_path / "restored.db"
    durable.mkdir()
    _database(source)

    first = durability.snapshot_state(source_db=str(source), durable_root=str(durable), authority_id="work")
    second = durability.snapshot_state(source_db=str(source), durable_root=str(durable), authority_id="work")
    result = durability.restore_latest(durable_root=str(durable), destination_db=str(restored), authority_id="work")

    assert first["action"] == "created"
    assert second["action"] == "reused"
    assert result["snapshot_id"] == first["manifest"]["snapshot_id"]
    assert len(list((durable / "work" / "snapshots").iterdir())) == 2
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [("alpha",), ("beta",)]
    json.dumps(first)


def test_v2_snapshot_restores_project_artifacts_and_empty_directories(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    durable = tmp_path / "durable"
    restored_db = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-projects"
    durable.mkdir()
    (artifacts / "alpha" / "problems").mkdir(parents=True)
    (artifacts / "alpha" / "specs").mkdir()
    (artifacts / "alpha" / "problems" / "P-1.md").write_text("# Evidence\n", encoding="utf-8")
    _database(source)

    first = durability.snapshot_state(
        source_db=source,
        source_artifacts=artifacts,
        durable_root=durable,
        authority_id="work",
    )
    second = durability.snapshot_state(
        source_db=source,
        source_artifacts=artifacts,
        durable_root=durable,
        authority_id="work",
    )
    # Record through the exact recorder copy durability is bound to: another test module purges
    # every "odibi_anchor" key (including the shared phase state) from sys.modules at collection.
    phases = durability._record_phase.__globals__
    with phases["recording"]() as recorder, phases["phase"]("runtime_preparation"):
        restored = durability.restore_latest(
            durable_root=durable,
            destination_db=restored_db,
            destination_artifacts=restored_artifacts,
            authority_id="work",
        )

    assert first["manifest"]["format"] == "odibi-anchor-durable-snapshot-v2"
    assert first["manifest"]["artifacts"]["file_count"] == 1
    assert second["action"] == "reused"
    assert restored["status"] == restored["classification"] == "restored"
    # Cold-start restore is attributed per step inside the enclosing bootstrap phase.
    (preparation,) = recorder.summary()["phases"]
    assert [item["phase"] for item in preparation["sub_phases"]] == [
        "restore_listing", "restore_verify_database", "restore_extract_artifacts",
        "restore_fill_artifacts", "restore_publish",
    ]
    assert all(item["outcome"] == "ok" for item in preparation["sub_phases"])
    assert restored["artifacts"]["status"] == "restored"
    assert (restored_artifacts / "alpha" / "specs").is_dir()
    assert (restored_artifacts / "alpha" / "problems" / "P-1.md").read_text() == "# Evidence\n"


def test_v2_restore_relocates_verified_continuity_without_changing_snapshot(
    tmp_path: Path,
) -> None:
    from odibi_anchor._dispatcher._project import RouteBinding
    from odibi_anchor._dispatcher._session import (
        _load_continuity_state,
        _save_continuity_state,
    )

    source_home = tmp_path / "old-state"
    source_projects = source_home / "workspace" / "projects"
    source_artifact = source_projects / "alpha"
    source_db = source_home / ".agent_memory.db"
    target = tmp_path / "target"
    durable = tmp_path / "durable"
    target.mkdir()
    durable.mkdir()
    source_home.mkdir()
    _database(source_db)
    binding = RouteBinding(
        project_id="alpha",
        target_root=str(target),
        artifact_root=str(source_artifact),
        anchor_home=str(source_home),
        binding_source="explicit",
        runtime_instance_id="runtime-a",
    )
    session = SimpleNamespace(
        session_id="session-a",
        task_window_id="task-a",
        continuity_generation=0,
        continuity_record_sha256=None,
        continuity_status="uninitialized",
    )
    _save_continuity_state(binding, session, {"open_task": "task-a"})
    snapshot = durability.snapshot_state(
        source_db=source_db,
        source_artifacts=source_projects,
        durable_root=durable,
        authority_id="work",
    )
    bundle = Path(snapshot["artifacts_path"])
    bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()

    destination_home = tmp_path / "new-state"
    destination_home.mkdir()
    destination_projects = destination_home / "workspace" / "projects"
    restored = durability.restore_latest(
        durable_root=durable,
        destination_db=destination_home / ".agent_memory.db",
        destination_artifacts=destination_projects,
        authority_id="work",
    )

    relocated_binding = RouteBinding(
        project_id="alpha",
        target_root=str(target),
        artifact_root=str(destination_projects / "alpha"),
        anchor_home=str(destination_home),
        binding_source="explicit",
        runtime_instance_id="runtime-a",
    )
    restored_session = SimpleNamespace(
        session_id="session-a",
        task_window_id="task-a",
        continuity_generation=0,
        continuity_record_sha256=None,
        continuity_status="uninitialized",
    )
    continuity = _load_continuity_state(relocated_binding, restored_session)

    attestation = restored["artifacts"]["continuity"]["attestation_id"]
    assert attestation.startswith("ar_") and len(attestation) == 67
    assert restored["artifacts"]["continuity"] == {
        "status": "relocated",
        "owners_relocated": 1,
        "records_relocated": 1,
        "source_home": str(source_home),
        "destination_home": str(destination_home),
        "attestation_id": attestation,
    }
    assert continuity["state"] == {"open_task": "task-a"}
    assert restored_session.continuity_generation == 1
    assert hashlib.sha256(bundle.read_bytes()).hexdigest() == bundle_sha256
    original_owner = json.loads(
        (source_artifact / "continuity" / "v1" / "OWNER.json").read_text()
    )
    assert original_owner["anchor_home"] == str(source_home)


def test_v2_checkpoint_advances_when_only_artifacts_change(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    durable = tmp_path / "durable"
    durable.mkdir()
    artifacts.mkdir()
    _database(source)
    first = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work"
    )
    (artifacts / "record.md").write_text("new\n", encoding="utf-8")
    second = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work"
    )

    assert second["action"] == "created"
    assert second["manifest"]["sha256"] == first["manifest"]["sha256"]
    assert second["manifest"]["artifacts"]["sha256"] != first["manifest"]["artifacts"]["sha256"]


def test_configured_retention_keeps_time_window_and_minimum_and_reclaims_only_unreferenced_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    durable = tmp_path / "durable"
    durable.mkdir()
    artifacts.mkdir()
    _database(source)

    class ControlledDateTime(datetime):
        current = datetime(2026, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(durability, "datetime", ControlledDateTime)
    snapshots = []
    for day in (1, 2, 3, 19, 20):
        ControlledDateTime.current = datetime(2026, 1, day, tzinfo=UTC)
        (artifacts / "record.md").write_text(f"state {day}\n", encoding="utf-8")
        snapshots.append(
            durability.snapshot_state(
                source_db=source,
                source_artifacts=artifacts,
                durable_root=durable,
                authority_id="work",
            )
        )

    ControlledDateTime.current = datetime(2026, 1, 20, 12, tzinfo=UTC)
    result = durability.snapshot_state(
        source_db=source,
        source_artifacts=artifacts,
        durable_root=durable,
        authority_id="work",
        retention_days=7,
        minimum_snapshots=2,
    )

    assert result["action"] == "reused"
    assert result["retention"] == {
        "status": "applied",
        "days": 7,
        "minimum_snapshots": 2,
        "removed_snapshots": 3,
        "removed_blobs": 3,
        "retained_snapshots": 2,
    }
    listing = durability.list_snapshots(
        durable_root=durable, authority_id="work"
    )["snapshots"]
    assert [item["snapshot_id"] for item in listing] == [
        snapshots[3]["manifest"]["snapshot_id"],
        snapshots[4]["manifest"]["snapshot_id"],
    ]
    shared_database = Path(snapshots[0]["snapshot_path"])
    assert shared_database.is_file()
    for expired in snapshots[:3]:
        assert not Path(expired["artifacts_path"]).exists()


def test_databricks_retention_deletes_expired_manifest_and_unreferenced_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    artifacts.mkdir()
    _database(source)
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)

    class ControlledDateTime(datetime):
        current = datetime(2026, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(durability, "datetime", ControlledDateTime)
    durable = "/Volumes/catalog/schema/anchor/odibi-anchor"
    for day in (1, 20):
        ControlledDateTime.current = datetime(2026, 1, day, tzinfo=UTC)
        (artifacts / "record.md").write_text(f"state {day}\n", encoding="utf-8")
        durability.snapshot_state(
            source_db=source,
            source_artifacts=artifacts,
            durable_root=durable,
            authority_id="work",
            databricks=True,
        )

    result = durability.snapshot_state(
        source_db=source,
        source_artifacts=artifacts,
        durable_root=durable,
        authority_id="work",
        databricks=True,
        retention_days=7,
        minimum_snapshots=1,
    )

    assert result["retention"]["removed_snapshots"] == 1
    assert result["retention"]["removed_blobs"] == 1
    assert len([name for name in files.files if name.endswith(".manifest.json")]) == 1
    assert len([name for name in files.files if name.endswith(".artifacts.tar")]) == 1
    assert len([name for name in files.files if name.endswith(".sqlite3")]) == 1


def test_databricks_retention_migrates_legacy_history_then_reads_constant_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    artifacts.mkdir()
    _database(source)
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    durable = "/Volumes/catalog/schema/anchor/odibi-anchor"
    for index in range(205):
        (artifacts / "record.md").write_text(f"state {index}\n", encoding="utf-8")
        durability.snapshot_state(
            source_db=source, source_artifacts=artifacts, durable_root=durable,
            authority_id="work", databricks=True,
        )

    before_migration = len(files.downloads)
    migrated = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable,
        authority_id="work", databricks=True,
        retention_days=3650, minimum_snapshots=300,
    )
    migration_downloads = files.downloads[before_migration:]
    assert sum(path.endswith(".manifest.json") for path, _, _ in migration_downloads) == 205
    assert migrated["retention"]["index"]["status"] == "migrated"

    (artifacts / "record.md").write_text("post-index state\n", encoding="utf-8")
    durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable,
        authority_id="work", databricks=True,
    )
    before_steady = len(files.downloads)
    steady = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable,
        authority_id="work", databricks=True,
        retention_days=3650, minimum_snapshots=300,
    )
    steady_downloads = files.downloads[before_steady:]

    assert steady["retention"]["index"]["status"] == "updated"
    assert sum(path.endswith(".manifest.json") for path, _, _ in steady_downloads) == 1
    assert len([name for name in files.files if name.endswith(".index.json")]) == 1


def test_databricks_retention_fails_closed_on_corrupt_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    _database(source)
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    arguments = {
        "source_db": source,
        "durable_root": "/Volumes/catalog/schema/anchor/odibi-anchor",
        "authority_id": "work",
        "databricks": True,
        "retention_days": 7,
        "minimum_snapshots": 1,
    }
    durability.snapshot_state(**arguments)
    index_name = next(name for name in files.files if name.endswith(".index.json"))
    files.files[index_name] = files.files[index_name].replace(b'"work"', b'"evil"')

    with pytest.raises(RuntimeError, match="snapshot index"):
        durability.snapshot_state(**arguments)


def test_databricks_interrupted_index_publication_recovers_from_canonical_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    _database(source)
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    arguments = {
        "source_db": source,
        "durable_root": "/Volumes/catalog/schema/anchor/odibi-anchor",
        "authority_id": "work",
        "databricks": True,
        "retention_days": 7,
        "minimum_snapshots": 1,
    }
    files.fail_index_upload = True
    with pytest.raises(OSError, match="index publication"):
        durability.snapshot_state(**arguments)
    assert len([name for name in files.files if name.endswith(".manifest.json")]) == 1
    assert not any(name.endswith(".index.json") for name in files.files)

    files.fail_index_upload = False
    recovered = durability.snapshot_state(**arguments)
    assert recovered["action"] == "reused"
    assert recovered["retention"]["index"]["status"] == "migrated"


def test_databricks_retention_reconciles_manifest_published_by_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    _database(source)
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    durable = "/Volumes/catalog/schema/anchor/odibi-anchor"
    retained = {
        "source_db": source, "durable_root": durable, "authority_id": "work",
        "databricks": True, "retention_days": 7, "minimum_snapshots": 2,
    }
    durability.snapshot_state(**retained)
    real_publish_index = durability._publish_snapshot_index
    interleaved = False

    def publish_with_concurrent_manifest(**kwargs):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            with sqlite3.connect(source) as connection:
                connection.execute("INSERT INTO example(value) VALUES ('concurrent')")
            durability.snapshot_state(
                source_db=source, durable_root=durable, authority_id="work",
                databricks=True,
            )
        return real_publish_index(**kwargs)

    monkeypatch.setattr(durability, "_publish_snapshot_index", publish_with_concurrent_manifest)
    durability.snapshot_state(**retained)
    monkeypatch.setattr(durability, "_publish_snapshot_index", real_publish_index)
    before_reconcile = len(files.downloads)
    reconciled = durability.snapshot_state(**retained)
    downloads = files.downloads[before_reconcile:]

    assert reconciled["action"] == "reused"
    assert sum(path.endswith(".manifest.json") for path, _, _ in downloads) == 1
    assert reconciled["retention"]["retained_snapshots"] == 2


@pytest.mark.parametrize(
    "days, minimum, message",
    [
        (7, None, "configured together"),
        (0, 3, "retention_days must be an integer"),
        (7, False, "minimum_snapshots must be an integer"),
    ],
)
def test_snapshot_retention_rejects_partial_or_unbounded_policy(
    tmp_path: Path, days, minimum, message
) -> None:
    source = tmp_path / "live.db"
    durable = tmp_path / "durable"
    durable.mkdir()
    _database(source)

    with pytest.raises(ValueError, match=message):
        durability.snapshot_state(
            source_db=source,
            durable_root=durable,
            authority_id="work",
            retention_days=days,
            minimum_snapshots=minimum,
        )


def test_databricks_snapshot_restore_uses_files_api_without_fuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    restored = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-projects"
    durable = "/Volumes/catalog/schema/anchor/odibi-anchor"
    _database(source)
    artifacts.mkdir()
    (artifacts / "PROJECT.md").write_text("# Project\n", encoding="utf-8")
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)

    path_methods = ("exists", "glob", "is_dir", "is_file", "is_symlink", "iterdir", "mkdir")
    for method_name in path_methods:
        original = getattr(Path, method_name)

        def reject_fuse(self, *args, _original=original, **kwargs):
            if str(self).startswith("/Volumes"):
                raise AssertionError(f"FUSE access attempted: {self}")
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method_name, reject_fuse)

    first = durability.snapshot_state(
        source_db=source,
        source_artifacts=artifacts,
        durable_root=durable,
        authority_id="work",
        databricks=True,
    )
    second = durability.snapshot_state(
        source_db=source,
        source_artifacts=artifacts,
        durable_root=durable,
        authority_id="work",
        databricks=True,
    )
    before_listing = len(files.downloads)
    listing = durability.list_snapshots(
        durable_root=durable,
        authority_id="work",
        databricks=True,
    )
    listing_downloads = files.downloads[before_listing:]
    before_restore = len(files.downloads)
    restored_result = durability.restore_latest(
        durable_root=durable,
        destination_db=restored,
        destination_artifacts=restored_artifacts,
        authority_id="work",
        databricks=True,
    )
    restore_downloads = files.downloads[before_restore:]

    assert first["action"] == "created"
    assert second["action"] == "reused"
    assert first["transport"] == restored_result["transport"] == "databricks_files_api"
    assert listing["snapshots"] == [
        {
            "created_at": first["manifest"]["created_at"],
            "format": "odibi-anchor-durable-snapshot-v2",
            "logical_digest": first["manifest"]["logical_digest"],
            "sha256": first["manifest"]["sha256"],
            "size_bytes": first["manifest"]["size_bytes"],
            "snapshot_id": first["manifest"]["snapshot_id"],
            "artifacts": first["manifest"]["artifacts"],
        }
    ]
    assert listing["next_operation"]["arguments"]["databricks"] is True
    uploaded = [path for path, _, _ in files.uploads]
    assert [path for path in uploaded if "/snapshots/" in path][-1].endswith(".manifest.json")
    assert uploaded[-1] == f"{durable}/work/AUTHORITY.json"
    assert first["authority_marker"] == "created"
    assert second["authority_marker"] == "present"
    assert all(not overwrite and not parallel for _, overwrite, parallel in files.uploads)
    assert all(not overwrite and not parallel for _, overwrite, parallel in files.downloads)
    assert [Path(path).suffixes for path, _, _ in listing_downloads] == [
        [".manifest", ".json"]
    ]
    assert sorted(Path(path).suffix for path, _, _ in restore_downloads) == [
        ".json", ".sqlite3", ".tar",
    ]
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [
            ("alpha",),
            ("beta",),
        ]
    assert (restored_artifacts / "PROJECT.md").read_text() == "# Project\n"


def test_databricks_listing_defers_payload_hash_verification_until_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    restored = tmp_path / "restored.db"
    durable = "/Volumes/catalog/schema/anchor/odibi-anchor"
    _database(source)
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    snapshot = durability.snapshot_state(
        source_db=source, durable_root=durable, authority_id="work", databricks=True,
    )
    remote_snapshot = snapshot["snapshot_path"]
    original = files.files[remote_snapshot]
    files.files[remote_snapshot] = bytes([original[0] ^ 1]) + original[1:]
    before_listing = len(files.downloads)

    listing = durability.list_snapshots(
        durable_root=durable, authority_id="work", databricks=True,
    )

    assert listing["snapshots"][0]["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert all(
        path.endswith(".manifest.json")
        for path, _, _ in files.downloads[before_listing:]
    )
    with pytest.raises(RuntimeError, match=r"snapshot (hash|size) mismatch"):
        durability.restore_latest(
            durable_root=durable, destination_db=restored,
            authority_id="work", databricks=True,
        )


def test_databricks_listing_rejects_missing_referenced_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    durable = "/Volumes/catalog/schema/anchor/odibi-anchor"
    _database(source)
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    snapshot = durability.snapshot_state(
        source_db=source, durable_root=durable, authority_id="work", databricks=True,
    )
    del files.files[snapshot["snapshot_path"]]

    with pytest.raises(RuntimeError, match="incomplete durable snapshot publication"):
        durability.list_snapshots(
            durable_root=durable, authority_id="work", databricks=True,
        )


def test_databricks_restore_downloads_only_latest_manifest_and_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    restored = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-projects"
    durable = "/Volumes/catalog/schema/anchor/odibi-anchor"
    _database(source)
    artifacts.mkdir()
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    snapshots = []
    for index in range(3):
        (artifacts / "record.md").write_text(f"state {index}\n", encoding="utf-8")
        snapshots.append(durability.snapshot_state(
            source_db=source, source_artifacts=artifacts, durable_root=durable,
            authority_id="work", databricks=True,
        ))
    before_restore = len(files.downloads)

    result = durability.restore_latest(
        durable_root=durable, destination_db=restored,
        destination_artifacts=restored_artifacts, authority_id="work", databricks=True,
    )

    downloads = files.downloads[before_restore:]
    assert result["snapshot_id"] == snapshots[-1]["manifest"]["snapshot_id"]
    assert len(downloads) == 3
    assert sum(path.endswith(".manifest.json") for path, _, _ in downloads) == 1
    assert sum(path.endswith(".sqlite3") for path, _, _ in downloads) == 1
    assert sum(path.endswith(".artifacts.tar") for path, _, _ in downloads) == 1
    assert (restored_artifacts / "record.md").read_text() == "state 2\n"


def test_databricks_manifest_failure_removes_uncommitted_remote_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "live.db"
    _database(source)
    files = FakeDatabricksFiles()
    files.fail_manifest_upload = True
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)

    with pytest.raises(OSError, match="remote manifest publication"):
        durability.snapshot_state(
            source_db=source,
            durable_root="/Volumes/catalog/schema/anchor/odibi-anchor",
            authority_id="work",
            databricks=True,
        )

    assert not any(path.endswith(".manifest.json") for path in files.files)
    assert any(path.endswith(".sqlite3") for path in files.files)


def test_databricks_missing_snapshot_root_is_typed_and_permission_errors_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    monkeypatch.setattr(
        durability,
        "_is_databricks_not_found",
        lambda error: isinstance(error, FakeDatabricksNotFound),
    )
    errors = [FakeDatabricksNotFound("absent"), PermissionError("denied")]
    monkeypatch.setattr(
        files,
        "list_directory_contents",
        lambda _path: (_ for _ in ()).throw(errors.pop(0)),
    )

    with pytest.raises(FileNotFoundError, match="snapshot root"):
        durability.list_snapshots(
            durable_root="/Volumes/catalog/schema/anchor",
            authority_id="work",
            databricks=True,
        )
    with pytest.raises(PermissionError, match="denied"):
        durability.list_snapshots(
            durable_root="/Volumes/catalog/schema/anchor",
            authority_id="work",
            databricks=True,
        )


def test_databricks_listing_rejects_non_child_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    files = FakeDatabricksFiles()
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    monkeypatch.setattr(
        files,
        "list_directory_contents",
        lambda _path: [SimpleNamespace(path="/Volumes/catalog/schema/other/fake.sqlite3")],
    )

    with pytest.raises(RuntimeError, match="invalid Databricks snapshot entry"):
        durability.list_snapshots(
            durable_root="/Volumes/catalog/schema/anchor",
            authority_id="work",
            databricks=True,
        )


def test_restore_latest_tracks_a_new_checkpoint_of_previously_seen_content(tmp_path: Path) -> None:
    source, durable, restored = tmp_path / "live.db", tmp_path / "durable", tmp_path / "restored.db"
    durable.mkdir()
    _database(source)
    original = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO example(value) VALUES ('newer')")
    changed = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    Path(source).unlink()
    original_snapshot = Path(original["snapshot_path"])
    with (
        sqlite3.connect(original_snapshot.as_uri() + "?mode=ro", uri=True) as old,
        sqlite3.connect(source) as live,
    ):
        old.backup(live)
    reverted = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    result = durability.restore_latest(durable_root=durable, destination_db=restored, authority_id="work")

    assert original["manifest"]["sha256"] != changed["manifest"]["sha256"]
    assert reverted["action"] == "created"
    assert reverted["manifest"]["sha256"] == original["manifest"]["sha256"]
    assert reverted["manifest"]["snapshot_id"] != original["manifest"]["snapshot_id"]
    assert result["snapshot_id"] == reverted["manifest"]["snapshot_id"]
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [
            ("alpha",),
            ("beta",),
        ]


@pytest.mark.parametrize("target", ["snapshot", "manifest"])
def test_tampering_fails_closed(tmp_path: Path, target: str) -> None:
    source, durable = tmp_path / "live.db", tmp_path / "durable"
    durable.mkdir()
    _database(source)
    result = durability.snapshot_state(source_db=str(source), durable_root=str(durable), authority_id="work")
    path = Path(result[f"{target}_path"])
    if target == "snapshot":
        path.write_bytes(path.read_bytes() + b"tamper")
    else:
        payload = json.loads(path.read_text())
        payload["size_bytes"] += 1
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    with pytest.raises(RuntimeError, match=r"(hash|checksum)"):
        durability.list_snapshots(durable_root=str(durable), authority_id="work")


def test_missing_referenced_snapshot_fails_closed(tmp_path: Path) -> None:
    source, durable = tmp_path / "live.db", tmp_path / "durable"
    durable.mkdir()
    _database(source)
    result = durability.snapshot_state(source_db=str(source), durable_root=str(durable), authority_id="work")
    Path(result["snapshot_path"]).unlink()
    with pytest.raises(RuntimeError, match="incomplete"):
        durability.list_snapshots(durable_root=str(durable), authority_id="work")


def test_uncommitted_orphan_snapshot_is_ignored(tmp_path: Path) -> None:
    source, durable = tmp_path / "live.db", tmp_path / "durable"
    durable.mkdir()
    _database(source)
    result = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    Path(result["manifest_path"]).unlink()

    assert durability.list_snapshots(
        durable_root=durable, authority_id="work"
    )["snapshots"] == []


def test_restore_never_overwrites(tmp_path: Path) -> None:
    source, durable, destination = tmp_path / "live.db", tmp_path / "durable", tmp_path / "existing.db"
    durable.mkdir()
    _database(source)
    durability.snapshot_state(source_db=str(source), durable_root=str(durable), authority_id="work")
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        durability.restore_latest(durable_root=str(durable), destination_db=str(destination), authority_id="work")
    assert destination.read_bytes() == b"keep"
    with pytest.raises(ValueError, match="overwrite"):
        durability.restore_latest(
            durable_root=str(durable), destination_db=str(destination), authority_id="work", overwrite=True
        )


def test_v2_restore_requires_absent_artifact_destination(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    durable = tmp_path / "durable"
    destination = tmp_path / "restored.db"
    existing = tmp_path / "existing-projects"
    durable.mkdir()
    artifacts.mkdir()
    existing.mkdir()
    (existing / "keep.md").write_text("keep\n", encoding="utf-8")
    _database(source)
    durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work"
    )
    before = _identity_and_tree(existing)

    with pytest.raises(FileExistsError, match="destination_artifacts") as caught:
        durability.restore_latest(
            durable_root=durable,
            destination_db=destination,
            destination_artifacts=existing,
            authority_id="work",
        )
    assert getattr(caught.value, "error_code", None) == "restore_destination_conflict"
    assert _identity_and_tree(existing) == before
    assert not destination.exists()


def test_v2_snapshot_rejects_symlinks_in_managed_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    durable = tmp_path / "durable"
    durable.mkdir()
    artifacts.mkdir()
    _database(source)
    (artifacts / "outside").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks"):
        durability.snapshot_state(
            source_db=source,
            source_artifacts=artifacts,
            durable_root=durable,
            authority_id="work",
        )


def test_v2_restore_rejects_traversal_archive_even_with_valid_manifest(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    durable = tmp_path / "durable"
    durable.mkdir()
    artifacts.mkdir()
    _database(source)
    result = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work"
    )
    bundle = Path(result["artifacts_path"])
    with tarfile.open(bundle, mode="w") as archive:
        info = tarfile.TarInfo("../escape.md")
        info.size = 0
        archive.addfile(info)
    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text())
    artifacts_manifest = manifest["artifacts"]
    artifacts_manifest["sha256"] = durability._sha256(bundle)
    artifacts_manifest["file"] = f"{artifacts_manifest['sha256']}.artifacts.tar"
    replacement = bundle.with_name(artifacts_manifest["file"])
    bundle.rename(replacement)
    artifacts_manifest["size_bytes"] = replacement.stat().st_size
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(
        durability._canonical_bytes(body)
    ).hexdigest()
    manifest_path.write_bytes(durability._canonical_bytes(manifest))

    with pytest.raises(RuntimeError, match="invalid artifact archive entry"):
        durability.restore_latest(
            durable_root=durable,
            destination_db=tmp_path / "restored.db",
            destination_artifacts=tmp_path / "restored-projects",
            authority_id="work",
        )
    assert not (tmp_path / "escape.md").exists()


def test_database_authority_cannot_be_reused_across_work_authorities(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    durable = tmp_path / "durable"
    durable.mkdir()
    _database(database)
    initialized = durability.ensure_database_authority(
        database, authority_id="authority-a", trust_domain="work", initialize=True
    )

    assert initialized["status"] == "initialized"
    assert (
        durability.ensure_database_authority(database, authority_id="authority-a", trust_domain="work")["status"]
        == "verified"
    )
    with pytest.raises(RuntimeError, match="authority identity conflicts"):
        durability.ensure_database_authority(database, authority_id="authority-b", trust_domain="work")
    with pytest.raises(RuntimeError, match="authority identity conflicts"):
        durability.snapshot_state(
            source_db=database,
            durable_root=durable,
            authority_id="authority-b",
        )


def test_unsafe_paths_are_rejected(tmp_path: Path) -> None:
    durable = tmp_path / "durable"
    databricks_durable = "/Volumes/catalog/schema/volume/anchor"
    durable.mkdir()
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            source_db="/Volumes/catalog/schema/live.db",
            durable_root=databricks_durable,
            databricks=True,
            authority_id="work",
        )
    with pytest.raises(ValueError, match="overlap"):
        durability.qualify_paths(source_db=str(durable / "live.db"), durable_root=str(durable), authority_id="work")
    with pytest.raises(ValueError, match="line breaks"):
        durability.qualify_paths(source_db=str(tmp_path / "bad\n.db"), durable_root=str(durable), authority_id="work")
    with pytest.raises(ValueError, match="absolute"):
        durability.qualify_paths(source_db="relative.db", durable_root=str(durable), authority_id="work")
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            source_db="/tmp/../Volumes/catalog/schema/live.db",
            durable_root=databricks_durable,
            databricks=True,
            authority_id="work",
        )
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            destination_db="/tmp/../Workspace/Users/owner/live.db",
            durable_root=databricks_durable,
            databricks=True,
            authority_id="work",
        )
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            source_db="//Volumes/catalog/schema/live.db",
            durable_root=databricks_durable,
            databricks=True,
            authority_id="work",
        )
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            destination_db="//Workspace/Users/owner/live.db",
            durable_root=databricks_durable,
            databricks=True,
            authority_id="work",
        )


def test_changed_checkpoint_advances_past_equal_or_rolled_back_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, durable = tmp_path / "live.db", tmp_path / "durable"
    durable.mkdir()
    _database(source)
    first = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO example(value) VALUES ('changed')")
    frozen = datetime.fromisoformat(first["manifest"]["created_at"].replace("Z", "+00:00"))

    class RolledBackDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(durability, "datetime", RolledBackDateTime)
    second = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")

    assert second["action"] == "created"
    assert second["manifest"]["created_at"] > first["manifest"]["created_at"]
    assert (
        durability.list_snapshots(durable_root=durable, authority_id="work")["snapshots"][-1]["snapshot_id"]
        == second["manifest"]["snapshot_id"]
    )


def test_symlink_path_is_rejected(tmp_path: Path) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        durability.qualify_paths(source_db=str(link / "live.db"), durable_root=str(durable), authority_id="work")


def test_manifest_is_published_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, durable = tmp_path / "live.db", tmp_path / "durable"
    durable.mkdir()
    _database(source)
    real_publish = durability._publish_file_exclusive
    calls = 0

    def fail_second(source_path: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated manifest publication failure")
        real_publish(source_path, destination)

    monkeypatch.setattr(durability, "_publish_file_exclusive", fail_second)
    with pytest.raises(OSError, match="manifest publication"):
        durability.snapshot_state(source_db=str(source), durable_root=str(durable), authority_id="work")
    snapshots = durable / "work" / "snapshots"
    assert list(snapshots.glob("*.sqlite3"))
    assert not list(snapshots.glob("*.manifest.json"))
    assert durability.list_snapshots(durable_root=str(durable), authority_id="work")["snapshots"] == []


def _v2_lineage(tmp_path: Path) -> tuple[Path, Path, dict]:
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    durable = tmp_path / "durable"
    durable.mkdir()
    (artifacts / "alpha" / "problems").mkdir(parents=True)
    (artifacts / "alpha" / "problems" / "P-1.md").write_text("# Evidence\n", encoding="utf-8")
    (artifacts / "alpha" / "problems" / "P-2.md").write_text("# Second\n", encoding="utf-8")
    (artifacts / "beta").mkdir()
    (artifacts / "beta" / "notes.md").write_text("beta\n", encoding="utf-8")
    _database(source)
    snapshot = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work",
    )
    return durable, artifacts, snapshot


def _before_copy(monkeypatch: pytest.MonkeyPatch, hook) -> None:
    from odibi_anchor._dispatcher import _session

    original = _session._relocate_restored_continuity

    def relocate_then_hook(staged, destination):
        result = original(staged, destination)
        hook(Path(destination))
        return result

    monkeypatch.setattr(_session, "_relocate_restored_continuity", relocate_then_hook)


def test_restore_never_deletes_destination_created_by_competing_writer_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, _artifacts, _snapshot = _v2_lineage(tmp_path)
    destination_db = tmp_path / "restored" / "restored.db"
    destination_artifacts = tmp_path / "restored" / "projects"
    destination_db.parent.mkdir()

    def competing_writer(destination: Path) -> None:
        destination.mkdir()
        (destination / "sentinel.txt").write_text("competitor\n", encoding="utf-8")

    _before_copy(monkeypatch, competing_writer)

    with pytest.raises(FileExistsError) as caught:
        durability.restore_latest(
            durable_root=durable,
            destination_db=destination_db,
            destination_artifacts=destination_artifacts,
            authority_id="work",
        )

    assert (destination_artifacts / "sentinel.txt").read_text() == "competitor\n"
    assert getattr(caught.value, "error_code", None) == "restore_destination_conflict"
    assert sorted(path.name for path in destination_artifacts.iterdir()) == ["sentinel.txt"]
    assert not destination_db.exists()


def _identity_and_tree(path: Path) -> tuple:
    metadata = path.lstat()
    return (
        (metadata.st_dev, metadata.st_ino),
        sorted(
            (item.relative_to(path).as_posix(), item.read_bytes() if item.is_file() else None)
            for item in path.rglob("*")
        ),
    )


def _fail_during_copy(monkeypatch: pytest.MonkeyPatch, *, after_files: int, action=None) -> None:
    """Fail inside the owned copy after `after_files` files were written."""
    armed: dict[str, Any] = {"copies": None}
    real_copy = durability.shutil.copyfileobj

    def arm(destination: Path) -> None:
        armed["copies"] = 0
        armed["destination"] = destination

    def copy(incoming, outgoing, *args, **kwargs):
        if armed["copies"] is not None:
            if armed["copies"] == after_files:
                if action is not None:
                    action(armed["destination"])
                raise OSError("simulated copy failure")
            armed["copies"] += 1
        return real_copy(incoming, outgoing, *args, **kwargs)

    _before_copy(monkeypatch, arm)
    monkeypatch.setattr(durability.shutil, "copyfileobj", copy)


def _restore_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    parent = tmp_path / "local"
    parent.mkdir()
    neighbor = parent / "neighbor"
    neighbor.mkdir()
    (neighbor / "keep.md").write_text("neighbor\n", encoding="utf-8")
    return parent / "restored.db", parent / "projects", neighbor


def test_owned_partial_restore_cleanup_removes_only_its_own_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, _artifacts, _snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, neighbor = _restore_paths(tmp_path)
    _fail_during_copy(monkeypatch, after_files=1)

    with pytest.raises(OSError, match="simulated copy failure"):
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )

    assert not destination_artifacts.exists()
    assert not destination_db.exists()
    assert sorted(path.name for path in destination_db.parent.iterdir()) == ["neighbor"]
    assert (neighbor / "keep.md").read_text() == "neighbor\n"


def test_proof_failure_cleanup_preserves_foreign_entries_and_records_incomplete_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from odibi_anchor.codebase import _workflow_artifact_restore

    durable, _artifacts, _snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)

    def foreign_entry_then_fail(_path, *, projects, manifest):
        (projects / "foreign.md").write_text("foreign\n", encoding="utf-8")
        raise RuntimeError("simulated proof failure")

    monkeypatch.setattr(
        _workflow_artifact_restore, "append_verified_restores", foreign_entry_then_fail,
    )

    with pytest.raises(RuntimeError, match="restore is incomplete") as caught:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )

    assert getattr(caught.value, "error_code", None) == "restore_incomplete"
    assert [item["operation"] for item in cast(Any, caught.value).next_operations] == [
        "abandon_restore",
    ]
    assert sorted(path.name for path in destination_artifacts.iterdir()) == [
        ".odibi-anchor-restore-owner.json", "foreign.md",
    ]
    assert not destination_db.exists()


@pytest.mark.parametrize("substitution", ["replaced_directory", "symlink"])
def test_destination_substitution_cannot_redirect_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, substitution: str,
) -> None:
    durable, _artifacts, _snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)
    victim = tmp_path / "victim"
    moved = tmp_path / "moved-owned"

    def substitute(destination: Path) -> None:
        destination.rename(moved)
        (victim / "alpha" / "problems").mkdir(parents=True)
        (victim / "alpha" / "problems" / "P-1.md").write_text("victim\n", encoding="utf-8")
        (victim / ".odibi-anchor-restore-owner.json").write_bytes(
            (moved / ".odibi-anchor-restore-owner.json").read_bytes()
        )
        if substitution == "symlink":
            destination.symlink_to(victim, target_is_directory=True)
        else:
            victim.rename(destination)

    _fail_during_copy(monkeypatch, after_files=1, action=substitute)
    with pytest.raises(FileExistsError, match="ownership marker changed") as caught:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )

    assert getattr(caught.value, "error_code", None) == "restore_destination_conflict"
    victim_root = victim if substitution == "symlink" else destination_artifacts
    assert (victim_root / "alpha" / "problems" / "P-1.md").read_text() == "victim\n"
    assert (moved / ".odibi-anchor-restore-owner.json").is_file()
    assert any(path.is_file() for path in moved.rglob("*.md"))
    assert not destination_db.exists()


def test_two_competing_restores_have_one_owner_and_the_loser_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, _artifacts, snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)
    winner: dict = {}

    def competing_restore(_destination: Path) -> None:
        if not winner:
            winner["result"] = None
            winner["result"] = durability.restore_latest(
                durable_root=durable, destination_db=destination_db,
                destination_artifacts=destination_artifacts, authority_id="work",
            )

    _before_copy(monkeypatch, competing_restore)
    with pytest.raises(FileExistsError) as caught:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )

    assert getattr(caught.value, "error_code", None) == "restore_destination_conflict"
    assert winner["result"]["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert (destination_artifacts / "alpha" / "problems" / "P-1.md").read_text() == "# Evidence\n"
    assert not (destination_artifacts / ".odibi-anchor-restore-owner.json").exists()
    assert durability.ensure_database_authority(
        destination_db, authority_id="work", trust_domain="work",
    )["status"] == "verified"


def _incomplete_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    durable, artifacts, snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)
    real_publish = durability._publish_file_exclusive

    def fail_database(source: Path, destination: Path) -> None:
        if destination == destination_db:
            raise OSError("simulated database publication failure")
        real_publish(source, destination)

    monkeypatch.setattr(durability, "_publish_file_exclusive", fail_database)
    with pytest.raises(RuntimeError, match="restore is incomplete") as caught:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )
    monkeypatch.setattr(durability, "_publish_file_exclusive", real_publish)
    return caught.value, durable, artifacts, snapshot, destination_db, destination_artifacts


def test_database_publication_failure_records_incomplete_restore_then_resume_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error, durable, artifacts, snapshot, destination_db, destination_artifacts = (
        _incomplete_restore(tmp_path, monkeypatch)
    )

    operations = {item["operation"]: item for item in error.next_operations}
    assert error.error_code == "restore_incomplete"
    assert error.context["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert operations["resume_restore"]["copy_ready"].startswith("anchor state resume ")
    assert operations["abandon_restore"]["copy_ready"].startswith("anchor state abandon ")
    assert not destination_db.exists()
    assert (destination_artifacts / ".odibi-anchor-restore-owner.json").is_file()
    with pytest.raises(RuntimeError, match="restore is incomplete") as again:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )
    assert cast(Any, again.value).error_code == "restore_incomplete"
    (destination_artifacts / "beta" / "notes.md").unlink()

    resumed = durability.resume_restore(**operations["resume_restore"]["arguments"])

    assert resumed["action"] == "resumed"
    assert resumed["classification"] == "restored"
    assert resumed["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert not (destination_artifacts / ".odibi-anchor-restore-owner.json").exists()
    assert _identity_and_tree(destination_artifacts)[1] == _identity_and_tree(artifacts)[1]
    with sqlite3.connect(destination_db) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [
            ("alpha",), ("beta",),
        ]


def test_resume_refuses_tampered_incomplete_tree_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error, _durable, _artifacts, _snapshot, destination_db, destination_artifacts = (
        _incomplete_restore(tmp_path, monkeypatch)
    )
    tampered = destination_artifacts / "beta" / "notes.md"
    tampered.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="restore is incomplete") as caught:
        durability.resume_restore(**error.next_operations[0]["arguments"])

    assert cast(Any, caught.value).error_code == "restore_incomplete"
    assert tampered.read_text() == "tampered\n"
    assert (destination_artifacts / ".odibi-anchor-restore-owner.json").is_file()
    assert not destination_db.exists()


@pytest.mark.parametrize("names", [
    ["a", "a/child"], ["a/child", "a"],
    ["a/child/", "a"], ["a", "a/child/"],
    ["a", "a"], ["a/", "a/"],
])
def test_artifact_archive_collisions_refused_before_extraction(tmp_path, names):
    bundle = tmp_path / "collision.tar"
    with tarfile.open(bundle, "w") as archive:
        for name in names:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE if name.endswith("/") else tarfile.REGTYPE
            archive.addfile(info)
    destination = tmp_path / "out"
    with pytest.raises(RuntimeError, match=r"collision|duplicate"):
        durability._extract_artifact_bundle(bundle, destination)
    assert not destination.exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_artifact_special_entries_refused(tmp_path, kind):
    bundle = tmp_path / "special.tar"
    with tarfile.open(bundle, "w") as archive:
        info = tarfile.TarInfo("special")
        info.type = kind
        info.linkname = "outside"
        archive.addfile(info)
    with pytest.raises(RuntimeError, match="special entry"):
        durability._extract_artifact_bundle(bundle, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("limit", ["_MAX_ARTIFACT_MEMBERS", "_MAX_ARTIFACT_BYTES"])
def test_artifact_limits_refuse_before_creation(tmp_path, monkeypatch, limit):
    import io

    bundle = tmp_path / "limits.tar"
    with tarfile.open(bundle, "w") as archive:
        for name in ("first", "second"):
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    monkeypatch.setattr(durability, limit, 1)
    with pytest.raises(RuntimeError, match="extraction limits"):
        durability._extract_artifact_bundle(bundle, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_artifact_directories_may_follow_children_and_destination_is_exclusive(tmp_path):
    bundle = tmp_path / "ordered.tar"
    with tarfile.open(bundle, "w") as archive:
        for name in ("a/b/file", "a/b/", "a/"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE if name.endswith("/") else tarfile.REGTYPE
            archive.addfile(info)
    destination = tmp_path / "out"
    assert durability._extract_artifact_bundle(bundle, destination) == {
        "file_count": 1, "directory_count": 2, "content_size_bytes": 0,
    }
    assert (destination / "a/b/file").read_bytes() == b""
    (destination / "a/b/file").write_text("keep")
    with pytest.raises(FileExistsError):
        durability._extract_artifact_bundle(bundle, destination)
    assert (destination / "a/b/file").read_text() == "keep"


def test_artifact_extraction_ancestor_work_scales_linearly(tmp_path, monkeypatch):
    # Count path ancestor enumeration, not machine speed. The old all-prior-members
    # collision scan performs n*(n-1)/2 extra enumerations at fixed path depth.
    original = durability.PurePosixPath.parents
    counts = []
    for count in (80, 160):
        bundle = tmp_path / f"{count}.tar"
        with tarfile.open(bundle, "w") as archive:
            for index in range(count):
                archive.addfile(tarfile.TarInfo(f"project/data/{index}.txt"))
        calls = 0

        def parents(path):
            nonlocal calls
            calls += 1
            return original.__get__(path)

        with monkeypatch.context() as patch:
            patch.setattr(durability.PurePosixPath, "parents", property(parents))
            result = durability._extract_artifact_bundle(bundle, tmp_path / f"out-{count}")
        assert result["file_count"] == count
        counts.append(calls)
    assert counts[1] <= counts[0] * 2 + 8, counts


def test_abandon_quarantines_incomplete_restore_and_allows_fresh_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error, _durable, artifacts, _snapshot, destination_db, destination_artifacts = (
        _incomplete_restore(tmp_path, monkeypatch)
    )
    incomplete_tree = _identity_and_tree(destination_artifacts)

    abandoned = durability.abandon_restore(**error.next_operations[1]["arguments"])

    quarantine = Path(abandoned["quarantine_path"])
    assert abandoned["action"] == "quarantined"
    assert not destination_artifacts.exists()
    assert quarantine.parent.parent == destination_artifacts.parent
    assert _identity_and_tree(quarantine) == incomplete_tree
    restored = durability.restore_latest(**abandoned["next_operation"]["arguments"])
    assert restored["classification"] == "restored"
    assert _identity_and_tree(destination_artifacts)[1] == _identity_and_tree(artifacts)[1]
    assert destination_db.is_file()


@pytest.mark.parametrize("operation", ["resume_restore", "abandon_restore"])
def test_restore_recovery_refuses_when_ownership_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    error, _durable, _artifacts, _snapshot, destination_db, destination_artifacts = (
        _incomplete_restore(tmp_path, monkeypatch)
    )
    arguments = error.next_operations[0]["arguments"]
    with pytest.raises(FileExistsError, match="does not match the requested authority_id") as mismatch:
        getattr(durability, operation)(**{**arguments, "authority_id": "other"})
    assert cast(Any, mismatch.value).error_code == "restore_destination_conflict"
    impostor = destination_artifacts.parent / "impostor"
    destination_artifacts.rename(destination_artifacts.parent / "original")
    impostor.mkdir()
    (impostor / ".odibi-anchor-restore-owner.json").write_bytes(
        (destination_artifacts.parent / "original" / ".odibi-anchor-restore-owner.json").read_bytes()
    )
    impostor.rename(destination_artifacts)

    with pytest.raises(FileExistsError, match="no incomplete restore owned by Anchor") as caught:
        getattr(durability, operation)(**arguments)

    assert cast(Any, caught.value).error_code == "restore_destination_conflict"
    assert sorted(path.name for path in destination_artifacts.iterdir()) == [
        ".odibi-anchor-restore-owner.json",
    ]
    assert not destination_db.exists()


def test_restore_classifies_local_first_use_missing_root_and_missing_lineage(tmp_path: Path) -> None:
    durable = tmp_path / "durable"
    destination = tmp_path / "restored.db"
    with pytest.raises(FileNotFoundError) as unavailable:
        durability.restore_latest(durable_root=durable, destination_db=destination, authority_id="work")
    assert cast(Any, unavailable.value).error_code == "durable_root_unavailable"

    durable.mkdir()
    with pytest.raises(durability.DurableSnapshotUnavailable) as first_use:
        durability.restore_latest(durable_root=durable, destination_db=destination, authority_id="work")
    assert cast(Any, first_use.value).classification == "no_lineage"

    source = tmp_path / "live.db"
    _database(source)
    published = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    assert published["authority_marker"] == "created"
    assert json.loads((durable / "work" / "AUTHORITY.json").read_text()) == {
        "authority_id": "work", "format": "odibi-anchor-durable-authority-v1",
    }
    Path(published["manifest_path"]).unlink()
    with pytest.raises(RuntimeError, match="durable lineage is missing") as missing:
        durability.restore_latest(durable_root=durable, destination_db=destination, authority_id="work")
    assert cast(Any, missing.value).error_code == "durable_lineage_missing"
    assert cast(Any, missing.value).next_operations[0]["copy_ready"].startswith("anchor state list ")
    assert not destination.exists()


@pytest.mark.parametrize("refusal", [errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EXDEV])
def test_snapshot_publishes_authority_marker_where_hard_links_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: int,
) -> None:
    """SMB/NAS/FUSE durable roots refuse link(); snapshots must keep working there."""
    durable = tmp_path / "durable"
    durable.mkdir()
    source = tmp_path / "live.db"
    _database(source)

    def refuse_hard_links(*_args, **_kwargs):
        raise OSError(refusal, "hard links refused")

    monkeypatch.setattr(durability.os, "link", refuse_hard_links)
    first = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    marker = durable / "work" / "AUTHORITY.json"
    assert first["authority_marker"] == "created"
    assert json.loads(marker.read_text()) == {
        "authority_id": "work", "format": "odibi-anchor-durable-authority-v1",
    }
    second = durability.snapshot_state(source_db=source, durable_root=durable, authority_id="work")
    assert second["authority_marker"] == "present"
    assert not [path.name for path in marker.parent.iterdir() if path.name.startswith(".AUTHORITY")]


def test_existing_lineage_without_marker_restores_and_backfills_marker(tmp_path: Path) -> None:
    durable, artifacts, snapshot = _v2_lineage(tmp_path)
    marker = durable / "work" / "AUTHORITY.json"
    marker.unlink()
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)

    restored = durability.restore_latest(
        durable_root=durable, destination_db=destination_db,
        destination_artifacts=destination_artifacts, authority_id="work",
    )
    assert restored["classification"] == "restored"
    assert restored["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert not marker.exists()

    again = durability.snapshot_state(
        source_db=tmp_path / "live.db", source_artifacts=artifacts,
        durable_root=durable, authority_id="work",
    )
    assert again["action"] == "reused"
    assert again["authority_marker"] == "created"
    assert marker.is_file()


def _strict_databricks(monkeypatch: pytest.MonkeyPatch) -> FakeDatabricksFiles:
    files = FakeDatabricksFiles()
    files.strict_listing = True
    monkeypatch.setattr(durability, "_databricks_files_api", lambda: files)
    monkeypatch.setattr(
        durability, "_is_databricks_not_found", lambda error: isinstance(error, FakeDatabricksNotFound),
    )
    return files


def test_databricks_restore_confirms_volume_root_before_first_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = "/Volumes/catalog/schema/anchor"
    destination = tmp_path / "restored.db"
    files = _strict_databricks(monkeypatch)

    def restore():
        return durability.restore_latest(
            durable_root=durable, destination_db=destination, authority_id="work", databricks=True,
        )

    files.missing_roots.add(durable)
    with pytest.raises(FileNotFoundError, match=r"reports .* not found") as missing_root:
        restore()
    assert cast(Any, missing_root.value).error_code == "durable_root_unavailable"
    with pytest.raises(FileNotFoundError, match=r"reports Volume /Volumes/catalog/schema/anchor not"):
        durability.restore_latest(
            durable_root=f"{durable}/never-created", destination_db=destination,
            authority_id="work", databricks=True,
        )

    files.missing_roots.clear()
    files.root_error = PermissionError("denied")
    with pytest.raises(RuntimeError, match=r"could not confirm .*PermissionError: denied") as unreachable:
        restore()
    assert cast(Any, unreachable.value).error_code == "durable_root_unavailable"
    assert isinstance(unreachable.value.__cause__, PermissionError)

    files.root_error = None
    with pytest.raises(durability.DurableSnapshotUnavailable) as first_use:
        restore()
    assert cast(Any, first_use.value).classification == "no_lineage"
    with pytest.raises(durability.DurableSnapshotUnavailable):
        durability.restore_latest(
            durable_root=f"{durable}/never-created", destination_db=destination,
            authority_id="work", databricks=True,
        )

    files.files[f"{durable}/work/AUTHORITY.json"] = b"{}"
    with pytest.raises(RuntimeError, match="durable lineage is missing") as lineage:
        restore()
    assert cast(Any, lineage.value).error_code == "durable_lineage_missing"
    assert not destination.exists()


@pytest.mark.parametrize("listing_order", ["native", "reversed"])
def test_parent_substitution_during_copy_cannot_redirect_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing_order: str,
) -> None:
    if listing_order == "reversed":
        # Directory listing order is filesystem-dependent (it differed on GitHub runners);
        # restore must visit artifacts in the same order regardless.
        real_walk = durability.os.walk

        def reversed_walk(top, *args, **kwargs):
            for directory, directories, files in real_walk(top, *args, **kwargs):
                directories.reverse()
                files.reverse()
                yield directory, directories, files

        monkeypatch.setattr(durability.os, "walk", reversed_walk)
    durable, _artifacts, _snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    real_copy = durability.shutil.copyfileobj
    state = {"armed": False, "copies": 0}

    def copy(incoming, outgoing, *args, **kwargs):
        result = real_copy(incoming, outgoing, *args, **kwargs)
        if state["armed"]:
            state["copies"] += 1
            if state["copies"] == 1:
                problems = destination_artifacts / "alpha" / "problems"
                problems.rename(tmp_path / "moved-problems")
                problems.symlink_to(victim, target_is_directory=True)
        return result

    _before_copy(monkeypatch, lambda _destination: state.update(armed=True))
    monkeypatch.setattr(durability.shutil, "copyfileobj", copy)

    with pytest.raises(RuntimeError, match="restore is incomplete") as caught:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )

    assert cast(Any, caught.value).error_code == "restore_incomplete"
    assert "changed during copy" in cast(Any, caught.value).context["observed"]
    assert list(victim.iterdir()) == []
    assert (tmp_path / "moved-problems" / "P-1.md").read_text() == "# Evidence\n"
    assert not destination_db.exists()


def test_resume_uses_the_recorded_snapshot_not_a_newer_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error, durable, artifacts, snapshot, _destination_db, destination_artifacts = (
        _incomplete_restore(tmp_path, monkeypatch)
    )
    recorded_tree = _identity_and_tree(artifacts)[1]
    (artifacts / "beta" / "notes.md").write_text("newer\n", encoding="utf-8")
    newer = durability.snapshot_state(
        source_db=tmp_path / "live.db", source_artifacts=artifacts,
        durable_root=durable, authority_id="work",
    )
    assert newer["manifest"]["snapshot_id"] != snapshot["manifest"]["snapshot_id"]

    resumed = durability.resume_restore(**error.next_operations[0]["arguments"])

    assert resumed["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert _identity_and_tree(destination_artifacts)[1] == recorded_tree


def test_resume_offers_only_abandon_when_recorded_manifest_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error, durable, _artifacts, snapshot, destination_db, destination_artifacts = (
        _incomplete_restore(tmp_path, monkeypatch)
    )
    (durable / "work" / "snapshots" / f"{snapshot['manifest']['snapshot_id']}.manifest.json").unlink()

    with pytest.raises(RuntimeError, match="recorded snapshot manifest cannot be verified") as caught:
        durability.resume_restore(**error.next_operations[0]["arguments"])

    assert [item["operation"] for item in cast(Any, caught.value).next_operations] == [
        "abandon_restore",
    ]
    assert (destination_artifacts / ".odibi-anchor-restore-owner.json").is_file()
    assert not destination_db.exists()


def test_marker_left_after_database_publication_is_finalized_by_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, artifacts, snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)
    marker = destination_artifacts / ".odibi-anchor-restore-owner.json"
    real_unlink = durability.os.unlink

    def crash_before_marker_removal(path, *args, **kwargs):
        if Path(path) == marker and destination_db.exists():
            raise KeyboardInterrupt("simulated crash after database publication")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(durability.os, "unlink", crash_before_marker_removal)
    with pytest.raises(KeyboardInterrupt):
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )
    monkeypatch.setattr(durability.os, "unlink", real_unlink)
    assert destination_db.is_file() and marker.is_file()

    with pytest.raises(RuntimeError, match="ownership marker was not removed") as caught:
        durability.snapshot_state(
            source_db=destination_db, source_artifacts=destination_artifacts,
            durable_root=durable, authority_id="work",
        )
    operations = {item["operation"]: item for item in cast(Any, caught.value).next_operations}
    with pytest.raises(FileExistsError, match="resume finalizes it"):
        durability.abandon_restore(**operations["abandon_restore"]["arguments"])

    finalized = durability.resume_restore(**operations["resume_restore"]["arguments"])

    assert finalized["action"] == "finalized"
    assert finalized["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    with sqlite3.connect(destination_db) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [
            ("alpha",), ("beta",),
        ]
    assert not marker.exists()
    assert _identity_and_tree(destination_artifacts)[1] == _identity_and_tree(artifacts)[1]


def test_foreign_database_blocks_resume_but_abandon_preserves_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, _artifacts, _snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)

    def competitor_wins_database(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"competitor database")
        raise FileExistsError(str(destination))

    monkeypatch.setattr(durability, "_publish_file_exclusive", competitor_wins_database)
    with pytest.raises(RuntimeError, match="restore is incomplete") as caught:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )
    operations = {item["operation"]: item for item in cast(Any, caught.value).next_operations}

    with pytest.raises(FileExistsError, match="not the database this restore published") as refused:
        durability.resume_restore(**operations["resume_restore"]["arguments"])
    assert cast(Any, refused.value).error_code == "restore_destination_conflict"
    abandoned = durability.abandon_restore(**operations["abandon_restore"]["arguments"])

    assert destination_db.read_bytes() == b"competitor database"
    assert not destination_artifacts.exists()
    assert (Path(abandoned["quarantine_path"]) / ".odibi-anchor-restore-owner.json").is_file()


def test_failed_ownership_marker_write_leaves_no_claimed_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, _artifacts, _snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, neighbor = _restore_paths(tmp_path)
    real_fsync = durability.os.fsync
    armed = {"on": False}

    def fail_marker_fsync(descriptor):
        if armed["on"]:
            raise OSError("simulated marker write failure")
        return real_fsync(descriptor)

    _before_copy(monkeypatch, lambda _destination: armed.update(on=True))
    monkeypatch.setattr(durability.os, "fsync", fail_marker_fsync)

    with pytest.raises(OSError, match="simulated marker write failure"):
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )

    assert not destination_artifacts.exists()
    assert not destination_db.exists()
    assert (neighbor / "keep.md").read_text() == "neighbor\n"


def test_copied_database_left_with_marker_is_finalized_but_a_partial_copy_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable, _artifacts, snapshot = _v2_lineage(tmp_path)
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)
    marker = destination_artifacts / ".odibi-anchor-restore-owner.json"
    real_link, real_unlink = durability.os.link, durability.os.unlink

    def refuse_hard_links(source, destination, *args, **kwargs):
        if Path(destination) == destination_db:
            raise PermissionError(1, "hard links refused")
        return real_link(source, destination, *args, **kwargs)

    def crash_before_marker_removal(path, *args, **kwargs):
        if Path(path) == marker and destination_db.exists():
            raise KeyboardInterrupt("simulated crash after copied publication")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(durability.os, "link", refuse_hard_links)
    monkeypatch.setattr(durability.os, "unlink", crash_before_marker_removal)
    with pytest.raises(KeyboardInterrupt):
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work",
        )
    monkeypatch.setattr(durability.os, "unlink", real_unlink)
    arguments = {
        "durable_root": durable, "authority_id": "work", "destination_db": destination_db,
        "destination_artifacts": destination_artifacts,
    }
    published = destination_db.read_bytes()
    destination_db.write_bytes(published[: len(published) // 2])

    with pytest.raises(FileExistsError, match="not the database this restore published"):
        durability.resume_restore(**arguments)
    destination_db.write_bytes(published)

    finalized = durability.resume_restore(**arguments)

    assert finalized["action"] == "finalized"
    assert finalized["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert not marker.exists()


def test_databricks_resume_maps_lost_lineage_to_abandon_and_lets_transport_errors_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _strict_databricks(monkeypatch)
    durable = "/Volumes/catalog/schema/anchor"
    source = tmp_path / "live.db"
    artifacts = tmp_path / "projects"
    (artifacts / "alpha").mkdir(parents=True)
    (artifacts / "alpha" / "PROJECT.md").write_text("# Project\n", encoding="utf-8")
    _database(source)
    durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable,
        authority_id="work", databricks=True,
    )
    destination_db, destination_artifacts, _neighbor = _restore_paths(tmp_path)
    real_publish = durability._publish_file_exclusive

    def fail_database(source_path: Path, destination: Path) -> None:
        if destination == destination_db:
            raise OSError("simulated database publication failure")
        real_publish(source_path, destination)

    monkeypatch.setattr(durability, "_publish_file_exclusive", fail_database)
    with pytest.raises(RuntimeError, match="restore is incomplete") as caught:
        durability.restore_latest(
            durable_root=durable, destination_db=destination_db,
            destination_artifacts=destination_artifacts, authority_id="work", databricks=True,
        )
    monkeypatch.setattr(durability, "_publish_file_exclusive", real_publish)
    arguments = cast(Any, caught.value).next_operations[0]["arguments"]
    real_download = files.download_to

    def transient_download(path, destination, **kwargs):
        raise ConnectionError("simulated transient Files API failure")

    monkeypatch.setattr(files, "download_to", transient_download)
    with pytest.raises(ConnectionError, match="transient"):
        durability.resume_restore(**arguments)
    monkeypatch.setattr(files, "download_to", real_download)

    for name in [name for name in files.files if "/snapshots/" in name]:
        del files.files[name]
    with pytest.raises(RuntimeError, match="recorded snapshot manifest cannot be verified") as lost:
        durability.resume_restore(**arguments)

    assert [item["operation"] for item in cast(Any, lost.value).next_operations] == [
        "abandon_restore",
    ]
    assert (destination_artifacts / ".odibi-anchor-restore-owner.json").is_file()
    assert not destination_db.exists()
