"""SQLite-backed memory storage for odibi_anchor.

Replaces the JSONL/MD dual-file system with a single SQLite database.
One central DB supports cross-project memory, FTS5 search, deduplication,
and confidence-based lifecycle management.

Usage:
    from odibi_anchor.codebase._memory_db import (
        get_db, resolve_project, insert_memory, query_memories,
        confirm_memory_entry, reject_memory_entry, archive_stale,
    )

    db_path = "/path/to/.agent_memory.db"
    conn = get_db(db_path)
    insert_memory(db_path, project="example-project", type="gotcha", content="...")
    results = query_memories(db_path, query="collect OOM", project="example-project")
"""

from __future__ import annotations

import hashlib  # noqa: F401 - retained to preserve legacy module import compatibility
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone  # noqa: F401 - legacy import compatibility
from fnmatch import fnmatch
from numbers import Real
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def _resolve_default_db_path() -> str:
    """Resolve the shared memory DB path from the active environment profile.

    Single source of truth is the dispatcher's resolved environment
    (``_boot._ENV["memory_db"]``), which derives ``<anchor_root>/.agent_memory.db``
    per environment and honors the ``ANCHOR_MEMORY_DB`` override. Falls back to the
    env var, then a Databricks default, if the boot module is unavailable.
    """
    env_override = os.environ.get("ANCHOR_MEMORY_DB")
    if env_override:
        return env_override
    try:
        from odibi_anchor._dispatcher._boot import _ENV
        path = _ENV.get("memory_db")
        if path:
            return path
    except Exception:  # pragma: no cover - defensive: boot import should always work
        pass
    from odibi_anchor._runtime_paths import resolve_runtime_paths

    return str(resolve_runtime_paths().anchor_home / ".agent_memory.db")


_DEFAULT_DB_PATH = _resolve_default_db_path()

_PROJECT_ROUTING: dict[str, str] = {
    "eaai-common-resources/eaai-utilities": "eaai-utilities",
    "data-engineering/queue-automation": "queue-automation",
    "odibi_anchor": "odibi_anchor",

    "document_profiler": "document_profiler",
}

VALID_TYPES = frozenset({
    "gotcha", "decision", "pattern", "convention",
    "failure_pattern", "discovery", "tool_call", "preference",
})

VALID_STATUSES = frozenset({
    "candidate", "active", "confirmed", "rejected", "superseded", "stale",
    "quarantined", "retired",
})

#: Maximum confidence any creation route may persist for a `candidate` row.
#: Every ingress path -- runtime, append, import, projection, and reviewed seed --
#: clamps to this ceiling. Raising it for reviewed seeds requires a separately
#: governed reviewed-seed confidence policy; until then the ceiling is uniform
#: so no route can imply earned authority through ranking (WI-2026-0019).
CANDIDATE_CONFIDENCE_CEILING = 0.5

# Legacy counters are deprecated non-authoritative compatibility fields:
# ``use_count``, ``surface_count``, ``applied_count``, and migrated ``sessions_seen``.
# Immutable lifecycle selections, applications, and evaluations are authoritative.
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS memories (
    id                   TEXT PRIMARY KEY,
    project              TEXT NOT NULL,
    type                 TEXT NOT NULL,
    content              TEXT NOT NULL,
    related_files        TEXT DEFAULT '[]',
    tags                 TEXT DEFAULT '[]',
    source               TEXT DEFAULT '',
    confidence           REAL DEFAULT 0.5,
    status               TEXT DEFAULT 'candidate',
    confirmation_count   INTEGER DEFAULT 0,
    false_positive_count INTEGER DEFAULT 0,
    evidence             TEXT DEFAULT '{}',
    created              TEXT NOT NULL,
    last_used            TEXT NOT NULL,
    use_count            INTEGER DEFAULT 0,
    surface_count        INTEGER DEFAULT 0,
    last_surfaced        TEXT DEFAULT NULL,
    applied_count        INTEGER DEFAULT 0,
    last_confirmed       TEXT DEFAULT NULL,
    superseded_by        TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_confidence ON memories(confidence);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id,
    content,
    tags
);

