from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from odibi_anchor import durability


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO example(value) VALUES (?)", [("alpha",), ("beta",)])


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


def test_restore_latest_tracks_a_new_checkpoint_of_previously_seen_content(tmp_path: Path) -> None:
    source, durable, restored = tmp_path / "live.db", tmp_path / "durable", tmp_path / "restored.db"
    durable.mkdir()
    _database(source)
    original = durability.snapshot_state(
        source_db=source, durable_root=durable, authority_id="work"
    )
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO example(value) VALUES ('newer')")
    changed = durability.snapshot_state(
        source_db=source, durable_root=durable, authority_id="work"
    )
    Path(source).unlink()
    original_snapshot = Path(original["snapshot_path"])
    with (
        sqlite3.connect(original_snapshot.as_uri() + "?mode=ro", uri=True) as old,
        sqlite3.connect(source) as live,
    ):
        old.backup(live)
    reverted = durability.snapshot_state(
        source_db=source, durable_root=durable, authority_id="work"
    )
    result = durability.restore_latest(
        durable_root=durable, destination_db=restored, authority_id="work"
    )

    assert original["manifest"]["sha256"] != changed["manifest"]["sha256"]
    assert reverted["action"] == "created"
    assert reverted["manifest"]["sha256"] == original["manifest"]["sha256"]
    assert reverted["manifest"]["snapshot_id"] != original["manifest"]["snapshot_id"]
    assert result["snapshot_id"] == reverted["manifest"]["snapshot_id"]
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM example ORDER BY id").fetchall() == [
            ("alpha",), ("beta",),
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


@pytest.mark.parametrize("missing", ["snapshot", "manifest"])
def test_incomplete_publication_fails_closed(tmp_path: Path, missing: str) -> None:
    source, durable = tmp_path / "live.db", tmp_path / "durable"
    durable.mkdir()
    _database(source)
    result = durability.snapshot_state(source_db=str(source), durable_root=str(durable), authority_id="work")
    Path(result[f"{missing}_path"]).unlink()
    with pytest.raises(RuntimeError, match="incomplete"):
        durability.list_snapshots(durable_root=str(durable), authority_id="work")


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


def test_database_authority_cannot_be_reused_across_work_authorities(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    durable = tmp_path / "durable"
    durable.mkdir()
    _database(database)
    initialized = durability.ensure_database_authority(
        database, authority_id="authority-a", trust_domain="work", initialize=True
    )

    assert initialized["status"] == "initialized"
    assert durability.ensure_database_authority(
        database, authority_id="authority-a", trust_domain="work"
    )["status"] == "verified"
    with pytest.raises(RuntimeError, match="authority identity conflicts"):
        durability.ensure_database_authority(
            database, authority_id="authority-b", trust_domain="work"
        )
    with pytest.raises(RuntimeError, match="authority identity conflicts"):
        durability.snapshot_state(
            source_db=database, durable_root=durable,
            authority_id="authority-b",
        )


def test_unsafe_paths_are_rejected(tmp_path: Path) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            source_db="/Volumes/catalog/schema/live.db", durable_root=str(durable), databricks=True, authority_id="work"
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
            durable_root=str(durable), databricks=True, authority_id="work",
        )
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            destination_db="/tmp/../Workspace/Users/owner/live.db",
            durable_root=str(durable), databricks=True, authority_id="work",
        )
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            source_db="//Volumes/catalog/schema/live.db",
            durable_root=str(durable), databricks=True, authority_id="work",
        )
    with pytest.raises(ValueError, match="local compute"):
        durability.qualify_paths(
            destination_db="//Workspace/Users/owner/live.db",
            durable_root=str(durable), databricks=True, authority_id="work",
        )


def test_changed_checkpoint_advances_past_equal_or_rolled_back_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, durable = tmp_path / "live.db", tmp_path / "durable"
    durable.mkdir()
    _database(source)
    first = durability.snapshot_state(
        source_db=source, durable_root=durable, authority_id="work"
    )
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO example(value) VALUES ('changed')")
    frozen = datetime.fromisoformat(first["manifest"]["created_at"].replace("Z", "+00:00"))

    class RolledBackDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(durability, "datetime", RolledBackDateTime)
    second = durability.snapshot_state(
        source_db=source, durable_root=durable, authority_id="work"
    )

    assert second["action"] == "created"
    assert second["manifest"]["created_at"] > first["manifest"]["created_at"]
    assert durability.list_snapshots(
        durable_root=durable, authority_id="work"
    )["snapshots"][-1]["snapshot_id"] == second["manifest"]["snapshot_id"]


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
    assert not list(snapshots.glob("*.sqlite3"))
    assert not list(snapshots.glob("*.manifest.json"))
    assert durability.list_snapshots(
        durable_root=str(durable), authority_id="work"
    )["snapshots"] == []
