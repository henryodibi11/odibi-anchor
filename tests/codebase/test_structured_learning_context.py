"""Phase-one qualification for the structured-learning store and explicit API."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from odibi_anchor._runtime_paths import RuntimePaths
from odibi_anchor.codebase import structured_learning_context as _learning_module


class _LearningStoreTestAPI:
    """Exercise the private store seam without reopening public authority injection."""

    def __init__(self) -> None:
        self._owners: dict[str, tuple[str, str]] = {}

    def structured_learning_context(self, **payload):
        obligation_id = payload.get("_obligation_id") or payload.get(
            "_latest_closed_obligation_id"
        )
        if payload.get("command") in {"capture", "assess"}:
            project_id, task_window_id = self._owners.get(
                str(obligation_id), ("project:test", "ltw_1")
            )
            payload.setdefault("_project_id", project_id)
            payload.setdefault("_task_window_id", task_window_id)
        return _learning_module._structured_learning_dispatch(**payload)

    def activate_learning_obligation(self, **payload):
        result = _learning_module.activate_learning_obligation(**payload)
        self._owners[result["obligation_id"]] = (
            result["project_id"], result["task_window_id"]
        )
        return result

    def ensure_learning_obligation(self, **payload):
        result = _learning_module.ensure_learning_obligation(**payload)
        self._owners[result["obligation_id"]] = (
            result["project_id"], result["task_window_id"]
        )
        return result

    def __getattr__(self, name):
        return getattr(_learning_module, name)


learning = _LearningStoreTestAPI()


def test_db_path_uses_dispatcher_runtime_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from odibi_anchor import _runtime_paths
    from odibi_anchor._dispatcher import _boot

    active_home = tmp_path / "active-home"
    fallback_home = tmp_path / "fallback-home"
    active = RuntimePaths(
        source_checkout=True,
        resource_root=active_home,
        instructions_file=active_home / ".assistant_instructions.md",
        skills_dir=active_home / ".assistant" / "skills",
        tools_dir=active_home / "tools",
        anchor_home=active_home,
    )
    fallback = RuntimePaths(
        source_checkout=True,
        resource_root=fallback_home,
        instructions_file=fallback_home / ".assistant_instructions.md",
        skills_dir=fallback_home / ".assistant" / "skills",
        tools_dir=fallback_home / "tools",
        anchor_home=fallback_home,
    )
    monkeypatch.setattr(_runtime_paths, "resolve_runtime_paths", lambda: fallback)
    monkeypatch.setitem(_boot._ENV, "runtime_paths", active)
    monkeypatch.setitem(_boot._ENV, "home", str(active_home))
    monkeypatch.setitem(_boot._ENV, "memory_db", str(active_home / ".agent_memory.db"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(active_home / ".agent_memory.db"))

    assert learning._db_path() == active_home / ".agent_memory.db"


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "anchor-home"
    path = home / ".agent_memory.db"
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(path))
    return path


def activate(number: int = 1) -> dict:
    return learning.activate_learning_obligation(
        task_window_id=f"ltw_{number}",
        session_ref=f"session:{number}",
        checkpoint_ref=f"gate:{number}",
        project_ref="project:test",
    )


def capture_payload(reference: str = "src/pkg/file.py#L10-L20", **updates: object) -> dict:
    payload = {
        "observation_type": "friction",
        "summary": "Qualification found an installed wheel drift.",
        "signal_key": "qualification.installed-wheel-drift",
        "impact": "medium",
        "applicability_scope": "workbench",
        "project_refs": ["project:test"],
        "work_package_refs": ["work:one"],
        "environment_refs": ["env:test"],
        "provenance": {"source_action": "gate", "source_version": "v1"},
        "evidence": [{"reference_type": "file", "reference": reference}],
    }
    payload.update(updates)
    return payload


def capture(obligation: dict, **updates: object) -> dict:
    return learning.structured_learning_context(
        command="capture", _obligation_id=obligation["obligation_id"], **capture_payload(**updates)
    )


def triage(item: dict, decision: str, **updates: object) -> dict:
    payload = {
        "decision": decision,
        "item_id": item["item_id"],
        "expected_version": item["version"],
        "actor_kind": "human",
        "actor_ref": "reviewer:one",
        "decision_source": f"decision:{decision}.1",
        "rationale": "The bounded evidence supports this explicit decision.",
    }
    payload.update(updates)
    return learning.structured_learning_context(command="triage", **payload)


def derive(source_items: list[dict], decision: str, **updates: object) -> dict:
    payload = {
        "decision": decision,
        "source_item_ids": [item["item_id"] for item in source_items],
        "expected_source_versions": {item["item_id"]: item["version"] for item in source_items},
        "summary": "Human review classified a reusable bounded finding.",
        "impact": "high",
        "applicability_scope": "workbench",
        "project_refs": [],
        "work_package_refs": [],
        "environment_refs": [],
        "provenance": {"source_action": "review", "source_version": "v1"},
        "evidence": [{"reference_type": "test", "reference": "tests/test_dispatcher.py::test_gate[legacy-v1]"}],
        "actor_kind": "human",
        "actor_ref": "reviewer:one",
        "decision_source": f"decision:{decision}.1",
        "rationale": "The bounded evidence supports this explicit classification.",
    }
    payload.update(updates)
    return learning.structured_learning_context(command="triage", **payload)


def test_read_only_uninitialized_does_not_create_database(ledger: Path) -> None:
    result = learning.structured_learning_context(command="list")
    assert result == {"schema_status": "uninitialized", "items": []}
    assert not ledger.exists()


def test_ensure_reuses_current_task_and_creates_next_cycle(ledger: Path) -> None:
    first = learning.ensure_learning_obligation(
        task_window_id="ltw_1", session_ref="session:1",
        checkpoint_ref="task:accepted", project_ref="project:test",
    )
    same = learning.ensure_learning_obligation(
        task_window_id="ltw_1", session_ref="session:1",
        checkpoint_ref="gate:1", project_ref="project:test",
    )
    assert first["created"] is True
    assert same == {
        "obligation_id": first["obligation_id"],
        "project_id": "project:test",
        "task_window_id": "ltw_1",
        "status": "active",
        "created": False,
    }
    next_task = learning.ensure_learning_obligation(
        task_window_id="ltw_2", session_ref="session:2",
        checkpoint_ref="task:accepted", project_ref="project:test",
    )
    assert next_task["created"] is True
    assert next_task["obligation_id"] != first["obligation_id"]


def test_triage_absent_and_legacy_database_fail_without_byte_changes(ledger: Path) -> None:
    payload = {
        "decision": "reject", "item_id": "li_missing", "expected_version": 1,
        "actor_kind": "human", "actor_ref": "reviewer:one",
        "decision_source": "decision:reject.1", "rationale": "Explicit review decision.",
    }
    with pytest.raises(RuntimeError, match="not initialized"):
        learning.structured_learning_context(command="triage", **payload)
    assert not ledger.exists()

    ledger.parent.mkdir(parents=True)
    connection = sqlite3.connect(ledger)
    connection.execute("CREATE TABLE legacy_memory (id TEXT PRIMARY KEY, content TEXT)")
    connection.execute("INSERT INTO legacy_memory VALUES ('one','unchanged')")
    connection.commit()
    connection.close()
    before = ledger.read_bytes()
    with pytest.raises(RuntimeError, match="not initialized"):
        learning.structured_learning_context(command="triage", **payload)
    assert ledger.read_bytes() == before


def test_public_api_rejects_internal_obligation_authority(ledger: Path) -> None:
    with pytest.raises(ValueError, match="not caller-accessible"):
        _learning_module.structured_learning_context(
            command="capture", _obligation_id="lob_" + "a" * 32
        )


def test_exact_schema_checksum_pragmas_and_introspection(ledger: Path) -> None:
    activate()
    connection = sqlite3.connect(ledger)
    try:
        row = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (learning.DOMAIN,)
        ).fetchone()
        assert row == (learning.VERSION, learning.SCHEMA_SHA256)
        assert learning._schema_signature(connection) == learning.EXPECTED_SCHEMA_SIGNATURE
        foreign_keys = {
            (table, tuple(entry[2:5]))
            for table in learning.TABLES
            for entry in connection.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert ("learning_evidence_refs", ("learning_items", "item_id", "item_id")) in foreign_keys
        assert ("learning_assessments", ("learning_obligations", "obligation_id", "obligation_id")) in foreign_keys
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_existing_legacy_database_migrates_with_verified_backup(ledger: Path) -> None:
    ledger.parent.mkdir(parents=True)
    connection = sqlite3.connect(ledger)
    connection.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO legacy(value) VALUES('preserved')")
    connection.commit()
    connection.close()

    activate()

    backups = sorted(ledger.parent.glob(f"{ledger.name}.pre-learning-v2.*.bak"))
    assert len(backups) == 1, backups
    backup = backups[0]
    backup_connection = sqlite3.connect(backup)
    try:
        assert backup_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup_connection.execute("SELECT value FROM legacy").fetchone()[0] == "preserved"
        assert "learning_items" not in {
            row[0] for row in backup_connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        backup_connection.close()
    assert sqlite3.connect(ledger).execute("SELECT value FROM legacy").fetchone()[0] == "preserved"


def test_legacy_fixed_name_backup_never_blocks_migration(ledger: Path) -> None:
    """Regression for WI-2026-0019: a stale fixed-name backup deadlocked learning init.

    Previously any file at ``<db>.pre-learning-v1.bak`` permanently blocked schema
    initialization, which in turn blocked gate and learning closure with no recovery
    path. Content-addressed names must ignore it and leave it untouched.
    """
    ledger.parent.mkdir(parents=True)
    sqlite3.connect(ledger).execute("CREATE TABLE legacy(value TEXT)").connection.commit()
    legacy = Path(f"{ledger}.pre-learning-v1.bak")
    legacy.write_bytes(b"occupied")

    activate()

    # Migration completed despite the occupied legacy name...
    connection = sqlite3.connect(ledger)
    try:
        assert "learning_items" in {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    # ...and the pre-existing file was neither read as a backup nor modified.
    assert legacy.read_bytes() == b"occupied"
    assert len(sorted(ledger.parent.glob(f"{ledger.name}.pre-learning-v2.*.bak"))) == 1


def test_migration_backup_is_idempotent_and_fails_closed_on_mismatch(tmp_path: Path) -> None:
    """Re-running over identical content reuses; a poisoned same-name backup stops."""
    from odibi_anchor.codebase._migration_backup import (
        ensure_migration_backup,
        logical_digest,
        migration_backup_path,
    )

    source = tmp_path / "db.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE t(v TEXT)")
    connection.execute("INSERT INTO t(v) VALUES('x')")
    connection.commit()
    connection.close()

    first = ensure_migration_backup(source, tag="demo-v1", error_prefix="demo")
    assert first["action"] == "created"
    backup = Path(first["backup_path"])
    assert backup.is_file()

    # A backup taken with Connection.backup is logically -- not byte -- identical.
    assert logical_digest(backup) == logical_digest(source) == first["source_digest"]

    # Idempotent: same content re-runs reuse the one backup, creating no duplicate.
    second = ensure_migration_backup(source, tag="demo-v1", error_prefix="demo")
    assert second["action"] == "reused"
    assert second["backup_path"] == first["backup_path"]
    assert len(sorted(tmp_path.glob("db.sqlite.pre-demo-v1.*.bak"))) == 1

    # Fail closed: same content address, different content, nothing mutated.
    poisoned = sqlite3.connect(backup)
    poisoned.execute("INSERT INTO t(v) VALUES('tampered')")
    poisoned.commit()
    poisoned.close()
    before = backup.read_bytes()
    with pytest.raises(RuntimeError, match="backup content mismatch"):
        ensure_migration_backup(source, tag="demo-v1", error_prefix="demo")
    assert backup.read_bytes() == before
    assert not Path(f"{backup}.tmp").exists()

    # Distinct pre-migration content gets a distinct name, so it can never collide.
    connection = sqlite3.connect(source)
    connection.execute("INSERT INTO t(v) VALUES('y')")
    connection.commit()
    connection.close()
    third = ensure_migration_backup(source, tag="demo-v1", error_prefix="demo")
    assert third["action"] == "created"
    assert third["backup_path"] != first["backup_path"]
    assert migration_backup_path(
        source, tag="demo-v1", digest=third["source_digest"]
    ) == Path(third["backup_path"])


@pytest.mark.parametrize("name", ["plain.sqlite", "auth#hash.sqlite", "auth%25pct.sqlite", "a b&c.sqlite"])
def test_migration_backup_uri_metacharacters_open_only_the_intended_file(
    tmp_path: Path, name: str,
) -> None:
    """URI metacharacters must not truncate the path or drop mode=ro.

    Interpolating a raw path into a ``file:`` URI lets ``#`` begin a fragment, which
    silently opened a different (empty) database and created a stray file, so the
    backup verified clean while containing none of the source data.
    """
    from odibi_anchor.codebase._migration_backup import (
        ensure_migration_backup,
        logical_digest,
    )

    source = tmp_path / name
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE marker(v TEXT)")
    connection.execute("INSERT INTO marker(v) VALUES('intended')")
    connection.commit()
    connection.close()
    before = {path.name for path in tmp_path.iterdir()}

    result = ensure_migration_backup(source, tag="meta-v1", error_prefix="meta")
    backup = Path(result["backup_path"])

    # No stray file from a truncated path, and the backup holds the real rows.
    assert {path.name for path in tmp_path.iterdir()} == before | {backup.name}
    assert logical_digest(backup) == logical_digest(source) == result["source_digest"]
    backup_connection = sqlite3.connect(backup)
    try:
        assert backup_connection.execute("SELECT v FROM marker").fetchone()[0] == "intended"
    finally:
        backup_connection.close()

    # mode=ro must still be enforced, so a missing database cannot be created.
    with pytest.raises(sqlite3.Error):
        logical_digest(tmp_path / f"missing-{name}")
    assert not (tmp_path / f"missing-{name}").exists()


def test_stale_staging_file_never_blocks_migration(tmp_path: Path) -> None:
    """A crashed run's leftover staging file must not create a new permanent dead-end.

    A shared ``.tmp`` name reproduced the very deadlock this module removes: once a
    crash left the file behind, every later migration failed forever.
    """
    from odibi_anchor.codebase._migration_backup import (
        ensure_migration_backup,
        logical_digest,
        migration_backup_path,
    )

    source = tmp_path / "db.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE t(v TEXT)")
    connection.execute("INSERT INTO t(v) VALUES('kept')")
    connection.commit()
    connection.close()

    destination = migration_backup_path(
        source, tag="stale-v1", digest=logical_digest(source)
    )
    legacy_staging = Path(f"{destination}.tmp")
    legacy_staging.write_bytes(b"leftover from a crashed run")

    result = ensure_migration_backup(source, tag="stale-v1", error_prefix="stale")

    assert result["action"] == "created"
    assert logical_digest(Path(result["backup_path"])) == result["source_digest"]
    # The leftover file is neither consumed nor trusted as a backup.
    assert legacy_staging.read_bytes() == b"leftover from a crashed run"
    # This run leaves no staging file of its own behind.
    assert [
        path.name for path in tmp_path.iterdir()
        if path.name.endswith(".tmp") and path != legacy_staging
    ] == []


def test_migration_backup_rejects_unreadable_same_name_backup(tmp_path: Path) -> None:
    """A non-database file at the content-addressed name fails closed, not silently."""
    from odibi_anchor.codebase._migration_backup import (
        ensure_migration_backup,
        logical_digest,
        migration_backup_path,
    )

    source = tmp_path / "db.sqlite"
    sqlite3.connect(source).execute("CREATE TABLE t(v TEXT)").connection.commit()
    occupied = migration_backup_path(
        source, tag="demo-v1", digest=logical_digest(source)
    )
    occupied.write_bytes(b"not a database")
    with pytest.raises(RuntimeError, match="not a readable database"):
        ensure_migration_backup(source, tag="demo-v1", error_prefix="demo")
    assert occupied.read_bytes() == b"not a database"


def test_migration_backup_rejects_snapshot_that_does_not_match_its_content_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source change between digest and copy cannot mislabel the backup."""
    from odibi_anchor.codebase import _migration_backup

    source = tmp_path / "db.sqlite"
    sqlite3.connect(source).execute("CREATE TABLE t(v TEXT)").connection.commit()
    real_digest = _migration_backup.logical_digest
    initial_digest = "0" * 64
    calls = 0

    def changed_digest(path):
        nonlocal calls
        calls += 1
        return initial_digest if calls == 1 else real_digest(path)

    monkeypatch.setattr(_migration_backup, "logical_digest", changed_digest)

    with pytest.raises(RuntimeError, match="source changed while backup was created"):
        _migration_backup.ensure_migration_backup(
            source, tag="race-v1", error_prefix="race",
        )

    assert not list(tmp_path.glob("db.sqlite.pre-race-v1.*.bak"))
    assert not list(tmp_path.glob("db.sqlite.pre-race-v1.*.tmp"))