-- FTS sync triggers: keep memory_fts consistent with memories table
CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts (id, content, tags) VALUES (NEW.id, NEW.content, NEW.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE OF content, tags ON memories BEGIN
    DELETE FROM memory_fts WHERE id = OLD.id;
    INSERT INTO memory_fts (id, content, tags) VALUES (NEW.id, NEW.content, NEW.tags);
END;

CREATE TABLE IF NOT EXISTS session_audits (
    id           TEXT PRIMARY KEY,
    framework_epoch TEXT DEFAULT 'legacy',
    project      TEXT NOT NULL,
    score        INTEGER NOT NULL,
    max_score    INTEGER NOT NULL,
    rating       TEXT NOT NULL,
    gaps         TEXT DEFAULT '[]',
    files_changed TEXT DEFAULT '[]',
    files_created TEXT DEFAULT '[]',
    actions      TEXT DEFAULT '[]',
    total_actions INTEGER DEFAULT 0,
    total_time_ms REAL DEFAULT 0.0,
    errors       INTEGER DEFAULT 0,
    learnings    TEXT DEFAULT '[]',
    created      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audits_project ON session_audits(project);
CREATE INDEX IF NOT EXISTS idx_audits_created ON session_audits(created);
CREATE INDEX IF NOT EXISTS idx_audits_score ON session_audits(score);
"""


# ---------------------------------------------------------------------------
# Connection Management
# ---------------------------------------------------------------------------

_connections: dict[str, tuple[sqlite3.Connection, float]] = {}


_FTS_TRIGGER_SQL = {
    "memory_fts_insert": (
        "CREATE TRIGGER memory_fts_insert AFTER INSERT ON memories BEGIN "
        "INSERT INTO memory_fts (id, content, tags) VALUES (NEW.id, NEW.content, NEW.tags); END"
    ),
    "memory_fts_delete": (
        "CREATE TRIGGER memory_fts_delete AFTER DELETE ON memories BEGIN "
        "DELETE FROM memory_fts WHERE id = OLD.id; END"
    ),
    "memory_fts_update": (
        "CREATE TRIGGER memory_fts_update AFTER UPDATE OF content, tags ON memories BEGIN "
        "DELETE FROM memory_fts WHERE id = OLD.id; "
        "INSERT INTO memory_fts (id, content, tags) VALUES (NEW.id, NEW.content, NEW.tags); END"
    ),
}


def _normalized_sql(value: str) -> str:
    return " ".join(value.rstrip(";").split()).lower()


def _fts_repair_required(db_path: str) -> bool:
    """Inspect an existing store read-only for missing or divergent FTS state."""
    path = Path(db_path).expanduser().resolve(strict=False)
    if not path.is_file():
        return False
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "memories" not in tables:
            return False
        if "memory_fts" not in tables:
            return True
        triggers = dict(connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('memory_fts_insert','memory_fts_delete','memory_fts_update')"
        ).fetchall())
        if any(
            name not in triggers
            or _normalized_sql(triggers[name]) != _normalized_sql(expected)
            for name, expected in _FTS_TRIGGER_SQL.items()
        ):
            return True
        missing_or_stale = connection.execute(
            "SELECT 1 FROM memories m LEFT JOIN memory_fts f ON f.id=m.id "
            "GROUP BY m.id HAVING count(f.rowid)!=1 OR max(f.content)!=m.content "
            "OR max(f.tags)!=m.tags LIMIT 1"
        ).fetchone()
        orphan = connection.execute(
            "SELECT 1 FROM memory_fts f LEFT JOIN memories m ON m.id=f.id "
            "WHERE m.id IS NULL LIMIT 1"
        ).fetchone()
        return missing_or_stale is not None or orphan is not None
    finally:
        connection.close()


def _repair_fts_index(conn: sqlite3.Connection) -> None:
    """Rebuild and verify the derived FTS index inside the caller transaction."""
    for name in _FTS_TRIGGER_SQL:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.executescript(";\n".join(_FTS_TRIGGER_SQL.values()) + ";")
    conn.execute("DELETE FROM memory_fts")
    conn.execute("INSERT INTO memory_fts(id,content,tags) SELECT id,content,tags FROM memories")
    mismatch = conn.execute(
        "SELECT 1 FROM memories m LEFT JOIN memory_fts f ON f.id=m.id "
        "GROUP BY m.id HAVING count(f.rowid)!=1 OR max(f.content)!=m.content "
        "OR max(f.tags)!=m.tags LIMIT 1"
    ).fetchone()
    orphan = conn.execute(
        "SELECT 1 FROM memory_fts f LEFT JOIN memories m ON m.id=f.id "
        "WHERE m.id IS NULL LIMIT 1"
    ).fetchone()
    if mismatch is not None or orphan is not None:
        raise RuntimeError("memory FTS repair verification failed")


def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """Get or create a SQLite connection with schema initialized.

    Validates that the cached connection is still pointing at the same file
    by checking mtime. If the file was replaced (e.g., git pull), the cached
    connection is closed and a fresh one is created.

    Args:
        db_path: Path to the SQLite database file. Defaults to central DB.

    Returns:
        sqlite3.Connection with row_factory=sqlite3.Row.
    """
    db_path = db_path or _DEFAULT_DB_PATH

    if db_path in _connections:
        conn, cached_mtime = _connections[db_path]
        # Check if file still exists and connection is functional
        try:
            current_mtime = os.path.getmtime(db_path)
            if current_mtime == cached_mtime:
                return conn
            # Mtime changed — likely our own writes. Verify connection is alive.
            try:
                conn.execute("SELECT 1")
                # Connection still works — update cached mtime and return
                _connections[db_path] = (conn, current_mtime)
                return conn
            except Exception:
                # SILENT-OK: connection broken — close and recreate
                logger.info("DB connection broken, reconnecting: %s", db_path)
                conn.close()
        except OSError:
            # File doesn't exist anymore — close and recreate
            conn.close()
        del _connections[db_path]

    repair_fts = _fts_repair_required(db_path)
    if repair_fts:
        from odibi_anchor.codebase._migration_backup import ensure_migration_backup

        ensure_migration_backup(
            Path(db_path).expanduser(), tag="memory-fts-v1",
            error_prefix="memory FTS migration",
        )

    # Ensure parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

    conn = connect_shared_memory(
        db_path, owner_key_kind="project", check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(_SCHEMA_SQL)
    if repair_fts:
        _repair_fts_index(conn)
    conn.commit()

    # Migration: ensure new columns exist
    ensure_sessions_seen_column(conn)
    ensure_lifecycle_columns(conn)
    ensure_session_audits_table(conn)
    ensure_co_occurrence_tables(conn)
    ensure_trails_tables(conn)
    ensure_annotations_table(conn)

    # Cache connection with current mtime
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        mtime = 0.0
    _connections[db_path] = (conn, mtime)
    return conn


def close_db(db_path: str | None = None) -> None:
    """Close and remove cached connection."""
    db_path = db_path or _DEFAULT_DB_PATH
    entry = _connections.pop(db_path, None)
    if entry:
        entry[0].close()


def close_all_dbs() -> None:
    """Close every cached connection held by the current process."""
    for db_path in list(_connections):
        close_db(db_path)


# Structured learning intentionally lives behind dedicated, short-lived connections.
# Imports are local to keep every legacy memory open/read path migration-free.
def activate_learning_obligation(**kwargs: Any) -> dict[str, Any]:
    """Atomically initialize the learning domain and activate one obligation."""
    from odibi_anchor.codebase.structured_learning_context import (
        activate_learning_obligation as _activate,
    )
    return _activate(**kwargs)


def active_learning_obligation(
    *, project_id: str, task_window_id: str,
) -> dict[str, Any] | None:
    """Return the exact owner's active structured-learning obligation."""
    from odibi_anchor.codebase.structured_learning_context import (
        active_learning_obligation as _active,
    )

    return _active(project_id=project_id, task_window_id=task_window_id)


def latest_closed_learning_obligation(
    *, project_id: str, task_window_id: str,
) -> dict[str, Any] | None:
    """Return the newest terminal obligation for one exact owner."""
    from odibi_anchor.codebase.structured_learning_context import (
        latest_closed_learning_obligation as _latest,
    )

    return _latest(project_id=project_id, task_window_id=task_window_id)


def close_learning_obligation_legacy(
    obligation_id: str, *, project_id: str, task_window_id: str,
) -> dict[str, Any]:
    """Close an active obligation after the legacy learning flow succeeds."""
    from odibi_anchor.codebase.structured_learning_context import (
        close_learning_obligation_legacy as _close,
    )

    return _close(
        obligation_id, project_id=project_id, task_window_id=task_window_id,
    )


# ---------------------------------------------------------------------------
# Schema Migrations
# ---------------------------------------------------------------------------


def ensure_sessions_seen_column(conn: sqlite3.Connection) -> None:
    """Retain the deprecated sessions_seen compatibility column.

    Called by get_db() after schema init. Uses PRAGMA table_info to check
    if the column exists, then ALTER TABLE to add it. Safe to call repeatedly.
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "sessions_seen" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN sessions_seen INTEGER DEFAULT 0")
        conn.commit()
        logger.info("Migration: added sessions_seen column to memories table")


def ensure_lifecycle_columns(conn: sqlite3.Connection) -> None:
    """Add evidence telemetry without reinterpreting legacy counters."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    additions = {
        "surface_count": "INTEGER DEFAULT 0",
        "last_surfaced": "TEXT DEFAULT NULL",
        "applied_count": "INTEGER DEFAULT 0",
        "last_confirmed": "TEXT DEFAULT NULL",
        "superseded_by": "TEXT DEFAULT NULL",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
    conn.commit()


def ensure_session_audits_table(conn: sqlite3.Connection) -> None:
    """Run migration to create session_audits table if not present.

    Called by get_db() after schema init. Safe to call repeatedly since
    CREATE TABLE IF NOT EXISTS is idempotent.
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS session_audits (
        id           TEXT PRIMARY KEY,
        framework_epoch TEXT DEFAULT 'legacy',
        project      TEXT NOT NULL,
        score        INTEGER NOT NULL,
        max_score    INTEGER NOT NULL,
        rating       TEXT NOT NULL,
        gaps         TEXT DEFAULT '[]',
        files_changed TEXT DEFAULT '[]',
        files_created TEXT DEFAULT '[]',
        actions      TEXT DEFAULT '[]',
        total_actions INTEGER DEFAULT 0,
        total_time_ms REAL DEFAULT 0.0,
        errors       INTEGER DEFAULT 0,
        learnings    TEXT DEFAULT '[]',
        created      TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_audits_project ON session_audits(project);
    CREATE INDEX IF NOT EXISTS idx_audits_created ON session_audits(created);
    CREATE INDEX IF NOT EXISTS idx_audits_score ON session_audits(score);
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(session_audits)")}
    if "framework_epoch" not in columns:
        conn.execute("ALTER TABLE session_audits ADD COLUMN framework_epoch TEXT DEFAULT 'legacy'")
    conn.commit()


def ensure_co_occurrence_tables(conn: sqlite3.Connection) -> None:
    """Create co-occurrence tables if not present.

    Called by get_db() after schema init. Creates:
    - session_memory_loads: tracks which memories are loaded per session
    - memory_co_occurrences: pairwise association strengths

    Safe to call repeatedly (CREATE TABLE IF NOT EXISTS).
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS session_memory_loads (
        session_id TEXT NOT NULL,
        memory_id  TEXT NOT NULL,
        loaded_at  TEXT NOT NULL,
        PRIMARY KEY (session_id, memory_id)
    );

    CREATE INDEX IF NOT EXISTS idx_sml_session ON session_memory_loads(session_id);
    CREATE INDEX IF NOT EXISTS idx_sml_memory ON session_memory_loads(memory_id);

    CREATE TABLE IF NOT EXISTS memory_co_occurrences (
        memory_id_a            TEXT NOT NULL,
        memory_id_b            TEXT NOT NULL,
        co_occurrence_count    INTEGER DEFAULT 1,
        success_weighted_count REAL DEFAULT 0.0,
        first_co_occurred      TEXT NOT NULL,
        last_co_occurred       TEXT NOT NULL,
        PRIMARY KEY (memory_id_a, memory_id_b),
        CHECK (memory_id_a < memory_id_b)
    );

    CREATE INDEX IF NOT EXISTS idx_co_occ_a ON memory_co_occurrences(memory_id_a);
    CREATE INDEX IF NOT EXISTS idx_co_occ_b ON memory_co_occurrences(memory_id_b);
    """)
    conn.commit()
def ensure_trails_tables(conn: sqlite3.Connection) -> None:
    """Create Named Trails tables if not present."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS memory_trails (
        trail_id    TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        task_goal   TEXT DEFAULT '',
        tags        TEXT DEFAULT '[]',
        step_count  INTEGER DEFAULT 0,
        created     TEXT NOT NULL,
        updated     TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_trails_name ON memory_trails(name);
    CREATE INDEX IF NOT EXISTS idx_trails_goal ON memory_trails(task_goal);

    CREATE TABLE IF NOT EXISTS trail_steps (
        trail_id   TEXT NOT NULL,
        step_order INTEGER NOT NULL,
        action     TEXT NOT NULL,
        detail     TEXT DEFAULT '',
        outcome    TEXT DEFAULT '',
        memory_id  TEXT DEFAULT NULL,
        PRIMARY KEY (trail_id, step_order),
        FOREIGN KEY (trail_id) REFERENCES memory_trails(trail_id)
    );
    """)
    conn.commit()


def ensure_annotations_table(conn: sqlite3.Connection) -> None:
    """Create Marginalia (contextual annotations) table if not present."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS memory_annotations (
        annotation_id TEXT PRIMARY KEY,
        content       TEXT NOT NULL,
        location_type TEXT NOT NULL CHECK (location_type IN ('file', 'table', 'column')),
        file_path     TEXT DEFAULT NULL,
        line_start    INTEGER DEFAULT NULL,
        line_end      INTEGER DEFAULT NULL,
        table_name    TEXT DEFAULT NULL,
        column_name   TEXT DEFAULT NULL,
        memory_id     TEXT DEFAULT NULL,
        tags          TEXT DEFAULT '[]',
        created       TEXT NOT NULL,
        updated       TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_ann_file ON memory_annotations(file_path);
    CREATE INDEX IF NOT EXISTS idx_ann_table ON memory_annotations(table_name);
    CREATE INDEX IF NOT EXISTS idx_ann_column ON memory_annotations(table_name, column_name);
    CREATE INDEX IF NOT EXISTS idx_ann_memory ON memory_annotations(memory_id);
    """)
    conn.commit()


def increment_sessions_seen(db_path: str | None = None, entry_ids: list[str] | None = None) -> int:
    """Increment sessions_seen counter for specified entries.

    Called during bootstrap to track how many sessions each entry has survived.
    Returns count of entries updated.
    """
    if not entry_ids:
        return 0
    conn = get_db(db_path)
    updated = 0
    for entry_id in entry_ids:
        try:
            conn.execute(
                "UPDATE memories SET sessions_seen = COALESCE(sessions_seen, 0) + 1 WHERE id = ?",
                (entry_id,),
            )
            updated += 1
        except Exception:
            pass  # SILENT-OK: individual session-seen update is best-effort
    conn.commit()
    return updated


def promote_by_sessions_seen(db_path: str | None = None, threshold: int = 3) -> int:
    """Compatibility no-op: sessions seen are not authority evidence."""
    return 0


def insert_audit(
    db_path: str | None = None,
    *,
    project: str,
    score: int,
    max_score: int,
    rating: str,
    gaps: list[str] | None = None,
    files_changed: list[str] | None = None,
    files_created: list[str] | None = None,
    actions: list[str] | None = None,
    total_actions: int = 0,
    total_time_ms: float = 0.0,
    errors: int = 0,
    learnings: list[str] | None = None,
    framework_epoch: str = "delivery-v2",
) -> str:
    """Insert a session audit record. Returns the audit ID."""
    conn = get_db(db_path)
    audit_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO session_audits
        (id, framework_epoch, project, score, max_score, rating, gaps, files_changed,
         files_created, actions, total_actions, total_time_ms, errors,
         learnings, created)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            audit_id, framework_epoch, project, score, max_score, rating,
            json.dumps(gaps or []),
            json.dumps(files_changed or []),
            json.dumps(files_created or []),
            json.dumps(actions or []),
            total_actions, round(total_time_ms, 1), errors,
            json.dumps(learnings or []),
            now,
        ),
    )
    conn.commit()
    return audit_id


def query_audits(
    db_path: str | None = None,
    *,
    project: str | None = None,
    framework_epoch: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Query recent session audits, newest first."""
    conn = get_db(db_path)
    sql = "SELECT * FROM session_audits WHERE 1=1"
    params: list[Any] = []
    if project:
        sql += " AND project = ?"
        params.append(project)
    if framework_epoch:
        sql += " AND framework_epoch = ?"
        params.append(framework_epoch)
    sql += " ORDER BY created DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        d["framework_epoch"] = d.get("framework_epoch") or "legacy"
        for field in ("gaps", "files_changed", "files_created", "actions", "learnings"):
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
        results.append(d)
    return results


def audit_trend(
    db_path: str | None = None,
    *,
    project: str | None = None,
    framework_epoch: str | None = "delivery-v2",
    limit: int = 10,
) -> dict[str, Any]:
    """Compute compliance trend from recent audits.

    Returns summary with avg score, score trend, and top recurring gaps.
    """
    audits = query_audits(
        db_path, project=project, framework_epoch=framework_epoch, limit=limit
    )
    if not audits:
        return {"avg_score": 0, "max_score": 15, "total_audits": 0, "trend": "none", "top_gaps": [], "recent_scores": []}

    scores = [a["score"] for a in audits]
    avg = round(sum(scores) / len(scores), 1)

    # Trend: compare first half vs second half
    mid = len(scores) // 2
    if mid > 0:
        recent_avg = sum(scores[:mid]) / mid  # newest
        older_avg = sum(scores[mid:]) / (len(scores) - mid)  # oldest
        if recent_avg > older_avg + 0.5:
            trend = "improving"
        elif recent_avg < older_avg - 0.5:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = "insufficient_data"

    # Top recurring gaps across all audits
    gap_counts: dict[str, int] = {}
    for a in audits:
        for g in a.get("gaps", []):
            gap_counts[g] = gap_counts.get(g, 0) + 1
    top_gaps = sorted(gap_counts.items(), key=lambda x: -x[1])[:5]

    return {
        "avg_score": avg,
        "max_score": audits[0]["max_score"] if audits else 15,
        "total_audits": len(audits),
        "trend": trend,
        "top_gaps": [{"gap": g, "count": c} for g, c in top_gaps],
        "recent_scores": scores[:5],
    }


# ---------------------------------------------------------------------------
# Project Resolution
# ---------------------------------------------------------------------------



def add_tag_to_entry(db_path: str | None = None, *, entry_id: str, tag: str) -> bool:
    """Add a tag to an existing memory entry (idempotent — won't duplicate).

    Args:
        db_path: Path to SQLite DB (uses default if None).
        entry_id: UUID of the entry to tag.
        tag: Tag string to add.

    Returns:
        True if tag was added, False if already present or entry not found.
    """
    conn = get_db(db_path)
    row = conn.execute("SELECT tags FROM memories WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        return False
    import json
    existing_tags = json.loads(row["tags"]) if row["tags"] else []
    if tag in existing_tags:
        return False
    existing_tags.append(tag)
    conn.execute(
        "UPDATE memories SET tags = ? WHERE id = ?",
        (json.dumps(existing_tags), entry_id),
    )
    conn.commit()
    return True


def count_similar_entries(db_path: str | None = None, *, content: str, project: str | None = None, limit: int = 10) -> int:
    """Count entries with similar content using FTS5 search.

    Extracts key terms from content and searches for matches.
    Used to detect recurring patterns (3+ similar = 'recurring' tag).

    Args:
        db_path: Path to SQLite DB.
        content: Content to find similar entries for.
        project: Optional project filter.
        limit: Max results to consider.

    Returns:
        Number of similar entries found.
    """
    conn = get_db(db_path)
    try:
        # Extract first 5 meaningful words (>3 chars) for FTS query
        words = [w for w in content.split() if len(w) > 3 and w.isalpha()][:5]
        if not words:
            return 0
        # Use OR-based FTS query for broad matching
        fts_query = " OR ".join(_sanitize_fts_query(w) for w in words)
        if not fts_query.strip():
            return 0

        sql = "SELECT COUNT(*) FROM memory_fts WHERE content MATCH ?"
        params = [fts_query]

        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0  # SILENT-OK: FTS count query is best-effort


def resolve_project(root: str | Path) -> str:
    """Map a project root path to a project name.

    Args:
        root: Absolute path to the project root.

    Returns:
        Short project name (e.g., "example-project", "queue-automation").
        Falls back to the last path component if no routing match.
    """
    root_str = str(root)
    for path_fragment, project_name in _PROJECT_ROUTING.items():
        if path_fragment in root_str:
            return project_name
    # Fallback: use directory name
    return Path(root_str).name


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


def insert_memory(
    db_path: str | None = None,
    *,
    project: str,
    type: str,
    content: str,
    related_files: list[str] | None = None,
    tags: list[str] | None = None,
    source: str = "",
    confidence: float = 0.5,
    status: str = "candidate",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a candidate memory entry with deduplication.

    If an entry with identical content AND project already exists,
    bumps use_count and last_used instead of creating a duplicate.
    Runtime and direct callers cannot assign lifecycle authority at creation;
    ``status`` remains accepted for compatibility but every new row is stored as
    ``candidate`` with confidence capped at ``CANDIDATE_CONFIDENCE_CEILING``.
    Historical status preservation belongs to schema migration, not this public
    creation boundary.

    Args:
        db_path: Path to SQLite DB (defaults to central).
        project: Project scope ('example-project', 'all', etc.).
        type: Entry type (must be in VALID_TYPES).
        content: The memory content text.
        related_files: List of file glob patterns.
        tags: List of tag strings.
        source: Origin description.
        confidence: Caller estimate (0.0-1.0), capped at the candidate ceiling.
        status: Compatibility input validated but not trusted as lifecycle authority.
        evidence: JSON-serializable evidence dict.

    Returns:
        Dict with 'id', 'action' ('inserted' or 'deduped'), and entry data.

    Raises:
        ValueError: If type, status, or confidence is invalid.
    """
    if type not in VALID_TYPES:
        raise ValueError(f"Invalid type '{type}'. Valid: {sorted(VALID_TYPES)}")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}")
    if not content.strip():
        raise ValueError("Content cannot be empty")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, Real)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ValueError("confidence must be a finite real number between 0.0 and 1.0")

    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    # Dedup only against retrievable lifecycle states. Fresh evidence must not
    # attach its lineage to stale, rejected, harmful, or superseded content.
    existing = conn.execute(
        "SELECT id, use_count FROM memories WHERE content = ? AND project = ? "
        "AND status NOT IN ('rejected','retired','superseded','stale','quarantined')",
        (content.strip(), project),
    ).fetchone()

    if existing:
        # Bump usage instead of duplicating
        conn.execute(
            "UPDATE memories SET use_count = use_count + 1, last_used = ? WHERE id = ?",
            (now, existing["id"]),
        )
        conn.commit()
        logger.debug("Deduped memory entry %s (use_count=%d)", existing["id"], existing["use_count"] + 1)
        return {
            "id": existing["id"],
            "action": "deduped",
            "use_count": existing["use_count"] + 1,
        }

    # New runtime/direct entries are always advisory candidates. Never let a
    # caller-authored creation payload establish active/confirmed authority (or
    # any other lifecycle mutation) by selecting an initial status.
    status = "candidate"
    confidence = min(confidence, CANDIDATE_CONFIDENCE_CEILING)

    # New entry
    entry_id = str(uuid.uuid4())
    related_files_json = json.dumps(related_files or [])
    tags_json = json.dumps(tags or [])
    evidence_json = json.dumps(evidence or {})
    content_clean = content.strip()

    conn.execute(
        """INSERT INTO memories
        (id, project, type, content, related_files, tags, source,
         confidence, status, evidence, created, last_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (entry_id, project, type, content_clean, related_files_json,
         tags_json, source, confidence, status, evidence_json, now, now),
    )

    conn.commit()

    logger.debug("Inserted memory entry %s [%s/%s]", entry_id, project, type)
    return {
        "id": entry_id,
        "action": "inserted",
        "project": project,
        "type": type,
        "content": content_clean,
    }


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def query_memories(
    db_path: str | None = None,
    *,
    project: str | None = None,
    query: str | None = None,
    type: str | None = None,
    status: str | list[str] | None = None,
    tags: list[str] | None = None,
    min_confidence: float | None = None,
    related_file: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """Query memories with optional FTS, project, type, and status filtering.

    Always includes project='all' entries alongside project-specific ones.

    Args:
        db_path: Path to SQLite DB.
        project: Filter to this project + 'all'. None = all projects.
        query: Free-text search (uses FTS5 MATCH).
        type: Filter by entry type.
        status: Filter by status (string or list).
        tags: Filter entries that contain ALL of these tags.
        min_confidence: Minimum confidence threshold.
        related_file: Match against related_files patterns (Python glob).
        limit: Max results to return. ``None`` returns the complete eligible
            scoped corpus for a caller that applies its own bounded ranking.

    Returns:
        List of memory entry dicts, ordered by relevance/recency.
    """
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer or None")
    conn = get_db(db_path)

    if query:
        results = _query_fts(conn, query=query, project=project, type=type,
                             status=status, tags=tags, min_confidence=min_confidence,
                             related_file=related_file, limit=limit)
    else:
        results = _query_sql(conn, project=project, type=type, status=status,
                             tags=tags, min_confidence=min_confidence,
                             related_file=related_file, limit=limit)
    _attach_lifecycle_evidence(conn, results)
    return results


def _query_fts(
    conn: sqlite3.Connection,
    *,
    query: str,
    project: str | None,
    type: str | None,
    status: str | list[str] | None,
    tags: list[str] | None,
    min_confidence: float | None,
    related_file: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    """FTS5-based query with JOIN to memories table for filtering."""
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return _query_sql(conn, project=project, type=type, status=status,
                          tags=tags, min_confidence=min_confidence,
                          related_file=related_file, limit=limit)

    sql = """
        SELECT m.*, rank
        FROM memory_fts f
        JOIN memories m ON m.id = f.id
        WHERE memory_fts MATCH ?
    """
    params: list[Any] = [fts_query]

    if project:
        sql += " AND m.project IN (?, 'all')"
        params.append(project)

    if type:
        sql += " AND m.type = ?"
        params.append(type)

    if status:
        if isinstance(status, str):
            sql += " AND m.status = ?"
            params.append(status)
        else:
            placeholders = ", ".join("?" * len(status))
            sql += f" AND m.status IN ({placeholders})"
            params.extend(status)

    if min_confidence is not None:
        sql += " AND m.confidence >= ?"
        params.append(min_confidence)

    if not status:
        sql += " AND m.status IN ('candidate', 'active', 'confirmed')"

    sql += " ORDER BY rank"
    if limit is not None and related_file is None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = [_row_to_dict(row) for row in rows]

    if tags:
        results = [r for r in results if _has_all_tags(r, tags)]
    if related_file:
        results = [r for r in results if _matches_related_file(r, related_file)]
    return results[:limit] if limit is not None else results


def _query_sql(
    conn: sqlite3.Connection,
    *,
    project: str | None,
    type: str | None,
    status: str | list[str] | None,
    tags: list[str] | None,
    min_confidence: float | None,
    related_file: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    """SQL-only query (no FTS) with filtering and recency ordering."""
    sql = "SELECT * FROM memories WHERE 1=1"
    params: list[Any] = []

    if project:
        sql += " AND project IN (?, 'all')"
        params.append(project)

    if type:
        sql += " AND type = ?"
        params.append(type)

    if status:
        if isinstance(status, str):
            sql += " AND status = ?"
            params.append(status)
        else:
            placeholders = ", ".join("?" * len(status))
            sql += f" AND status IN ({placeholders})"
            params.extend(status)
    else:
        sql += " AND status IN ('candidate', 'active', 'confirmed')"

    if min_confidence is not None:
        sql += " AND confidence >= ?"
        params.append(min_confidence)

    # Pre-filter tags in SQL (LIKE) to avoid LIMIT cutting them off
    if tags:
        for tag in tags:
            sql += " AND tags LIKE ?"
            params.append(f'%"{tag}"%')

    sql += " ORDER BY confidence DESC, created DESC"
    if limit is not None and related_file is None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    results = [_row_to_dict(row) for row in rows]

    if tags:
        results = [r for r in results if _has_all_tags(r, tags)]
    if related_file:
        results = [r for r in results if _matches_related_file(r, related_file)]
    return results[:limit] if limit is not None else results


def _attach_lifecycle_evidence(
    conn: sqlite3.Connection, entries: list[dict[str, Any]],
) -> None:
    """Attach immutable retrieval/application/evaluation facts for ranking."""
    keys = (
        "selection_count", "application_count", "helpful_count",
        "not_helpful_count", "harmful_count", "superseded_evaluation_count",
    )
    for entry in entries:
        entry.update(dict.fromkeys(keys, 0))
    if not entries:
        return
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('memory_selections','memory_applications','memory_evaluations')"
    )}
    if tables != {"memory_selections", "memory_applications", "memory_evaluations"}:
        return
    ids = [entry["id"] for entry in entries]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT s.memory_id,count(DISTINCT s.selection_id),count(DISTINCT a.application_id),"
        "sum(CASE WHEN e.outcome='helpful' THEN 1 ELSE 0 END),"
        "sum(CASE WHEN e.outcome='not_helpful' THEN 1 ELSE 0 END),"
        "sum(CASE WHEN e.outcome='harmful' THEN 1 ELSE 0 END),"
        "sum(CASE WHEN e.outcome='superseded' THEN 1 ELSE 0 END) "
        "FROM memory_selections s "
        "LEFT JOIN memory_applications a ON a.selection_id=s.selection_id "
        "LEFT JOIN memory_evaluations e ON e.application_id=a.application_id "
        f"WHERE s.memory_id IN ({placeholders}) GROUP BY s.memory_id",
        ids,
    ).fetchall()
    evidence = {row[0]: tuple(int(value or 0) for value in row[1:]) for row in rows}
    for entry in entries:
        values = evidence.get(entry["id"], (0,) * len(keys))
        entry.update(dict(zip(keys, values, strict=True)))


def record_surfaced(
    db_path: str | None = None, *, entry_ids: list[str], session_id: str = ""
) -> int:
    """Record final agent-visible IDs; candidate fetching never calls this."""
    ids = list(dict.fromkeys(entry_id for entry_id in entry_ids if entry_id))
    if not ids:
        return 0
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    if session_id:
        new_ids = []
        for entry_id in ids:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO session_memory_loads "
                "(session_id, memory_id, loaded_at) VALUES (?, ?, ?)",
                (session_id, entry_id, now),
            )
            if cursor.rowcount:
                new_ids.append(entry_id)
        ids = new_ids
    updated = 0
    for entry_id in ids:
        cursor = conn.execute(
            "UPDATE memories SET surface_count = COALESCE(surface_count, 0) + 1, "
            "last_surfaced = ? WHERE id = ?", (now, entry_id),
        )
        updated += cursor.rowcount
    conn.commit()
    return updated


def record_applied(db_path: str | None = None, *, entry_id: str) -> dict[str, Any]:
    """Record explicit evidence that an entry affected completed work."""
    conn = get_db(db_path)
    cursor = conn.execute(
        "UPDATE memories SET applied_count = COALESCE(applied_count, 0) + 1 WHERE id = ?",
        (entry_id,),
    )
    if not cursor.rowcount:
        raise ValueError(f"Memory entry not found: {entry_id}")
    conn.commit()
    return {"id": entry_id, "action": "applied"}


# ---------------------------------------------------------------------------
# Update Operations
# ---------------------------------------------------------------------------


def confirm_memory_entry(
    db_path: str | None = None, *, entry_id: str,
    human_review: dict[str, Any] | None = None,
    **unverified_authority: Any,
) -> dict[str, Any]:
    """Return the fail-closed legacy compatibility boundary.

    This legacy entry point accepts caller-authored ``human_review`` and therefore
    cannot establish owner authority. Use the challenge-bound memory-promotion
    owner commands instead.
    """
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise ValueError(f"Memory entry not found: {entry_id}")
    return {
        "id": entry_id, "action": "confirmation_blocked", "status": row["status"],
        "confirmation_count": row["confirmation_count"], "confidence": row["confidence"],
        "reason": (
            "legacy confirmation is unauthenticated; use memory promotion "
            "request_owner_activation or request_owner_confirmation"
        ),
        "supported_recurrence_count": 0,
        "human_promotion": {
            "status": "available_via_governed_owner_request",
            "commands": ["request_owner_activation", "request_owner_confirmation"],
        },
        "recurrence_promotion": {"status": "unavailable",
                                 "reason": "terminal_evidence_authoritative_recurrence_absent"},
        "unverified_authority_supplied": human_review is not None or bool(unverified_authority),
    }


def reject_memory_entry(db_path: str | None = None, *, entry_id: str) -> dict[str, Any]:
    """Reject a memory entry and immediately exclude it from retrieval.

    Returns:
        Updated entry dict.
    """
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    row = conn.execute("SELECT * FROM memories WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise ValueError(f"Memory entry not found: {entry_id}")

    new_fp_count = row["false_positive_count"] + 1
    new_confidence = max(0.0, row["confidence"] - 0.15)
    new_status = "rejected"

    conn.execute(
        """UPDATE memories
        SET false_positive_count = ?, status = ?, confidence = ?, last_used = ?
        WHERE id = ?""",
        (new_fp_count, new_status, new_confidence, now, entry_id),
    )
    conn.commit()

    return {
        "id": entry_id,
        "action": "rejected",
        "false_positive_count": new_fp_count,
        "status": new_status,
        "confidence": new_confidence,
    }


def set_memory_lifecycle(
    db_path: str | None = None, *, entry_id: str, status: str,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    """Explicitly set an inspectable, non-retrievable lifecycle state."""
    if status not in {"retired", "quarantined", "stale", "rejected", "superseded"}:
        raise ValueError(f"Invalid terminal lifecycle status: {status}")
    if status == "superseded" and not superseded_by:
        raise ValueError("superseded_by is required when superseding a memory")
    conn = get_db(db_path)
    if status == "superseded":
        rows = conn.execute(
            "SELECT id,project,status FROM memories WHERE id IN (?,?)", (entry_id, superseded_by),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        if entry_id not in by_id or superseded_by not in by_id:
            raise ValueError("memory or replacement memory not found")
        if by_id[entry_id]["project"] != by_id[superseded_by]["project"]:
            raise ValueError("replacement memory must share the same project/trust domain")
        if by_id[superseded_by]["status"] not in {"candidate", "active", "confirmed"}:
            raise ValueError("replacement memory is not in an eligible lifecycle state")
    cursor = conn.execute(
        "UPDATE memories SET status = ?, superseded_by = ? WHERE id = ?",
        (status, superseded_by, entry_id),
    )
    if not cursor.rowcount:
        raise ValueError(f"Memory entry not found: {entry_id}")
    conn.commit()
    return {"id": entry_id, "action": status, "status": status,
            "superseded_by": superseded_by}


def delete_memory_entry(db_path: str | None = None, *, entry_id: str) -> dict[str, Any]:
    """Permanently delete a memory entry and its FTS index.

    The FTS deletion is handled by the memory_fts_delete trigger.

    Returns:
        Dict confirming deletion.

    Raises:
        ValueError: If entry not found.
    """
    conn = get_db(db_path)

    row = conn.execute("SELECT id FROM memories WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise ValueError(f"Memory entry not found: {entry_id}")

    conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
    conn.commit()

    return {"id": entry_id, "action": "deleted"}


def archive_stale(
    db_path: str | None = None,
    *,
    max_unused_days: int = 90,
    min_use_count: int = 2,
) -> dict[str, Any]:
    """Archive entries that haven't been used recently.

    Marks entries as 'stale' if:
    - last_used is older than max_unused_days
    - use_count is below min_use_count
    - status is not 'confirmed' (confirmed entries are protected)
    - tags do NOT contain 'protective' (protective entries are never archived)

    Returns:
        Summary dict with count of archived entries.
    """
    return {
        "action": "archive",
        "archived_count": 0,
        "compatibility_noop": True,
        "message": "Use memory_hygiene dry-run and reviewed exact IDs.",
        "cutoff_date": None,
        "min_use_count": min_use_count,
    }


# ---------------------------------------------------------------------------
# Bulk Operations
# ---------------------------------------------------------------------------


def get_all_entries(
    db_path: str | None = None,
    *,
    project: str | None = None,
    status: str | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get all entries matching filters (for export/migration).

    Unlike query_memories, this does NOT update last_used timestamps.
    """
    conn = get_db(db_path)
    sql = "SELECT * FROM memories WHERE 1=1"
    params: list[Any] = []

    if project:
        sql += " AND project IN (?, 'all')"
        params.append(project)

    if status:
        if isinstance(status, str):
            sql += " AND status = ?"
            params.append(status)
        else:
            placeholders = ", ".join("?" * len(status))
            sql += f" AND status IN ({placeholders})"
            params.extend(status)

    sql += " ORDER BY confidence DESC, last_used DESC"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def entry_count(db_path: str | None = None, *, project: str | None = None) -> dict[str, int]:
    """Get counts by status for diagnostics."""
    conn = get_db(db_path)
    where, params = (" WHERE project IN (?, 'all')", (project,)) if project else ("", ())
    rows = conn.execute(
        f"SELECT status, COUNT(*) as cnt FROM memories{where} GROUP BY status", params,
    ).fetchall()
    counts = {row["status"]: row["cnt"] for row in rows}
    counts["total"] = sum(counts.values())
    return counts



# ---------------------------------------------------------------------------
# Co-Occurrence Linking (Associative Memory)
# ---------------------------------------------------------------------------

_CO_OCCURRENCE_CONFIG = {
    "min_co_occurrences": 3,
    "max_associations": 3,
    "decay_days": 30,
    "min_session_score_ratio": 0.5,
    "hub_threshold": 0.8,
}


def record_memory_load(db_path: str | None = None, *, session_id: str, memory_ids: list[str]) -> int:
    """Record which memories were loaded in a session."""
    if not session_id or not memory_ids:
        return 0
    conn = get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for mid in memory_ids:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO session_memory_loads (session_id, memory_id, loaded_at) VALUES (?, ?, ?)",
                (session_id, mid, now),
            )
            inserted += 1
        except Exception:
            pass
    conn.commit()
    return inserted


def compute_co_occurrences(db_path: str | None = None, *, session_id: str, session_score: int = 0, max_score: int = 15) -> int:
    """Compute pairwise co-occurrences for a completed session."""
    if not session_id:
        return 0
    score_ratio = session_score / max_score if max_score > 0 else 0
    if score_ratio < _CO_OCCURRENCE_CONFIG["min_session_score_ratio"]:
        return 0

    conn = get_db(db_path)
    rows = conn.execute("SELECT memory_id FROM session_memory_loads WHERE session_id = ?", (session_id,)).fetchall()
    memory_ids = sorted(row[0] for row in rows)
    if len(memory_ids) < 2:
        return 0

    # Exclude hub memories (only when enough sessions to be meaningful)
    total_sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM session_memory_loads").fetchone()[0]
    if total_sessions >= 5:
        hub_threshold_count = total_sessions * _CO_OCCURRENCE_CONFIG["hub_threshold"]
        non_hub_ids = []
        for mid in memory_ids:
            load_count = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM session_memory_loads WHERE memory_id = ?", (mid,)
            ).fetchone()[0]
            if load_count <= hub_threshold_count:
                non_hub_ids.append(mid)
        memory_ids = non_hub_ids

    if len(memory_ids) < 2:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    weight = score_ratio
    pairs_updated = 0

    for i in range(len(memory_ids)):
        for j in range(i + 1, len(memory_ids)):
            a, b = memory_ids[i], memory_ids[j]
            try:
                existing = conn.execute(
                    "SELECT co_occurrence_count FROM memory_co_occurrences WHERE memory_id_a = ? AND memory_id_b = ?",
                    (a, b),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE memory_co_occurrences SET co_occurrence_count = co_occurrence_count + 1,
                            success_weighted_count = success_weighted_count + ?, last_co_occurred = ?
                        WHERE memory_id_a = ? AND memory_id_b = ?""",
                        (weight, now, a, b),
                    )
                else:
                    conn.execute(
                        """INSERT INTO memory_co_occurrences
                        (memory_id_a, memory_id_b, co_occurrence_count, success_weighted_count, first_co_occurred, last_co_occurred)
                        VALUES (?, ?, 1, ?, ?, ?)""",
                        (a, b, weight, now, now),
                    )
                pairs_updated += 1
            except Exception:
                pass
    conn.commit()
    return pairs_updated


def get_associations(db_path: str | None = None, *, memory_ids: list[str], exclude_ids: list[str] | None = None, max_results: int | None = None) -> list[dict[str, Any]]:
    """Get associated memories based on co-occurrence history."""
    if not memory_ids:
        return []
    max_results = max_results or _CO_OCCURRENCE_CONFIG["max_associations"]
    min_count = _CO_OCCURRENCE_CONFIG["min_co_occurrences"]
    exclude_ids_set = set(exclude_ids or []) | set(memory_ids)
    conn = get_db(db_path)

    placeholders = ", ".join("?" * len(memory_ids))
    rows = conn.execute(
        f"""SELECT memory_id_a, memory_id_b, co_occurrence_count, success_weighted_count, last_co_occurred
            FROM memory_co_occurrences
            WHERE (memory_id_a IN ({placeholders}) OR memory_id_b IN ({placeholders}))
              AND co_occurrence_count >= ?""",
        (*memory_ids, *memory_ids, min_count),
    ).fetchall()
    if not rows:
        return []

    candidates: dict[str, dict[str, Any]] = {}
    decay_days = _CO_OCCURRENCE_CONFIG["decay_days"]
    now = datetime.now(timezone.utc)
    input_set = set(memory_ids)

    for row in rows:
        a, b = row[0], row[1]
        other_id = b if a in input_set else a
        associated_with = a if a in input_set else b
        if other_id in exclude_ids_set:
            continue
        try:
            last_co = datetime.fromisoformat(row[4])
            decay_factor = 0.5 if (now - last_co).days > decay_days else 1.0
        except (ValueError, TypeError):
            decay_factor = 0.5
        strength = row[3] * decay_factor
        if other_id in candidates:
            candidates[other_id]["strength"] += strength
            candidates[other_id]["co_occurrence_count"] = max(candidates[other_id]["co_occurrence_count"], row[2])
        else:
            candidates[other_id] = {"memory_id": other_id, "strength": strength, "co_occurrence_count": row[2], "associated_with": associated_with}

    if candidates:
        candidate_ids = list(candidates)
        marks = ", ".join("?" * len(candidate_ids))
        allowed = {row[0] for row in conn.execute(
            f"SELECT id FROM memories WHERE id IN ({marks}) "
            "AND status IN ('candidate', 'active', 'confirmed')", candidate_ids
        )}
        candidates = {mid: value for mid, value in candidates.items() if mid in allowed}
    return sorted(candidates.values(), key=lambda x: -x["strength"])[:max_results]


def get_co_occurrence_stats(db_path: str | None = None) -> dict[str, Any]:
    """Get summary statistics for the co-occurrence system."""
    conn = get_db(db_path)
    total_pairs = conn.execute("SELECT COUNT(*) FROM memory_co_occurrences").fetchone()[0]
    total_sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM session_memory_loads").fetchone()[0]
    avg_strength = 0.0
    if total_pairs:
        avg_strength = conn.execute("SELECT AVG(success_weighted_count) FROM memory_co_occurrences").fetchone()[0] or 0.0
    top_rows = conn.execute(
        """SELECT memory_id_a, memory_id_b, co_occurrence_count, success_weighted_count
           FROM memory_co_occurrences ORDER BY success_weighted_count DESC LIMIT 5"""
    ).fetchall()
    return {
        "total_pairs": total_pairs,
        "total_sessions_tracked": total_sessions,
        "avg_strength": round(avg_strength, 3),
        "top_associations": [{"memory_id_a": r[0], "memory_id_b": r[1], "count": r[2], "strength": round(r[3], 3)} for r in top_rows],
    }



# ---------------------------------------------------------------------------
# Named Trails (Directed Associative Sequences)
# ---------------------------------------------------------------------------


def record_trail(
    db_path: str | None = None,
    *,
    name: str,
    description: str = "",
    task_goal: str = "",
    tags: list[str] | None = None,
    steps: list[dict[str, str]],
) -> str:
    """Record a named trail (directed sequence of investigative steps).

    A trail captures the *order* of actions that solved a problem,
    creating a replayable playbook for future similar tasks.

    Args:
        db_path: Path to SQLite DB.
        name: Short trail name (e.g., "schema_drift_debug").
        description: What this trail solves.
        task_goal: The goal/task this trail addresses.
        tags: Categorization tags.
        steps: Ordered list of dicts with keys: action, detail, outcome, memory_id (optional).

    Returns:
        The trail_id of the created trail.
    """
    if not name or not steps:
        return ""
    conn = get_db(db_path)
    trail_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps(tags or [])

    conn.execute(
        """INSERT INTO memory_trails (trail_id, name, description, task_goal, tags, step_count, created, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (trail_id, name, description, task_goal, tags_json, len(steps), now, now),
    )

    for i, step in enumerate(steps):
        conn.execute(
            """INSERT INTO trail_steps (trail_id, step_order, action, detail, outcome, memory_id)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (trail_id, i, step.get("action", ""), step.get("detail", ""),
             step.get("outcome", ""), step.get("memory_id")),
        )

    conn.commit()
    return trail_id


def get_relevant_trails(
    db_path: str | None = None,
    *,
    task_goal: str = "",
    tags: list[str] | None = None,
    keyword: str = "",
    max_results: int = 3,
) -> list[dict[str, Any]]:
    """Find trails relevant to the current task.

    Searches by task_goal similarity, tags overlap, and keyword match
    against trail name/description.

    Args:
        db_path: Path to SQLite DB.
        task_goal: Current task goal to match against.
        tags: Tags to filter by (any overlap counts).
        keyword: Keyword to search in name/description.
        max_results: Maximum trails to return.

    Returns:
        List of trail dicts with metadata and steps.
    """
    conn = get_db(db_path)
    conditions = []
    params: list[Any] = []

    if keyword:
        conditions.append("(name LIKE ? OR description LIKE ? OR task_goal LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])

    if task_goal:
        # Match on task_goal words (OR logic — any word match counts)
        words = [w for w in task_goal.lower().split() if len(w) > 3][:5]
        if words:
            word_conditions = []
            for word in words:
                word_conditions.append("(LOWER(task_goal) LIKE ? OR LOWER(description) LIKE ?)")
                params.extend([f"%{word}%", f"%{word}%"])
            conditions.append("(" + " OR ".join(word_conditions) + ")")

    where = " AND ".join(conditions) if conditions else "1=1"
    query = f"SELECT * FROM memory_trails WHERE {where} ORDER BY updated DESC LIMIT ?"
    params.append(max_results * 3)  # Over-fetch for scoring

    rows = conn.execute(query, params).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM memory_trails LIMIT 0").description]

    results = []
    for row in rows:
        trail = dict(zip(cols, row))
        trail["tags"] = json.loads(trail.get("tags", "[]"))

        # Score by tag overlap
        score = 1.0
        if tags and trail["tags"]:
            overlap = len(set(tags) & set(trail["tags"]))
            score += overlap * 0.5

        # Load steps
        step_rows = conn.execute(
            "SELECT action, detail, outcome, memory_id FROM trail_steps WHERE trail_id = ? ORDER BY step_order",
            (trail["trail_id"],),
        ).fetchall()
        trail["steps"] = [
            {"action": r[0], "detail": r[1], "outcome": r[2], "memory_id": r[3]}
            for r in step_rows
        ]
        trail["_score"] = score
        results.append(trail)

    results.sort(key=lambda x: -x["_score"])
    for r in results:
        del r["_score"]
    return results[:max_results]


def get_trail_by_id(db_path: str | None = None, *, trail_id: str) -> dict[str, Any] | None:
    """Get a single trail by ID with its steps."""
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM memory_trails WHERE trail_id = ?", (trail_id,)).fetchone()
    if not row:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM memory_trails LIMIT 0").description]
    trail = dict(zip(cols, row))
    trail["tags"] = json.loads(trail.get("tags", "[]"))
    step_rows = conn.execute(
        "SELECT action, detail, outcome, memory_id FROM trail_steps WHERE trail_id = ? ORDER BY step_order",
        (trail_id,),
    ).fetchall()
    trail["steps"] = [{"action": r[0], "detail": r[1], "outcome": r[2], "memory_id": r[3]} for r in step_rows]
    return trail


# ---------------------------------------------------------------------------
# Marginalia (Contextual Annotations)
# ---------------------------------------------------------------------------


def annotate_location(
    db_path: str | None = None,
    *,
    content: str,
    location_type: str,
    file_path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    table_name: str | None = None,
    column_name: str | None = None,
    memory_id: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Create a contextual annotation bound to a specific location.

    Marginalia: knowledge attached to WHERE it applies, not just WHAT it says.
    When you revisit the same file/line or table/column, the annotation resurfaces.

    Args:
        db_path: Path to SQLite DB.
        content: The annotation text.
        location_type: One of 'file', 'table', 'column'.
        file_path: For file annotations — the workspace path.
        line_start: Optional start line.
        line_end: Optional end line.
        table_name: For table/column annotations — fully qualified table name.
        column_name: For column annotations — the column name.
        memory_id: Optional link to a memory entry.
        tags: Optional tags.

    Returns:
        The annotation_id.
    """
    if not content or location_type not in ("file", "table", "column"):
        return ""
    if location_type == "file" and not file_path:
        return ""
    if location_type in ("table", "column") and not table_name:
        return ""
    if location_type == "column" and not column_name:
        return ""

    conn = get_db(db_path)
    ann_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps(tags or [])

    conn.execute(
        """INSERT INTO memory_annotations
        (annotation_id, content, location_type, file_path, line_start, line_end,
         table_name, column_name, memory_id, tags, created, updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ann_id, content, location_type, file_path, line_start, line_end,
         table_name, column_name, memory_id, tags_json, now, now),
    )
    conn.commit()
    return ann_id


def get_annotations_for_file(
    db_path: str | None = None,
    *,
    file_path: str,
    line: int | None = None,
) -> list[dict[str, Any]]:
    """Get all annotations for a file, optionally filtered to a line range.

    Args:
        db_path: Path to SQLite DB.
        file_path: The file path to look up.
        line: If provided, only return annotations covering this line.

    Returns:
        List of annotation dicts.
    """
    if not file_path:
        return []
    conn = get_db(db_path)

    if line is not None:
        rows = conn.execute(
            """SELECT * FROM memory_annotations
            WHERE file_path = ? AND location_type = 'file'
              AND (line_start IS NULL OR line_start <= ?)
              AND (line_end IS NULL OR line_end >= ?)
            ORDER BY line_start""",
            (file_path, line, line),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memory_annotations WHERE file_path = ? AND location_type = 'file' ORDER BY line_start",
            (file_path,),
        ).fetchall()

    cols = [d[0] for d in conn.execute("SELECT * FROM memory_annotations LIMIT 0").description]
    results = []
    for row in rows:
        ann = dict(zip(cols, row))
        ann["tags"] = json.loads(ann.get("tags", "[]"))
        results.append(ann)
    return results


def get_annotations_for_table(
    db_path: str | None = None,
    *,
    table_name: str,
    column_name: str | None = None,
) -> list[dict[str, Any]]:
    """Get annotations for a table or specific column.

    Args:
        db_path: Path to SQLite DB.
        table_name: Fully qualified table name.
        column_name: If provided, only return column-level annotations.

    Returns:
        List of annotation dicts.
    """
    if not table_name:
        return []
    conn = get_db(db_path)

    if column_name:
        rows = conn.execute(
            "SELECT * FROM memory_annotations WHERE table_name = ? AND column_name = ? ORDER BY created",
            (table_name, column_name),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memory_annotations WHERE table_name = ? ORDER BY location_type, column_name, created",
            (table_name,),
        ).fetchall()

    cols = [d[0] for d in conn.execute("SELECT * FROM memory_annotations LIMIT 0").description]
    results = []
    for row in rows:
        ann = dict(zip(cols, row))
        ann["tags"] = json.loads(ann.get("tags", "[]"))
        results.append(ann)
    return results


def get_annotation_stats(db_path: str | None = None) -> dict[str, Any]:
    """Get summary statistics for the annotation system."""
    conn = get_db(db_path)
    total = conn.execute("SELECT COUNT(*) FROM memory_annotations").fetchone()[0]
    by_type = conn.execute(
        "SELECT location_type, COUNT(*) FROM memory_annotations GROUP BY location_type"
    ).fetchall()
    return {
        "total_annotations": total,
        "by_type": {r[0]: r[1] for r in by_type},
    }


def get_trail_stats(db_path: str | None = None) -> dict[str, Any]:
    """Get summary statistics for the trails system."""
    conn = get_db(db_path)
    total_trails = conn.execute("SELECT COUNT(*) FROM memory_trails").fetchone()[0]
    total_steps = conn.execute("SELECT COUNT(*) FROM trail_steps").fetchone()[0]
    avg_steps = 0
    if total_trails:
        avg_steps = conn.execute("SELECT AVG(step_count) FROM memory_trails").fetchone()[0] or 0
    return {
        "total_trails": total_trails,
        "total_steps": total_steps,
        "avg_steps_per_trail": round(avg_steps, 1),
    }


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert sqlite3.Row to a plain dict with parsed JSON fields."""
    d = dict(row)
    d.pop("rank", None)
    for field in ("related_files", "tags"):
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = []
    if "evidence" in d and isinstance(d["evidence"], str):
        try:
            d["evidence"] = json.loads(d["evidence"])
        except (json.JSONDecodeError, TypeError):
            d["evidence"] = {}
    return d


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a query string for FTS5 MATCH syntax.

    Removes special FTS5 operators that could cause syntax errors.
    Keeps alphanumeric tokens and allows prefix search (*).
    Splits on hyphens, underscores, and dots to match FTS5 unicode61
    tokenization (which treats all non-alphanumeric chars as separators).

    Multi-word queries use implicit OR (any token matches) to maximize
    recall. Explicit OR tokens in the input are preserved.
    """
    if not query or not query.strip():
        return ""

    # Replace common separators with spaces (FTS5 tokenizes on these)
    # Dots, hyphens, underscores, backticks, parens, brackets
    normalized = re.sub(r'[\-_\.`()\[\]{}]', ' ', query.strip())

    tokens = normalized.split()
    safe_tokens = []
    for token in tokens:
        if token == "OR":
            # Explicit OR already present — user is building their own query
            safe_tokens.append("OR")
            continue
        cleaned = re.sub(r'[^\w*]', '', token)
        if cleaned:
            safe_tokens.append(cleaned)

    # Join with OR for multi-word queries to maximize recall
    # Single-word queries don't need OR
    if len(safe_tokens) > 1 and "OR" not in safe_tokens:
        return " OR ".join(safe_tokens)
    return " ".join(safe_tokens)


def _has_all_tags(entry: dict[str, Any], required_tags: list[str]) -> bool:
    """Check if entry has all required tags."""
    entry_tags = set(entry.get("tags", []))
    return all(t in entry_tags for t in required_tags)


def _matches_related_file(entry: dict[str, Any], file_path: str) -> bool:
    """Check if file_path matches any pattern in entry's related_files."""
    patterns = entry.get("related_files", [])
    return any(fnmatch(file_path, pattern) for pattern in patterns)


def _touch_entries(conn: sqlite3.Connection, entry_ids: list[str]) -> None:
    """Update last_used and use_count for accessed entries."""
    if not entry_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    for entry_id in entry_ids:
        conn.execute(
            "UPDATE memories SET last_used = ?, use_count = use_count + 1 WHERE id = ?",
            (now, entry_id),
        )
    conn.commit()
