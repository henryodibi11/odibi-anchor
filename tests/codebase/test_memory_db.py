"""Tests for _memory_db SQLite storage layer."""

import os
import sys
from pathlib import Path
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from odibi_anchor.codebase._memory_db import (
    get_db,
    close_db,
    resolve_project,
    insert_memory,
    query_memories,
    confirm_memory_entry,
    reject_memory_entry,
    archive_stale,
    get_all_entries,
    entry_count,
    VALID_TYPES,
    VALID_STATUSES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite DB path."""
    path = str(tmp_path / "test_memory.db")
    yield path
    close_db(path)
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def populated_db(db_path):
    """DB with several entries for query testing."""
    entries = [
        {"project": "odibi", "type": "gotcha", "content": "Never use collect() on large DataFrames — causes OOM on driver", "tags": ["spark", "performance"]},
        {"project": "odibi", "type": "pattern", "content": "ReaderProvider.read_table() returns DataFrame with ingestion metadata columns", "tags": ["odibi", "io"]},
        {"project": "all", "type": "gotcha", "content": "Always use TRY_CAST instead of CAST on Excel-sourced columns", "tags": ["sql", "excel"]},
        {"project": "all", "type": "failure_pattern", "content": "ImportError on odibi.transformers — broken __init__.py chain, use _load() helper", "tags": ["import", "test"]},
        {"project": "queue-automation", "type": "decision", "content": "Pipeline uses 5-cell structure: Config Read Transform Quality Persist", "tags": ["pipeline", "convention"]},
        {"project": "odibi_anchor", "type": "convention", "content": "All anchor() tools default to markdown output format", "tags": ["anchor", "output"]},
    ]
    for e in entries:
        insert_memory(db_path, project=e["project"], type=e["type"],
                      content=e["content"], tags=e.get("tags"),
                      confidence=0.8, status="confirmed")
    # Model historical confirmed rows without reopening creation-time authority.
    get_db(db_path).execute("UPDATE memories SET status='confirmed'")
    get_db(db_path).commit()
    return db_path


# ---------------------------------------------------------------------------
# Connection & Schema
# ---------------------------------------------------------------------------


class TestConnection:
    """Test database connection and schema creation."""

    def test_creates_db_file(self, db_path):
        get_db(db_path)
        assert os.path.exists(db_path)

    def test_returns_same_connection(self, db_path):
        conn1 = get_db(db_path)
        conn2 = get_db(db_path)
        assert conn1 is conn2

    def test_close_removes_cached_connection(self, db_path):
        get_db(db_path)
        close_db(db_path)
        # Next call should create a new connection
        conn = get_db(db_path)
        assert conn is not None

    def test_schema_tables_exist(self, db_path):
        conn = get_db(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r["name"] for r in tables}
        assert "memories" in table_names
        assert "memory_fts" in table_names

    def test_insert_trigger_keeps_exactly_one_fts_row(self, db_path):
        memory = insert_memory(
            db_path, project="project:test", type="pattern", content="FTS insert trigger",
        )
        rows = get_db(db_path).execute(
            "SELECT content,tags FROM memory_fts WHERE id=?", (memory["id"],),
        ).fetchall()
        assert [tuple(row) for row in rows] == [("FTS insert trigger", "[]")]

    def test_reopen_repairs_fts_after_verified_content_addressed_backup(self, db_path):
        memory = insert_memory(
            db_path, project="project:test", type="pattern", content="Repair missing FTS row",
        )
        close_db(db_path)
        with __import__("sqlite3").connect(db_path) as connection:
            connection.execute("DELETE FROM memory_fts WHERE id=?", (memory["id"],))
        connection.close()
        reopened = get_db(db_path)
        cursor = reopened.execute(
            "SELECT count(*) FROM memory_fts WHERE id=?", (memory["id"],),
        )
        assert cursor.fetchone()[0] == 1
        cursor.close()
        assert len(list(Path(db_path).parent.glob(Path(db_path).name + ".pre-memory-fts-v1.*.bak"))) == 1
        close_db(db_path)
        del cursor, reopened
        __import__("gc").collect()

    def test_reopen_replaces_name_squatted_fts_trigger_before_future_updates(self, db_path):
        memory = insert_memory(
            db_path, project="project:test", type="pattern", content="Repair FTS trigger",
        )
        close_db(db_path)
        connection = __import__("sqlite3").connect(db_path)
        connection.executescript(
            "DROP TRIGGER memory_fts_update; "
            "CREATE TRIGGER memory_fts_update AFTER UPDATE ON memories BEGIN SELECT 1; END;"
        )
        connection.close()

        reopened = get_db(db_path)
        reopened.execute("UPDATE memories SET tags='[\"repaired\"]' WHERE id=?", (memory["id"],))
        reopened.commit()
        assert reopened.execute(
            "SELECT tags FROM memory_fts WHERE id=?", (memory["id"],),
        ).fetchone()[0] == '["repaired"]'
        assert len(list(Path(db_path).parent.glob(
            Path(db_path).name + ".pre-memory-fts-v1.*.bak"
        ))) == 1
        close_db(db_path)


# ---------------------------------------------------------------------------
# Project Resolution
# ---------------------------------------------------------------------------


class TestResolveProject:
    """Test project root to name mapping."""

    def test_odibi(self):
        assert resolve_project("/Workspace/Users/user@example.com/data-engineering/odibi") == "odibi"

    def test_queue_automation(self):
        assert resolve_project("/Workspace/Users/user@example.com/data-engineering/queue-automation") == "queue-automation"

    def test_odibi_anchor(self):
        assert resolve_project("/Workspace/Users/user@example.com/odibi_anchor") == "odibi_anchor"

    def test_eaai_utilities(self):
        assert resolve_project("/Workspace/Repos/eaai-common-resources/eaai-utilities") == "eaai-utilities"

    def test_unknown_fallback_to_dirname(self):
        assert resolve_project("/some/path/my-cool-project") == "my-cool-project"

    def test_path_object(self):
                assert resolve_project(Path("/Workspace/Users/user@example.com/data-engineering/odibi")) == "odibi"


# ---------------------------------------------------------------------------
# Insert & Deduplication
# ---------------------------------------------------------------------------


class TestInsert:
    """Test memory insertion and deduplication."""

    def test_basic_insert(self, db_path):
        result = insert_memory(db_path, project="odibi", type="gotcha",
                               content="Test gotcha content")
        assert result["action"] == "inserted"
        assert result["id"]
        assert result["project"] == "odibi"
        assert result["type"] == "gotcha"

    def test_dedup_same_content_same_project(self, db_path):
        r1 = insert_memory(db_path, project="odibi", type="gotcha", content="Same content")
        r2 = insert_memory(db_path, project="odibi", type="gotcha", content="Same content")
        assert r1["action"] == "inserted"
        assert r2["action"] == "deduped"
        assert r2["id"] == r1["id"]
        assert r2["use_count"] == 1

    def test_terminal_memory_does_not_poison_fresh_same_scope_candidate(self, db_path):
        stale = insert_memory(
            db_path, project="project:a", type="gotcha", content="Fresh evidence matters.",
            status="stale",
        )
        # Simulate a historical lifecycle transition; creation itself is
        # intentionally candidate-only.
        get_db(db_path).execute(
            "UPDATE memories SET status='stale' WHERE id=?", (stale["id"],)
        )
        get_db(db_path).commit()
        fresh = insert_memory(
            db_path, project="project:a", type="gotcha", content="Fresh evidence matters.",
            status="candidate", evidence={"test": "new task"},
        )
        assert fresh["action"] == "inserted"
        assert fresh["id"] != stale["id"]
        assert {
            row[0] for row in get_db(db_path).execute(
                "SELECT status FROM memories WHERE project='project:a'"
            )
        } == {"candidate", "stale"}

    def test_no_dedup_different_project(self, db_path):
        r1 = insert_memory(db_path, project="odibi", type="gotcha", content="Same content")
        r2 = insert_memory(db_path, project="all", type="gotcha", content="Same content")
        assert r1["action"] == "inserted"
        assert r2["action"] == "inserted"
        assert r2["id"] != r1["id"]

    def test_strips_whitespace(self, db_path):
        r1 = insert_memory(db_path, project="all", type="gotcha", content="  padded content  ")
        assert r1["content"] == "padded content"
        # Dedup should match stripped
        r2 = insert_memory(db_path, project="all", type="gotcha", content="padded content")
        assert r2["action"] == "deduped"

    def test_invalid_type_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid type"):
            insert_memory(db_path, project="all", type="INVALID", content="test")

    def test_invalid_status_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid status"):
            insert_memory(db_path, project="all", type="gotcha", content="test", status="WRONG")

    def test_empty_content_raises(self, db_path):
        with pytest.raises(ValueError, match="Content cannot be empty"):
            insert_memory(db_path, project="all", type="gotcha", content="   ")

    def test_insert_with_all_fields(self, db_path):
        result = insert_memory(
            db_path,
            project="odibi",
            type="pattern",
            content="Use dual-engine pattern for transformers",
            related_files=["src/transformers/*.py"],
            tags=["transformer", "pattern"],
            source="code review",
            confidence=0.9,
            status="confirmed",
            evidence={"reviewed_by": "hodibi", "date": "2025-01-01"},
        )
        assert result["action"] == "inserted"

        # Verify stored correctly
        entries = get_all_entries(db_path, project="odibi")
        match = [e for e in entries if e["id"] == result["id"]]
        assert len(match) == 1
        entry = match[0]
        assert entry["related_files"] == ["src/transformers/*.py"]
        assert entry["tags"] == ["transformer", "pattern"]
        assert entry["source"] == "code review"
        assert entry["confidence"] == 0.5
        assert entry["status"] == "candidate"
        assert entry["evidence"] == {"reviewed_by": "hodibi", "date": "2025-01-01"}

    @pytest.mark.parametrize("requested_status", ["active", "confirmed"])
    def test_insert_cannot_assign_lifecycle_authority(self, db_path, requested_status):
        result = insert_memory(
            db_path, project="all", type="gotcha",
            content=f"Caller requested {requested_status}", status=requested_status,
            confidence=1.0,
        )

        stored = get_db(db_path).execute(
            "SELECT status,confidence FROM memories WHERE id=?", (result["id"],)
        ).fetchone()
        assert stored["status"] == "candidate"
        assert stored["confidence"] == 0.5

    @pytest.mark.parametrize(
        "confidence",
        [float("nan"), float("inf"), float("-inf"), -0.1, 1.1, True, "0.5", None],
    )
    def test_insert_rejects_invalid_confidence_before_database_mutation(
        self, db_path, confidence,
    ):
        with pytest.raises(ValueError, match="finite real number"):
            insert_memory(
                db_path, project="all", type="gotcha", content="Invalid confidence",
                confidence=confidence,
            )
        assert not Path(db_path).exists()
        assert entry_count(db_path)["total"] == 0

    @pytest.mark.parametrize("confidence,expected", [(0.0, 0.0), (0.5, 0.5), (1.0, 0.5)])
    def test_insert_accepts_bounded_confidence_and_caps_candidates(
        self, db_path, confidence, expected,
    ):
        result = insert_memory(
            db_path, project="all", type="gotcha", content=f"Valid confidence {confidence}",
            confidence=confidence,
        )
        stored = get_db(db_path).execute(
            "SELECT status,confidence FROM memories WHERE id=?", (result["id"],)
        ).fetchone()
        assert tuple(stored) == ("candidate", expected)

    @pytest.mark.parametrize("confidence", [float("nan"), -1.0, True, "invalid"])
    def test_invalid_confidence_cannot_mutate_dedup_use_count(self, db_path, confidence):
        inserted = insert_memory(
            db_path, project="all", type="gotcha", content="Existing candidate",
        )
        with pytest.raises(ValueError, match="finite real number"):
            insert_memory(
                db_path, project="all", type="gotcha", content="Existing candidate",
                confidence=confidence,
            )
        stored = get_db(db_path).execute(
            "SELECT use_count,confidence,status FROM memories WHERE id=?", (inserted["id"],)
        ).fetchone()
        assert tuple(stored) == (0, 0.5, "candidate")


# ---------------------------------------------------------------------------
# Query — FTS
# ---------------------------------------------------------------------------


class TestQueryFTS:
    """Test full-text search queries."""

    def test_single_token(self, populated_db):
        results = query_memories(populated_db, query="collect")
        assert len(results) >= 1
        assert any("collect" in r["content"].lower() for r in results)

    def test_multi_token_and(self, populated_db):
        results = query_memories(populated_db, query="collect OOM")
        assert len(results) >= 1
        # Both tokens should appear in result
        assert any("collect" in r["content"] and "OOM" in r["content"] for r in results)

    def test_prefix_search(self, populated_db):
        results = query_memories(populated_db, query="Reader*")
        assert len(results) >= 1
        assert any("ReaderProvider" in r["content"] for r in results)

    def test_or_query(self, populated_db):
        results = query_memories(populated_db, query="collect OR ReaderProvider")
        assert len(results) >= 2

    def test_no_results(self, populated_db):
        results = query_memories(populated_db, query="xyznonexistent")
        assert results == []

    def test_empty_query_falls_back_to_sql(self, populated_db):
        results = query_memories(populated_db, query="", project="odibi")
        # Should return entries for odibi + 'all'
        assert len(results) >= 1

    def test_special_chars_sanitized(self, populated_db):
        # Should not crash on FTS5 special chars
        results = query_memories(populated_db, query="collect() - OOM;")
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Query — Project Filtering
# ---------------------------------------------------------------------------


class TestQueryProjectFiltering:
    """Test cross-project query behavior."""

    def test_project_includes_all(self, populated_db):
        results = query_memories(populated_db, project="odibi")
        projects = {r["project"] for r in results}
        # Should include both 'odibi' and 'all' entries
        assert "odibi" in projects
        assert "all" in projects

    def test_project_excludes_other_projects(self, populated_db):
        results = query_memories(populated_db, project="odibi")
        projects = {r["project"] for r in results}
        assert "queue-automation" not in projects
        assert "odibi_anchor" not in projects

    def test_no_project_returns_all(self, populated_db):
        results = query_memories(populated_db)
        # Should return entries from all projects
        projects = {r["project"] for r in results}
        assert len(projects) >= 3

    def test_cross_project_with_fts(self, populated_db):
        # Search from queue-automation should still find 'all' entries
        results = query_memories(populated_db, project="queue-automation", query="TRY_CAST")
        assert len(results) >= 1
        assert results[0]["project"] == "all"


# ---------------------------------------------------------------------------
# Query — Type/Status/Confidence Filtering
# ---------------------------------------------------------------------------


class TestQueryFilters:
    """Test type, status, and confidence filters."""

    def test_filter_by_type(self, populated_db):
        results = query_memories(populated_db, type="gotcha")
        assert all(r["type"] == "gotcha" for r in results)

    def test_filter_by_status_string(self, populated_db):
        results = query_memories(populated_db, status="confirmed")
        assert all(r["status"] == "confirmed" for r in results)

    def test_filter_by_status_list(self, populated_db):
        results = query_memories(populated_db, status=["confirmed", "active"])
        assert all(r["status"] in ("confirmed", "active") for r in results)

    def test_retired_excluded_by_default(self, db_path):
        insert_memory(db_path, project="all", type="gotcha", content="Active entry")
        retired = insert_memory(db_path, project="all", type="gotcha", content="Retired entry")
        get_db(db_path).execute(
            "UPDATE memories SET status='retired' WHERE id=?", (retired["id"],)
        )
        get_db(db_path).commit()
        results = query_memories(db_path)
        contents = [r["content"] for r in results]
        assert "Active entry" in contents
        assert "Retired entry" not in contents

    def test_min_confidence(self, db_path):
        insert_memory(db_path, project="all", type="gotcha",
                      content="Low confidence", confidence=0.3)
        high = insert_memory(db_path, project="all", type="gotcha",
                             content="High confidence", confidence=0.9)
        get_db(db_path).execute(
            "UPDATE memories SET confidence=0.9 WHERE id=?", (high["id"],)
        )
        get_db(db_path).commit()
        results = query_memories(db_path, min_confidence=0.7)
        assert len(results) == 1
        assert results[0]["content"] == "High confidence"

    def test_filter_by_tags(self, populated_db):
        results = query_memories(populated_db, tags=["spark"])
        assert len(results) >= 1
        assert all("spark" in r["tags"] for r in results)

    def test_filter_by_multiple_tags(self, populated_db):
        results = query_memories(populated_db, tags=["spark", "performance"])
        assert len(results) >= 1
        assert all("spark" in r["tags"] and "performance" in r["tags"] for r in results)

    def test_related_file_filter(self, db_path):
        insert_memory(db_path, project="all", type="pattern",
                      content="Transform pattern",
                      related_files=["src/transformers/*.py"])
        results = query_memories(db_path, related_file="src/transformers/deduplicate.py")
        assert len(results) >= 1

    def test_limit(self, populated_db):
        results = query_memories(populated_db, limit=2)
        assert len(results) <= 2


# ---------------------------------------------------------------------------
# Confirm / Reject Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Test confirm/reject transitions."""

    def test_free_form_human_review_is_blocked_without_mutation(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha",
                          content="Test entry", status="candidate")
        c = confirm_memory_entry(db_path, entry_id=r["id"], human_review={
            "actor_ref": "Henry", "decision_source": "agent supplied", "evidence": "claimed approval",
        })
        assert c["action"] == "confirmation_blocked"
        assert c["status"] == "candidate" and c["confirmation_count"] == 0
        assert c["human_promotion"] == {
            "status": "available_via_governed_owner_request",
            "commands": ["request_owner_activation", "request_owner_confirmation"],
        }
        assert c["unverified_authority_supplied"] is True

    def test_unverified_human_or_receipt_fields_are_blocked(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha", content="Test entry")
        c = confirm_memory_entry(db_path, entry_id=r["id"], human_review={
            "actor_ref": "henry", "decision_source": "review", "evidence": "reviewed exact content",
        }, actor_kind="human", approval_receipt={"id": "caller-asserted"})
        assert c["action"] == "confirmation_blocked"
        assert c["recurrence_promotion"]["status"] == "unavailable"

    def test_confirmation_does_not_change_confidence(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha",
                          content="Test", confidence=0.95)
        c = confirm_memory_entry(db_path, entry_id=r["id"], human_review={
            "actor_ref": "henry", "decision_source": "review", "evidence": "reviewed exact content",
        })
        assert c["confidence"] == 0.5

    def test_repeated_agent_confirmation_calls_cannot_self_confirm(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha", content="Unreviewed claim")
        results = [confirm_memory_entry(db_path, entry_id=r["id"]) for _ in range(3)]
        assert {item["action"] for item in results} == {"confirmation_blocked"}
        retained = get_db(db_path).execute(
            "SELECT status,confirmation_count FROM memories WHERE id=?", (r["id"],),
        ).fetchone()
        assert tuple(retained) == ("candidate", 0)

    def test_public_confirmation_returns_blocked_for_unauthenticated_human_review(self, db_path):
        from odibi_anchor.codebase.memory_context import confirm_memory

        r = insert_memory(db_path, project="all", type="gotcha", content="Unreviewed claim")
        result = confirm_memory(
            ".", r["id"], db_path=db_path,
            human_review={
                "actor_ref": "claimed-human", "decision_source": "prompt",
                "evidence": "agent-authored claim",
            },
        )
        assert result and result["action"] == "confirmation_blocked"
        retained = get_db(db_path).execute(
            "SELECT status,confirmation_count FROM memories WHERE id=?", (r["id"],),
        ).fetchone()
        assert tuple(retained) == ("candidate", 0)

    def test_fabricated_multi_task_recurrence_cannot_promote(self, db_path):
        from odibi_anchor.codebase._memory_lifecycle import (
            evaluate_application_effects,
            record_application,
            record_selection,
        )

        memory = insert_memory(
            db_path, project="private:a", type="gotcha", content="Unverified recurrence",
        )
        for number in range(3):
            task = f"fabricated-task-{number}"
            selection = record_selection(
                db_path, task_window_id=task, query={"number": number},
                memory_id=memory["id"], reason={"claim": "match"},
            )
            application = record_application(
                db_path, selection_id=selection["selection_id"],
                task_window_id=task, action=f"claimed-use-{number}",
            )
            evaluate_application_effects(
                db_path, application_id=application["application_id"], task_window_id=task,
                outcome="helpful", evidence={"arbitrary_note": f"agent claim {number}"},
            )
        result = confirm_memory_entry(db_path, entry_id=memory["id"])
        assert result["action"] == "confirmation_blocked"
        assert result["supported_recurrence_count"] == 0
        retained = get_db(db_path).execute(
            "SELECT status,confirmation_count FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()
        assert tuple(retained) == ("candidate", 0)

    def test_reject_decreases_confidence(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha",
                          content="Bad entry", confidence=0.8)
        get_db(db_path).execute(
            "UPDATE memories SET confidence=0.8 WHERE id=?", (r["id"],)
        )
        get_db(db_path).commit()
        c = reject_memory_entry(db_path, entry_id=r["id"])
        assert c["confidence"] == pytest.approx(0.65, abs=0.01)
        assert c["false_positive_count"] == 1

    def test_first_rejection_excludes_immediately(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha", content="Bad")
        c = reject_memory_entry(db_path, entry_id=r["id"])
        assert c["status"] == "rejected"
        assert c["false_positive_count"] == 1
        assert r["id"] not in {
            row["id"] for row in query_memories(db_path, project="all")
        }

    def test_reject_floors_confidence_at_0(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha",
                          content="Test", confidence=0.1)
        c = reject_memory_entry(db_path, entry_id=r["id"])
        assert c["confidence"] == 0.0

    def test_confirm_nonexistent_raises(self, db_path):
        get_db(db_path)  # ensure DB exists
        with pytest.raises(ValueError, match="not found"):
            confirm_memory_entry(db_path, entry_id="nonexistent-uuid")

    def test_reject_nonexistent_raises(self, db_path):
        get_db(db_path)
        with pytest.raises(ValueError, match="not found"):
            reject_memory_entry(db_path, entry_id="nonexistent-uuid")


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


class TestArchive:
    """Test stale entry archival."""

    def test_legacy_archive_is_noop_for_old_unused_entries(self, db_path):
        from datetime import datetime, timedelta, timezone
        conn = get_db(db_path)
        old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        conn.execute(
            """INSERT INTO memories (id, project, type, content, confidence, status,
               created, last_used, use_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("old-entry", "all", "gotcha", "Old unused entry", 0.5, "candidate",
             old_date, old_date, 0),
        )
        conn.commit()

        result = archive_stale(db_path, max_unused_days=90, min_use_count=2)
        assert result["archived_count"] == 0
        assert result["compatibility_noop"] is True

        entries = get_all_entries(db_path, status="stale")
        assert entries == []
        assert get_all_entries(db_path, status="candidate")[0]["id"] == "old-entry"

    def test_does_not_archive_confirmed(self, db_path):
        from datetime import datetime, timedelta, timezone
        conn = get_db(db_path)
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        conn.execute(
            """INSERT INTO memories (id, project, type, content, confidence, status,
               created, last_used, use_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("confirmed-old", "all", "gotcha", "Old but confirmed", 0.9, "confirmed",
             old_date, old_date, 0),
        )
        conn.commit()

        result = archive_stale(db_path, max_unused_days=90, min_use_count=2)
        assert result["archived_count"] == 0

    def test_does_not_archive_high_use(self, db_path):
        from datetime import datetime, timedelta, timezone
        conn = get_db(db_path)
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        conn.execute(
            """INSERT INTO memories (id, project, type, content, confidence, status,
               created, last_used, use_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("used-old", "all", "gotcha", "Old but heavily used", 0.5, "active",
             old_date, old_date, 10),
        )
        conn.commit()

        result = archive_stale(db_path, max_unused_days=90, min_use_count=2)
        assert result["archived_count"] == 0


# ---------------------------------------------------------------------------
# Bulk Operations
# ---------------------------------------------------------------------------


class TestBulkOps:
    """Test get_all_entries and entry_count."""

    def test_get_all_no_filter(self, populated_db):
        entries = get_all_entries(populated_db)
        assert len(entries) == 6  # all entries from fixture

    def test_get_all_by_project(self, populated_db):
        entries = get_all_entries(populated_db, project="odibi")
        projects = {e["project"] for e in entries}
        assert projects <= {"odibi", "all"}

    def test_get_all_by_status(self, populated_db):
        entries = get_all_entries(populated_db, status="confirmed")
        assert all(e["status"] == "confirmed" for e in entries)

    def test_entry_count(self, populated_db):
        counts = entry_count(populated_db)
        assert counts["total"] == 6
        assert counts["confirmed"] == 6


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_unicode_content(self, db_path):
        r = insert_memory(db_path, project="all", type="gotcha",
                          content="Use — em dash and 'smart quotes' and émojis 🎉")
        assert r["action"] == "inserted"
        results = query_memories(db_path, query="dash")
        assert len(results) >= 1

    def test_very_long_content(self, db_path):
        long_content = "word " * 5000
        r = insert_memory(db_path, project="all", type="discovery", content=long_content)
        assert r["action"] == "inserted"
        results = query_memories(db_path, query="word")
        assert len(results) >= 1

    def test_concurrent_inserts_no_crash(self, db_path):
        """Multiple rapid inserts don't cause DB lock issues."""
        for i in range(50):
            insert_memory(db_path, project="all", type="discovery",
                          content=f"Entry number {i} about topic {i % 5}")
        counts = entry_count(db_path)
        assert counts["total"] == 50

    def test_json_fields_roundtrip(self, db_path):
        insert_memory(
            db_path, project="all", type="pattern",
            content="Test JSON roundtrip",
            related_files=["src/**/*.py", "tests/**/*.py"],
            tags=["meta", "test"],
            evidence={"nested": {"key": [1, 2, 3]}, "bool": True},
        )
        entries = get_all_entries(db_path)
        e = entries[0]
        assert e["related_files"] == ["src/**/*.py", "tests/**/*.py"]
        assert e["tags"] == ["meta", "test"]
        assert e["evidence"]["nested"]["key"] == [1, 2, 3]
        assert e["evidence"]["bool"] is True

    def test_fts_update_after_content_change(self, db_path):
        """After manual UPDATE, FTS trigger syncs new content and removes old."""
        r = insert_memory(db_path, project="all", type="gotcha", content="Original content here")
        # Manually update content — trigger memory_fts_update syncs FTS automatically
        conn = get_db(db_path)
        conn.execute("UPDATE memories SET content = 'Changed content now' WHERE id = ?", (r["id"],))
        conn.commit()
        # FTS trigger removes old content and inserts new — "Original" no longer matches
        results_old = query_memories(db_path, query="Original")
        assert len(results_old) == 0  # Old content removed from FTS by trigger
        # New content IS findable via FTS
        results_new = query_memories(db_path, query="Changed")
        assert len(results_new) >= 1  # New content synced by trigger
