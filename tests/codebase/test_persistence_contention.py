"""Deterministic multi-process qualification for the shared persistence boundary."""

from __future__ import annotations

import multiprocessing
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from odibi_anchor.codebase._memory_db import close_db, get_db
from odibi_anchor.codebase._memory_lifecycle import (
    initialize_schema as initialize_lifecycle,
)
from odibi_anchor.codebase._memory_lifecycle import (
    record_disposition_effects,
    record_selections,
)
from odibi_anchor.codebase._sqlite_contention import connect_shared_memory
from odibi_anchor.codebase._task_authority import initialize_schema as initialize_authority


def _context() -> multiprocessing.context.BaseContext:
    return multiprocessing.get_context("spawn")


def _blocked_authority_initialization(db_path: str, results: Any) -> None:
    from odibi_anchor.codebase._task_authority import initialize_schema

    started = time.monotonic()
    try:
        initialize_schema(db_path)
    except Exception as exc:  # process boundary reports the observable error contract
        cause = exc.__cause__
        results.put({
            "type": type(exc).__name__,
            "message": str(exc),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "store": getattr(exc, "store", None),
            "operation": getattr(exc, "operation", None),
            "timeout_ms": getattr(exc, "timeout_ms", None),
            "retryable": getattr(exc, "retryable", None),
            "cause_type": type(cause).__name__ if cause else None,
            "cause_code": getattr(cause, "sqlite_errorcode", None),
        })
    else:
        results.put({"type": None})


def _record_owned_selection(
    db_path: str,
    task_window_id: str,
    memory_id: str,
    label: str,
    start: Any,
    ready: Any,
    results: Any,
) -> None:
    from odibi_anchor.codebase._memory_lifecycle import record_selections

    ready.put(task_window_id)
    if not start.wait(timeout=10):
        results.put({"task": task_window_id, "error": "start barrier timed out"})
        return
    try:
        rows = record_selections(
            db_path,
            task_window_id=task_window_id,
            query={"fixture": label},
            selections=[{"memory_id": memory_id, "reason": {"fixture": label}}],
        )
    except Exception as exc:
        results.put({"task": task_window_id, "error": f"{type(exc).__name__}: {exc}"})
    else:
        results.put({"task": task_window_id, "selection": rows[0]})


def _foreign_disposition_after_observed_lock(
    db_path: str, selection_id: str, ready: Any, results: Any,
) -> None:
    from odibi_anchor.codebase._memory_lifecycle import record_disposition_effects

    probe = sqlite3.connect(db_path, isolation_level=None, timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        ready.put(getattr(error, "sqlite_errorcode", None))
    else:  # pragma: no cover - the parent owns the write lock before spawn
        probe.rollback()
        ready.put(None)
    finally:
        probe.close()
    try:
        record_disposition_effects(
            db_path,
            selection_id=selection_id,
            task_window_id="task-b",
            disposition="irrelevant",
            reason={"fixture": "foreign-owner"},
        )
    except Exception as exc:
        results.put({"type": type(exc).__name__, "message": str(exc)})
    else:
        results.put({"type": None})


def _publish_backup(db_path: str, start: Any, ready: Any, results: Any) -> None:
    from odibi_anchor.codebase._migration_backup import ensure_migration_backup

    ready.put("ready")
    if not start.wait(timeout=10):
        results.put({"error": "start barrier timed out"})
        return
    try:
        result = ensure_migration_backup(
            Path(db_path), tag="contention-v1", error_prefix="contention test",
        )
    except Exception as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})
    else:
        results.put(result)


def _queue_get(queue: Any) -> Any:
    try:
        return queue.get(timeout=15)
    except Empty as exc:  # pragma: no cover - diagnostic for a failed child process
        raise AssertionError("child process did not return a result") from exc


def _join(process: Any) -> None:
    process.join(timeout=15)
    assert process.exitcode == 0


