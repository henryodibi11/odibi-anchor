"""Internal, additive persistence for the semantic-memory lifecycle.

Retrieval, application, evaluation, and projection are deliberately separate
facts.  This module owns no long-lived connection and does not alter legacy
memory counters or confidence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOMAIN = "memory_lifecycle"
SCHEMA_VERSION = 3
EVALUATIONS = frozenset({"helpful", "not_helpful", "harmful", "superseded"})
DISPOSITIONS = frozenset({"applied", "irrelevant", "suspect", "superseded"})
RECOVERY_UNAVAILABLE = "memory selection recovery is unavailable for the current exact owner"

_V2_DDL = (
    "CREATE TABLE anchor_schema_versions (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64), applied_at TEXT NOT NULL)",
    "CREATE TABLE memory_selections (selection_id TEXT PRIMARY KEY, task_window_id TEXT NOT NULL, query_json TEXT NOT NULL, memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT, reason_json TEXT NOT NULL, selected_at TEXT NOT NULL, UNIQUE(task_window_id,query_json,memory_id))",
    "CREATE INDEX idx_memory_selections_task ON memory_selections(task_window_id,selected_at,selection_id)",
    "CREATE TABLE memory_applications (application_id TEXT PRIMARY KEY, selection_id TEXT NOT NULL REFERENCES memory_selections(selection_id) ON DELETE RESTRICT, task_window_id TEXT NOT NULL, action TEXT NOT NULL, context_json TEXT NOT NULL, applied_at TEXT NOT NULL, UNIQUE(selection_id,task_window_id,action))",
    "CREATE INDEX idx_memory_applications_task ON memory_applications(task_window_id,applied_at,application_id)",
    "CREATE TABLE memory_evaluations (evaluation_id TEXT PRIMARY KEY, application_id TEXT NOT NULL UNIQUE REFERENCES memory_applications(application_id) ON DELETE RESTRICT, outcome TEXT NOT NULL CHECK(outcome IN ('helpful','not_helpful','harmful','superseded')), evidence_json TEXT NOT NULL, evaluated_at TEXT NOT NULL)",
    "CREATE TABLE memory_projections (projection_id TEXT PRIMARY KEY, learning_item_id TEXT NOT NULL UNIQUE REFERENCES learning_items(item_id) ON DELETE RESTRICT, memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT, lineage_json TEXT NOT NULL, projected_at TEXT NOT NULL)",
    "CREATE INDEX idx_memory_projections_memory ON memory_projections(memory_id,projected_at,projection_id)",
    "CREATE TABLE memory_dispositions (disposition_id TEXT PRIMARY KEY, selection_id TEXT NOT NULL UNIQUE REFERENCES memory_selections(selection_id) ON DELETE RESTRICT, task_window_id TEXT NOT NULL, disposition TEXT NOT NULL CHECK(disposition IN ('applied','irrelevant','suspect','superseded')), reason_json TEXT NOT NULL, replacement_memory_id TEXT REFERENCES memories(id) ON DELETE RESTRICT, disposed_at TEXT NOT NULL)",
    "CREATE INDEX idx_memory_dispositions_task ON memory_dispositions(task_window_id,disposed_at,disposition_id)",
)
_V3_DDL = (
    "CREATE TABLE memory_selection_abandonments (abandonment_id TEXT PRIMARY KEY, selection_id TEXT NOT NULL UNIQUE REFERENCES memory_selections(selection_id) ON DELETE RESTRICT, origin_task_window_id TEXT NOT NULL, project_id TEXT NOT NULL, trust_domain TEXT, target_identity_sha256 TEXT NOT NULL CHECK(length(target_identity_sha256)=64), artifact_identity_sha256 TEXT NOT NULL CHECK(length(artifact_identity_sha256)=64), closure_event_id TEXT NOT NULL, closure_status TEXT NOT NULL CHECK(closure_status IN ('blocked','failed','superseded')), authority_sha256 TEXT NOT NULL CHECK(length(authority_sha256)=64), reason_json TEXT NOT NULL, evidence_json TEXT NOT NULL, actor_kind TEXT NOT NULL, actor_ref TEXT NOT NULL, abandoned_at TEXT NOT NULL)",
    "CREATE INDEX idx_memory_abandonments_owner ON memory_selection_abandonments(project_id,trust_domain,origin_task_window_id,abandoned_at,abandonment_id)",
    "CREATE TRIGGER memory_selection_abandonments_no_update BEFORE UPDATE ON memory_selection_abandonments BEGIN SELECT RAISE(ABORT,'memory selection abandonments are immutable'); END",
    "CREATE TRIGGER memory_selection_abandonments_no_delete BEFORE DELETE ON memory_selection_abandonments BEGIN SELECT RAISE(ABORT,'memory selection abandonments are immutable'); END",
    "CREATE TABLE memory_selection_recoveries (recovery_id TEXT PRIMARY KEY, abandonment_id TEXT NOT NULL UNIQUE REFERENCES memory_selection_abandonments(abandonment_id) ON DELETE RESTRICT, selection_id TEXT NOT NULL UNIQUE REFERENCES memory_selections(selection_id) ON DELETE RESTRICT, origin_task_window_id TEXT NOT NULL, replacement_task_window_id TEXT NOT NULL, project_id TEXT NOT NULL, trust_domain TEXT, target_identity_sha256 TEXT NOT NULL CHECK(length(target_identity_sha256)=64), artifact_identity_sha256 TEXT NOT NULL CHECK(length(artifact_identity_sha256)=64), reason_json TEXT NOT NULL, evidence_json TEXT NOT NULL, actor_kind TEXT NOT NULL, actor_ref TEXT NOT NULL, recovered_at TEXT NOT NULL)",
    "CREATE INDEX idx_memory_recoveries_owner ON memory_selection_recoveries(project_id,trust_domain,replacement_task_window_id,recovered_at,recovery_id)",
    "CREATE TRIGGER memory_selection_recoveries_no_update BEFORE UPDATE ON memory_selection_recoveries BEGIN SELECT RAISE(ABORT,'memory selection recoveries are immutable'); END",
    "CREATE TRIGGER memory_selection_recoveries_no_delete BEFORE DELETE ON memory_selection_recoveries BEGIN SELECT RAISE(ABORT,'memory selection recoveries are immutable'); END",
    "CREATE TABLE memory_lifecycle_migrations (migration_id TEXT PRIMARY KEY, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, backup_path TEXT NOT NULL, pre_v3_digest TEXT NOT NULL CHECK(length(pre_v3_digest)=64), applied_at TEXT NOT NULL)",
    "CREATE TRIGGER memory_lifecycle_migrations_no_update BEFORE UPDATE ON memory_lifecycle_migrations BEGIN SELECT RAISE(ABORT,'memory lifecycle migrations are immutable'); END",
    "CREATE TRIGGER memory_lifecycle_migrations_no_delete BEFORE DELETE ON memory_lifecycle_migrations BEGIN SELECT RAISE(ABORT,'memory lifecycle migrations are immutable'); END",
)
_DDL = _V2_DDL + _V3_DDL
SCHEMA_TEXT = ";\n".join(_DDL) + ";\n"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_TEXT.encode("utf-8")).hexdigest()
V2_SCHEMA_SHA256 = hashlib.sha256((";\n".join(_V2_DDL) + ";\n").encode("utf-8")).hexdigest()
_V1_DDL = _V2_DDL[:-2]
V1_SCHEMA_SHA256 = hashlib.sha256((";\n".join(_V1_DDL) + ";\n").encode("utf-8")).hexdigest()


class RecoveryUnavailable(RuntimeError):
    """Selection recovery authority is absent, foreign, ambiguous, or ineligible."""


def canonical_json(value: Any) -> str:
    """Serialize JSON in the lifecycle's deterministic representation."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _object(value: Any, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, Mapping) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ValueError(f"{name} must be a {qualifier}JSON object")
    from odibi_anchor._forensic_replay.journal import redact_payload

    return canonical_json(redact_payload(value))


