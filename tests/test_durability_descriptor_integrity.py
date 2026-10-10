"""Descriptor integrity in v2 snapshot manifests and dir_fd-relative restore cleanup (#29)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from odibi_anchor import durability

INTACT = "---\nid: alpha\nproject_type: referenced\ntarget_root: /targets/alpha\n---\n\n# Alpha\n"
DAMAGED = "# Beta\n\nThe frontmatter was replaced.\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _lineage(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY)")
    artifacts = tmp_path / "projects"
    for project, text in (("alpha", INTACT), ("beta", DAMAGED)):
        (artifacts / project).mkdir(parents=True)
        (artifacts / project / "PROJECT.md").write_text(text, encoding="utf-8")
    (artifacts / "gamma" / "notes").mkdir(parents=True)
    durable = tmp_path / "durable"
    durable.mkdir()
    snapshot = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work",
    )
    return source, artifacts, durable, snapshot


EXPECTED = [
    {"project_id": "alpha", "status": "intact", "sha256": _sha(INTACT)},
    {"project_id": "beta", "status": "missing_frontmatter", "sha256": _sha(DAMAGED)},
]


def test_snapshot_reports_descriptor_integrity_and_does_not_block_on_damage(tmp_path: Path) -> None:
    source, artifacts, durable, snapshot = _lineage(tmp_path)

    assert snapshot["action"] == "created"
    assert snapshot["manifest"]["descriptor_integrity"] == EXPECTED
    assert snapshot["descriptor_integrity"] == EXPECTED
    on_disk = durability._load_manifest(Path(snapshot["manifest_path"]))
    assert on_disk["descriptor_integrity"] == EXPECTED

    again = durability.snapshot_state(
        source_db=source, source_artifacts=artifacts, durable_root=durable, authority_id="work",
    )
    assert again["action"] == "reused"
    assert again["descriptor_integrity"] == EXPECTED


def test_restore_surfaces_descriptor_integrity(tmp_path: Path) -> None:
    _source, _artifacts, durable, _snapshot = _lineage(tmp_path)
    (tmp_path / "out").mkdir()

    restored = durability.restore_latest(
        durable_root=durable, destination_db=tmp_path / "out" / "restored.db",
        destination_artifacts=tmp_path / "out" / "projects", authority_id="work",
    )

    assert restored["descriptor_integrity"] == EXPECTED
    assert (tmp_path / "out" / "projects" / "beta" / "PROJECT.md").read_text() == DAMAGED


def test_manifest_without_the_key_restores_and_reports_none(tmp_path: Path) -> None:
    # A v0.3.24 writer produced the same manifest without descriptor_integrity. The reader
    # functions are unchanged since v0.3.24 and accept both shapes.
    _source, _artifacts, durable, snapshot = _lineage(tmp_path)
    manifest_path = Path(snapshot["manifest_path"])
    current = json.loads(manifest_path.read_bytes())
    durability._validate_manifest(current, manifest_path.name)
    legacy = {key: value for key, value in current.items()
              if key not in {"descriptor_integrity", "manifest_sha256"}}
    legacy["manifest_sha256"] = hashlib.sha256(durability._canonical_bytes(legacy)).hexdigest()
    manifest_path.write_bytes(durability._canonical_bytes(legacy))
    (tmp_path / "out").mkdir()

    restored = durability.restore_latest(
        durable_root=durable, destination_db=tmp_path / "out" / "restored.db",
        destination_artifacts=tmp_path / "out" / "projects", authority_id="work",
    )

    assert restored["action"] == "created"
    assert restored["descriptor_integrity"] is None


def _claimed(tmp_path: Path) -> tuple[Path, dict]:
    destination = tmp_path / "local" / "projects"
    destination.parent.mkdir()
    record = durability._claim_restore_destination(destination, {
        "authority_id": "work", "databricks": False, "destination_artifacts": str(destination),
        "destination_db": str(tmp_path / "local" / "restored.db"), "durable_root": str(tmp_path),
        "manifest_sha256": "0" * 64, "snapshot_id": "snapshot",
    })
    return destination, record


def _create(destination: Path) -> list[tuple[Path, str, tuple[int, int]]]:
    (destination / "sub").mkdir()
    (destination / "sub" / "f.md").write_text("restored\n", encoding="utf-8")
    return [
        (Path("sub"), "directory", durability._identity(destination / "sub")),
        (Path("sub/f.md"), "file", durability._identity(destination / "sub" / "f.md")),
    ]


def test_release_never_unlinks_through_a_parent_swapped_for_a_symlink(tmp_path: Path) -> None:
    assert durability._DIR_FD_CLEANUP is True  # this platform supports dir_fd cleanup
    destination, record = _claimed(tmp_path)
    created = _create(destination)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Same inode as the created file, reachable only through the substituted parent.
    os.link(destination / "sub" / "f.md", outside / "f.md")
    (destination / "sub").rename(tmp_path / "moved-sub")
    (destination / "sub").symlink_to(outside, target_is_directory=True)

    released = durability._release_owned_destination(destination, record, created, remove_root=True)

    assert released == "retained"
    assert (outside / "f.md").read_text(encoding="utf-8") == "restored\n"
    assert (tmp_path / "moved-sub" / "f.md").exists()


@pytest.mark.parametrize("dir_fd", [True, False])
def test_release_removes_only_created_entries_with_and_without_dir_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_fd: bool,
) -> None:
    monkeypatch.setattr(durability, "_DIR_FD_CLEANUP", dir_fd)
    destination, record = _claimed(tmp_path)
    created = _create(destination)

    assert durability._release_owned_destination(destination, record, created, remove_root=False) == "retained"
    assert sorted(entry.name for entry in destination.iterdir()) == [durability._RESTORE_OWNER]
    assert durability._release_owned_destination(destination, record, [], remove_root=True) == "removed"
    assert not destination.exists()


@pytest.mark.parametrize("dir_fd", [True, False])
def test_restore_fills_the_tree_with_and_without_dir_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_fd: bool,
) -> None:
    monkeypatch.setattr(durability, "_DIR_FD_CLEANUP", dir_fd)
    _source, artifacts, durable, _snapshot = _lineage(tmp_path)
    (tmp_path / "out").mkdir()

    durability.restore_latest(
        durable_root=durable, destination_db=tmp_path / "out" / "restored.db",
        destination_artifacts=tmp_path / "out" / "projects", authority_id="work",
    )

    restored = tmp_path / "out" / "projects"
    assert sorted(p.relative_to(restored).as_posix() for p in restored.rglob("*")) == sorted(
        p.relative_to(artifacts).as_posix() for p in artifacts.rglob("*")
    )
    assert not (restored / durability._RESTORE_OWNER).exists()