def _race_backup(db_path: Path) -> list[dict[str, Any]]:
    context = _context()
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    processes = [
        context.Process(target=_publish_backup, args=(str(db_path), start, ready, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    assert [_queue_get(ready) for _ in processes] == ["ready", "ready"]
    start.set()
    returned = [_queue_get(results) for _ in processes]
    for process in processes:
        _join(process)
    assert all(isinstance(item, dict) for item in returned)
    return returned


def _memory_fixture(db_path: Path) -> None:
    connection = get_db(str(db_path))
    now = "2026-01-01T00:00:00+00:00"
    for memory_id, project, content in (
        ("memory-a", "project-a", "same text, asymmetric owner A"),
        ("memory-b", "project-b", "same text, asymmetric owner B"),
    ):
        connection.execute(
            "INSERT INTO memories "
            "(id,project,type,content,status,created,last_used) VALUES(?,?,?,?,?,?,?)",
            (memory_id, project, "gotcha", content, "candidate", now, now),
        )
    connection.commit()
    close_db(str(db_path))
    initialize_lifecycle(db_path)


@pytest.mark.skipif(not hasattr(sqlite3, "SQLITE_BUSY"), reason="SQLite error codes unavailable")
def test_shared_writer_timeout_is_typed_bounded_and_redacted(tmp_path: Path) -> None:
    db_path = tmp_path / "shared-memory.db"
    initialize_authority(db_path)
    blocker = sqlite3.connect(db_path, isolation_level=None, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    context = _context()
    results = context.Queue()
    process = context.Process(
        target=_blocked_authority_initialization, args=(str(db_path), results),
    )
    process.start()
    try:
        result = _queue_get(results)
    finally:
        blocker.rollback()
        blocker.close()
        _join(process)

    assert isinstance(result, dict)
    assert result["type"] == "PersistenceContentionError"
    assert result["store"] == "shared_memory"
    assert result["operation"] in {"configure_journal", "acquire_write"}
    assert result["timeout_ms"] == 5000
    assert result["retryable"] is True
    assert result["cause_type"] == "OperationalError"
    assert result["cause_code"] in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    assert 4500 <= result["duration_ms"] <= 9000
    assert str(db_path) not in result["message"]
    assert "BEGIN" not in result["message"]


def test_distinct_owner_processes_persist_asymmetric_rows_from_one_barrier(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "shared-memory.db"
    _memory_fixture(db_path)
    context = _context()
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    processes = [
        context.Process(
            target=_record_owned_selection,
            args=(str(db_path), task, memory, label, start, ready, results),
        )
        for task, memory, label in (
            ("task-a", "memory-a", "owner-a"),
            ("task-b", "memory-b", "owner-b"),
        )
    ]
    for process in processes:
        process.start()
    assert {_queue_get(ready) for _ in processes} == {"task-a", "task-b"}
    start.set()
    returned: dict[str, dict[str, Any]] = {}
    for _ in processes:
        item = _queue_get(results)
        assert isinstance(item, dict)
        returned[item["task"]] = item
    for process in processes:
        _join(process)

    assert set(returned) == {"task-a", "task-b"}
    assert all("error" not in item for item in returned.values())
    assert returned["task-a"]["selection"]["memory_id"] == "memory-a"
    assert returned["task-b"]["selection"]["memory_id"] == "memory-b"
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT task_window_id,memory_id FROM memory_selections ORDER BY task_window_id"
        ).fetchall() == [("task-a", "memory-a"), ("task-b", "memory-b")]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_integrity_conflict_is_not_reclassified_as_contention(tmp_path: Path) -> None:
    db_path = tmp_path / "shared-memory.db"
    _memory_fixture(db_path)
    record_selections(
        db_path,
        task_window_id="task-a",
        query={"fixture": "same-query"},
        selections=[{"memory_id": "memory-a", "reason": {"version": 1}}],
    )

    with pytest.raises(ValueError, match="conflicting selection reason") as exc_info:
        record_selections(
            db_path,
            task_window_id="task-a",
            query={"fixture": "same-query"},
            selections=[{"memory_id": "memory-a", "reason": {"version": 2}}],
        )

    assert type(exc_info.value).__name__ != "PersistenceContentionError"


def test_sqlite_integrity_error_is_not_reclassified_as_contention(tmp_path: Path) -> None:
    connection = connect_shared_memory(
        tmp_path / "shared-memory.db", owner_key_kind="project_id",
    )
    try:
        connection.execute("CREATE TABLE fixture(value TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO fixture VALUES('fixed')")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            connection.execute("INSERT INTO fixture VALUES('fixed')")
    finally:
        connection.close()

    assert type(exc_info.value).__name__ != "PersistenceContentionError"


def test_owner_denial_survives_a_real_busy_wait(tmp_path: Path) -> None:
    db_path = tmp_path / "shared-memory.db"
    _memory_fixture(db_path)
    selection = record_selections(
        db_path,
        task_window_id="task-a",
        query={"fixture": "foreign-owner"},
        selections=[{"memory_id": "memory-a", "reason": {"fixture": "foreign-owner"}}],
    )[0]
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    context = _context()
    ready = context.Queue()
    results = context.Queue()
    process = context.Process(
        target=_foreign_disposition_after_observed_lock,
        args=(str(db_path), selection["selection_id"], ready, results),
    )
    process.start()
    try:
        assert _queue_get(ready) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    finally:
        blocker.rollback()
        blocker.close()
    result = _queue_get(results)
    _join(process)

    assert result == {
        "type": "ValueError",
        "message": "unknown selection_id or selection belongs to a different task window",
    }
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT count(*) FROM memory_dispositions").fetchone() == (0,)
    finally:
        connection.close()


def test_injected_second_statement_failure_rolls_back_disposition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "shared-memory.db"
    _memory_fixture(db_path)
    selection = record_selections(
        db_path,
        task_window_id="task-a",
        query={"fixture": "rollback"},
        selections=[{"memory_id": "memory-a", "reason": {"fixture": "rollback"}}],
    )[0]
    lifecycle_globals = record_disposition_effects.__globals__
    original_connection = lifecycle_globals["_connection"]

    @contextmanager
    def fail_application_insert(path: str | Path) -> Any:
        with original_connection(path) as connection:
            connection.set_authorizer(
                lambda action, table, _column, _database, _trigger: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_INSERT and table == "memory_applications"
                    else sqlite3.SQLITE_OK
                )
            )
            yield connection

    # Bootstrap tests can reload the module after this function was collected.
    # Patch the exact global namespace used by the collected callable.
    monkeypatch.setitem(lifecycle_globals, "_connection", fail_application_insert)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        record_disposition_effects(
            db_path,
            selection_id=selection["selection_id"],
            task_window_id="task-a",
            disposition="applied",
            reason={"fixture": "rollback"},
            action="used-memory",
            context={"fixture": "rollback"},
        )

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT count(*) FROM memory_dispositions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM memory_applications").fetchone() == (0,)
    finally:
        connection.close()


def test_content_addressed_backup_has_one_verified_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "shared-memory.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
    connection.execute("INSERT INTO fixture VALUES('fixed')")
    connection.commit()
    connection.close()

    first_generation = _race_backup(db_path)
    assert all("error" not in item for item in first_generation)
    assert len({item["backup_path"] for item in first_generation}) == 1
    assert len({item["source_digest"] for item in first_generation}) == 1
    first_backup = Path(first_generation[0]["backup_path"])
    assert first_backup.is_file()

    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO fixture VALUES('second-generation')")
    connection.commit()
    connection.close()
    second_generation = _race_backup(db_path)
    assert all("error" not in item for item in second_generation)
    assert len({item["backup_path"] for item in second_generation}) == 1
    assert len({item["source_digest"] for item in second_generation}) == 1
    second_backup = Path(second_generation[0]["backup_path"])
    assert second_backup.is_file()
    assert first_backup != second_backup
    assert first_generation[0]["source_digest"] != second_generation[0]["source_digest"]
    assert not list(tmp_path.glob("*.tmp"))

    verified = sqlite3.connect(first_backup)
    try:
        assert verified.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert verified.execute("SELECT value FROM fixture").fetchone() == ("fixed",)
    finally:
        verified.close()
    verified = sqlite3.connect(second_backup)
    try:
        assert verified.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert verified.execute("SELECT value FROM fixture ORDER BY rowid").fetchall() == [
            ("fixed",), ("second-generation",),
        ]
    finally:
        verified.close()
