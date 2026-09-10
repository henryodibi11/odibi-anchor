"""Tests for db_migrate_context.

Verifies:
- Creates missing tables on minimal DB
- Idempotent on fully migrated DB
- Output follows standard Anchor contract
"""
import sqlite3

import pytest

from odibi_anchor.codebase.db_migrate_context import (
    db_migrate_context,
    run_full_migration,
)


@pytest.fixture
def minimal_db(tmp_path):
    """Create a DB with only the base tables (no migrations)."""
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            related_files TEXT DEFAULT '[]',
            tags TEXT DEFAULT '[]',
            source TEXT DEFAULT '',
            confidence REAL DEFAULT 0.5,
            status TEXT DEFAULT 'candidate',
            confirmation_count INTEGER DEFAULT 0,
            false_positive_count INTEGER DEFAULT 0,
            evidence TEXT DEFAULT '{}',
            created TEXT NOT NULL,
            last_used TEXT NOT NULL,
            use_count INTEGER DEFAULT 0,
            sessions_seen INTEGER DEFAULT 0
        );
        CREATE TABLE session_audits (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            score INTEGER NOT NULL,
            max_score INTEGER NOT NULL,
            rating TEXT NOT NULL,
            gaps TEXT DEFAULT '[]',
            files_changed TEXT DEFAULT '[]',
            files_created TEXT DEFAULT '[]',
            actions TEXT DEFAULT '[]',
            total_actions INTEGER DEFAULT 0,
            total_time_ms REAL DEFAULT 0.0,
            errors INTEGER DEFAULT 0,
            learnings TEXT DEFAULT '[]',
            created TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return db


def test_creates_missing_tables(minimal_db):
    """Given a DB with only base tables, migration creates all relational tables."""
    result = run_full_migration(minimal_db)

    assert "session_memory_loads" in result["tables_created"]
    assert "memory_co_occurrences" in result["tables_created"]
    assert "memory_trails" in result["tables_created"]
    assert "trail_steps" in result["tables_created"]
    assert "memory_annotations" in result["tables_created"]
    assert result["errors"] == []
    assert len(result["tables_created"]) == 5


def test_idempotent_on_full_db(minimal_db):
    """Running migration twice produces no errors and creates nothing new."""
    # First run creates all tables
    first = run_full_migration(minimal_db)
    assert len(first["tables_created"]) == 5

    # Second run is idempotent
    second = run_full_migration(minimal_db)
    assert second["tables_created"] == []
    assert second["errors"] == []
    assert second["tables_before"] == second["tables_after"]


def test_context_builder_output_contract(minimal_db):
    """Output follows standard Anchor contract: kind, subject, summary, metrics, findings, risks."""
    result = db_migrate_context(db_path=minimal_db)

    assert result["kind"] == "db_migrate"
    assert isinstance(result["subject"], str)
    assert isinstance(result["summary"], str)
    assert isinstance(result["metrics"], dict)
    assert isinstance(result["findings"], list)
    assert isinstance(result["risks"], list)
    assert isinstance(result["samples"], dict)
    assert isinstance(result["suggested_next_actions"], list)

    # Metrics should have expected keys
    assert "tables_created" in result["metrics"]
    assert "indexes_created" in result["metrics"]
    assert "errors" in result["metrics"]
    assert "total_tables" in result["metrics"]
