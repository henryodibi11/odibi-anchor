"""Storage governance is observational and planning-only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from odibi_anchor.codebase._memory_storage import (
    inspect_memory_storage,
    plan_memory_migration,
    resolve_memory_storage,
)


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_diagnostic_is_byte_and_filesystem_read_only(tmp_path: Path) -> None:
    database = tmp_path / "state" / "memory.db"
    database.parent.mkdir()
    _database(database)
    before = _snapshot(tmp_path)

    result = inspect_memory_storage(environment={"ANCHOR_MEMORY_DB": str(database)})

    assert _snapshot(tmp_path) == before
    assert result["source"] == "environment:ANCHOR_MEMORY_DB"
    assert result["explicit"] is True
    assert result["schema_tables"] == ["memories"]
    assert result["integrity"] == "ok"


def test_missing_default_is_not_created_and_reports_default_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = inspect_memory_storage(environment={"ANCHOR_HOME": str(home)})
    assert result["path"] == str(home / ".agent_memory.db")
    assert result["source"] == "default:anchor_home"
    assert result["exists"] is False
    assert not (home / ".agent_memory.db").exists()


def test_reports_every_root_overlap_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    database = root / "state" / "memory.db"
    database.parent.mkdir()
    _database(database)
    result = inspect_memory_storage(
        environment={"ANCHOR_MEMORY_DB": str(database)},
        source_roots=[root], target_roots=[database.parent],
        artifact_roots=[root], resource_roots=[tmp_path],
    )
    assert [item["kind"] for item in result["overlaps"]] == ["artifact", "resource", "source", "target"]
    assert result["safe_location"] is False


def test_only_safe_profile_metadata_is_disclosed(tmp_path: Path) -> None:
    secret = "do-not-disclose-token"
    config = {"environment": {"profiles": {"work": {
        "anchor_root": str(tmp_path), "memory_db": str(tmp_path / "memory.db"),
        "trust_domain": "employer", "api_token": secret,
    }}}}
    result = resolve_memory_storage(
        environment={"ANCHOR_PROFILE": "work", "API_TOKEN": secret, "PASSWORD": secret}, config=config,
    )
    assert result["profile"] == "work"
    assert result["trust_domain"] == "employer"
    assert secret not in json.dumps(result, sort_keys=True)


def test_unknown_explicit_profile_fails_closed() -> None:
    config = {"environment": {"profiles": {"local": {"platform": "local"}}}}
    with pytest.raises(ValueError, match=r"Unknown ANCHOR_PROFILE.*local"):
        resolve_memory_storage(environment={"ANCHOR_PROFILE": "typo"}, config=config)


def test_content_addressed_backups_are_discovered_and_integrity_qualified(tmp_path: Path) -> None:
    database = tmp_path / "memory.db"
    _database(database)
    valid = tmp_path / "memory.db.pre-learning-v1.abc123.bak"
    invalid = tmp_path / "memory.db.pre-memory-fts-v1.bad.bak"
    _database(valid)
    invalid.write_text("not sqlite", encoding="utf-8")

    result = inspect_memory_storage(environment={"ANCHOR_MEMORY_DB": str(database)})

    assert result["backups"] == sorted([str(valid), str(invalid)])
    assert result["usable_backups"] == [str(valid)]
    details = {item["path"]: item for item in result["backup_details"]}
    assert details[str(valid)]["integrity"] == "ok"
    assert details[str(invalid)]["usable"] is False


@pytest.mark.parametrize("destination", ["relative.db", "~/memory.db"])
def test_plan_requires_explicit_absolute_destination(destination: str) -> None:
    with pytest.raises(ValueError, match="explicit absolute"):
        plan_memory_migration(destination, environment={"ANCHOR_MEMORY_DB": "/safe/source.db"})


def test_plan_rejects_forbidden_roots_and_domain_crossing(tmp_path: Path) -> None:
    source = tmp_path / "state" / "source.db"
    source.parent.mkdir()
    _database(source)
    forbidden = tmp_path / "checkout"
    with pytest.raises(ValueError, match="forbidden root"):
        plan_memory_migration(
            forbidden / "memory.db", environment={"ANCHOR_MEMORY_DB": str(source)}, source_roots=[forbidden],
        )
    with pytest.raises(ValueError, match="trust-domain crossing"):
        plan_memory_migration(
            str(tmp_path / "destination.db"), environment={"ANCHOR_MEMORY_DB": str(source), "ANCHOR_TRUST_DOMAIN": "personal"},
            destination_trust_domain="employer",
        )


def test_plan_is_deterministic_and_never_mutates(tmp_path: Path) -> None:
    source = tmp_path / "state" / "source.db"
    source.parent.mkdir()
    _database(source)
    destination = tmp_path / "migration" / "memory.db"
    environment = {"ANCHOR_MEMORY_DB": str(source), "ANCHOR_TRUST_DOMAIN": "personal"}
    before = _snapshot(tmp_path)

    first = plan_memory_migration(str(destination), environment=environment)
    second = plan_memory_migration(str(destination), environment=environment)

    assert first == second
    assert first["execution_authorized"] is False
    assert first["destructive_actions"] == []
    assert any("backup" in step for step in first["steps"])
    assert any("atomically publish" in step for step in first["steps"])
    assert _snapshot(tmp_path) == before
    assert not destination.exists()
    assert not destination.parent.exists()
