"""Database migration context builder for odibi_anchor.

Runs all schema migrations on the active memory DB to fix schema drift.
The ensure_* functions in _memory_db.py are idempotent (CREATE IF NOT EXISTS),
so this is always safe to run.

Dependencies:
    - odibi_anchor.codebase._memory_db
"""
from __future__ import annotations

import sqlite3
from typing import Any


def run_full_migration(db_path: str) -> dict:
    """Run all schema migrations on the target DB.

    Forces a fresh connection (bypasses cache) to ensure all ensure_*
    functions execute. Reports what tables existed before vs. after.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Dict with keys: tables_before, tables_after, tables_created,
        indexes_created, errors.
    """
    from odibi_anchor.codebase._memory_db import (
        _connections,
        close_db,
        ensure_annotations_table,
        ensure_co_occurrence_tables,
        ensure_session_audits_table,
        ensure_sessions_seen_column,
        ensure_trails_tables,
    )

    results: dict[str, Any] = {
        "tables_before": [],
        "tables_after": [],
        "tables_created": [],
        "indexes_created": [],
        "errors": [],
    }

    # Close cached connection to force fresh migration
    close_db(db_path)

    # Open raw connection for pre-migration snapshot
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%fts%'"
    )
    tables_before = {r[0] for r in cur.fetchall()}
    results["tables_before"] = sorted(tables_before)

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite%'"
    )
    indexes_before = {r[0] for r in cur.fetchall()}

    # Run all migrations in dependency order
    migrations = [
        ("sessions_seen_column", ensure_sessions_seen_column),
        ("session_audits", ensure_session_audits_table),
        ("co_occurrence_tables", ensure_co_occurrence_tables),
        ("trails_tables", ensure_trails_tables),
        ("annotations_table", ensure_annotations_table),
    ]

    for name, func in migrations:
        try:
            func(conn)
        except Exception as e:
            results["errors"].append(f"{name}: {type(e).__name__}: {e}")

    conn.commit()

    # Post-migration snapshot
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '%fts%'"
    )
    tables_after = {r[0] for r in cur.fetchall()}
    results["tables_after"] = sorted(tables_after)
    results["tables_created"] = sorted(tables_after - tables_before)

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite%'"
    )
    indexes_after = {r[0] for r in cur.fetchall()}
    results["indexes_created"] = sorted(indexes_after - indexes_before)

    conn.close()

    return results


def db_migrate_context(*, db_path: str | None = None, **kwargs) -> dict:
    """Run full schema migration on the active memory DB.

    Context builder for anchor("db_migrate"). Calls all ensure_* functions
    to create any missing tables/indexes.

    Args:
        db_path: Override path. Defaults to the central DB.

    Returns:
        Standard Anchor output contract dict.
    """
    from odibi_anchor.codebase._memory_db import _DEFAULT_DB_PATH

    target = db_path or _DEFAULT_DB_PATH
    results = run_full_migration(target)

    created_count = len(results["tables_created"])
    idx_count = len(results["indexes_created"])
    error_count = len(results["errors"])

    if created_count > 0:
        summary = (f"Migration complete: {created_count} tables created, "
                   f"{idx_count} indexes created, {error_count} errors")
    else:
        summary = "All tables already exist — schema is up to date."

    findings = []
    if results["tables_created"]:
        findings.append(f"Created tables: {', '.join(results['tables_created'])}")
    if results["indexes_created"]:
        findings.append(f"Created indexes: {', '.join(results['indexes_created'])}")
    if not results["tables_created"] and not results["indexes_created"]:
        findings.append("Schema fully migrated — no changes needed.")
    findings.append(f"Total tables: {len(results['tables_after'])}")

    return {
        "kind": "db_migrate",
        "subject": target,
        "summary": summary,
        "metrics": {
            "tables_created": created_count,
            "indexes_created": idx_count,
            "errors": error_count,
            "total_tables": len(results["tables_after"]),
        },
        "findings": findings,
        "risks": [f"Migration error: {e}" for e in results["errors"]],
        "samples": {
            "tables_before": results["tables_before"],
            "tables_after": results["tables_after"],
        },
        "suggested_next_actions": [
            "Run anchor('memory_stats') to verify DB health"
            if not results["errors"]
            else "Investigate errors and re-run",
        ],
    }