def _id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _path_digest(value: str | Path) -> str:
    normalized = str(Path(value).expanduser().resolve())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _effective_selection_owner(connection: sqlite3.Connection, selection_id: str) -> str | None:
    row = connection.execute(
        "SELECT COALESCE(r.replacement_task_window_id,s.task_window_id) "
        "FROM memory_selections s LEFT JOIN memory_selection_recoveries r "
        "ON r.selection_id=s.selection_id WHERE s.selection_id=?",
        (selection_id,),
    ).fetchone()
    return row[0] if row else None


@contextmanager
def _connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_shared_memory(
        path, owner_key_kind="task_window_id", isolation_level="DEFERRED",
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("memory lifecycle foreign keys disabled")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        yield connection
    finally:
        connection.close()


@contextmanager
def _readonly_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open an existing lifecycle database without creating files or changing pragmas."""
    path = Path(db_path).expanduser()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _expected_objects(version: int) -> list[tuple[Any, ...]]:
    statements = {
        1: _V1_DDL[1:],
        2: _V2_DDL[1:],
        3: _DDL[1:],
    }[version]
    expected_db = sqlite3.connect(":memory:")
    try:
        expected_db.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
        expected_db.execute("CREATE TABLE learning_items (item_id TEXT PRIMARY KEY)")
        for statement in statements:
            expected_db.execute(statement)
        names = tuple(
            row[0] for row in expected_db.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'memory_%' OR name LIKE 'idx_memory_%'"
            )
        )
        placeholders = ",".join("?" for _ in names)
        return [tuple(row) for row in expected_db.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY type,name",
            names,
        )]
    finally:
        expected_db.close()


def _verify_objects(connection: sqlite3.Connection, *, version: int) -> None:
    expected = _expected_objects(version)
    expected_names = [row[1] for row in expected]
    table_names = [row[1] for row in expected if row[0] == "table"]
    placeholders = ",".join("?" for _ in expected_names)
    actual = [tuple(row) for row in connection.execute(
        f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY type,name",
        expected_names,
    )]
    table_placeholders = ",".join("?" for _ in table_names)
    lifecycle_names = {row[0] for row in connection.execute(
        f"SELECT name FROM sqlite_master WHERE tbl_name IN ({table_placeholders}) AND sql IS NOT NULL",
        table_names,
    )}
    if actual != expected or lifecycle_names != set(expected_names):
        raise RuntimeError("memory lifecycle schema checksum mismatch")


def _verified_migration_backup(db_path: str | Path, *, version: int) -> str:
    """Create or reuse a content-addressed lifecycle backup without overwriting one."""
    from odibi_anchor.codebase._migration_backup import ensure_migration_backup

    result = ensure_migration_backup(
        Path(db_path).expanduser(),
        tag=f"memory-lifecycle-v{version}",
        error_prefix="memory lifecycle migration",
    )
    return result["backup_path"]


def _pre_v3_digest(connection: sqlite3.Connection) -> str:
    payload = []
    for table in (
        "memory_selections", "memory_applications", "memory_evaluations",
        "memory_projections", "memory_dispositions",
    ):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if exists:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            rows = connection.execute(
                f"SELECT {','.join(columns)} FROM {table} ORDER BY 1"
            ).fetchall()
            payload.append([table, columns, [list(row) for row in rows]])
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def initialize_schema(db_path: str | Path) -> dict[str, Any]:
    """Add or exactly verify lifecycle schema v3 and its immutable recovery history."""
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            schema_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='anchor_schema_versions'"
            ).fetchone()
            if schema_table is None:
                connection.execute(_DDL[0])
            row = connection.execute(
                "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)
            ).fetchone()
            supported_v1 = row and tuple(row) == (1, V1_SCHEMA_SHA256)
            supported_v2 = row and tuple(row) == (2, V2_SCHEMA_SHA256)
            if row and not supported_v1 and not supported_v2 and (
                row["version"] != SCHEMA_VERSION or row["schema_sha256"] != SCHEMA_SHA256
            ):
                raise RuntimeError("memory lifecycle schema version/checksum mismatch")
            if not row:
                for statement in _DDL[1:]:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO anchor_schema_versions(domain,version,schema_sha256,applied_at) VALUES(?,?,?,?)",
                    (DOMAIN, SCHEMA_VERSION, SCHEMA_SHA256, _now()),
                )
            elif supported_v1:
                _verify_objects(connection, version=1)
                connection.commit()
                _verified_migration_backup(db_path, version=2)
                connection.execute("BEGIN IMMEDIATE")
                _verify_objects(connection, version=1)
                for statement in _V2_DDL[-2:]:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE anchor_schema_versions SET version=?,schema_sha256=?,applied_at=? WHERE domain=?",
                    (2, V2_SCHEMA_SHA256, _now(), DOMAIN),
                )
                connection.commit()
                supported_v2 = True
            if supported_v2:
                _verify_objects(connection, version=2)
                before = _pre_v3_digest(connection)
                connection.commit()
                backup = _verified_migration_backup(db_path, version=3)
                connection.execute("BEGIN IMMEDIATE")
                _verify_objects(connection, version=2)
                if _pre_v3_digest(connection) != before:
                    raise RuntimeError("memory lifecycle changed during v3 migration")
                for statement in _V3_DDL:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO memory_lifecycle_migrations VALUES(?,?,?,?,?,?)",
                    ("memory-lifecycle-v2-to-v3", 2, 3, backup, before, _now()),
                )
                connection.execute(
                    "UPDATE anchor_schema_versions SET version=?,schema_sha256=?,applied_at=? WHERE domain=?",
                    (SCHEMA_VERSION, SCHEMA_SHA256, _now(), DOMAIN),
                )
            _verify_objects(connection, version=SCHEMA_VERSION)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"domain": DOMAIN, "version": SCHEMA_VERSION, "schema_sha256": SCHEMA_SHA256}


def record_selection(
    db_path: str | Path, *, task_window_id: str, query: Mapping[str, Any], memory_id: str,
    reason: Mapping[str, Any],
) -> dict[str, Any]:
    """Record deterministic exposure of one memory; this is not application."""
    return record_selections(
        db_path, task_window_id=task_window_id, query=query,
        selections=[{"memory_id": memory_id, "reason": reason}],
    )[0]


def record_selections(
    db_path: str | Path, *, task_window_id: str, query: Mapping[str, Any],
    selections: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Atomically record one bounded retrieval result."""
    initialize_schema(db_path)
    task = _text(task_window_id, "task_window_id")
    query_json = _object(query, "query")
    normalized = []
    for item in selections:
        if set(item) != {"memory_id", "reason"}:
            raise ValueError("selection must contain memory_id and reason")
        memory = _text(item["memory_id"], "memory_id")
        reason_json = _object(item["reason"], "reason", nonempty=True)
        normalized.append((_id("sel", task, query_json, memory), memory, reason_json))
    rows = []
    with _connection(db_path) as connection, connection:
        for selection_id, memory, reason_json in normalized:
            connection.execute(
                "INSERT OR IGNORE INTO memory_selections VALUES(?,?,?,?,?,?)",
                (selection_id, task, query_json, memory, reason_json, _now()),
            )
            row = connection.execute(
                "SELECT * FROM memory_selections WHERE selection_id=?", (selection_id,)
            ).fetchone()
            if row["reason_json"] != reason_json:
                raise ValueError("conflicting selection reason")
            rows.append(row)
    return [_selection(row) for row in rows]


def record_application(
    db_path: str | Path, *, selection_id: str, task_window_id: str, action: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicitly record use of a selected memory in a task action."""
    initialize_schema(db_path)
    selection = _text(selection_id, "selection_id")
    task, action = _text(task_window_id, "task_window_id"), _text(action, "action")
    context_json = _object(context or {}, "context")
    application_id = _id("app", selection, task, action)
    with _connection(db_path) as connection, connection:
        owner = connection.execute(
            "SELECT s.task_window_id,d.disposition FROM memory_selections s "
            "LEFT JOIN memory_dispositions d ON d.selection_id=s.selection_id "
            "WHERE s.selection_id=?", (selection,),
        ).fetchone()
        if owner is None:
            raise ValueError("unknown selection_id")
        if _effective_selection_owner(connection, selection) != task:
            raise ValueError("selection belongs to a different task window")
        if owner["disposition"] not in {None, "applied"}:
            raise ValueError("memory application contradicts the recorded disposition")
        connection.execute(
            "INSERT OR IGNORE INTO memory_applications VALUES(?,?,?,?,?,?)",
            (application_id, selection, task, action, context_json, _now()),
        )
        row = connection.execute("SELECT * FROM memory_applications WHERE application_id=?", (application_id,)).fetchone()
        if row["context_json"] != context_json:
            raise ValueError("conflicting application context")
    return _application(row)


def evaluate_application(
    db_path: str | Path, *, application_id: str, task_window_id: str, outcome: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach one immutable, evidence-backed evaluation to an application."""
    initialize_schema(db_path)
    application = _text(application_id, "application_id")
    task = _text(task_window_id, "task_window_id")
    if outcome not in EVALUATIONS:
        raise ValueError("invalid evaluation outcome")
    evidence_json = _object(evidence, "evidence", nonempty=True)
    evaluation_id = _id("eval", application)
    with _connection(db_path) as connection, connection:
        owner = connection.execute(
            "SELECT task_window_id FROM memory_applications WHERE application_id=?", (application,)
        ).fetchone()
        if owner is None:
            raise ValueError("unknown application_id")
        if owner[0] != task:
            raise ValueError("application belongs to a different task window")
        connection.execute(
            "INSERT OR IGNORE INTO memory_evaluations VALUES(?,?,?,?,?)",
            (evaluation_id, application, outcome, evidence_json, _now()),
        )
        row = connection.execute("SELECT * FROM memory_evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
        if row["outcome"] != outcome or row["evidence_json"] != evidence_json:
            raise ValueError("conflicting application evaluation")
    return _evaluation(row)


def record_disposition(
    db_path: str | Path, *, selection_id: str, task_window_id: str, disposition: str,
    reason: Mapping[str, Any], replacement_memory_id: str | None = None,
) -> dict[str, Any]:
    """Record one immutable task-local disposition for a selected memory."""
    initialize_schema(db_path)
    selection = _text(selection_id, "selection_id")
    task = _text(task_window_id, "task_window_id")
    if disposition not in DISPOSITIONS:
        raise ValueError("invalid memory disposition")
    reason_json = _object(reason, "reason", nonempty=True)
    replacement = (
        _text(replacement_memory_id, "replacement_memory_id")
        if replacement_memory_id is not None else None
    )
    if disposition == "superseded" and replacement is None:
        raise ValueError("superseded disposition requires replacement_memory_id")
    if disposition != "superseded" and replacement is not None:
        raise ValueError("replacement_memory_id is only valid for superseded disposition")
    disposition_id = _id("disp", selection)
    with _connection(db_path) as connection, connection:
        owner = connection.execute(
            "SELECT task_window_id,memory_id FROM memory_selections WHERE selection_id=?", (selection,)
        ).fetchone()
        if owner is None:
            raise ValueError("unknown selection_id")
        if _effective_selection_owner(connection, selection) != task:
            raise ValueError("selection belongs to a different task window")
        if replacement == owner["memory_id"]:
            raise ValueError("replacement memory must differ from selected memory")
        connection.execute(
            "INSERT OR IGNORE INTO memory_dispositions VALUES(?,?,?,?,?,?,?)",
            (disposition_id, selection, task, disposition, reason_json, replacement, _now()),
        )
        row = connection.execute(
            "SELECT * FROM memory_dispositions WHERE disposition_id=?", (disposition_id,)
        ).fetchone()
        if (row["disposition"], row["reason_json"], row["replacement_memory_id"]) != (
            disposition, reason_json, replacement,
        ):
            raise ValueError("conflicting memory disposition")
    return _disposition(row)


def record_disposition_effects(
    db_path: str | Path, *, selection_id: str, task_window_id: str, disposition: str,
    reason: Mapping[str, Any], replacement_memory_id: str | None = None,
    action: str | None = None, context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Atomically persist disposition, application, and retrieval-exclusion effects."""
    initialize_schema(db_path)
    selection = _text(selection_id, "selection_id")
    task = _text(task_window_id, "task_window_id")
    if disposition not in DISPOSITIONS:
        raise ValueError("invalid memory disposition")
    reason_json = _object(reason, "reason", nonempty=True)
    replacement = _text(replacement_memory_id, "replacement_memory_id") if replacement_memory_id else None
    if disposition == "applied":
        normalized_action = _text(action, "action")
    else:
        if action is not None or context is not None:
            raise ValueError("action and context are only valid for applied disposition")
        normalized_action = None
    if disposition == "superseded" and replacement is None:
        raise ValueError("superseded disposition requires replacement_memory_id")
    if disposition != "superseded" and replacement is not None:
        raise ValueError("replacement_memory_id is only valid for superseded disposition")
    disposition_id = _id("disp", selection)
    context_json = _object(context or {}, "context")
    application_id = _id("app", selection, task, normalized_action) if normalized_action else None
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            owner = connection.execute(
                "SELECT s.task_window_id,s.memory_id,m.project,m.status FROM memory_selections s "
                "JOIN memories m ON m.id=s.memory_id WHERE s.selection_id=?", (selection,),
            ).fetchone()
            if owner is None or _effective_selection_owner(connection, selection) != task:
                raise ValueError("unknown selection_id or selection belongs to a different task window")
            existing_app = connection.execute(
                "SELECT 1 FROM memory_applications WHERE selection_id=?", (selection,),
            ).fetchone()
            if existing_app and disposition != "applied":
                raise ValueError("an applied selection cannot receive a contradictory disposition")
            if replacement:
                replacement_row = connection.execute(
                    "SELECT project,status FROM memories WHERE id=?", (replacement,),
                ).fetchone()
                if replacement_row is None:
                    raise ValueError("replacement memory does not exist")
                if replacement_row["project"] != owner["project"]:
                    raise ValueError("replacement memory must share the same project/trust domain")
                if replacement_row["status"] not in {"candidate", "active", "confirmed"}:
                    raise ValueError("replacement memory is not in an eligible lifecycle state")
            connection.execute(
                "INSERT OR IGNORE INTO memory_dispositions VALUES(?,?,?,?,?,?,?)",
                (disposition_id, selection, task, disposition, reason_json, replacement, _now()),
            )
            row = connection.execute(
                "SELECT * FROM memory_dispositions WHERE disposition_id=?", (disposition_id,),
            ).fetchone()
            if (row["disposition"], row["reason_json"], row["replacement_memory_id"]) != (
                disposition, reason_json, replacement,
            ):
                raise ValueError("conflicting memory disposition")
            application_row = None
            if application_id:
                connection.execute(
                    "INSERT OR IGNORE INTO memory_applications VALUES(?,?,?,?,?,?)",
                    (application_id, selection, task, normalized_action, context_json, _now()),
                )
                application_row = connection.execute(
                    "SELECT * FROM memory_applications WHERE application_id=?", (application_id,),
                ).fetchone()
                if application_row["context_json"] != context_json:
                    raise ValueError("conflicting application context")
            if disposition in {"suspect", "superseded"}:
                from odibi_anchor.codebase._memory_promotion import (
                    withdraw_memory_in_transaction,
                )

                withdraw_memory_in_transaction(
                    connection, memory_id=owner["memory_id"], project_id=owner["project"],
                    final_status="quarantined" if disposition == "suspect" else "superseded",
                    superseded_by=replacement,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _disposition(row), _application(application_row) if application_row else None


def evaluate_application_effects(
    db_path: str | Path, *, application_id: str, task_window_id: str, outcome: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically evaluate one application and quarantine harmful memory."""
    initialize_schema(db_path)
    application = _text(application_id, "application_id")
    task = _text(task_window_id, "task_window_id")
    if outcome not in EVALUATIONS:
        raise ValueError("invalid evaluation outcome")
    evidence_json = _object(evidence, "evidence", nonempty=True)
    evaluation_id = _id("eval", application)
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            owner = connection.execute(
                "SELECT a.task_window_id,s.memory_id FROM memory_applications a "
                "JOIN memory_selections s ON s.selection_id=a.selection_id WHERE a.application_id=?",
                (application,),
            ).fetchone()
            if owner is None or owner["task_window_id"] != task:
                raise ValueError("unknown application_id or application belongs to a different task window")
            connection.execute(
                "INSERT OR IGNORE INTO memory_evaluations VALUES(?,?,?,?,?)",
                (evaluation_id, application, outcome, evidence_json, _now()),
            )
            row = connection.execute(
                "SELECT * FROM memory_evaluations WHERE evaluation_id=?", (evaluation_id,),
            ).fetchone()
            if row["outcome"] != outcome or row["evidence_json"] != evidence_json:
                raise ValueError("conflicting application evaluation")
            if outcome in {"harmful", "superseded"}:
                from odibi_anchor.codebase._memory_promotion import (
                    withdraw_memory_in_transaction,
                )

                memory = connection.execute(
                    "SELECT project FROM memories WHERE id=?", (owner["memory_id"],),
                ).fetchone()
                withdraw_memory_in_transaction(
                    connection, memory_id=owner["memory_id"], project_id=memory["project"],
                    final_status="quarantined",
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _evaluation(row)


def resolve_task_selection(
    db_path: str | Path, *, task_window_id: str, selection_id: str | None = None,
    memory_id: str | None = None, pending_only: bool = False,
) -> dict[str, Any]:
    """Resolve exactly one task-local selection without exposing ID bookkeeping."""
    initialize_schema(db_path)
    task = _text(task_window_id, "task_window_id")
    if selection_id and memory_id:
        raise ValueError("provide selection_id or memory_id, not both")
    clauses, params = ["COALESCE(r.replacement_task_window_id,s.task_window_id)=?"], [task]
    if selection_id:
        clauses.append("s.selection_id=?")
        params.append(_text(selection_id, "selection_id"))
    if memory_id:
        clauses.append("s.memory_id=?")
        params.append(_text(memory_id, "memory_id"))
    if pending_only:
        clauses.append("d.disposition_id IS NULL")
    with _connection(db_path) as connection:
        rows = connection.execute(
            "SELECT s.* FROM memory_selections s "
            "LEFT JOIN memory_dispositions d ON d.selection_id=s.selection_id "
            "LEFT JOIN memory_selection_recoveries r ON r.selection_id=s.selection_id WHERE "
            + " AND ".join(clauses) + " ORDER BY s.selected_at,s.selection_id",
            params,
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("memory selection resolution requires exactly one task-local match")
    return _selection(rows[0])


def pending_task_selections(db_path: str | Path, *, task_window_id: str) -> list[dict[str, Any]]:
    """Return selected memories still missing an explicit disposition."""
    initialize_schema(db_path)
    task = _text(task_window_id, "task_window_id")
    with _connection(db_path) as connection:
        rows = connection.execute(
            "SELECT s.* FROM memory_selections s LEFT JOIN memory_dispositions d ON d.selection_id=s.selection_id "
            "LEFT JOIN memory_selection_recoveries r ON r.selection_id=s.selection_id "
            "WHERE COALESCE(r.replacement_task_window_id,s.task_window_id)=? "
            "AND d.disposition_id IS NULL ORDER BY s.selected_at,s.selection_id",
            (task,),
        ).fetchall()
    return [_selection(row) for row in rows]


def unevaluated_task_applications(
    db_path: str | Path, *, task_window_id: str,
) -> list[dict[str, Any]]:
    """Return task-local memory applications still missing an evaluation."""
    initialize_schema(db_path)
    task = _text(task_window_id, "task_window_id")
    with _connection(db_path) as connection:
        rows = connection.execute(
            "SELECT a.* FROM memory_applications a "
            "LEFT JOIN memory_evaluations e ON e.application_id=a.application_id "
            "WHERE a.task_window_id=? AND e.evaluation_id IS NULL "
            "ORDER BY a.applied_at,a.application_id",
            (task,),
        ).fetchall()
    return [_application(row) for row in rows]


def record_projection(
    db_path: str | Path, *, learning_item_id: str, memory_id: str, lineage: Mapping[str, Any],
) -> dict[str, Any]:
    """Map one learning item to exactly one semantic memory with lineage."""
    initialize_schema(db_path)
    learning = _text(learning_item_id, "learning_item_id")
    memory = _text(memory_id, "memory_id")
    lineage_json = _object(lineage, "lineage", nonempty=True)
    projection_id = _id("proj", learning)
    with _connection(db_path) as connection, connection:
        existing = connection.execute(
            "SELECT * FROM memory_projections WHERE learning_item_id=?", (learning,)
        ).fetchone()
        if existing and existing["memory_id"] != memory:
            raise ValueError("learning item is already projected to a different memory")
        if existing and existing["lineage_json"] != lineage_json:
            raise ValueError("conflicting projection lineage")
        if not existing:
            connection.execute(
                "INSERT INTO memory_projections VALUES(?,?,?,?,?)",
                (projection_id, learning, memory, lineage_json, _now()),
            )
        row = connection.execute("SELECT * FROM memory_projections WHERE projection_id=?", (projection_id,)).fetchone()
    return _projection(row)


def get_projection(
    db_path: str | Path, *, learning_item_id: str,
) -> dict[str, Any] | None:
    """Return an existing episodic-to-semantic projection without writing."""
    learning = _text(learning_item_id, "learning_item_id")
    path = Path(db_path).expanduser()
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "memory_projections" not in tables:
            return None
        row = connection.execute(
            "SELECT * FROM memory_projections WHERE learning_item_id=?", (learning,)
        ).fetchone()
        return _projection(row) if row else None
    finally:
        connection.close()


def _accepted_task(
    connection: sqlite3.Connection, task_window_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not {"accepted_task_records", "accepted_task_events"}.issubset(tables):
        return None
    row = connection.execute(
        "SELECT * FROM accepted_task_records WHERE task_window_id=?", (task_window_id,),
    ).fetchone()
    if row is None:
        return None
    from odibi_anchor.codebase._task_authority import _load_verified_record

    record = _load_verified_record(row)
    event_row = connection.execute(
        "SELECT * FROM accepted_task_events WHERE task_window_id=? AND event_type='closed'",
        (task_window_id,),
    ).fetchone()
    event = None
    if event_row is not None:
        raw = event_row["event_json"]
        event = json.loads(raw)
        if (
            canonical_json(event) != raw
            or hashlib.sha256(raw.encode("utf-8")).hexdigest() != event_row["event_sha256"]
            or event.get("event_id") != event_row["event_id"]
            or event.get("task_window_id") != task_window_id
            or event.get("event_type") != "closed"
        ):
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        event["event_sha256"] = event_row["event_sha256"]
    return record, event


def _owner_matches(identity: Mapping[str, Any], owner: Mapping[str, Any]) -> bool:
    expected = {
        "project_id": owner["project_id"],
        "anchor_home": str(Path(owner["anchor_home"]).expanduser().resolve()),
        "project_root": str(Path(owner["project_root"]).expanduser().resolve()),
        "artifact_root": str(Path(owner["artifact_root"]).expanduser().resolve()),
        "target_root": str(Path(owner["target_root"]).expanduser().resolve()),
        "repository_provider_id": owner.get("repository_provider_id"),
        "trust_domain": owner.get("trust_domain"),
    }
    return all(identity.get(key) == value for key, value in expected.items())


def _recovery_authority_sha(record: Mapping[str, Any], closure: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({
        "record_sha256": hashlib.sha256(
            canonical_json(record).encode("utf-8")
        ).hexdigest(),
        "closure_sha256": closure["event_sha256"],
    }).encode("utf-8")).hexdigest()


def _selection_projection(
    connection: sqlite3.Connection, row: sqlite3.Row, owner: Mapping[str, Any],
) -> dict[str, Any]:
    selection = _selection(row)
    disposition = connection.execute(
        "SELECT disposition_id FROM memory_dispositions WHERE selection_id=?",
        (row["selection_id"],),
    ).fetchone()
    abandonment = connection.execute(
        "SELECT * FROM memory_selection_abandonments WHERE selection_id=?",
        (row["selection_id"],),
    ).fetchone()
    recovery = connection.execute(
        "SELECT * FROM memory_selection_recoveries WHERE selection_id=?",
        (row["selection_id"],),
    ).fetchone()
    authority = _accepted_task(connection, row["task_window_id"])
    if disposition:
        state = "semantically_disposed"
    elif recovery:
        replacement = _accepted_task(connection, recovery["replacement_task_window_id"])
        state = (
            "recovered"
            if replacement and replacement[1] is None
            and _owner_matches(replacement[0]["identity"], owner)
            else "ambiguous"
        )
    elif abandonment:
        state = "abandoned"
    elif authority is None or not _owner_matches(authority[0]["identity"], owner):
        state = "ambiguous"
    elif authority[1] is None:
        state = "active_pending"
    else:
        state = "terminal_unresolved"
    applied = connection.execute(
        "SELECT 1 FROM memory_applications WHERE selection_id=?", (row["selection_id"],),
    ).fetchone()
    return {
        **selection,
        "state": state,
        "selected_never_applied": applied is None,
        "abandonment_id": abandonment["abandonment_id"] if abandonment else None,
        "recovery_id": recovery["recovery_id"] if recovery else None,
        "replacement_task_window_id": (
            recovery["replacement_task_window_id"] if recovery else None
        ),
    }


def selection_recovery(
    db_path: str | Path, *, action: str, current_task_window_id: str,
    project_id: str, anchor_home: str, project_root: str, target_root: str,
    artifact_root: str, repository_provider_id: str | None, trust_domain: str | None,
    selection_id: str | None = None, prior_task_window_id: str | None = None,
    reason: Mapping[str, Any] | None = None, evidence: Mapping[str, Any] | None = None,
    actor_kind: str = "agent", actor_ref: str = "dispatcher", limit: int = 50,
) -> dict[str, Any]:
    """Inspect or append governed abandonment/recovery events for one exact owner."""
    command = _text(action, "action").lower()
    if command not in {"inspect", "declare_abandoned", "recover"}:
        raise ValueError("recovery action must be inspect, declare_abandoned, or recover")
    current_task = _text(current_task_window_id, "current_task_window_id")
    project = _text(project_id, "project_id")
    if type(limit) is not int or limit < 1 or limit > 200:
        raise ValueError("limit must be an integer from 1 through 200")
    owner = {
        "project_id": project,
        "anchor_home": anchor_home,
        "project_root": project_root,
        "target_root": target_root,
        "artifact_root": artifact_root,
        "repository_provider_id": repository_provider_id,
        "trust_domain": trust_domain,
    }
    if command == "inspect":
        return _inspect_selection_recovery(
            db_path,
            current_task=current_task,
            project=project,
            owner=owner,
            selection_id=selection_id,
            prior_task_window_id=prior_task_window_id,
            limit=limit,
        )
    initialize_schema(db_path)
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _accepted_task(connection, current_task)
        if current is None or current[1] is not None or not _owner_matches(current[0]["identity"], owner):
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        clauses = ["m.project=?"]
        params: list[Any] = [project]
        if selection_id is not None:
            clauses.append("s.selection_id=?")
            params.append(_text(selection_id, "selection_id"))
        if prior_task_window_id is not None:
            clauses.append("s.task_window_id=?")
            params.append(_text(prior_task_window_id, "prior_task_window_id"))
        rows = connection.execute(
            "SELECT s.* FROM memory_selections s JOIN memories m ON m.id=s.memory_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY s.selected_at,s.selection_id LIMIT ?",
            (*params, limit),
        ).fetchall()
        if len(rows) != 1 or prior_task_window_id is None or selection_id is None:
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        row = rows[0]
        if row["task_window_id"] == current_task:
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        if connection.execute(
            "SELECT 1 FROM memory_dispositions WHERE selection_id=?", (selection_id,),
        ).fetchone():
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        origin = _accepted_task(connection, row["task_window_id"])
        if origin is None or not _owner_matches(origin[0]["identity"], owner):
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        target_sha, artifact_sha = _path_digest(target_root), _path_digest(artifact_root)
        reason_json = _object(reason, "reason", nonempty=True)
        evidence_json = _object(evidence, "evidence", nonempty=True)
        normalized_actor_kind = _text(actor_kind, "actor_kind")
        normalized_actor_ref = _text(actor_ref, "actor_ref")
        if command == "declare_abandoned":
            closure = origin[1]
            status = closure and closure.get("details", {}).get("terminal_status")
            if status not in {"blocked", "failed", "superseded"}:
                raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
            event_id = _id("msa", selection_id)
            authority_sha = _recovery_authority_sha(origin[0], closure)
            values = (
                event_id, selection_id, row["task_window_id"], project, trust_domain,
                target_sha, artifact_sha, closure["event_id"], status, authority_sha,
                reason_json, evidence_json, normalized_actor_kind, normalized_actor_ref,
            )
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO memory_selection_abandonments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, _now()),
                )
                stored = connection.execute(
                    "SELECT * FROM memory_selection_abandonments WHERE selection_id=?",
                    (selection_id,),
                ).fetchone()
                if tuple(stored)[:-1] != values:
                    raise ValueError("memory abandonment idempotency conflict")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return _abandonment(stored)
        abandonment = connection.execute(
            "SELECT * FROM memory_selection_abandonments WHERE selection_id=?",
            (selection_id,),
        ).fetchone()
        closure = origin[1]
        status = closure and closure.get("details", {}).get("terminal_status")
        expected_abandonment = {
            "selection_id": selection_id,
            "origin_task_window_id": row["task_window_id"],
            "project_id": project,
            "trust_domain": trust_domain,
            "target_identity_sha256": target_sha,
            "artifact_identity_sha256": artifact_sha,
            "closure_event_id": closure and closure["event_id"],
            "closure_status": status,
            "authority_sha256": (
                _recovery_authority_sha(origin[0], closure) if closure else None
            ),
        }
        if abandonment is None or any(
            abandonment[key] != value for key, value in expected_abandonment.items()
        ):
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        recovery_id = _id("msr", abandonment["abandonment_id"])
        values = (
            recovery_id, abandonment["abandonment_id"], selection_id,
            row["task_window_id"], current_task, project, trust_domain,
            target_sha, artifact_sha, reason_json, evidence_json,
            normalized_actor_kind, normalized_actor_ref,
        )
        try:
            connection.execute(
                "INSERT OR IGNORE INTO memory_selection_recoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values, _now()),
            )
            stored = connection.execute(
                "SELECT * FROM memory_selection_recoveries WHERE selection_id=?",
                (selection_id,),
            ).fetchone()
            if tuple(stored)[:-1] != values:
                raise ValueError("memory recovery idempotency conflict")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return _recovery(stored)


def _inspect_selection_recovery(
    db_path: str | Path, *, current_task: str, project: str,
    owner: Mapping[str, Any], selection_id: str | None,
    prior_task_window_id: str | None, limit: int,
) -> dict[str, Any]:
    """Project recovery state through a strictly read-only SQLite connection."""
    status = selection_recovery_migration_status(db_path)
    labels = (
        "active_pending", "terminal_unresolved", "abandoned", "recovered",
        "semantically_disposed", "ambiguous",
    )
    if status["schema_status"] == "uninitialized":
        return {
            "schema_status": "uninitialized", "schema_version": None,
            "project_id": project, "states": [],
            "counts": {label: 0 for label in labels},
            "selected_never_applied": 0,
            "next_actions": ["Apply the selection_recovery migration before inspection."],
        }
    if status["schema_status"] != "exact":
        raise RuntimeError("memory lifecycle schema version/checksum mismatch")
    with _readonly_connection(db_path) as connection:
        current = _accepted_task(connection, current_task)
        if current is None or current[1] is not None or not _owner_matches(current[0]["identity"], owner):
            raise RecoveryUnavailable(RECOVERY_UNAVAILABLE)
        clauses = ["m.project=?"]
        params: list[Any] = [project]
        if selection_id is not None:
            clauses.append("s.selection_id=?")
            params.append(_text(selection_id, "selection_id"))
        if prior_task_window_id is not None:
            clauses.append("s.task_window_id=?")
            params.append(_text(prior_task_window_id, "prior_task_window_id"))
        rows = connection.execute(
            "SELECT s.* FROM memory_selections s JOIN memories m ON m.id=s.memory_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY s.selected_at,s.selection_id LIMIT ?",
            (*params, limit),
        ).fetchall()
        projected = [_selection_projection(connection, row, owner) for row in rows]
    return {
        "schema_status": "exact", "schema_version": SCHEMA_VERSION,
        "project_id": project, "states": projected,
        "counts": {
            label: sum(item["state"] == label for item in projected)
            for label in labels
        },
        "selected_never_applied": sum(item["selected_never_applied"] for item in projected),
        "next_actions": [
            "Attempt exact task_rebind before declaring abandonment.",
            "Declare abandonment only for blocked, failed, or superseded exact-owner tasks.",
            "Recover an abandonment into the current open exact-owner task.",
            "Record semantic application or disposition separately after recovery.",
        ],
    }


def selection_recovery_migration_status(db_path: str | Path) -> dict[str, Any]:
    """Inspect memory lifecycle migration and rollback eligibility without mutation."""
    path = Path(db_path).expanduser()
    if not path.is_file():
        return {
            "schema_status": "uninitialized", "version": None,
            "target_version": SCHEMA_VERSION, "rollback_eligible": False,
            "planned_action": "initialize_v3",
        }
    with _readonly_connection(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "anchor_schema_versions" not in tables:
            return {
                "schema_status": "uninitialized", "version": None,
                "target_version": SCHEMA_VERSION, "rollback_eligible": False,
                "planned_action": "initialize_v3",
            }
        row = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
        ).fetchone()
        if row is None:
            return {
                "schema_status": "uninitialized", "version": None,
                "target_version": SCHEMA_VERSION, "rollback_eligible": False,
                "planned_action": "initialize_v3",
            }
        version, checksum = row
        expected = {1: V1_SCHEMA_SHA256, 2: V2_SCHEMA_SHA256, 3: SCHEMA_SHA256}
        if version not in expected or checksum != expected[version]:
            return {
                "schema_status": "drift", "version": version,
                "target_version": SCHEMA_VERSION, "rollback_eligible": False,
                "rollback_blocker": "schema version/checksum mismatch",
                "planned_action": "blocked",
            }
        _verify_objects(connection, version=version)
        if version < SCHEMA_VERSION:
            return {
                "schema_status": f"legacy_v{version}", "version": version,
                "target_version": SCHEMA_VERSION, "rollback_eligible": False,
                "planned_action": f"migrate_v{version}_to_v3",
            }
        marker = connection.execute(
            "SELECT * FROM memory_lifecycle_migrations WHERE migration_id=?",
            ("memory-lifecycle-v2-to-v3",),
        ).fetchone()
        event_counts = {
            "abandonments": connection.execute(
                "SELECT count(*) FROM memory_selection_abandonments"
            ).fetchone()[0],
            "recoveries": connection.execute(
                "SELECT count(*) FROM memory_selection_recoveries"
            ).fetchone()[0],
        }
        blocker = None
        if marker is None:
            blocker = "no v2-to-v3 migration record"
        elif not Path(marker["backup_path"]).is_file():
            blocker = "verified migration backup is unavailable"
        else:
            try:
                with _readonly_connection(marker["backup_path"]) as backup:
                    if (
                        backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or _pre_v3_digest(backup) != marker["pre_v3_digest"]
                    ):
                        blocker = "verified migration backup does not match the pre-v3 digest"
            except sqlite3.DatabaseError:
                blocker = "verified migration backup is unreadable"
        if blocker is None and any(event_counts.values()):
            blocker = "immutable abandonment or recovery history exists"
        elif blocker is None and _pre_v3_digest(connection) != marker["pre_v3_digest"]:
            blocker = "pre-v3 lifecycle rows changed"
        return {
            "schema_status": "exact", "version": SCHEMA_VERSION,
            "target_version": SCHEMA_VERSION, "schema_sha256": SCHEMA_SHA256,
            "migration": dict(marker) if marker is not None else None,
            "event_counts": event_counts,
            "rollback_eligible": blocker is None,
            "rollback_blocker": blocker,
            "planned_action": "none",
        }


def rollback_recovery_schema(db_path: str | Path) -> dict[str, Any]:
    """Downgrade v3 only while no immutable abandonment or recovery event exists."""
    status = selection_recovery_migration_status(db_path)
    if not status.get("rollback_eligible"):
        raise RuntimeError(
            "memory lifecycle v3 rollback refused; "
            + str(status.get("rollback_blocker") or "no eligible migration")
        )
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            marker = connection.execute(
                "SELECT * FROM memory_lifecycle_migrations WHERE migration_id=?",
                ("memory-lifecycle-v2-to-v3",),
            ).fetchone()
            counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("memory_selection_abandonments", "memory_selection_recoveries")
            }
            if marker is None or any(counts.values()):
                raise RuntimeError(
                    "memory lifecycle v3 rollback refused; immutable recovery history must be retained"
                )
            if _pre_v3_digest(connection) != marker["pre_v3_digest"]:
                raise RuntimeError("memory lifecycle v3 rollback refused; pre-v3 rows changed")
            for statement in reversed(_V3_DDL):
                kind, name = statement.split()[1:3]
                normalized = name.split("(", 1)[0]
                connection.execute(f"DROP {kind} IF EXISTS {normalized}")
            connection.execute(
                "UPDATE anchor_schema_versions SET version=?,schema_sha256=?,applied_at=? WHERE domain=?",
                (2, V2_SCHEMA_SHA256, _now(), DOMAIN),
            )
            _verify_objects(connection, version=2)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"status": "rolled_back", "version": 2, "backup_path": marker["backup_path"]}


def task_memory_activity(
    db_path: str | Path, *, task_window_id: str,
) -> dict[str, list[str]]:
    """Restore durable selection/application IDs for one task without writing."""
    task = _text(task_window_id, "task_window_id")
    path = Path(db_path).expanduser()
    empty = {"memory_selections": [], "memory_dispositions": [], "memory_applications": [], "memory_evaluations": []}
    if not path.is_file():
        return empty
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"memory_selections", "memory_applications"}.issubset(tables):
            return empty
        row = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)
        ).fetchone()
        if row != (SCHEMA_VERSION, SCHEMA_SHA256):
            raise RuntimeError("memory lifecycle schema version/checksum mismatch")
        selections = [
            item[0] for item in connection.execute(
                "SELECT s.selection_id FROM memory_selections s "
                "LEFT JOIN memory_selection_recoveries r ON r.selection_id=s.selection_id "
                "WHERE COALESCE(r.replacement_task_window_id,s.task_window_id)=? "
                "ORDER BY s.selected_at,s.selection_id",
                (task,),
            )
        ]
        applications = [
            item[0] for item in connection.execute(
                "SELECT application_id FROM memory_applications WHERE task_window_id=? "
                "ORDER BY applied_at,application_id",
                (task,),
            )
        ]
        dispositions = [
            item[0] for item in connection.execute(
                "SELECT disposition_id FROM memory_dispositions WHERE task_window_id=? ORDER BY disposed_at,disposition_id",
                (task,),
            )
        ] if "memory_dispositions" in tables else []
        evaluations = [
            item[0] for item in connection.execute(
                "SELECT e.evaluation_id FROM memory_evaluations e JOIN memory_applications a ON a.application_id=e.application_id WHERE a.task_window_id=? ORDER BY e.evaluated_at,e.evaluation_id",
                (task,),
            )
        ]
        return {"memory_selections": selections, "memory_dispositions": dispositions,
                "memory_applications": applications, "memory_evaluations": evaluations}
    finally:
        connection.close()