def test_migration_backup_publish_fallback_never_overwrites_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-hard-link fallback atomically reserves rather than renames over a race."""
    from odibi_anchor.codebase import _migration_backup

    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"verified backup")
    real_open = _migration_backup.os.open
    raced = False

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EPERM, "hard links are unsupported")

    def racing_open(path, flags, mode=0o777):
        nonlocal raced
        if Path(path) == destination and flags & os.O_EXCL and not raced:
            raced = True
            destination.write_bytes(b"concurrent backup")
        return real_open(path, flags, mode)

    monkeypatch.setattr(_migration_backup.os, "link", unsupported_link)
    monkeypatch.setattr(_migration_backup.os, "open", racing_open)

    with pytest.raises(RuntimeError, match="destination collision"):
        _migration_backup._publish_without_overwrite(
            temporary, destination, collision_error="destination collision",
        )

    assert destination.read_bytes() == b"concurrent backup"
    assert not temporary.exists()


def test_publish_backup_windows_uses_writable_fsync_and_skips_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"verified backup")
    real_open = open
    opened_modes = []
    fsyncs = []

    def tracked_open(path, mode):
        opened_modes.append(mode)
        return real_open(path, mode)

    def unexpected_directory_open(*_args, **_kwargs):
        raise AssertionError("Windows backup publication must not open the parent directory")

    monkeypatch.setattr(_learning_module, "_IS_WINDOWS", True)
    monkeypatch.setattr("builtins.open", tracked_open)
    monkeypatch.setattr(_learning_module.os, "fsync", lambda fd: fsyncs.append(fd))
    monkeypatch.setattr(_learning_module.os, "open", unexpected_directory_open)

    learning._publish_backup(temporary, destination, collision_error="collision")

    assert opened_modes == ["r+b"]
    assert len(fsyncs) == 1
    assert destination.read_bytes() == b"verified backup"
    assert not temporary.exists()


def test_publish_backup_posix_fsyncs_file_and_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"verified backup")
    directory_fd = 8675309
    opened_directories = []
    closed = []
    fsyncs = []

    monkeypatch.setattr(_learning_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        _learning_module.os,
        "open",
        lambda path, flags: opened_directories.append((path, flags)) or directory_fd,
    )
    monkeypatch.setattr(_learning_module.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(_learning_module.os, "fsync", lambda fd: fsyncs.append(fd))

    learning._publish_backup(temporary, destination, collision_error="collision")

    assert opened_directories == [(tmp_path, _learning_module.os.O_RDONLY)]
    assert fsyncs[-1] == directory_fd
    assert len(fsyncs) == 2
    assert closed == [directory_fd]
    assert destination.read_bytes() == b"verified backup"
    assert not temporary.exists()


def test_publish_backup_file_fsync_failure_is_fatal_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"unpublished backup")

    def fail_fsync(_fd):
        raise OSError("file fsync failed")

    monkeypatch.setattr(_learning_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="file fsync failed"):
        learning._publish_backup(temporary, destination, collision_error="collision")

    assert not temporary.exists()
    assert not destination.exists()


def test_publish_backup_collision_fails_closed_and_cleans_temporary(tmp_path: Path) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"new backup")
    destination.write_bytes(b"existing backup")

    with pytest.raises(RuntimeError, match="destination collision"):
        learning._publish_backup(
            temporary, destination, collision_error="destination collision"
        )

    assert destination.read_bytes() == b"existing backup"
    assert not temporary.exists()


@pytest.mark.parametrize("unsupported_errno", [errno.ENOSYS, errno.EPERM])
def test_publish_backup_falls_back_when_hard_links_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported_errno: int,
) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"verified backup")

    def unsupported_link(*_args, **_kwargs):
        raise OSError(unsupported_errno, "hard links are unsupported")

    monkeypatch.setattr(_learning_module.os, "link", unsupported_link)

    learning._publish_backup(temporary, destination, collision_error="collision")

    assert destination.read_bytes() == b"verified backup"
    assert not temporary.exists()


def test_publish_backup_propagates_unexpected_hard_link_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"verified backup")

    def failed_link(*_args, **_kwargs):
        raise OSError(errno.EIO, "storage failure")

    monkeypatch.setattr(_learning_module.os, "link", failed_link)

    with pytest.raises(OSError, match="storage failure"):
        learning._publish_backup(temporary, destination, collision_error="collision")

    assert not temporary.exists()
    assert not destination.exists()


@pytest.mark.parametrize("unsupported_errno", [errno.ENOSYS, errno.EPERM])
def test_publish_backup_fallback_preserves_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported_errno: int,
) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"new backup")
    destination.write_bytes(b"existing backup")

    def unsupported_link(*_args, **_kwargs):
        raise OSError(unsupported_errno, "hard links are unsupported")

    monkeypatch.setattr(_learning_module.os, "link", unsupported_link)

    with pytest.raises(RuntimeError, match="destination collision"):
        learning._publish_backup(
            temporary, destination, collision_error="destination collision"
        )

    assert destination.read_bytes() == b"existing backup"
    assert not temporary.exists()


@pytest.mark.parametrize("unsupported_errno", [errno.ENOSYS, errno.EPERM])
def test_publish_backup_fallback_preserves_dangling_destination_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported_errno: int,
) -> None:
    temporary = tmp_path / "backup.tmp"
    destination = tmp_path / "backup.sqlite"
    temporary.write_bytes(b"new backup")
    try:
        destination.symlink_to("missing.sqlite")
    except OSError:
        pytest.skip("symlink creation is not permitted")

    def unsupported_link(*_args, **_kwargs):
        raise OSError(unsupported_errno, "hard links are unsupported")

    monkeypatch.setattr(_learning_module.os, "link", unsupported_link)

    with pytest.raises(RuntimeError, match="destination collision"):
        learning._publish_backup(
            temporary, destination, collision_error="destination collision"
        )

    assert destination.is_symlink()
    assert destination.readlink() == Path("missing.sqlite")
    assert not temporary.exists()


def test_shared_schema_version_table_without_learning_domain_initializes(ledger: Path) -> None:
    ledger.parent.mkdir(parents=True)
    connection = sqlite3.connect(ledger)
    connection.execute(learning.DDL[0])
    connection.execute("INSERT INTO anchor_schema_versions VALUES('other',1,?,datetime('now'))", ("a" * 64,))
    connection.commit()
    connection.close()

    activate()

    connection = sqlite3.connect(ledger)
    assert connection.execute(
        "SELECT version FROM anchor_schema_versions WHERE domain=?", (learning.DOMAIN,)
    ).fetchone() == (learning.VERSION,)
    assert connection.execute("SELECT version FROM anchor_schema_versions WHERE domain='other'").fetchone() == (1,)


@pytest.mark.parametrize(
    ("env_path", "suffix"),
    [("workspace/projects/demo/state.db", "managed"), ("workspace/projects/state.db", "managed")],
)
def test_unsafe_managed_project_override_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_path: str, suffix: str
) -> None:
    home = tmp_path / suffix
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / env_path))
    with pytest.raises(ValueError, match="unsafe"):
        learning.structured_learning_context(command="list")


def test_capture_exact_retry_conflict_and_closed_retry_latest(ledger: Path) -> None:
    obligation = activate()
    scoped = {
        "observation_type": "reusable_practice",
        "applicability_scope": "project_local", "project_refs": ["project:test"],
    }
    first = capture(obligation, **scoped)
    second = capture(obligation, **scoped)
    assert second["deduped"] is True
    assert second["item"]["item_id"] == first["item"]["item_id"]
    assessment = learning.structured_learning_context(
        command="assess",
        _obligation_id=obligation["obligation_id"],
        outcome="observations_recorded",
        observation_ids=[first["item"]["item_id"]],
    )
    assert isinstance(assessment, dict)
    retried = learning.structured_learning_context(
        command="capture",
        retry_latest=True,
        _latest_closed_obligation_id=obligation["obligation_id"],
        **capture_payload(**scoped),
    )
    assert retried["deduped"] is True
    with pytest.raises(ValueError, match="idempotency conflict"):
        learning.structured_learning_context(
            command="capture",
            retry_latest=True,
            _latest_closed_obligation_id=obligation["obligation_id"],
            **capture_payload(summary="Changed retry payload.", **scoped),
        )
    with pytest.raises(ValueError, match="active learning obligation"):
        capture(obligation, reference="src/other.py")
    assert assessment["deduped"] is False
    assert len(assessment["semantic_candidate_projections"]) == 1
    with sqlite3.connect(ledger) as connection:
        projected = connection.execute(
            "SELECT m.status,m.project,m.evidence FROM memories m "
            "JOIN memory_projections p ON p.memory_id=m.id WHERE p.learning_item_id=?",
            (first["item"]["item_id"],),
        ).fetchone()
    assert projected[0:2] == ("candidate", "project:test")
    assert json.loads(projected[2])["assessment_id"] == assessment["assessment"]["assessment_id"]
    counts = (
        sqlite3.connect(ledger)
        .execute("SELECT (SELECT count(*) FROM learning_items),(SELECT count(*) FROM learning_events)")
        .fetchone()
    )
    assert counts == (1, 1)


def test_assessment_does_not_widen_workbench_observation_to_global_memory(ledger: Path) -> None:
    obligation = activate()
    item = capture(obligation, observation_type="reusable_practice")
    assessment = learning.structured_learning_context(
        command="assess",
        _obligation_id=obligation["obligation_id"],
        outcome="observations_recorded",
        observation_ids=[item["item"]["item_id"]],
    )
    assert isinstance(assessment, dict)
    assert assessment["semantic_candidate_projections"] == []
    with sqlite3.connect(ledger) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='memory_projections'"
        ).fetchone()[0] == 0


def test_assessment_projects_workbench_observation_inside_explicit_work_authority(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANCHOR_AUTHORITY_ID", "enterprise-analytics-ai")
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "work")
    obligation = activate()
    item = capture(obligation, observation_type="reusable_practice")

    assessment = learning.structured_learning_context(
        command="assess",
        _obligation_id=obligation["obligation_id"],
        outcome="observations_recorded",
        observation_ids=[item["item"]["item_id"]],
    )

    assert isinstance(assessment, dict)
    projection = assessment["semantic_candidate_projections"][0]
    assert isinstance(projection, dict)
    with sqlite3.connect(ledger) as connection:
        memory = connection.execute(
            "SELECT project,evidence FROM memories WHERE id=?", (projection["memory_id"],)
        ).fetchone()
    assert memory[0] == "all"
    assert json.loads(memory[1])["authority_id"] == "enterprise-analytics-ai"


def test_retry_latest_cannot_cross_a_new_active_obligation(ledger: Path) -> None:
    old = activate()
    captured = capture(old)
    learning.structured_learning_context(
        command="assess",
        _obligation_id=old["obligation_id"],
        outcome="observations_recorded",
        observation_ids=[captured["item"]["item_id"]],
    )
    current = activate()

    with pytest.raises(ValueError, match="unavailable while a learning obligation is active"):
        learning.structured_learning_context(
            command="assess",
            retry_latest=True,
            _latest_closed_obligation_id=old["obligation_id"],
            outcome="observations_recorded",
            observation_ids=[captured["item"]["item_id"]],
        )
    assert learning.active_learning_obligation(
        project_id="project:test", task_window_id="ltw_1"
    )["obligation_id"] == current["obligation_id"]


def test_capture_hash_includes_authoritative_obligation_provenance(ledger: Path) -> None:
    obligation = activate()
    item = capture(obligation)["item"]
    provenance = json.loads(item["provenance"])
    assert provenance["learning_obligation_id"] == obligation["obligation_id"]
    assert provenance["task_window_id"] == "ltw_1"
    assert provenance["session_id"] == "session:1"
    assert "session_ref" not in provenance


def test_assessment_key_first_retry_conflict_and_no_learning(ledger: Path) -> None:
    obligation = activate()
    first = learning.structured_learning_context(
        command="assess",
        _obligation_id=obligation["obligation_id"],
        outcome="nothing_reusable_learned",
        notes="No bounded reusable observation was found.",
    )
    retry = learning.structured_learning_context(
        command="assess",
        retry_latest=True,
        _latest_closed_obligation_id=obligation["obligation_id"],
        outcome="nothing_reusable_learned",
        notes="No bounded reusable observation was found.",
    )
    assert retry["deduped"] is True
    assert retry["assessment"]["assessment_id"] == first["assessment"]["assessment_id"]
    with pytest.raises(ValueError, match="idempotency conflict"):
        learning.structured_learning_context(
            command="assess",
            retry_latest=True,
            _latest_closed_obligation_id=obligation["obligation_id"],
            outcome="nothing_reusable_learned",
            notes="Different notes.",
        )
    assert sqlite3.connect(ledger).execute("SELECT count(*) FROM learning_items").fetchone()[0] == 0


def test_assessment_validation_explains_structured_learning_routes(ledger: Path) -> None:
    obligation = activate()

    with pytest.raises(ValueError, match=r"use 'observations_recorded'.*'nothing_reusable_learned'"):
        learning.structured_learning_context(
            command="assess",
            _obligation_id=obligation["obligation_id"],
            outcome="gotchas_recorded",
        )
    with pytest.raises(ValueError, match=r"first call anchor\('learning', 'capture'"):
        learning.structured_learning_context(
            command="assess",
            _obligation_id=obligation["obligation_id"],
            outcome="observations_recorded",
        )
    with pytest.raises(ValueError, match="does not accept observation_ids"):
        learning.structured_learning_context(
            command="assess",
            _obligation_id=obligation["obligation_id"],
            outcome="nothing_reusable_learned",
            observation_ids=["lrn_unused"],
        )
    with pytest.raises(ValueError, match="actor_kind must be 'agent' or 'human'"):
        learning.structured_learning_context(
            command="assess",
            _obligation_id=obligation["obligation_id"],
            outcome="nothing_reusable_learned",
            actor_kind="system",
        )


def test_active_legacy_close_and_latest_closed_primitives(ledger: Path) -> None:
    obligation = activate()
    owner = {"project_id": "project:test", "task_window_id": "ltw_1"}
    assert learning.active_learning_obligation(**owner)["obligation_id"] == obligation["obligation_id"]
    closed = learning.close_learning_obligation_legacy(obligation["obligation_id"], **owner)
    assert closed["status"] == "legacy_closed"
    assert learning.close_learning_obligation_legacy(
        obligation["obligation_id"], **owner
    )["closed_at"] == closed["closed_at"]
    assert learning.active_learning_obligation(**owner) is None
    assert learning.latest_closed_learning_obligation(**owner)["obligation_id"] == obligation["obligation_id"]


def test_recurrence_list_show_and_insights_are_complete(ledger: Path) -> None:
    captured = []
    for number in range(3):
        obligation = activate(number + 1)
        captured.append(capture(obligation, reference=f"src/pkg/file{number}.py"))
        learning.structured_learning_context(
            command="assess",
            _obligation_id=obligation["obligation_id"],
            outcome="observations_recorded",
            observation_ids=[captured[-1]["item"]["item_id"]],
        )
    listed = learning.structured_learning_context(command="list")
    assert [item["recurrence_count"] for item in listed["items"]] == [3, 3, 3]
    assert all(item["attention_due"] for item in listed["items"])
    assert all(item["evidence_refs"] and item["latest_event"] for item in listed["items"])
    shown = learning.structured_learning_context(
        command="show", item_id=captured[0]["item"]["item_id"], include_history=True
    )["item"]
    assert [event["event_type"] for event in shown["events"]] == ["captured"]
    insight = learning.structured_learning_context(command="insights")["insights"][0]
    assert insight["recurrence_count"] == 3
    assert insight["attention_due"] is True


@pytest.mark.parametrize("decision", ["derive_lesson", "derive_watch", "derive_improvement"])
def test_allowed_derivations_are_key_first_idempotent(ledger: Path, decision: str) -> None:
    source = capture(activate())["item"]
    first = derive([source], decision)
    retry = derive([source], decision)
    assert retry["deduped"] is True
    assert retry["item"]["item_id"] == first["item"]["item_id"]
    with pytest.raises(ValueError, match="idempotency conflict"):
        derive([source], decision, summary="A conflicting classification payload.")
    shown = learning.structured_learning_context(
        command="show", item_id=first["item"]["item_id"], include_history=True
    )["item"]
    assert shown["events"][0]["event_type"] == "derived"
    assert shown["links"][0]["source_item_id"] == source["item_id"]
    with sqlite3.connect(ledger) as connection:
        if decision in {"derive_lesson", "derive_watch"}:
            assert first["semantic_projection"] == retry["semantic_projection"]
            assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM memory_projections").fetchone()[0] == 1
        else:
            assert "semantic_projection" not in first


@pytest.mark.parametrize("decision", ["derive_lesson", "derive_watch"])
def test_derived_semantic_projection_creates_candidate_only_memory(
    ledger: Path, decision: str,
) -> None:
    """The derive projection route cannot create active/confirmed authority."""
    source = capture(activate())["item"]
    derived = derive([source], decision)
    with sqlite3.connect(ledger) as connection:
        projected = connection.execute(
            "SELECT m.status,m.confidence FROM memories m "
            "JOIN memory_projections p ON p.memory_id=m.id WHERE p.learning_item_id=?",
            (derived["item"]["item_id"],),
        ).fetchone()
    assert projected == ("candidate", 0.5)


@pytest.mark.parametrize(("decision", "expected"), [("reject", "rejected"), ("retire", "retired")])
def test_learning_correction_terminalizes_its_semantic_projection(
    ledger: Path, decision: str, expected: str,
) -> None:
    source = capture(activate())["item"]
    derived = derive([source], "derive_lesson")["item"]
    triage(derived, decision)
    with sqlite3.connect(ledger) as connection:
        assert connection.execute(
            "SELECT m.status FROM memories m JOIN memory_projections p ON p.memory_id=m.id "
            "WHERE p.learning_item_id=?", (derived["item_id"],),
        ).fetchone()[0] == expected


def test_semantic_projection_retry_recovers_without_duplicate_memory(
    ledger: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from odibi_anchor.codebase import _memory_lifecycle

    source = capture(activate())["item"]
    original = _memory_lifecycle.record_projection
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated projection interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(_memory_lifecycle, "record_projection", fail_once)
    with pytest.raises(RuntimeError, match="projection interruption"):
        derive([source], "derive_lesson")
    recovered = derive([source], "derive_lesson")
    assert recovered["semantic_projection"]["learning_item_id"] == recovered["item"]["item_id"]
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM memory_projections").fetchone()[0] == 1


def test_derivation_kind_and_source_version_rules(ledger: Path) -> None:
    observation = capture(activate())["item"]
    lesson = derive([observation], "derive_lesson")["item"]
    with pytest.raises(ValueError, match="version/status conflict"):
        derive([lesson], "derive_watch")
    with pytest.raises(ValueError, match="version/status conflict"):
        derive([observation], "derive_improvement", expected_source_versions={observation["item_id"]: 2})
    assert derive([lesson], "derive_improvement")["item"]["kind"] == "improvement_candidate"


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [("defer", "reopen", "open"), ("reject", None, "rejected"), ("retire", None, "retired")],
)
def test_exact_status_transitions_and_history(ledger: Path, first: str, second: str | None, expected: str) -> None:
    item = capture(activate())["item"]
    changed = triage(item, first)["item"]
    if second:
        changed = triage(changed, second)["item"]
    assert changed["status"] == expected
    shown = learning.structured_learning_context(command="show", item_id=item["item_id"], include_history=True)["item"]
    assert shown["events"][-1]["rationale"]
    if expected in {"rejected", "retired"}:
        assert item["item_id"] not in {
            entry["item_id"] for entry in learning.structured_learning_context(command="list")["items"]
        }
        with pytest.raises(ValueError, match="invalid status transition"):
            triage(changed, "reopen")


@pytest.mark.parametrize("missing", ["actor_kind", "rationale", "expected_version"])
def test_triage_attestation_failures_do_not_write(ledger: Path, missing: str) -> None:
    item = capture(activate())["item"]
    payload = {
        "decision": "defer",
        "item_id": item["item_id"],
        "expected_version": 1,
        "actor_kind": "human",
        "actor_ref": "reviewer:one",
        "decision_source": "decision:one",
        "rationale": "Explicit bounded rationale.",
    }
    payload.pop(missing)
    with pytest.raises(ValueError):
        learning.structured_learning_context(command="triage", **payload)
    assert learning.structured_learning_context(command="show", item_id=item["item_id"])["item"]["version"] == 1


@pytest.mark.parametrize(
    "value",
    [
        "https://host.example/path",
        "user@example.com",
        "token=abc",
        "X-Amz-Signature=abc",
        "(`/workspace/customer/file`)",
        "`/dbfs/table`",
        '"/home/user/file"',
        "path=/Volumes/catalog/schema",
        "-----BEGIN PRIVATE KEY-----",
        "first line\nsecond line",
    ],
)
def test_privacy_negative_vectors_are_rejected_without_partial_write(ledger: Path, value: str) -> None:
    obligation = activate()
    with pytest.raises(ValueError, match="invalid summary"):
        capture(obligation, summary=value)
    assert sqlite3.connect(ledger).execute("SELECT count(*) FROM learning_items").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("reference_type", "reference"),
    [
        ("test", "tests/test_dispatcher.py::test_gate[legacy-v1]"),
        ("file", "src/pkg/file.py#L10-L20"),
        ("problem", "PRB-2026-0001"),
        ("spec", "FOUNDATION_SPEC"),
        ("git_commit", "a" * 40),
        ("session", "decision:thread.123"),
    ],
)
def test_evidence_positive_vectors(ledger: Path, reference_type: str, reference: str) -> None:
    result = capture(activate(), evidence=[{"reference_type": reference_type, "reference": reference}])
    assert result["item"]["kind"] == "observation"


def test_dotted_hostname_signal_is_rejected(ledger: Path) -> None:
    obligation = activate()
    with pytest.raises(ValueError, match="invalid signal_key"):
        capture(obligation, signal_key="prod.example.com")


def test_schema_drift_and_checksum_drift_fail_closed(ledger: Path) -> None:
    activate()
    connection = sqlite3.connect(ledger)
    connection.execute("CREATE INDEX idx_learning_unexpected ON learning_items(summary)")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="schema drift"):
        learning.structured_learning_context(command="list")
    connection = sqlite3.connect(ledger)
    connection.execute("DROP INDEX idx_learning_unexpected")
    connection.execute("UPDATE anchor_schema_versions SET schema_sha256=?", ("0" * 64,))
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="checksum"):
        capture({"obligation_id": "lob_missing"})


def test_schema_drift_detects_arbitrarily_named_learning_table_index(ledger: Path) -> None:
    activate()
    connection = sqlite3.connect(ledger)
    connection.execute("CREATE INDEX arbitrary_name ON learning_items(summary)")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="schema drift"):
        learning.structured_learning_context(command="list")


def test_dedicated_connection_does_not_use_legacy_cache(ledger: Path) -> None:
    from odibi_anchor.codebase import _memory_db

    legacy = _memory_db.get_db(str(ledger))
    assert str(ledger) in _memory_db._connections
    activate()
    assert _memory_db._connections[str(ledger)][0] is legacy
    capture(learning.active_learning_obligation(
        project_id="project:test", task_window_id="ltw_1"
    ))
    assert _memory_db._connections[str(ledger)][0] is legacy
    _memory_db.close_db(str(ledger))


def test_concurrent_same_and_distinct_captures(ledger: Path) -> None:
    obligation = activate()

    def run(reference: str) -> dict:
        return capture(obligation, reference=reference)

    with ThreadPoolExecutor(max_workers=4) as pool:
        same = list(pool.map(run, ["src/same.py"] * 4))
    assert len({result["item"]["item_id"] for result in same}) == 1
    assert sum(not result["deduped"] for result in same) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        distinct = list(pool.map(run, ["src/one.py", "src/two.py"]))
    assert len({result["item"]["item_id"] for result in distinct}) == 2
    assert sqlite3.connect(ledger).execute("SELECT count(*) FROM learning_items").fetchone()[0] == 3


def test_concurrent_status_version_has_one_winner(ledger: Path) -> None:
    item = capture(activate())["item"]

    def run(decision: str) -> str:
        try:
            triage(item, decision, decision_source=f"decision:{decision}.race")
        except ValueError as error:
            return str(error)
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, ["reject", "retire"]))
    assert outcomes.count("won") == 1
    assert any(outcome == "learning version conflict" for outcome in outcomes)
    shown = learning.structured_learning_context(command="show", item_id=item["item_id"], include_history=True)["item"]
    assert len(shown["events"]) == 2


def test_deterministic_export_filter_and_verified_backup(ledger: Path) -> None:
    item = capture(activate())["item"]
    triage(item, "reject")
    first = learning.structured_learning_context(command="export")
    second = learning.structured_learning_context(command="export")
    assert first["exported_at"] != second["exported_at"]
    assert first["content_sha256"] == second["content_sha256"]
    assert first["complete"] is True
    assert first["items"][0]["status"] == "rejected"
    filtered = learning.structured_learning_context(command="export", status="rejected")
    assert filtered["complete"] is False
    assert filtered["filters"] == {"status": "rejected"}
    backup = learning.structured_learning_context(command="backup")
    assert backup["integrity_check"] == "ok"
    assert backup["row_counts"]["learning_items"] == 1
    destination = sqlite3.connect(backup["backup_path"])
    assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert destination.execute("SELECT count(*) FROM learning_items").fetchone()[0] == 1
    assert json.dumps(first, sort_keys=True)
