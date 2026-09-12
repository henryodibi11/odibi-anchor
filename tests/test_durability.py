from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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

    def create_directory(self, path: str) -> None:
        self.directories.add(path)

    def list_directory_contents(self, path: str):
        prefix = path.rstrip("/") + "/"
        return [
            SimpleNamespace(path=name)
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
    restored = durability.restore_latest(
        durable_root=durable,
        destination_db=restored_db,
        destination_artifacts=restored_artifacts,
        authority_id="work",
    )

    assert first["manifest"]["format"] == "odibi-anchor-durable-snapshot-v2"
    assert first["manifest"]["artifacts"]["file_count"] == 1
    assert second["action"] == "reused"
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

    assert restored["artifacts"]["continuity"] == {
        "status": "relocated",
        "owners_relocated": 1,
        "records_relocated": 1,
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
    listing = durability.list_snapshots(
        durable_root=durable,
        authority_id="work",
        databricks=True,
    )
    restored_result = durability.restore_latest(
        durable_root=durable,
        destination_db=restored,
        destination_artifacts=restored_artifacts,
        authority_id="work",
        databricks=True,
    )

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
    assert [path for path, _, _ in files.uploads][-1].endswith(".manifest.json")
    assert all(not overwrite and not parallel for _, overwrite, parallel in files.uploads)
    assert all(not overwrite and not parallel for _, overwrite, parallel in files.downloads)
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [
            ("alpha",),
            ("beta",),
        ]
    assert (restored_artifacts / "PROJECT.md").read_text() == "# Project\n"


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
    _database(source)
    durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work"
    )

    with pytest.raises(FileExistsError, match="destination_artifacts"):
        durability.restore_latest(
            durable_root=durable,
            destination_db=destination,
            destination_artifacts=existing,
            authority_id="work",
        )
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
