"""Owner isolation and v1-to-v2 migration tests for learning obligations."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from odibi_anchor.codebase import structured_learning_context as learning
from odibi_anchor.codebase._migration_backup import logical_digest


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "anchor-home" / ".agent_memory.db"
    monkeypatch.setenv("ANCHOR_HOME", str(path.parent))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(path))
    return path


def _legacy_ledger(path: Path, rows: list[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        for statement in learning.LEGACY_DDL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
            (learning.DOMAIN, 1, learning.LEGACY_SCHEMA_SHA256, "2026-09-01T00:00:00Z"),
        )
        connection.executemany(
            "INSERT INTO learning_obligations "
            "(obligation_id,task_window_id,session_ref,checkpoint_ref,project_ref,"
            "status,created_at,activated_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )


def _row(
    obligation_id: str,
    *,
    project_ref: str | None,
    status: str,
    task_window_id: str,
) -> tuple[object, ...]:
    closed_at = None if status == "active" else "2026-09-01T00:02:00Z"
    return (
        obligation_id,
        task_window_id,
        f"session:{obligation_id}",
        f"gate:{obligation_id}",
        project_ref,
        status,
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:01:00Z",
        closed_at,
    )


def _activate(path: Path, project: str, task: str) -> dict[str, object]:
    os.environ["ANCHOR_MEMORY_DB"] = str(path)
    return learning.activate_learning_obligation(
        project_id=project,
        task_window_id=task,
        session_ref=f"session:{project}:{task}",
        checkpoint_ref=f"gate:{project}:{task}",
    )


def test_v1_migration_preserves_rows_quarantines_ambiguity_and_is_idempotent(
    ledger: Path,
) -> None:
    legacy_rows = [
        _row(
            "lob_z_ambiguous",
            project_ref=None,
            status="active",
            task_window_id="task-z",
        ),
        _row(
            "lob_a_owned",
            project_ref="project-a",
            status="assessed",
            task_window_id="task-a",
        ),
    ]
    _legacy_ledger(ledger, legacy_rows)
    source_digest = logical_digest(ledger)

    first = learning.initialize_learning_schema()
    second = learning.initialize_learning_schema()

    assert first["schema_status"] == "migrated"
    assert second["schema_status"] == "exact"
    migration = second["migration"]
    assert migration["source_digest"] == source_digest
    assert migration["row_count"] == 2
    assert migration["quarantined_count"] == 1
    assert migration["pre_row_digest"] == migration["post_row_digest"]
    assert logical_digest(migration["backup_path"]) == source_digest
    assert first["marker_path"] == second["marker_path"]
    assert len(list(ledger.parent.glob(f"{ledger.name}.pre-learning-v1-to-v2.*.bak"))) == 1
    assert len(list(ledger.parent.glob(f"{ledger.name}.learning-v2.*.json"))) == 1

    with sqlite3.connect(ledger) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
            "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at "
            "FROM learning_obligations ORDER BY obligation_id"
        ).fetchall()
    assert dict(rows[0]) == {
        "obligation_id": "lob_a_owned",
        "project_id": "project-a",
        "task_window_id": "task-a",
        "session_ref": "session:lob_a_owned",
        "checkpoint_ref": "gate:lob_a_owned",
        "project_ref": "project-a",
        "owner_schema_version": 1,
        "owner_state": "owned",
        "status": "assessed",
        "created_at": "2026-09-01T00:00:00Z",
        "activated_at": "2026-09-01T00:01:00Z",
        "closed_at": "2026-09-01T00:02:00Z",
    }
    assert dict(rows[1]) == {
        "obligation_id": "lob_z_ambiguous",
        "project_id": None,
        "task_window_id": "task-z",
        "session_ref": "session:lob_z_ambiguous",
        "checkpoint_ref": "gate:lob_z_ambiguous",
        "project_ref": None,
        "owner_schema_version": 1,
        "owner_state": "quarantined",
        "status": "active",
        "created_at": "2026-09-01T00:00:00Z",
        "activated_at": "2026-09-01T00:01:00Z",
        "closed_at": None,
    }
    status = learning.learning_migration_status()
    assert status["quarantined_obligations"][0]["obligation_id"] == "lob_z_ambiguous"
    assert learning.active_learning_obligation(
        project_id="project-z", task_window_id="task-z"
    ) is None


def test_owner_key_allows_a_and_b_but_rejects_same_owner_duplicate(ledger: Path) -> None:
    a = _activate(ledger, "project-a", "task-1")
    b = _activate(ledger, "project-b", "task-1")

    assert learning.active_learning_obligation(
        project_id="project-a", task_window_id="task-1"
    )["obligation_id"] == a["obligation_id"]
    assert learning.active_learning_obligation(
        project_id="project-b", task_window_id="task-1"
    )["obligation_id"] == b["obligation_id"]
    with pytest.raises(RuntimeError, match="already exists for owner"):
        _activate(ledger, "project-a", "task-1")


def test_cross_owner_get_capture_close_and_latest_do_not_disclose_or_mutate(
    ledger: Path,
) -> None:
    obligation = _activate(ledger, "project-a", "task-a")
    owner = {
        "_project_id": "project-a",
        "_task_window_id": "task-a",
        "_obligation_id": obligation["obligation_id"],
    }
    foreign = owner | {"_project_id": "project-b"}

    with pytest.raises(ValueError, match="unavailable for owner"):
        learning._structured_learning_dispatch(
            command="capture",
            **foreign,
            observation_type="friction",
            summary="A bounded owner-isolation observation.",
            signal_key="learning.owner-isolation",
            impact="medium",
            applicability_scope="project_local",
            project_refs=["project-a"],
            work_package_refs=[],
            environment_refs=[],
            provenance={"source_action": "test", "source_version": "v1"},
            evidence=[{
                "reference_type": "test",
                "reference": "tests/codebase/test_learning_owner_migration.py",
            }],
        )
    assert learning.learning_obligation(
        str(obligation["obligation_id"]),
        project_id="project-b",
        task_window_id="task-a",
    ) is None
    with pytest.raises(ValueError, match="unavailable for owner"):
        learning.close_learning_obligation_legacy(
            str(obligation["obligation_id"]),
            project_id="project-b",
            task_window_id="task-a",
        )
    assert learning.active_learning_obligation(
        project_id="project-a", task_window_id="task-a"
    )["obligation_id"] == obligation["obligation_id"]

    learning.close_learning_obligation_legacy(
        str(obligation["obligation_id"]),
        project_id="project-a",
        task_window_id="task-a",
    )
    assert learning.latest_closed_learning_obligation(
        project_id="project-b", task_window_id="task-a"
    ) is None
    assert learning.latest_closed_learning_obligation(
        project_id="project-a", task_window_id="task-a"
    )["obligation_id"] == obligation["obligation_id"]


def test_interrupted_migration_rolls_back_and_reruns(
    ledger: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy_ledger(
        ledger,
        [_row("lob_owned", project_ref="project-a", status="active", task_window_id="task-a")],
    )
    source_digest = logical_digest(ledger)
    original = learning._migrate_v1_obligations

    def interrupt(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(learning, "_migrate_v1_obligations", interrupt)
    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        learning.initialize_learning_schema()
    assert logical_digest(ledger) == source_digest
    with sqlite3.connect(ledger) as connection:
        assert learning._domain_schema_status(connection) == "legacy_v1"

    monkeypatch.setattr(learning, "_migrate_v1_obligations", original)
    assert learning.initialize_learning_schema()["schema_status"] == "migrated"


def test_backup_failure_and_partial_schema_fail_without_additional_mutation(
    ledger: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy_ledger(
        ledger,
        [_row("lob_owned", project_ref="project-a", status="active", task_window_id="task-a")],
    )
    source_digest = logical_digest(ledger)
    from odibi_anchor.codebase import _migration_backup
    original_backup = _migration_backup.ensure_migration_backup

    def fail_backup(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated backup failure")

    monkeypatch.setattr(_migration_backup, "ensure_migration_backup", fail_backup)
    with pytest.raises(RuntimeError, match="simulated backup failure"):
        learning.initialize_learning_schema()
    assert logical_digest(ledger) == source_digest

    monkeypatch.setattr(_migration_backup, "ensure_migration_backup", original_backup)
    with sqlite3.connect(ledger) as connection:
        connection.execute("ALTER TABLE learning_obligations ADD COLUMN project_id TEXT")
    partial_digest = logical_digest(ledger)
    with pytest.raises(RuntimeError, match="schema drift"):
        learning.initialize_learning_schema()
    assert logical_digest(ledger) == partial_digest


def test_restart_refuses_stale_version_tampered_marker_and_missing_backup(
    ledger: Path,
) -> None:
    _legacy_ledger(
        ledger,
        [_row("lob_owned", project_ref="project-a", status="active", task_window_id="task-a")],
    )
    migrated = learning.initialize_learning_schema()
    marker = Path(migrated["marker_path"])
    backup = Path(migrated["migration"]["backup_path"])

    original_marker = marker.read_bytes()
    marker_payload = json.loads(original_marker)
    marker_payload["post_migration_digest"] = "0" * 64
    marker.write_text(json.dumps(marker_payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="marker digest mismatch"):
        learning.initialize_learning_schema()
    marker.write_bytes(original_marker)

    hidden_backup = backup.with_suffix(backup.suffix + ".hidden")
    backup.rename(hidden_backup)
    with pytest.raises(RuntimeError, match="backup verification failed"):
        learning.initialize_learning_schema()
    hidden_backup.rename(backup)

    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "UPDATE anchor_schema_versions SET version=? WHERE domain=?",
            (learning.VERSION + 1, learning.DOMAIN),
        )
    with pytest.raises(RuntimeError, match="schema drift"):
        learning.initialize_learning_schema()


def test_restart_ignores_unrelated_shared_database_writes(ledger: Path) -> None:
    _legacy_ledger(
        ledger,
        [_row("lob_owned", project_ref="project-a", status="active", task_window_id="task-a")],
    )
    migrated = learning.initialize_learning_schema()

    with sqlite3.connect(ledger) as connection:
        connection.execute(
            "CREATE TABLE unrelated_task_authority "
            "(record_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO unrelated_task_authority VALUES(?,?)",
            ("task-1", "unrelated durable state"),
        )

    restarted = learning.initialize_learning_schema()

    assert restarted["schema_status"] == "exact"
    assert restarted["marker_path"] == migrated["marker_path"]


def test_rollback_requires_unchanged_marker_and_refuses_incompatible_write(
    ledger: Path,
) -> None:
    _legacy_ledger(
        ledger,
        [_row("lob_owned", project_ref="project-a", status="active", task_window_id="task-a")],
    )
    source_digest = logical_digest(ledger)
    learning.initialize_learning_schema()
    assert learning.learning_migration_status()["rollback_eligible"] is True

    from odibi_anchor.codebase import _memory_db

    cached = sqlite3.connect(ledger)
    _memory_db._connections[str(ledger)] = (cached, ledger.stat().st_mtime)

    rolled_back = learning.rollback_learning_obligation_migration()
    assert rolled_back["status"] == "rolled_back"
    assert logical_digest(ledger) == source_digest
    assert str(ledger) not in _memory_db._connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        cached.execute("SELECT 1")

    learning.initialize_learning_schema()
    _activate(ledger, "project-b", "task-b")
    before_refusal = logical_digest(ledger)
    status = learning.learning_migration_status()
    assert status["rollback_eligible"] is False
    assert status["rollback_blocker"] == "incompatible learning writes followed migration"
    with pytest.raises(RuntimeError, match="incompatible learning writes"):
        learning.rollback_learning_obligation_migration()
    assert logical_digest(ledger) == before_refusal


def test_two_processes_can_activate_distinct_project_owners(ledger: Path) -> None:
    learning.initialize_learning_schema()
    script = """
from odibi_anchor.codebase.structured_learning_context import activate_learning_obligation
import os
activate_learning_obligation(
    project_id=os.environ['OWNER_PROJECT'], task_window_id='shared-task',
    session_ref='session:' + os.environ['OWNER_PROJECT'],
    checkpoint_ref='gate:' + os.environ['OWNER_PROJECT'],
)
"""
    processes = []
    for project in ("project-a", "project-b"):
        environment = os.environ.copy()
        environment["ANCHOR_MEMORY_DB"] = str(ledger)
        environment["ANCHOR_HOME"] = str(ledger.parent)
        environment["OWNER_PROJECT"] = project
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).parents[2],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    results = [process.communicate(timeout=20) for process in processes]
    assert [
        (process.returncode, stderr)
        for process, (_, stderr) in zip(processes, results, strict=True)
    ] == [
        (0, ""),
        (0, ""),
    ]
    with sqlite3.connect(ledger) as connection:
        owners = connection.execute(
            "SELECT project_id,task_window_id FROM learning_obligations "
            "WHERE status='active' ORDER BY project_id"
        ).fetchall()
    assert owners == [("project-a", "shared-task"), ("project-b", "shared-task")]
