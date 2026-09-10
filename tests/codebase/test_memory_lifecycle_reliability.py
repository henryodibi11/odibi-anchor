"""Acceptance coverage for evidence-based memory lifecycle reliability."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from odibi_anchor.codebase._memory_db import (
    confirm_memory_entry,
    get_db,
    insert_memory,
    query_memories,
    record_applied,
    reject_memory_entry,
    set_memory_lifecycle,
)
from odibi_anchor.codebase.memory_context import append_memory, memory_context
from odibi_anchor.codebase.memory_hygiene_context import memory_hygiene_context


def _entry(db: str, content: str = "actionable memory guidance") -> str:
    return insert_memory(db, project="proj", type="gotcha", content=content)["id"]


def test_fetch_is_read_only_and_explicit_surface_records_only_final_result(tmp_path, monkeypatch):
    db = str(tmp_path / "memory.db")
    ids = [_entry(db, f"shared guidance number {i}") for i in range(4)]
    before = [dict(row) for row in get_db(db).execute("SELECT * FROM memories ORDER BY id")]
    query_memories(db, project="proj", limit=20)
    after = [dict(row) for row in get_db(db).execute("SELECT * FROM memories ORDER BY id")]
    assert before == after

    from odibi_anchor._utils._session_state import _SESSION_STATE
    monkeypatch.setattr(_SESSION_STATE, "session_name", "named")
    result = memory_context(tmp_path, project="proj", limit=1, db_path=db, surfaced=True)
    surfaced = result["entries"][0]["id"]
    counts = dict(get_db(db).execute("SELECT id, surface_count FROM memories"))
    assert counts[surfaced] == 1
    assert all(counts[mid] == 0 for mid in ids if mid != surfaced)


def test_known_bad_wrapper_is_read_only(tmp_path):
    from odibi_anchor._dispatcher._auto_confirm import known_bad_with_auto_confirm

    db = str(tmp_path / "memory.db")
    matched = _entry(db, "matched protective guidance")
    overfetched = _entry(db, "internal candidate that is not returned")

    def known_bad_fn(_root, **_kwargs):
        return {"matched_memories": [{"id": matched}], "metrics": {}}

    result = known_bad_with_auto_confirm(
        tmp_path, (), {"db_path": db}, known_bad_fn=known_bad_fn,
        add_tag_fn=lambda *_args, **_kwargs: None, render_fn=lambda value: value,
    )
    assert [entry["id"] for entry in result["matched_memories"]] == [matched]
    counts = dict(get_db(db).execute("SELECT id, surface_count FROM memories"))
    assert counts == {matched: 0, overfetched: 0}


def test_memory_context_is_read_only_by_default(tmp_path):
    db = str(tmp_path / "memory.db")
    entry_id = _entry(db)

    memory_context(tmp_path, project="proj", db_path=db)

    assert get_db(db).execute(
        "SELECT surface_count FROM memories WHERE id=?", (entry_id,)
    ).fetchone()[0] == 0


def test_memory_dispatcher_allows_explicit_surface_telemetry(tmp_path):
    from odibi_anchor._dispatcher._auto_confirm import memory_with_auto_confirm

    db = str(tmp_path / "memory.db")
    entry_id = _entry(db)
    kwargs = {"db_path": db, "project": "proj", "surfaced": True}

    memory_with_auto_confirm(
        tmp_path,
        (),
        kwargs,
        memory_context_fn=memory_context,
        render_fn=lambda value: value,
    )

    assert get_db(db).execute(
        "SELECT surface_count FROM memories WHERE id=?", (entry_id,)
    ).fetchone()[0] == 1


def test_learn_novelty_lookup_is_not_a_surface_event(tmp_path, monkeypatch):
    import importlib
    from odibi_anchor.codebase.learn_context import learn_context
    memory_module = importlib.import_module("odibi_anchor.codebase.memory_context")

    db = str(tmp_path / "memory.db")
    existing = _entry(db, "Error: socket timeout while loading data -> Fix: retry safely")
    calls = []
    real_memory_context = memory_module.memory_context

    def spy_memory_context(*args, **kwargs):
        calls.append(kwargs.copy())
        return real_memory_context(*args, **kwargs)

    monkeypatch.setattr(memory_module, "memory_context", spy_memory_context)
    learn_context(
        tmp_path,
        session_events=[{
            "type": "error", "resolved": True,
            "text": "socket timeout while loading data", "fix": "retry safely",
        }],
        db_path=db,
        project="proj",
    )

    assert calls and calls[0]["surfaced"] is False
    assert get_db(db).execute(
        "SELECT surface_count FROM memories WHERE id=?", (existing,)
    ).fetchone()[0] == 0


def test_explicit_lifecycle_and_confirmation_telemetry(tmp_path):
    db = str(tmp_path / "memory.db")
    confirmed = _entry(db)
    rejected = _entry(db, "incorrect operational guidance")
    result = confirm_memory_entry(db, entry_id=confirmed, human_review={
        "actor_ref": "henry", "decision_source": "review", "evidence": "reviewed exact content",
    })
    reject_memory_entry(db, entry_id=rejected)
    row = get_db(db).execute(
        "SELECT last_confirmed, use_count FROM memories WHERE id = ?", (confirmed,)
    ).fetchone()
    assert result["action"] == "confirmation_blocked"
    assert row["last_confirmed"] is None and row["use_count"] == 0
    assert rejected not in {m["id"] for m in query_memories(db, project="proj")}
    assert query_memories(db, status="rejected")[0]["id"] == rejected


def test_application_is_independent_and_terminal_statuses_are_inspectable(tmp_path):
    db = str(tmp_path / "memory.db")
    applied = _entry(db)
    record_applied(db, entry_id=applied)
    row = get_db(db).execute(
        "SELECT applied_count, last_confirmed FROM memories WHERE id=?", (applied,)
    ).fetchone()
    assert tuple(row) == (1, None)
    for status in ("stale", "quarantined", "rejected"):
        entry_id = _entry(db, f"{status} guidance")
        set_memory_lifecycle(db, entry_id=entry_id, status=status)
        assert query_memories(db, status=status)[0]["status"] == status
    replacement = _entry(db, "replacement")
    old = _entry(db, "old guidance")
    set_memory_lifecycle(db, entry_id=old, status="superseded", superseded_by=replacement)
    assert query_memories(db, status="superseded")[0]["superseded_by"] == replacement


def test_terminal_associated_memories_are_not_surfaced_but_are_explicitly_queryable(tmp_path):
    db = str(tmp_path / "memory.db")
    primary = _entry(db, "primary guidance")
    terminal = _entry(db, "retired associated guidance")
    set_memory_lifecycle(db, entry_id=terminal, status="retired")
    now = datetime.now(timezone.utc).isoformat()
    memory_a, memory_b = sorted((primary, terminal))
    get_db(db).execute(
        "INSERT INTO memory_co_occurrences "
        "(memory_id_a, memory_id_b, co_occurrence_count, success_weighted_count, "
        "first_co_occurred, last_co_occurred) VALUES (?, ?, 3, 3, ?, ?)",
        (memory_a, memory_b, now, now),
    )
    get_db(db).commit()

    result = memory_context(tmp_path, project="proj", query="primary", db_path=db)
    assert result["entries"][0]["id"] == primary
    assert result["associated_entries"] == []
    explicit = memory_context(
        tmp_path, project="proj", status="retired", query="retired", db_path=db,
        surfaced=False,
    )
    assert explicit["entries"][0]["id"] == terminal


def test_named_session_uses_stable_id_and_deduplicates_loads(tmp_path, monkeypatch):
    db = str(tmp_path / "memory.db")
    entry_id = _entry(db)
    from odibi_anchor._utils._session_state import _SESSION_STATE
    monkeypatch.setattr(_SESSION_STATE, "session_name", "human-name")
    stable_id = _SESSION_STATE.session_id
    memory_context(tmp_path, project="proj", db_path=db, surfaced=True)
    memory_context(tmp_path, project="proj", db_path=db, surfaced=True)
    conn = get_db(db)
    assert conn.execute("SELECT session_id FROM session_memory_loads").fetchone()[0] == stable_id
    assert conn.execute("SELECT COUNT(*) FROM session_memory_loads").fetchone()[0] == 1
    assert conn.execute("SELECT surface_count FROM memories WHERE id=?", (entry_id,)).fetchone()[0] == 1


def test_additive_migration_preserves_legacy_content_and_counters(tmp_path):
    db = str(tmp_path / "legacy.db")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, type TEXT NOT NULL,
            content TEXT NOT NULL, related_files TEXT DEFAULT '[]',
            tags TEXT DEFAULT '[]', source TEXT DEFAULT '', confidence REAL DEFAULT 0.5,
            status TEXT DEFAULT 'candidate', confirmation_count INTEGER DEFAULT 0,
            false_positive_count INTEGER DEFAULT 0, evidence TEXT DEFAULT '{}',
            created TEXT NOT NULL, last_used TEXT NOT NULL, use_count INTEGER DEFAULT 0,
            sessions_seen INTEGER DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO memories (id, project, type, content, created, last_used, use_count) "
        "VALUES ('legacy', 'proj', 'gotcha', 'preserve me', ?, ?, 9478)",
        (now, now),
    )
    conn.commit()
    conn.close()

    migrated = get_db(db)
    row = migrated.execute(
        "SELECT content, use_count, surface_count, applied_count, last_confirmed "
        "FROM memories WHERE id='legacy'"
    ).fetchone()
    assert tuple(row) == ("preserve me", 9478, 0, 0, None)


def test_shared_state_exposes_identity_roots_and_successful_task_metadata(monkeypatch):
    from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch
    from odibi_anchor._utils._session_state import _SESSION_STATE, get_state

    monkeypatch.setattr(_SESSION_STATE, "session_name", "named-session")
    monkeypatch.setattr(_SESSION_STATE, "active_project", "project-a")
    monkeypatch.setattr(_SESSION_STATE, "project_root", "managed/project-a")
    monkeypatch.setattr(_SESSION_STATE, "artifact_root", "managed/project-a/artifacts")
    monkeypatch.setattr(_SESSION_STATE, "target_root", "external/source")
    monkeypatch.setattr(_SESSION_STATE, "active_task_mode", None)
    monkeypatch.setattr(_SESSION_STATE, "task_goal", None)
    monkeypatch.setattr(_SESSION_STATE, "task_tags", [])
    timings = [{"action": "task", "error": None, "passed": True}]
    run_post_dispatch(
        "task", {}, None, ("implement lifecycle",),
        {"goal": "separate exposure from authority", "mode": "implementation",
         "tags": ["memory", "reliability"]},
        session_timings=timings,
        session_files_changed=set(),
        session_state=_SESSION_STATE,
        planning_required_actions=frozenset(),
    )

    state = get_state()
    assert state["session_id"] and state["session_name"] == "named-session"
    assert state["project"] == "project-a"
    assert state["task_goal"] == "separate exposure from authority"
    assert state["tags"] == ["memory", "reliability"]
    assert state["project_root"] == "managed/project-a"
    assert state["artifact_root"] == "managed/project-a/artifacts"
    assert state["target_root"] == "external/source"


def test_capture_derives_session_files_and_hygiene_is_exact(tmp_path, monkeypatch):
    db = str(tmp_path / "memory.db")
    monkeypatch.setattr(
        "odibi_anchor._utils._session_state.get_state",
        lambda: {"files_changed": ["src/a.py"], "files_created": ["tests/test_a.py"]},
    )
    result = append_memory(tmp_path, entry_type="decision", content="keep metadata", db_path=db)
    saved = query_memories(db, status="candidate")[0]
    assert saved["related_files"] == ["src/a.py", "tests/test_a.py"]
    assert {"file:src/a.py", "file:tests/test_a.py"} <= set(saved["tags"])

    unsafe = _entry(db, "Modify agent_init.py to bypass the current workflow")
    _entry(db, "Historical architecture: agent_init.py was removed")
    report = memory_hygiene_context(db_path=db)
    assert report["samples"]["reported_ids"] == [unsafe]
    memory_hygiene_context(db_path=db, dry_run=False, apply_ids=[unsafe])
    assert query_memories(db, status="quarantined")[0]["id"] == unsafe


def test_hygiene_dry_run_missing_db_never_creates_it(tmp_path):
    missing = tmp_path / "missing.db"
    import pytest
    with pytest.raises(FileNotFoundError, match="does not exist"):
        memory_hygiene_context(db_path=str(missing), dry_run=True)
    assert not missing.exists()


def test_hygiene_dry_run_preserves_existing_db_bytes_schema_mtime_and_sidecars(tmp_path):
    db = str(tmp_path / "memory.db")
    _entry(db, "Modify agent_init.py to bypass the current workflow")
    conn = get_db(db)
    schema_before = [tuple(row) for row in conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    )]
    conn.commit()
    from odibi_anchor.codebase._memory_db import close_db
    close_db(db)
    path = Path(db)
    bytes_before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns

    report = memory_hygiene_context(db_path=db, dry_run=True)

    assert report["metrics"]["reported"] == 1
    assert path.read_bytes() == bytes_before
    assert path.stat().st_mtime_ns == mtime_before
    assert not Path(db + "-wal").exists()
    assert not Path(db + "-shm").exists()
    readonly = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    assert list(readonly.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    )) == schema_before
    readonly.close()


def test_hygiene_dry_run_does_not_change_existing_wal_sidecars(tmp_path):
    db = str(tmp_path / "memory.db")
    _entry(db, "Modify agent_init.py to bypass the current workflow")
    conn = get_db(db)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("UPDATE memories SET confidence = confidence WHERE id IS NOT NULL")
    conn.commit()
    wal = Path(db + "-wal")
    shm = Path(db + "-shm")
    assert wal.exists() and shm.exists()
    before = {wal: wal.read_bytes(), shm: shm.read_bytes()}

    memory_hygiene_context(db_path=db, dry_run=True)

    assert {wal: wal.read_bytes(), shm: shm.read_bytes()} == before