def task_memory_records(
    db_path: str | Path, *, task_window_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Read detailed, redacted lifecycle facts for a durable task projection."""
    task = _text(task_window_id, "task_window_id")
    path = Path(db_path).expanduser()
    empty: dict[str, list[dict[str, Any]]] = {
        "selections": [], "dispositions": [], "applications": [], "evaluations": [],
        "projections": [],
    }
    if not path.is_file():
        return empty
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"memory_selections", "memory_applications", "memory_evaluations"}.issubset(tables):
            return empty
        selections = connection.execute(
            "SELECT s.* FROM memory_selections s "
            "LEFT JOIN memory_selection_recoveries r ON r.selection_id=s.selection_id "
            "WHERE COALESCE(r.replacement_task_window_id,s.task_window_id)=? "
            "ORDER BY s.selected_at,s.selection_id",
            (task,),
        ).fetchall()
        applications = connection.execute(
            "SELECT * FROM memory_applications WHERE task_window_id=? ORDER BY applied_at,application_id",
            (task,),
        ).fetchall()
        evaluations = connection.execute(
            "SELECT e.* FROM memory_evaluations e JOIN memory_applications a ON a.application_id=e.application_id WHERE a.task_window_id=? ORDER BY e.evaluated_at,e.evaluation_id",
            (task,),
        ).fetchall()
        dispositions = connection.execute(
            "SELECT * FROM memory_dispositions WHERE task_window_id=? ORDER BY disposed_at,disposition_id",
            (task,),
        ).fetchall() if "memory_dispositions" in tables else []
        projections = connection.execute(
            "SELECT * FROM memory_projections WHERE json_extract(lineage_json,'$.task_window_id')=? "
            "ORDER BY projected_at,projection_id", (task,),
        ).fetchall() if "memory_projections" in tables else []
        return {
            "selections": [_selection(row) for row in selections],
            "dispositions": [_disposition(row) for row in dispositions],
            "applications": [_application(row) for row in applications],
            "evaluations": [_evaluation(row) for row in evaluations],
            "projections": [_projection(row) for row in projections],
        }
    finally:
        connection.close()


def _selection(row: sqlite3.Row) -> dict[str, Any]:
    return {"selection_id": row["selection_id"], "task_window_id": row["task_window_id"],
            "query": json.loads(row["query_json"]), "memory_id": row["memory_id"],
            "reason": json.loads(row["reason_json"]), "selected_at": row["selected_at"]}


def _application(row: sqlite3.Row) -> dict[str, Any]:
    return {"application_id": row["application_id"], "selection_id": row["selection_id"],
            "task_window_id": row["task_window_id"], "action": row["action"],
            "context": json.loads(row["context_json"]), "applied_at": row["applied_at"]}


def _evaluation(row: sqlite3.Row) -> dict[str, Any]:
    return {"evaluation_id": row["evaluation_id"], "application_id": row["application_id"],
            "outcome": row["outcome"], "evidence": json.loads(row["evidence_json"]),
            "evaluated_at": row["evaluated_at"]}


def _disposition(row: sqlite3.Row) -> dict[str, Any]:
    return {"disposition_id": row["disposition_id"], "selection_id": row["selection_id"],
            "task_window_id": row["task_window_id"], "disposition": row["disposition"],
            "reason": json.loads(row["reason_json"]),
            "replacement_memory_id": row["replacement_memory_id"], "disposed_at": row["disposed_at"]}


def _projection(row: sqlite3.Row) -> dict[str, Any]:
    return {"projection_id": row["projection_id"], "learning_item_id": row["learning_item_id"],
            "memory_id": row["memory_id"], "lineage": json.loads(row["lineage_json"]),
            "projected_at": row["projected_at"]}


def _abandonment(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "abandonment_id": row["abandonment_id"],
        "selection_id": row["selection_id"],
        "origin_task_window_id": row["origin_task_window_id"],
        "project_id": row["project_id"],
        "closure_event_id": row["closure_event_id"],
        "closure_status": row["closure_status"],
        "authority_sha256": row["authority_sha256"],
        "reason": json.loads(row["reason_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "actor_kind": row["actor_kind"],
        "actor_ref": row["actor_ref"],
        "abandoned_at": row["abandoned_at"],
    }


def _recovery(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "recovery_id": row["recovery_id"],
        "abandonment_id": row["abandonment_id"],
        "selection_id": row["selection_id"],
        "origin_task_window_id": row["origin_task_window_id"],
        "replacement_task_window_id": row["replacement_task_window_id"],
        "project_id": row["project_id"],
        "reason": json.loads(row["reason_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "actor_kind": row["actor_kind"],
        "actor_ref": row["actor_ref"],
        "recovered_at": row["recovered_at"],
    }
