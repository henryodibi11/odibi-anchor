"""Workbench-owned structured learning ledger.

This module deliberately uses short-lived SQLite connections and does not share the
legacy memory connection.  Mutation entry points used by the dispatcher are private;
the public dispatcher only supplies an already-active obligation.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_IS_WINDOWS = os.name == "nt"

DOMAIN = "structured_learning"
VERSION = 2
OWNER_SCHEMA_VERSION = 1
MIGRATION_ID = "learning-obligations-v1-to-v2"
MIGRATION_DOMAIN_DIGEST_KIND = "structured-learning-domain-v1"
TABLES = (
    "learning_items",
    "learning_evidence_refs",
    "learning_item_links",
    "learning_events",
    "learning_obligations",
    "learning_assessments",
    "learning_assessment_observations",
    "learning_obligation_migrations",
)
LEGACY_DDL = (
    "CREATE TABLE anchor_schema_versions (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64), applied_at TEXT NOT NULL)",
    "CREATE TABLE learning_items (item_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('observation','lesson','watch_item','improvement_candidate')), observation_type TEXT DEFAULT NULL, status TEXT NOT NULL CHECK(status IN ('open','active','deferred','rejected','retired')), summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 1000), signal_key TEXT DEFAULT NULL, recurrence_key TEXT DEFAULT NULL, occurrence_key TEXT DEFAULT NULL UNIQUE, decision_key TEXT DEFAULT NULL UNIQUE, payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64), impact TEXT NOT NULL CHECK(impact IN ('low','medium','high','critical')), applicability_scope TEXT NOT NULL CHECK(applicability_scope IN ('project_local','workbench','cross_project')), project_refs TEXT NOT NULL DEFAULT '[]', work_package_refs TEXT NOT NULL DEFAULT '[]', environment_refs TEXT NOT NULL DEFAULT '[]', provenance TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 1 CHECK(version>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, CHECK((kind='observation' AND observation_type IN ('friction','blocker','near_miss','reusable_practice','evidence_gap') AND signal_key IS NOT NULL AND length(signal_key) BETWEEN 1 AND 128 AND recurrence_key IS NOT NULL AND occurrence_key IS NOT NULL AND decision_key IS NULL AND status IN ('open','deferred','rejected','retired')) OR (kind!='observation' AND observation_type IS NULL AND signal_key IS NULL AND recurrence_key IS NULL AND occurrence_key IS NULL AND decision_key IS NOT NULL AND status IN ('active','deferred','rejected','retired'))))",
    "CREATE INDEX idx_learning_recurrence ON learning_items(recurrence_key,created_at,item_id)",
    "CREATE INDEX idx_learning_kind_status ON learning_items(kind,status,updated_at,item_id)",
    "CREATE TABLE learning_evidence_refs (evidence_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES learning_items(item_id) ON DELETE CASCADE, reference_type TEXT NOT NULL CHECK(reference_type IN ('file','git_commit','problem','spec','test','session')), reference TEXT NOT NULL CHECK(length(reference) BETWEEN 1 AND 512), reference_sha256 TEXT NOT NULL CHECK(length(reference_sha256)=64), summary TEXT NOT NULL DEFAULT '' CHECK(length(summary)<=500), observed_at TEXT DEFAULT NULL, created_at TEXT NOT NULL, UNIQUE(item_id,reference_sha256))",
    "CREATE TABLE learning_item_links (source_item_id TEXT NOT NULL REFERENCES learning_items(item_id) ON DELETE RESTRICT, derived_item_id TEXT NOT NULL REFERENCES learning_items(item_id) ON DELETE CASCADE, relation TEXT NOT NULL CHECK(relation='derived_from'), created_at TEXT NOT NULL, PRIMARY KEY(source_item_id,derived_item_id,relation), CHECK(source_item_id!=derived_item_id))",
    "CREATE INDEX idx_learning_links_derived ON learning_item_links(derived_item_id,source_item_id)",
    "CREATE TABLE learning_events (event_id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES learning_items(item_id) ON DELETE CASCADE, event_type TEXT NOT NULL CHECK(event_type IN ('captured','derived','deferred','reopened','rejected','retired')), from_status TEXT DEFAULT NULL, to_status TEXT NOT NULL, actor_kind TEXT NOT NULL CHECK(actor_kind IN ('agent','human','system')), actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 128), decision_source TEXT NOT NULL DEFAULT '' CHECK(length(decision_source)<=256), rationale TEXT NOT NULL DEFAULT '' CHECK(length(rationale)<=1000), item_version INTEGER NOT NULL CHECK(item_version>0), created_at TEXT NOT NULL)",
    "CREATE INDEX idx_learning_events_item ON learning_events(item_id,created_at,event_id)",
    "CREATE TABLE learning_obligations (obligation_id TEXT PRIMARY KEY, task_window_id TEXT NOT NULL CHECK(length(task_window_id) BETWEEN 1 AND 256), session_ref TEXT NOT NULL CHECK(length(session_ref) BETWEEN 1 AND 256), checkpoint_ref TEXT NOT NULL CHECK(length(checkpoint_ref) BETWEEN 1 AND 256), project_ref TEXT DEFAULT NULL, status TEXT NOT NULL CHECK(status IN ('active','assessed','legacy_closed')), created_at TEXT NOT NULL, activated_at TEXT NOT NULL, closed_at TEXT DEFAULT NULL, CHECK((status='active' AND closed_at IS NULL) OR (status IN ('assessed','legacy_closed') AND closed_at IS NOT NULL)))",
    "CREATE UNIQUE INDEX idx_learning_one_active_obligation ON learning_obligations((1)) WHERE status='active'",
    "CREATE TABLE learning_assessments (assessment_id TEXT PRIMARY KEY, obligation_id TEXT NOT NULL UNIQUE REFERENCES learning_obligations(obligation_id) ON DELETE RESTRICT, outcome TEXT NOT NULL CHECK(outcome IN ('observations_recorded','nothing_reusable_learned')), notes TEXT NOT NULL DEFAULT '' CHECK(length(notes)<=500), actor_kind TEXT NOT NULL CHECK(actor_kind IN ('agent','human')), actor_ref TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 128), created_at TEXT NOT NULL)",
    "CREATE TABLE learning_assessment_observations (assessment_id TEXT NOT NULL REFERENCES learning_assessments(assessment_id) ON DELETE CASCADE, observation_id TEXT NOT NULL REFERENCES learning_items(item_id) ON DELETE RESTRICT, PRIMARY KEY(assessment_id,observation_id))",
)
_OWNED_OBLIGATION_DDL = "CREATE TABLE learning_obligations (obligation_id TEXT PRIMARY KEY, project_id TEXT DEFAULT NULL CHECK(project_id IS NULL OR length(project_id) BETWEEN 1 AND 256), task_window_id TEXT NOT NULL CHECK(length(task_window_id) BETWEEN 1 AND 256), session_ref TEXT NOT NULL CHECK(length(session_ref) BETWEEN 1 AND 256), checkpoint_ref TEXT NOT NULL CHECK(length(checkpoint_ref) BETWEEN 1 AND 256), project_ref TEXT DEFAULT NULL, owner_schema_version INTEGER NOT NULL CHECK(owner_schema_version=1), owner_state TEXT NOT NULL CHECK(owner_state IN ('owned','quarantined')), status TEXT NOT NULL CHECK(status IN ('active','assessed','legacy_closed')), created_at TEXT NOT NULL, activated_at TEXT NOT NULL, closed_at TEXT DEFAULT NULL, CHECK((owner_state='owned' AND project_id IS NOT NULL) OR (owner_state='quarantined' AND project_id IS NULL)), CHECK((status='active' AND closed_at IS NULL) OR (status IN ('assessed','legacy_closed') AND closed_at IS NOT NULL)))"
_OWNED_ACTIVE_INDEX_DDL = "CREATE UNIQUE INDEX idx_learning_one_active_obligation ON learning_obligations(project_id,task_window_id) WHERE status='active' AND owner_state='owned'"
_MIGRATION_TABLE_DDL = "CREATE TABLE learning_obligation_migrations (migration_id TEXT PRIMARY KEY, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, source_digest TEXT NOT NULL CHECK(length(source_digest)=64), backup_path TEXT NOT NULL, prior_table_sql TEXT NOT NULL, prior_index_sql TEXT NOT NULL, row_count INTEGER NOT NULL CHECK(row_count>=0), quarantined_count INTEGER NOT NULL CHECK(quarantined_count>=0), pre_row_digest TEXT NOT NULL CHECK(length(pre_row_digest)=64), post_row_digest TEXT NOT NULL CHECK(length(post_row_digest)=64), incompatible_writes INTEGER NOT NULL DEFAULT 0 CHECK(incompatible_writes>=0), applied_at TEXT NOT NULL)"
_MIGRATION_WRITE_TABLES = (
    "learning_items",
    "learning_evidence_refs",
    "learning_item_links",
    "learning_events",
    "learning_obligations",
    "learning_assessments",
    "learning_assessment_observations",
)
_MIGRATION_WRITE_TRIGGERS = tuple(
    f"CREATE TRIGGER trg_learning_migration_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN UPDATE learning_obligation_migrations SET incompatible_writes=incompatible_writes+1 WHERE migration_id='{MIGRATION_ID}'; END"
    for table in _MIGRATION_WRITE_TABLES
    for operation in ("INSERT", "UPDATE", "DELETE")
)
DDL = (
    *LEGACY_DDL[:9],
    _OWNED_OBLIGATION_DDL,
    _OWNED_ACTIVE_INDEX_DDL,
    *LEGACY_DDL[11:],
    _MIGRATION_TABLE_DDL,
    *_MIGRATION_WRITE_TRIGGERS,
)
LEGACY_SCHEMA_TEXT = ";\n".join(LEGACY_DDL) + ";\n"
LEGACY_SCHEMA_SHA256 = hashlib.sha256(LEGACY_SCHEMA_TEXT.encode()).hexdigest()
SCHEMA_TEXT = ";\n".join(DDL) + ";\n"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_TEXT.encode()).hexdigest()
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
SIGNAL = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
REFS = {
    "file": re.compile(
        r"(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*(?:#L[1-9][0-9]*(?:-L[1-9][0-9]*)?)?\Z"
    ),
    "git_commit": re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z"),
    "problem": re.compile(r"PRB-[0-9]{4}-[0-9]{4}\Z"),
    "spec": re.compile(r"[A-Z0-9][A-Z0-9_]{0,127}\Z"),
    "test": re.compile(r"tests/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py(?:::[A-Za-z0-9_\[\].,=-]{1,128}){0,8}\Z"),
    "session": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z"),
}
SENSITIVE = re.compile(
    r"\b[a-z][a-z0-9+.-]{1,31}://|//\S+|\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|cloud|app|local)\b|\b(?:token|password|secret|authorization|api[_-]?key)\s*[:=]|\bbearer\s+[A-Za-z0-9._~+/-]+=*|\b(?:x-amz-signature|x-goog-signature|signature|sig|se|sp|sv)=|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?<![A-Za-z0-9._-])/(?:home|workspace|Users|dbfs|Volumes)(?:/|(?=$|\s|[^A-Za-z0-9._-]))",
    re.I,
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _text(value: Any, field: str, limit: int, *, empty: bool = False, free: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    value = unicodedata.normalize("NFC", value)
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError(f"invalid {field}")
    value = " ".join(value.strip().split())
    if (not empty and not value) or len(value) > limit:
        raise ValueError(f"invalid {field}")
    if free and SENSITIVE.search(value):
        raise ValueError(f"invalid {field}")
    return value


def _ident(value: Any, field: str, limit: int = 256) -> str:
    value = _text(value, field, limit)
    if not ID.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError(f"invalid {field}")
    return sorted(set(_ident(v, field) for v in value))


def _db_path(*, forbidden_roots: tuple[str, ...] = ()) -> Path:
    from odibi_anchor._runtime_paths import resolve_runtime_paths
    from odibi_anchor.codebase._memory_db import _resolve_default_db_path

    path = Path(_resolve_default_db_path()).expanduser().resolve()
    # Use dispatcher runtime_paths only when the resolved DB matches the
    # dispatcher's configured DB — this avoids the Databricks ANCHOR_HOME guard
    # for the common case while still picking up monkeypatched ANCHOR_HOME for
    # explicit/custom DB paths (needed by safety tests).
    runtime_paths = None
    dispatcher_environment: dict[str, Any] = {}
    try:
        from odibi_anchor._dispatcher._boot import _ENV

        dispatcher_environment = _ENV
        dispatcher_db = Path(_ENV.get("memory_db", "")).expanduser().resolve()
        if path == dispatcher_db:
            runtime_paths = _ENV.get("runtime_paths")
    except ImportError:  # pragma: no cover - package can be used without dispatcher
        pass
    if runtime_paths is None:
        runtime_paths = resolve_runtime_paths()
    home = runtime_paths.anchor_home
    default = (home / ".agent_memory.db").resolve()
    forbidden = [home / "workspace" / "projects", runtime_paths.resource_root]
    forbidden.extend(
        Path(value).expanduser().resolve()
        for value in (
            dispatcher_environment.get("home"),
            dispatcher_environment.get("framework_root"),
            dispatcher_environment.get("sync_target"),
            *(dispatcher_environment.get("project_roots") or []),
        )
        if value
    )
    try:
        from odibi_anchor._utils._session_state import _SESSION_STATE

        forbidden.extend(
            Path(value).expanduser().resolve()
            for value in (
                _SESSION_STATE.target_root,
                _SESSION_STATE.artifact_root,
                _SESSION_STATE.project_root,
            )
            if value
        )
    except ImportError:  # pragma: no cover - package can be used without dispatcher
        pass
    cwd = Path.cwd().resolve()
    if home not in (cwd, *cwd.parents):
        forbidden.append(cwd)
    forbidden.extend(Path(value).expanduser().resolve() for value in forbidden_roots if value)
    if path != default and any(root == path or root in path.parents for root in forbidden):
        raise ValueError("learning database path is unsafe")
    return path


def _connect(path: Path, *, ro: bool = False) -> sqlite3.Connection:
    from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

    target = f"file:{path}?mode=ro" if ro else str(path)
    c = connect_shared_memory(
        target,
        owner_key_kind="project_id,task_window_id",
        uri=ro,
        isolation_level=None,
    )
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    if c.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        c.close()
        raise RuntimeError("learning schema foreign keys disabled")
    if not ro:
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA synchronous=FULL")
    return c


def _publish_backup(temporary: Path, destination: Path, *, collision_error: str) -> None:
    """Durably publish a verified backup without overwriting an existing destination."""
    try:
        # Windows' _commit rejects read-only descriptors, so fsync through a
        # writable handle while preserving fatal file-durability failures.
        with open(temporary, "r+b") as backup_file:
            os.fsync(backup_file.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise RuntimeError(collision_error) from exc
        except OSError as exc:
            if exc.errno not in (errno.ENOSYS, errno.EPERM):
                raise
            # Filesystem lacks or prohibits hard links (e.g. Databricks workspace FS).
            # Fall back to check-then-rename — safe under Anchor single-writer.
            try:
                destination.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(collision_error) from exc
            os.rename(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    # Opening a directory descriptor is unsupported on Windows. File fsync above
    # remains mandatory there; POSIX additionally persists the directory entry.
    if not _IS_WINDOWS:
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _schema_signature(c: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = c.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name='anchor_schema_versions' OR name LIKE 'learning_%' "
        "OR tbl_name LIKE 'learning_%' ORDER BY type,name"
    )
    return {(r[0], r[1]): re.sub(r"\s+", "", r[2] or "").lower() for r in rows}


def _expected_schema_signature(
    statements: tuple[str, ...] = DDL,
) -> dict[tuple[str, str], str]:
    c = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            c.execute(statement)
        return _schema_signature(c)
    finally:
        c.close()


EXPECTED_SCHEMA_SIGNATURE = _expected_schema_signature()
LEGACY_EXPECTED_SCHEMA_SIGNATURE = _expected_schema_signature(LEGACY_DDL)

_LEGACY_OBLIGATION_COLUMNS = (
    "obligation_id",
    "task_window_id",
    "session_ref",
    "checkpoint_ref",
    "project_ref",
    "status",
    "created_at",
    "activated_at",
    "closed_at",
)
_OBLIGATION_COLUMNS = (
    "obligation_id",
    "project_id",
    "task_window_id",
    "session_ref",
    "checkpoint_ref",
    "project_ref",
    "owner_schema_version",
    "owner_state",
    "status",
    "created_at",
    "activated_at",
    "closed_at",
)


def _owner(project_id: Any, task_window_id: Any) -> tuple[str, str]:
    """Validate the complete authoritative learning-obligation owner key."""
    return (
        _ident(project_id, "project_id"),
        _ident(task_window_id, "task_window_id"),
    )


def _obligation_projection_digest(
    c: sqlite3.Connection, *, legacy_table: str = "learning_obligations",
) -> tuple[int, str]:
    """Hash the legacy column projection in stable obligation order."""
    columns = ",".join(_LEGACY_OBLIGATION_COLUMNS)
    rows = [
        list(row)
        for row in c.execute(
            f"SELECT {columns} FROM {legacy_table} ORDER BY obligation_id"
        )
    ]
    return len(rows), _sha(rows)


def _learning_domain_digest(c: sqlite3.Connection) -> str:
    """Hash only structured-learning schema and rows in the shared database."""
    schema = [
        [object_type, name, sql]
        for (object_type, name), sql in sorted(_schema_signature(c).items())
    ]
    version = c.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?",
        (DOMAIN,),
    ).fetchone()
    rows = {}
    for table in TABLES:
        values = [list(row) for row in c.execute(f"SELECT * FROM {table}")]
        rows[table] = sorted(values, key=_json)
    return _sha({
        "digest_kind": MIGRATION_DOMAIN_DIGEST_KIND,
        "schema": schema,
        "version": list(version or ()),
        "rows": rows,
    })


def _migration_marker_path(path: Path, source_digest: str) -> Path:
    return Path(f"{path}.learning-v2.{source_digest[:16]}.json")


def _write_migration_marker(path: Path, payload: dict[str, Any]) -> None:
    """Publish one durable marker without replacing prior migration evidence."""
    encoded = (_json(payload) + "\n").encode()
    if path.exists():
        if path.read_bytes() != encoded:
            raise RuntimeError("learning migration marker content mismatch")
        return
    temporary = Path(f"{path}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as marker:
            marker.write(encoded)
            marker.flush()
            os.fsync(marker.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            if path.read_bytes() != encoded:
                raise RuntimeError("learning migration marker content mismatch") from exc
        finally:
            temporary.unlink(missing_ok=True)
        if not _IS_WINDOWS:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _domain_schema_status(c: sqlite3.Connection) -> str:
    """Classify the complete learning sqlite_master surface, including indexes."""
    signature = _schema_signature(c)
    domain_row = None
    if ("table", "anchor_schema_versions") in signature:
        domain_row = c.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)
        ).fetchone()
    learning_objects = {
        key for key in signature
        if key[1].startswith("learning_") or key[1].startswith("idx_learning_")
        or key[1].startswith("sqlite_autoindex_learning_")
    }
    if not learning_objects and domain_row is None:
        return "pristine"
    if (
        signature == EXPECTED_SCHEMA_SIGNATURE
        and domain_row
        and tuple(domain_row) == (VERSION, SCHEMA_SHA256)
    ):
        return "exact"
    if (
        signature == LEGACY_EXPECTED_SCHEMA_SIGNATURE
        and domain_row
        and tuple(domain_row) == (1, LEGACY_SCHEMA_SHA256)
    ):
        return "legacy_v1"
    return "drift"


def _verify(c: sqlite3.Connection) -> None:
    if _schema_signature(c) != EXPECTED_SCHEMA_SIGNATURE:
        raise RuntimeError("learning schema drift")
    row = c.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)
    ).fetchone()
    if not row or row[0] != VERSION or row[1] != SCHEMA_SHA256:
        raise RuntimeError("learning schema version/checksum mismatch")


def _migration_record(c: sqlite3.Connection) -> sqlite3.Row | None:
    return c.execute(
        "SELECT migration_id,from_version,to_version,source_digest,backup_path,"
        "prior_table_sql,prior_index_sql,row_count,quarantined_count,pre_row_digest,"
        "post_row_digest,incompatible_writes,applied_at "
        "FROM learning_obligation_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()


def _ensure_migration_marker(path: Path, c: sqlite3.Connection) -> Path | None:
    record = _migration_record(c)
    if record is None:
        return None
    from odibi_anchor.codebase._migration_backup import logical_digest

    backup = Path(record["backup_path"])
    if not backup.is_file() or logical_digest(backup) != record["source_digest"]:
        raise RuntimeError("learning migration backup verification failed")
    marker = _migration_marker_path(path, record["source_digest"])
    if marker.exists():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        expected = {
            "migration_id": MIGRATION_ID,
            "source_digest": record["source_digest"],
            "backup_path": record["backup_path"],
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise RuntimeError("learning migration marker metadata mismatch")
        if payload.get("post_migration_digest_kind") != MIGRATION_DOMAIN_DIGEST_KIND:
            raise RuntimeError("learning migration marker digest kind mismatch")
        post_digest = payload.get("post_migration_digest")
        if not isinstance(post_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", post_digest):
            raise RuntimeError("learning migration marker digest invalid")
        rollback_digest = payload.get("rollback_database_digest")
        if not isinstance(rollback_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", rollback_digest
        ):
            raise RuntimeError("learning migration marker rollback digest invalid")
        if not record["incompatible_writes"] and post_digest != _learning_domain_digest(c):
            raise RuntimeError("learning migration marker digest mismatch")
        return marker
    if record["incompatible_writes"]:
        raise RuntimeError("learning migration marker missing after incompatible writes")

    _write_migration_marker(
        marker,
        {
            "migration_id": MIGRATION_ID,
            "source_digest": record["source_digest"],
            "backup_path": record["backup_path"],
            "post_migration_digest_kind": MIGRATION_DOMAIN_DIGEST_KIND,
            "post_migration_digest": _learning_domain_digest(c),
            "rollback_database_digest": logical_digest(path),
        },
    )
    return marker


def _migrate_v1_obligations(
    path: Path, c: sqlite3.Connection, backup: dict[str, Any],
) -> None:
    """Rebuild v1 obligations into the explicit owner schema in one transaction."""
    prior_table_sql = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='learning_obligations'"
    ).fetchone()[0]
    prior_index_sql = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_learning_one_active_obligation'"
    ).fetchone()[0]
    row_count, pre_digest = _obligation_projection_digest(c)
    legacy_rows = c.execute(
        "SELECT obligation_id,task_window_id,session_ref,checkpoint_ref,project_ref,"
        "status,created_at,activated_at,closed_at "
        "FROM learning_obligations ORDER BY obligation_id"
    ).fetchall()

    c.execute("DROP INDEX idx_learning_one_active_obligation")
    c.execute("ALTER TABLE learning_obligations RENAME TO learning_obligations_v1")
    c.execute(_OWNED_OBLIGATION_DDL)
    quarantined_count = 0
    for row in legacy_rows:
        project_id = None
        if row["project_ref"] is not None:
            try:
                project_id = _ident(row["project_ref"], "legacy project_ref")
            except ValueError:
                project_id = None
        owner_state = "owned" if project_id is not None else "quarantined"
        quarantined_count += owner_state == "quarantined"
        c.execute(
            "INSERT INTO learning_obligations "
            "(obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
            "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at) "
            "VALUES(?,?,?,?,?,?,?, ?,?,?,?,?)",
            (
                row["obligation_id"],
                project_id,
                row["task_window_id"],
                row["session_ref"],
                row["checkpoint_ref"],
                row["project_ref"],
                OWNER_SCHEMA_VERSION,
                owner_state,
                row["status"],
                row["created_at"],
                row["activated_at"],
                row["closed_at"],
            ),
        )
    c.execute("DROP TABLE learning_obligations_v1")
    c.execute(_OWNED_ACTIVE_INDEX_DDL)
    c.execute(_MIGRATION_TABLE_DDL)
    for statement in _MIGRATION_WRITE_TRIGGERS:
        c.execute(statement)
    c.execute(
        "UPDATE anchor_schema_versions SET version=?,schema_sha256=?,applied_at=? WHERE domain=?",
        (VERSION, SCHEMA_SHA256, _now(), DOMAIN),
    )
    migrated_count, post_digest = _obligation_projection_digest(c)
    if (migrated_count, post_digest) != (row_count, pre_digest):
        raise RuntimeError("learning obligation migration content verification failed")
    c.execute(
        "INSERT INTO learning_obligation_migrations "
        "(migration_id,from_version,to_version,source_digest,backup_path,prior_table_sql,"
        "prior_index_sql,row_count,quarantined_count,pre_row_digest,post_row_digest,"
        "incompatible_writes,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            MIGRATION_ID,
            1,
            VERSION,
            backup["source_digest"],
            backup["backup_path"],
            prior_table_sql,
            prior_index_sql,
            row_count,
            quarantined_count,
            pre_digest,
            post_digest,
            0,
            _now(),
        ),
    )
    if c.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("learning obligation migration foreign-key verification failed")


def initialize_learning_schema() -> dict[str, Any]:
    """Create or migrate the learning schema deterministically and idempotently."""
    from odibi_anchor.codebase._migration_backup import (
        ensure_migration_backup,
        logical_digest,
    )

    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists() and path.stat().st_size > 0
    c = _connect(path)
    try:
        status = _domain_schema_status(c)
        if status == "exact":
            marker = _ensure_migration_marker(path, c)
            return {
                "schema_status": "exact",
                "version": VERSION,
                "migration": dict(_migration_record(c) or {}),
                "marker_path": str(marker) if marker else None,
            }
        if status == "drift":
            raise RuntimeError("learning schema drift")

        backup = None
        if existed:
            tag = "learning-v1-to-v2" if status == "legacy_v1" else "learning-v2"
            backup = ensure_migration_backup(
                path, tag=tag, error_prefix="learning migration"
            )
        c.execute("PRAGMA foreign_keys=OFF")
        if c.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("learning migration could not disable foreign keys")
        c.execute("PRAGMA legacy_alter_table=ON")
        c.execute("BEGIN IMMEDIATE")
        try:
            if backup is not None and logical_digest(path) != backup["source_digest"]:
                raise RuntimeError("learning migration source changed after backup")
            names = {
                row[0]
                for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if status == "pristine":
                for statement in DDL if "anchor_schema_versions" not in names else DDL[1:]:
                    c.execute(statement)
                c.execute(
                    "INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                    (DOMAIN, VERSION, SCHEMA_SHA256, _now()),
                )
            else:
                assert backup is not None
                _migrate_v1_obligations(path, c, backup)
            _verify(c)
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.execute("PRAGMA legacy_alter_table=OFF")
            c.execute("PRAGMA foreign_keys=ON")
        _verify(c)
        if c.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("learning schema foreign-key verification failed")
        marker = _ensure_migration_marker(path, c)
        return {
            "schema_status": "migrated" if status == "legacy_v1" else "initialized",
            "version": VERSION,
            "migration": dict(_migration_record(c) or {}),
            "marker_path": str(marker) if marker else None,
        }
    finally:
        c.close()


def learning_migration_status() -> dict[str, Any]:
    """Inspect migration, quarantine, and rollback eligibility without mutation."""
    path = _db_path()
    if not path.exists():
        return {"schema_status": "uninitialized", "rollback_eligible": False}
    c = _connect(path, ro=True)
    try:
        status = _domain_schema_status(c)
        if status != "exact":
            return {"schema_status": status, "rollback_eligible": False}
        record = _migration_record(c)
        quarantined = [
            dict(row)
            for row in c.execute(
                "SELECT obligation_id,task_window_id,session_ref,checkpoint_ref,project_ref,"
                "status,created_at,activated_at,closed_at,owner_state "
                "FROM learning_obligations WHERE owner_state='quarantined' ORDER BY obligation_id"
            )
        ]
        if record is None:
            return {
                "schema_status": "exact",
                "migration": None,
                "quarantined_obligations": quarantined,
                "rollback_eligible": False,
            }
        marker = _migration_marker_path(path, record["source_digest"])
        reason = None
        if record["incompatible_writes"]:
            reason = "incompatible learning writes followed migration"
        elif not marker.exists():
            reason = "migration marker missing"
        else:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            from odibi_anchor.codebase._migration_backup import logical_digest

            if payload.get("rollback_database_digest") != logical_digest(path):
                reason = "post-migration database digest changed"
        return {
            "schema_status": "exact",
            "migration": dict(record),
            "quarantined_obligations": quarantined,
            "rollback_eligible": reason is None,
            "rollback_blocker": reason,
        }
    finally:
        c.close()


def rollback_learning_obligation_migration() -> dict[str, Any]:
    """Atomically restore the verified v1 backup only while rollback is eligible."""
    from odibi_anchor.codebase._migration_backup import (
        _integrity_check,
        logical_digest,
    )

    path = _db_path()
    status = learning_migration_status()
    if not status.get("rollback_eligible"):
        raise RuntimeError(
            "learning migration rollback is unavailable: "
            + str(status.get("rollback_blocker") or "no eligible migration")
        )
    record = status["migration"]
    backup = Path(record["backup_path"])
    if (
        not backup.is_file()
        or _integrity_check(backup) != "ok"
        or logical_digest(backup) != record["source_digest"]
    ):
        raise RuntimeError("learning migration rollback backup verification failed")
    marker = _migration_marker_path(path, record["source_digest"])
    temporary = Path(f"{path}.rollback.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    receipt = Path(f"{marker}.rolled-back.{uuid.uuid4().hex[:12]}")
    try:
        shutil.copyfile(backup, temporary)
        with open(temporary, "r+b") as restored:
            os.fsync(restored.fileno())
        if _integrity_check(temporary) != "ok" or logical_digest(temporary) != record["source_digest"]:
            raise RuntimeError("learning migration rollback staging verification failed")
        # Replacing a SQLite file while the legacy memory facade still holds a
        # connection would leave that facade writing to the detached old inode.
        from odibi_anchor.codebase._memory_db import close_db

        close_db(str(path))
        os.replace(temporary, path)
        os.replace(marker, receipt)
        if not _IS_WINDOWS:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "rolled_back",
        "restored_version": 1,
        "source_digest": record["source_digest"],
        "receipt_path": str(receipt),
    }


def activate_learning_obligation(
    *,
    task_window_id: str,
    session_ref: str,
    checkpoint_ref: str,
    project_id: str | None = None,
    project_ref: str | None = None,
) -> dict:
    """Activate one obligation for an explicit project/task owner."""
    if project_id is not None and project_ref is not None and project_id != project_ref:
        raise ValueError("project_id and project_ref identify different owners")
    owner_project, owner_task = _owner(project_id or project_ref, task_window_id)
    session = _ident(session_ref, "session_ref")
    checkpoint = _ident(checkpoint_ref, "checkpoint_ref")
    legacy_project = _ident(project_ref, "project_ref") if project_ref else owner_project
    initialize_learning_schema()
    c = _connect(_db_path())
    try:
        c.execute("BEGIN IMMEDIATE")
        _verify(c)
        oid = _uid("lob_")
        now = _now()
        try:
            c.execute(
                "INSERT INTO learning_obligations "
                "(obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
                "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at) "
                "VALUES(?,?,?,?,?,?,?,'owned','active',?,?,NULL)",
                (
                    oid,
                    owner_project,
                    owner_task,
                    session,
                    checkpoint,
                    legacy_project,
                    OWNER_SCHEMA_VERSION,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if c.execute(
                "SELECT 1 FROM learning_obligations WHERE project_id=? AND task_window_id=? "
                "AND owner_state='owned' AND status='active'",
                (owner_project, owner_task),
            ).fetchone():
                raise RuntimeError(
                    "active learning obligation already exists for owner"
                ) from exc
            raise
        c.commit()
        return {
            "obligation_id": oid,
            "project_id": owner_project,
            "task_window_id": owner_task,
            "status": "active",
        }
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def ensure_learning_obligation(
    *,
    task_window_id: str,
    session_ref: str,
    checkpoint_ref: str,
    project_id: str | None = None,
    project_ref: str | None = None,
) -> dict[str, Any]:
    """Return this task's active obligation or create its next learning cycle."""
    if project_id is not None and project_ref is not None and project_id != project_ref:
        raise ValueError("project_id and project_ref identify different owners")
    owner_project, owner_task = _owner(project_id or project_ref, task_window_id)
    active = active_learning_obligation(
        project_id=owner_project, task_window_id=owner_task,
    )
    if active is not None:
        return {
            "obligation_id": active["obligation_id"],
            "project_id": owner_project,
            "task_window_id": owner_task,
            "status": "active",
            "created": False,
        }
    created = activate_learning_obligation(
        task_window_id=owner_task,
        session_ref=session_ref,
        checkpoint_ref=checkpoint_ref,
        project_id=owner_project,
        project_ref=project_ref or owner_project,
    )
    return {**created, "created": True}


def active_learning_obligation(
    *,
    project_id: str,
    task_window_id: str,
    _forbidden_roots: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Return this exact owner's active obligation without migration."""
    owner_project, owner_task = _owner(project_id, task_window_id)
    p = _db_path(forbidden_roots=_forbidden_roots)
    if not p.exists():
        return None
    c = _connect(p, ro=True)
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        learning_objects = {name for name in names if name.startswith("learning_")}
        if not learning_objects:
            return None
        _verify(c)
        row = c.execute(
            "SELECT obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
            "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at "
            "FROM learning_obligations WHERE project_id=? AND task_window_id=? "
            "AND owner_state='owned' AND status='active'",
            (owner_project, owner_task),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def learning_obligation(
    obligation_id: str, *, project_id: str, task_window_id: str,
) -> dict[str, Any] | None:
    """Return one exact obligation without substituting another lifecycle row."""
    oid = _ident(obligation_id, "obligation_id")
    owner_project, owner_task = _owner(project_id, task_window_id)
    p = _db_path()
    if not p.exists():
        return None
    c = _connect(p, ro=True)
    try:
        names = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "learning_obligations" not in names:
            return None
        _verify(c)
        row = c.execute(
            "SELECT obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
            "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at "
            "FROM learning_obligations WHERE obligation_id=? AND project_id=? "
            "AND task_window_id=? AND owner_state='owned'",
            (oid, owner_project, owner_task),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def latest_closed_learning_obligation(
    *, project_id: str, task_window_id: str,
) -> dict[str, Any] | None:
    """Return the most recently closed obligation for one exact owner."""
    owner_project, owner_task = _owner(project_id, task_window_id)
    p = _db_path()
    if not p.exists():
        return None
    c = _connect(p, ro=True)
    try:
        names = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        learning_objects = {name for name in names if name.startswith("learning_")}
        if not learning_objects:
            return None
        _verify(c)
        row = c.execute(
            "SELECT obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
            "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at "
            "FROM learning_obligations WHERE project_id=? AND task_window_id=? "
            "AND owner_state='owned' AND status IN ('assessed','legacy_closed') "
            "ORDER BY closed_at DESC,obligation_id DESC LIMIT 1",
            (owner_project, owner_task),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def close_learning_obligation_legacy(
    obligation_id: str, *, project_id: str, task_window_id: str,
) -> dict[str, Any]:
    """Atomically close an active obligation after successful legacy learning."""
    oid = _ident(obligation_id, "obligation_id")
    owner_project, owner_task = _owner(project_id, task_window_id)
    c = _connect(_db_path())
    try:
        c.execute("BEGIN IMMEDIATE")
        _verify(c)
        row = c.execute(
            "SELECT obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
            "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at "
            "FROM learning_obligations WHERE obligation_id=? AND project_id=? "
            "AND task_window_id=? AND owner_state='owned'",
            (oid, owner_project, owner_task),
        ).fetchone()
        if not row:
            raise ValueError("learning obligation unavailable for owner")
        if row["status"] == "legacy_closed":
            c.commit()
            return dict(row)
        if row["status"] != "active":
            raise ValueError("learning obligation is not active")
        now = _now()
        c.execute(
            "UPDATE learning_obligations SET status='legacy_closed',closed_at=? "
            "WHERE obligation_id=? AND project_id=? AND task_window_id=? "
            "AND owner_state='owned' AND status='active'",
            (now, oid, owner_project, owner_task),
        )
        result = dict(
            c.execute(
                "SELECT obligation_id,project_id,task_window_id,session_ref,checkpoint_ref,"
                "project_ref,owner_schema_version,owner_state,status,created_at,activated_at,closed_at "
                "FROM learning_obligations WHERE obligation_id=? AND project_id=? "
                "AND task_window_id=? AND owner_state='owned'",
                (oid, owner_project, owner_task),
            ).fetchone()
        )
        c.commit()
        return result
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _read(command: str, payload: dict[str, Any]) -> dict:
    p = _db_path()
    if not p.exists():
        return {"schema_status": "uninitialized", "items": [] if command == "list" else None}
    c = _connect(p, ro=True)
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        learning_objects = {name for name in names if name.startswith("learning_")}
        if not learning_objects and "anchor_schema_versions" not in names:
            return {"schema_status": "uninitialized", "items": [] if command == "list" else None}
        _verify(c)

        def hydrate(row: sqlite3.Row, include_history: bool = False) -> dict[str, Any]:
            item = dict(row)
            for field in ("project_refs", "work_package_refs", "environment_refs", "provenance"):
                item[field] = json.loads(item[field])
            item["evidence_refs"] = [
                dict(entry)
                for entry in c.execute(
                    "SELECT * FROM learning_evidence_refs WHERE item_id=? ORDER BY evidence_id",
                    (row["item_id"],),
                )
            ]
            item["links"] = [
                dict(link)
                for link in c.execute(
                    "SELECT * FROM learning_item_links WHERE source_item_id=? OR derived_item_id=? "
                    "ORDER BY source_item_id,derived_item_id,relation",
                    (row["item_id"], row["item_id"]),
                )
            ]
            events = [
                dict(event)
                for event in c.execute(
                    "SELECT * FROM learning_events WHERE item_id=? ORDER BY created_at,event_id",
                    (row["item_id"],),
                )
            ]
            item["latest_event"] = events[-1] if events else None
            if include_history:
                item["events"] = events
            return item

        if command == "show":
            item_id = _ident(payload.get("item_id"), "item_id")
            unknown = set(payload) - {"item_id", "include_history"}
            if unknown or not isinstance(payload.get("include_history", False), bool):
                raise ValueError("invalid show payload")
            row = c.execute("SELECT * FROM learning_items WHERE item_id=?", (item_id,)).fetchone()
            if not row:
                raise ValueError("learning item not found")
            return {"schema_status": "ready", "item": hydrate(row, payload.get("include_history", False))}
        unknown = set(payload) - {"kind", "status", "applicability_project"}
        if unknown:
            raise ValueError(f"invalid {command} payload")
        where = ["status NOT IN ('rejected','retired')"]
        params: list[str] = []
        kind = payload.get("kind")
        if kind is not None:
            if kind not in {"observation", "lesson", "watch_item", "improvement_candidate"}:
                raise ValueError("invalid kind")
            where.append("kind=?")
            params.append(kind)
        status = payload.get("status")
        if status is not None:
            if status not in {"open", "active", "deferred", "rejected", "retired"}:
                raise ValueError("invalid status")
            where = ["status=?"]
            params = [status]
            if kind is not None:
                where.append("kind=?")
                params.append(kind)
        project = payload.get("applicability_project")
        if project is not None:
            project = _ident(project, "applicability_project")
        rows = [
            hydrate(r)
            for r in c.execute(
                "SELECT * FROM learning_items WHERE " + " AND ".join(where) + " ORDER BY created_at,item_id", params
            )
        ]
        if project is not None:
            rows = [row for row in rows if row["applicability_scope"] == "workbench" or project in row["project_refs"]]
        counts = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT recurrence_key,count(*) FROM learning_items WHERE kind='observation' AND status NOT IN ('rejected','retired') GROUP BY recurrence_key"
            )
        }
        for r in rows:
            r["recurrence_count"] = counts.get(r["recurrence_key"], 1 if r["kind"] == "observation" else 0)
            r["attention_due"] = r["recurrence_count"] >= 3 or r["impact"] in ("high", "critical")
        if command == "list":
            return {"schema_status": "ready", "items": rows}
        groups = []
        for key, count in sorted(counts.items()):
            rs = [r for r in rows if r["recurrence_key"] == key]
            if rs:
                source_ids = [row["item_id"] for row in rs]
                placeholders = ",".join("?" for _ in source_ids)
                linked = [
                    dict(link)
                    for link in c.execute(
                        "SELECT i.item_id,i.kind,i.status FROM learning_item_links l "
                        "JOIN learning_items i ON i.item_id=l.derived_item_id "
                        f"WHERE l.source_item_id IN ({placeholders}) ORDER BY i.item_id",
                        source_ids,
                    )
                ]
                groups.append(
                    {
                        "recurrence_key": key,
                        "recurrence_count": count,
                        "first_observed_at": rs[0]["created_at"],
                        "last_observed_at": rs[-1]["created_at"],
                        "highest_impact": max(
                            (r["impact"] for r in rs), key=("low", "medium", "high", "critical").index
                        ),
                        "applicability_scope": rs[0]["applicability_scope"],
                        "project_refs": rs[0]["project_refs"],
                        "linked_derived_items": linked,
                        "attention_due": count >= 3 or any(r["impact"] in ("high", "critical") for r in rs),
                    }
                )
        return {"schema_status": "ready", "insights": groups}
    finally:
        c.close()


def _evidence(value: Any) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise ValueError("invalid evidence")
    out = []
    for e in value:
        if not isinstance(e, dict) or set(e) - {"reference_type", "reference", "summary", "observed_at"}:
            raise ValueError("invalid evidence")
        typ = e.get("reference_type")
        ref = _text(e.get("reference"), "evidence.reference", 512)
        if typ not in REFS or not REFS[typ].fullmatch(ref):
            raise ValueError("invalid evidence.reference")
        summary = _text(e.get("summary", ""), "evidence.summary", 500, empty=True, free=True)
        observed = e.get("observed_at")
        if observed is not None:
            observed = _text(observed, "evidence.observed_at", 32)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", observed):
                raise ValueError("invalid evidence.observed_at")
            try:
                datetime.fromisoformat(observed[:-1] + "+00:00")
            except ValueError as exc:
                raise ValueError("invalid evidence.observed_at") from exc
        out.append(
            {
                "reference_type": typ,
                "reference": ref,
                "summary": summary,
                "observed_at": observed,
                "reference_sha256": _sha({"reference_type": typ, "reference": ref}),
            }
        )
    result = sorted(out, key=lambda x: x["reference_sha256"])
    if len({entry["reference_sha256"] for entry in result}) != len(result):
        raise ValueError("invalid evidence")
    return result


def _obligation_for_operation(
    payload: dict[str, Any], operation: str,
) -> tuple[str, bool, str, str]:
    retry_latest = payload.pop("retry_latest", False)
    if not isinstance(retry_latest, bool):
        raise ValueError("invalid retry_latest")
    active = payload.pop("_obligation_id", None)
    latest = payload.pop("_latest_closed_obligation_id", None)
    owner_project, owner_task = _owner(
        payload.pop("_project_id", None), payload.pop("_task_window_id", None),
    )
    selected = latest if retry_latest else active
    if selected is None:
        raise ValueError(f"{operation} requires an active learning obligation")
    return _ident(selected, "obligation_id"), retry_latest, owner_project, owner_task


def _capture(payload: dict[str, Any]) -> dict:
    oid, retry_latest, owner_project, owner_task = _obligation_for_operation(
        payload, "capture"
    )
    allowed_keys = {
        "observation_type",
        "summary",
        "signal_key",
        "impact",
        "applicability_scope",
        "project_refs",
        "work_package_refs",
        "environment_refs",
        "provenance",
        "evidence",
    }
    if set(payload) - allowed_keys:
        raise ValueError("invalid capture payload")
    otype = payload.get("observation_type")
    if otype not in ("friction", "blocker", "near_miss", "reusable_practice", "evidence_gap"):
        raise ValueError("invalid observation_type")
    summary = _text(payload.get("summary"), "summary", 1000, free=True)
    signal = _text(payload.get("signal_key"), "signal_key", 128)
    if not SIGNAL.fullmatch(signal) or SENSITIVE.search(signal):
        raise ValueError("invalid signal_key")
    impact = payload.get("impact", "medium")
    scope = payload.get("applicability_scope", "workbench")
    if impact not in ("low", "medium", "high", "critical") or scope not in (
        "project_local",
        "workbench",
        "cross_project",
    ):
        raise ValueError("invalid capture field")
    projects = _list(payload.get("project_refs"), "project_refs")
    wp = _list(payload.get("work_package_refs"), "work_package_refs")
    env = _list(payload.get("environment_refs"), "environment_refs")
    if (scope == "project_local" and len(projects) != 1) or (scope == "cross_project" and len(projects) < 2):
        raise ValueError("invalid project_refs")
    ev = _evidence(payload.get("evidence"))
    prov = payload.get("provenance", {})
    if not isinstance(prov, dict) or set(prov) - {"source_action", "source_version"}:
        raise ValueError("invalid provenance")
    prov = {k: _ident(v, f"provenance.{k}") for k, v in prov.items()}
    provenance_connection = _connect(_db_path(), ro=True)
    try:
        _verify(provenance_connection)
        obligation_row = provenance_connection.execute(
            "SELECT task_window_id,session_ref FROM learning_obligations "
            "WHERE obligation_id=? AND project_id=? AND task_window_id=? "
            "AND owner_state='owned'",
            (oid, owner_project, owner_task),
        ).fetchone()
        if obligation_row is None:
            raise ValueError("learning obligation unavailable for owner")
        if retry_latest and provenance_connection.execute(
            "SELECT 1 FROM learning_obligations WHERE project_id=? AND task_window_id=? "
            "AND owner_state='owned' AND status='active'",
            (owner_project, owner_task),
        ).fetchone():
            raise ValueError("retry_latest is unavailable while a learning obligation is active")
    finally:
        provenance_connection.close()
    prov.update(
        learning_obligation_id=oid,
        task_window_id=obligation_row["task_window_id"],
        session_id=obligation_row["session_ref"],
    )
    complete = {
        "observation_type": otype,
        "summary": summary,
        "signal_key": signal,
        "impact": impact,
        "applicability_scope": scope,
        "project_refs": projects,
        "work_package_refs": wp,
        "environment_refs": env,
        "provenance": prov,
        "evidence": ev,
    }
    recurrence = "lrk:v1:" + _sha(
        {
            "observation_type": otype,
            "signal_key": signal,
            "applicability_scope": scope,
            "project_refs": projects if scope == "project_local" else [],
        }
    )
    occurrence = "loc:v1:" + _sha(
        {"recurrence_key": recurrence, "learning_obligation_id": oid, "evidence": [x["reference_sha256"] for x in ev]}
    )
    ph = _sha(complete)
    c = _connect(_db_path())
    try:
        c.execute("BEGIN IMMEDIATE")
        _verify(c)
        if retry_latest and c.execute(
            "SELECT 1 FROM learning_obligations WHERE project_id=? AND task_window_id=? "
            "AND owner_state='owned' AND status='active'",
            (owner_project, owner_task),
        ).fetchone():
            raise ValueError("retry_latest is unavailable while a learning obligation is active")
        old = c.execute("SELECT * FROM learning_items WHERE occurrence_key=?", (occurrence,)).fetchone()
        if old:
            if old["payload_sha256"] != ph:
                raise ValueError("learning idempotency conflict")
            count = c.execute(
                "SELECT count(*) FROM learning_items WHERE recurrence_key=? AND status NOT IN ('rejected','retired')",
                (recurrence,),
            ).fetchone()[0]
            c.commit()
            return {
                "message": "Observation recorded",
                "item": dict(old),
                "deduped": True,
                "recurrence_count": count,
                "attention_due": count >= 3 or old["impact"] in ("high", "critical"),
            }
        obligation = c.execute(
            "SELECT status FROM learning_obligations WHERE obligation_id=? "
            "AND project_id=? AND task_window_id=? AND owner_state='owned'",
            (oid, owner_project, owner_task),
        ).fetchone()
        if not obligation or obligation[0] != "active":
            raise ValueError("capture requires an active learning obligation")
        item = _uid("lrn_")
        now = _now()
        c.execute(
            "INSERT INTO learning_items (item_id,kind,observation_type,status,summary,signal_key,recurrence_key,occurrence_key,decision_key,payload_sha256,impact,applicability_scope,project_refs,work_package_refs,environment_refs,provenance,version,created_at,updated_at) VALUES(?,'observation',?,'open',?,?,?,?,NULL,?,?,?,?,?,?,?,1,?,?)",
            (
                item,
                otype,
                summary,
                signal,
                recurrence,
                occurrence,
                ph,
                impact,
                scope,
                _json(projects),
                _json(wp),
                _json(env),
                _json(prov),
                now,
                now,
            ),
        )
        for e in ev:
            c.execute(
                "INSERT INTO learning_evidence_refs VALUES(?,?,?,?,?,?,?,?)",
                (
                    _uid("lev_"),
                    item,
                    e["reference_type"],
                    e["reference"],
                    e["reference_sha256"],
                    e["summary"],
                    e["observed_at"],
                    now,
                ),
            )
        c.execute(
            "INSERT INTO learning_events VALUES(?,?, 'captured',NULL,'open','agent',?,'','',1,?)",
            (_uid("lte_"), item, "dispatcher", now),
        )
        count = c.execute(
            "SELECT count(*) FROM learning_items WHERE recurrence_key=? AND status NOT IN ('rejected','retired')",
            (recurrence,),
        ).fetchone()[0]
        result_item = dict(c.execute("SELECT * FROM learning_items WHERE item_id=?", (item,)).fetchone())
        c.commit()
        return {
            "message": "Observation recorded",
            "item": result_item,
            "deduped": False,
            "recurrence_count": count,
            "attention_due": count >= 3 or impact in ("high", "critical"),
        }
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _assess(payload: dict[str, Any]) -> dict:
    oid, retry_latest, owner_project, owner_task = _obligation_for_operation(
        payload, "assessment"
    )
    if set(payload) - {"outcome", "observation_ids", "notes", "actor_kind", "actor_ref"}:
        raise ValueError("invalid assessment payload")
    outcome = payload.get("outcome")
    supplied_ids = payload.get("observation_ids", [])
    if not isinstance(supplied_ids, list) or len(supplied_ids) > 32:
        raise ValueError("invalid observation_ids")
    ids = sorted({_ident(item, "observation_ids") for item in supplied_ids})
    notes = _text(payload.get("notes", ""), "notes", 500, empty=True, free=True)
    actor = payload.get("actor_kind", "agent")
    actor_ref = _ident(payload.get("actor_ref", "dispatcher"), "actor_ref", 128)
    if outcome not in ("observations_recorded", "nothing_reusable_learned"):
        raise ValueError(
            "invalid assessment outcome: use 'observations_recorded' with Observation IDs "
            "returned by anchor('learning', 'capture', ...), or 'nothing_reusable_learned' "
            "without observation_ids"
        )
    if outcome == "observations_recorded" and not ids:
        raise ValueError(
            "observations_recorded requires observation_ids: first call "
            "anchor('learning', 'capture', ...), then assess the returned item IDs"
        )
    if outcome == "nothing_reusable_learned" and ids:
        raise ValueError("nothing_reusable_learned does not accept observation_ids")
    if actor not in ("agent", "human"):
        raise ValueError("actor_kind must be 'agent' or 'human'")
    c = _connect(_db_path())
    try:
        c.execute("BEGIN IMMEDIATE")
        _verify(c)
        if retry_latest and c.execute(
            "SELECT 1 FROM learning_obligations WHERE project_id=? AND task_window_id=? "
            "AND owner_state='owned' AND status='active'",
            (owner_project, owner_task),
        ).fetchone():
            raise ValueError("retry_latest is unavailable while a learning obligation is active")
        obligation = c.execute(
            "SELECT status FROM learning_obligations WHERE obligation_id=? "
            "AND project_id=? AND task_window_id=? AND owner_state='owned'",
            (oid, owner_project, owner_task),
        ).fetchone()
        if not obligation:
            raise ValueError("learning obligation unavailable for owner")
        old = c.execute("SELECT * FROM learning_assessments WHERE obligation_id=?", (oid,)).fetchone()
        if old:
            oldids = [
                r[0]
                for r in c.execute(
                    "SELECT observation_id FROM learning_assessment_observations WHERE assessment_id=? ORDER BY observation_id",
                    (old["assessment_id"],),
                )
            ]
            if (old["outcome"], old["notes"], old["actor_kind"], old["actor_ref"], oldids) != (
                outcome,
                notes,
                actor,
                actor_ref,
                ids,
            ):
                raise ValueError("learning idempotency conflict")
            c.commit()
            assessment = dict(old)
            assessment["observation_ids"] = sorted(oldids)
            return {"assessment": assessment, "deduped": True}
        if obligation[0] != "active":
            raise ValueError("assessment requires an active learning obligation")
        for item in ids:
            row = c.execute("SELECT kind,provenance FROM learning_items WHERE item_id=?", (item,)).fetchone()
            if not row or row[0] != "observation" or json.loads(row[1]).get("learning_obligation_id") != oid:
                raise ValueError("invalid assessment observation")
        aid = _uid("las_")
        now = _now()
        c.execute(
            "INSERT INTO learning_assessments VALUES(?,?,?,?,?,?,?)", (aid, oid, outcome, notes, actor, actor_ref, now)
        )
        for item in ids:
            c.execute("INSERT INTO learning_assessment_observations VALUES(?,?)", (aid, item))
        c.execute(
            "UPDATE learning_obligations SET status='assessed',closed_at=? "
            "WHERE obligation_id=? AND project_id=? AND task_window_id=? "
            "AND owner_state='owned' AND status='active'",
            (now, oid, owner_project, owner_task),
        )
        c.commit()
        return {
            "assessment": {
                "assessment_id": aid,
                "obligation_id": oid,
                "outcome": outcome,
                "notes": notes,
                "observation_ids": sorted(ids),
                "actor_kind": actor,
                "actor_ref": actor_ref,
                "created_at": now,
            },
            "deduped": False,
        }
    except:
        c.rollback()
        raise
    finally:
        c.close()


def _triage(payload: dict[str, Any]) -> dict[str, Any]:
    decision = payload.get("decision")
    actor = payload.get("actor_kind")
    actor_ref = _ident(payload.get("actor_ref"), "actor_ref", 128)
    decision_source = _ident(payload.get("decision_source"), "decision_source")
    rationale = _text(payload.get("rationale"), "rationale", 1000, free=True)
    if actor != "human":
        raise ValueError("triage requires human attestation")
    common = {"decision", "actor_kind", "actor_ref", "decision_source", "rationale"}
    derive_keys = common | {
        "source_item_ids",
        "expected_source_versions",
        "summary",
        "impact",
        "applicability_scope",
        "project_refs",
        "work_package_refs",
        "environment_refs",
        "evidence",
        "provenance",
    }
    status_keys = common | {"item_id", "expected_version"}
    expected_keys = derive_keys if decision in {"derive_lesson", "derive_watch", "derive_improvement"} else status_keys
    if set(payload) - expected_keys:
        raise ValueError("invalid triage payload")
    path = _db_path()
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("learning schema is not initialized")
    # Triage is never an initialization or migration entry point.  Classify via
    # a read-only handle first so absent, pristine, and legacy databases retain
    # their exact bytes when the operation fails closed.
    preflight = _connect(path, ro=True)
    try:
        if _domain_schema_status(preflight) != "exact":
            raise RuntimeError("learning schema is not initialized")
        _verify(preflight)
    finally:
        preflight.close()
    c = _connect(path)
    try:
        c.execute("BEGIN IMMEDIATE")
        _verify(c)
        if decision in {"derive_lesson", "derive_watch", "derive_improvement"}:
            kinds = {
                "derive_lesson": "lesson",
                "derive_watch": "watch_item",
                "derive_improvement": "improvement_candidate",
            }
            kind = kinds[decision]
            supplied_sources = payload.get("source_item_ids")
            if not isinstance(supplied_sources, list) or len(supplied_sources) > 32:
                raise ValueError("invalid source_item_ids")
            source_ids = sorted({_ident(item, "source_item_ids") for item in supplied_sources})
            versions = payload.get("expected_source_versions")
            if not source_ids or not isinstance(versions, dict) or set(versions) != set(source_ids):
                raise ValueError("invalid expected_source_versions")
            if any(type(version) is not int or version < 1 for version in versions.values()):
                raise ValueError("invalid expected_source_versions")
            summary = _text(payload.get("summary"), "summary", 1000, free=True)
            impact = payload.get("impact", "medium")
            scope = payload.get("applicability_scope", "workbench")
            projects = _list(payload.get("project_refs"), "project_refs")
            packages = _list(payload.get("work_package_refs"), "work_package_refs")
            environments = _list(payload.get("environment_refs"), "environment_refs")
            if impact not in {"low", "medium", "high", "critical"} or scope not in {
                "project_local",
                "workbench",
                "cross_project",
            }:
                raise ValueError("invalid derived item")
            if (scope == "project_local" and len(projects) != 1) or (scope == "cross_project" and len(projects) < 2):
                raise ValueError("invalid project_refs")
            evidence = _evidence(payload.get("evidence"))
            provenance = payload.get("provenance", {})
            if not isinstance(provenance, dict) or set(provenance) - {"source_action", "source_version"}:
                raise ValueError("invalid provenance")
            provenance = {k: _ident(v, f"provenance.{k}") for k, v in provenance.items()}
            key = "ldc:v1:" + _sha(
                {"derived_kind": kind, "sorted_source_ids": source_ids, "decision_source": decision_source}
            )
            complete = {
                "derived_kind": kind,
                "summary": summary,
                "impact": impact,
                "applicability_scope": scope,
                "project_refs": projects,
                "work_package_refs": packages,
                "environment_refs": environments,
                "provenance": provenance,
                "evidence": evidence,
                "source_item_ids": source_ids,
                "expected_source_versions": {i: versions[i] for i in source_ids},
                "actor_kind": actor,
                "actor_ref": actor_ref,
                "decision_source": decision_source,
                "rationale": rationale,
            }
            payload_hash = _sha(complete)
            old = c.execute("SELECT * FROM learning_items WHERE decision_key=?", (key,)).fetchone()
            if old:
                if old["payload_sha256"] != payload_hash:
                    raise ValueError("learning idempotency conflict")
                c.commit()
                return {"item": dict(old), "deduped": True}
            allowed = {
                "lesson": {"observation"},
                "watch_item": {"observation"},
                "improvement_candidate": {"observation", "lesson"},
            }[kind]
            for source_id in source_ids:
                row = c.execute(
                    "SELECT kind,status,version FROM learning_items WHERE item_id=?", (source_id,)
                ).fetchone()
                if (
                    not row
                    or row[0] not in allowed
                    or row[1] in {"rejected", "retired"}
                    or row[2] != versions[source_id]
                ):
                    raise ValueError("source item version/status conflict")
            item_id, now = _uid("lrn_"), _now()
            c.execute(
                "INSERT INTO learning_items(item_id,kind,status,summary,decision_key,payload_sha256,impact,applicability_scope,project_refs,work_package_refs,environment_refs,provenance,created_at,updated_at) VALUES(?,?,'active',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    kind,
                    summary,
                    key,
                    payload_hash,
                    impact,
                    scope,
                    _json(projects),
                    _json(packages),
                    _json(environments),
                    _json(provenance),
                    now,
                    now,
                ),
            )
            for source_id in source_ids:
                c.execute("INSERT INTO learning_item_links VALUES(?,?,'derived_from',?)", (source_id, item_id, now))
            for e in evidence:
                c.execute(
                    "INSERT INTO learning_evidence_refs VALUES(?,?,?,?,?,?,?,?)",
                    (
                        _uid("lev_"),
                        item_id,
                        e["reference_type"],
                        e["reference"],
                        e["reference_sha256"],
                        e["summary"],
                        e["observed_at"],
                        now,
                    ),
                )
            c.execute(
                "INSERT INTO learning_events VALUES(?,?,'derived',NULL,'active','human',?,?,?,1,?)",
                (_uid("lte_"), item_id, actor_ref, decision_source, rationale, now),
            )
            c.commit()
            return {
                "item": dict(c.execute("SELECT * FROM learning_items WHERE item_id=?", (item_id,)).fetchone()),
                "deduped": False,
            }
        if decision not in {"defer", "reopen", "reject", "retire"}:
            raise ValueError("invalid triage decision")
        item_id = _ident(payload.get("item_id"), "item_id")
        expected = payload.get("expected_version")
        if type(expected) is not int or expected < 1:
            raise ValueError("invalid expected_version")
        row = c.execute("SELECT kind,status,version FROM learning_items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("learning item not found")
        if row["version"] != expected:
            raise ValueError("learning version conflict")
        current = row[1]
        next_status = {"defer": "deferred", "reject": "rejected", "retire": "retired"}.get(decision) or (
            "open" if row[0] == "observation" else "active"
        )
        valid = (
            (decision == "defer" and current in {"open", "active"})
            or (decision == "reopen" and current == "deferred")
            or (decision in {"reject", "retire"} and current in {"open", "active", "deferred"})
        )
        if not valid:
            raise ValueError("invalid status transition")
        now = _now()
        updated = c.execute(
            "UPDATE learning_items SET status=?,version=version+1,updated_at=? WHERE item_id=? AND version=?",
            (next_status, now, item_id, expected),
        )
        if updated.rowcount != 1:
            raise ValueError("learning version conflict")
        event_type = {"defer": "deferred", "reopen": "reopened", "reject": "rejected", "retire": "retired"}[decision]
        c.execute(
            "INSERT INTO learning_events VALUES(?,?,?,?,?,'human',?,?,?,?,?)",
            (
                _uid("lte_"),
                item_id,
                event_type,
                current,
                next_status,
                actor_ref,
                decision_source,
                rationale,
                expected + 1,
                now,
            ),
        )
        if decision in {"reject", "retire"}:
            projection_table = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_projections'"
            ).fetchone()
            projection = (
                c.execute(
                    "SELECT p.memory_id,m.project FROM memory_projections p "
                    "JOIN memories m ON m.id=p.memory_id WHERE p.learning_item_id=?",
                    (item_id,),
                ).fetchone()
                if projection_table else None
            )
            if projection is not None:
                from odibi_anchor.codebase._memory_promotion import (
                    withdraw_memory_in_transaction,
                )

                withdraw_memory_in_transaction(
                    c, memory_id=projection["memory_id"], project_id=projection["project"],
                    final_status=next_status,
                )
        c.commit()
        return {"item": dict(c.execute("SELECT * FROM learning_items WHERE item_id=?", (item_id,)).fetchone())}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def structured_learning_context(
    *, command: str = "list", output_format: str = "dict", **payload: Any
) -> dict[str, Any] | str:
    """Capture, assess, inspect, triage, export, or back up Workbench learning."""
    reserved = {
        "task_window_id", "obligation_id", "retry_latest",
        "_obligation_id", "_latest_closed_obligation_id",
    }
    if reserved.intersection(payload) or any(str(key).startswith("_") for key in payload):
        raise ValueError("internal learning payload keys are not caller-accessible")
    return _structured_learning_dispatch(command=command, output_format=output_format, **payload)


def _project_semantic_candidate(result: dict[str, Any]) -> dict[str, Any]:
    """Project a human-derived lesson/watch once, preserving learning lineage."""
    item = result.get("item") or {}
    if item.get("kind") not in {"lesson", "watch_item"} or item.get("status") != "active":
        return result
    path = _db_path()
    from odibi_anchor.codebase._memory_lifecycle import get_projection, record_projection

    existing = get_projection(path, learning_item_id=item["item_id"])
    if existing is not None:
        return {**result, "semantic_projection": existing}
    hydrated = _read("show", {"item_id": item["item_id"], "include_history": True})["item"]
    from odibi_anchor.codebase.memory_context import append_memory

    project_refs = hydrated["project_refs"]
    project = (
        project_refs[0]
        if hydrated["applicability_scope"] == "project_local" and project_refs
        else "all"
    )
    source_ids = sorted(
        link["source_item_id"] for link in hydrated["links"]
        if link["derived_item_id"] == hydrated["item_id"]
    )
    evidence_refs = hydrated["evidence_refs"]
    related_files = sorted({
        evidence["reference"].split("#L", 1)[0]
        for evidence in evidence_refs if evidence["reference_type"] == "file"
    })
    memory = append_memory(
        ".", entry_type="pattern" if hydrated["kind"] == "lesson" else "gotcha",
        content=hydrated["summary"], related_files=related_files,
        tags=["structured-learning", f"learning:{hydrated['kind']}", *(
            f"project:{reference}" for reference in project_refs
        )],
        source=f"structured_learning:{hydrated['item_id']}", confidence=0.5,
        evidence={
            "learning_item_id": hydrated["item_id"],
            "decision_key": hydrated["decision_key"],
            "source_item_ids": source_ids,
            "evidence_ref_sha256": sorted(
                evidence["reference_sha256"] for evidence in evidence_refs
            ),
        },
        project=project, db_path=str(path),
    )
    lineage = {
        "learning_item_id": hydrated["item_id"],
        "learning_kind": hydrated["kind"],
        "decision_key": hydrated["decision_key"],
        "source_item_ids": source_ids,
        "learning_event_ids": sorted(event["event_id"] for event in hydrated["events"]),
    }
    projection = record_projection(
        path, learning_item_id=hydrated["item_id"], memory_id=memory["id"], lineage=lineage,
    )
    return {**result, "semantic_projection": projection}


def _project_assessed_observations(result: dict[str, Any]) -> dict[str, Any]:
    """Project evidence-backed assessed observations as advisory candidates only."""
    assessment = result.get("assessment") or {}
    if assessment.get("outcome") != "observations_recorded":
        return result
    path = _db_path()
    from odibi_anchor.codebase._memory_lifecycle import get_projection, record_projection
    from odibi_anchor.codebase.memory_context import append_memory

    projections = []
    for item_id in assessment.get("observation_ids", []):
        existing = get_projection(path, learning_item_id=item_id)
        if existing is not None:
            projections.append(existing)
            continue
        hydrated = _read("show", {"item_id": item_id, "include_history": True})["item"]
        evidence_refs = hydrated["evidence_refs"]
        if hydrated.get("observation_type") not in {
            "reusable_practice", "near_miss", "evidence_gap",
        } or not evidence_refs:
            # Capture permits bounded observations without evidence; they remain in
            # structured learning and cannot poison semantic retrieval.
            continue
        project_refs = hydrated["project_refs"]
        if hydrated["applicability_scope"] != "project_local" or len(project_refs) != 1:
            # Automatic projection cannot infer that a workbench/cross-project claim
            # is safe to disclose globally. Human triage owns any scope widening.
            continue
        project = project_refs[0]
        with sqlite3.connect(path) as recurrence_connection:
            recurrence_count = recurrence_connection.execute(
                "SELECT count(*) FROM learning_items WHERE recurrence_key=? "
                "AND status NOT IN ('rejected','retired')", (hydrated["recurrence_key"],),
            ).fetchone()[0]
        related_files = sorted({
            evidence["reference"].split("#L", 1)[0]
            for evidence in evidence_refs if evidence["reference_type"] == "file"
        })
        memory = append_memory(
            ".", entry_type="discovery", content=hydrated["summary"],
            related_files=related_files,
            tags=["structured-learning", "learning:assessed-observation", *(
                f"project:{reference}" for reference in project_refs
            )],
            source=f"structured_learning:{item_id}", confidence=0.35,
            evidence={
                "learning_item_id": item_id,
                "learning_obligation_id": hydrated["provenance"]["learning_obligation_id"],
                "assessment_id": assessment["assessment_id"],
                "evidence_ref_sha256": sorted(
                    evidence["reference_sha256"] for evidence in evidence_refs
                ),
                "promotion_status": "candidate",
                "recurrence_key": hydrated["recurrence_key"],
                "recurrence_count": recurrence_count,
                "task_window_id": hydrated["provenance"]["task_window_id"],
                "project_refs": project_refs,
            },
            project=project, db_path=str(path),
        )
        projection = record_projection(
            path, learning_item_id=item_id, memory_id=memory["id"],
            lineage={
                "learning_item_id": item_id,
                "learning_kind": "observation",
                "assessment_id": assessment["assessment_id"],
                "assessment_actor_kind": assessment["actor_kind"],
                "learning_obligation_id": hydrated["provenance"]["learning_obligation_id"],
                "recurrence_key": hydrated["recurrence_key"],
                "recurrence_count": recurrence_count,
                "task_window_id": hydrated["provenance"]["task_window_id"],
                "project_refs": project_refs,
                "evidence_ref_sha256": sorted(
                    evidence["reference_sha256"] for evidence in evidence_refs
                ),
                "producing_task_record": {
                    "status": "unavailable",
                    "reason": "terminal record is persisted after assessment projection",
                },
            },
        )
        projections.append(projection)
    return {**result, "semantic_candidate_projections": projections}


def _structured_learning_dispatch(
    *, command: str = "list", output_format: str = "dict", **payload: Any
) -> dict[str, Any] | str:
    """Dispatcher-only entry point carrying authoritative obligation fields."""
    if output_format not in {"dict", "json", "markdown"}:
        raise ValueError("output_format must be dict, json, or markdown")
    command = command.strip().lower()
    if command in ("list", "show", "insights"):
        result = _read(command, payload)
    elif command == "capture":
        result = _capture(payload)
    elif command == "assess":
        result = _project_assessed_observations(_assess(payload))
    elif command == "triage":
        result = _project_semantic_candidate(_triage(payload))
    elif command == "export":
        if set(payload) - {"status"}:
            raise ValueError("invalid export payload")
        status = payload.get("status")
        if status is not None and status not in {"open", "active", "deferred", "rejected", "retired"}:
            raise ValueError("invalid status")
        p = _db_path()
        result = {
            "schema": "odibi-anchor.structured-learning",
            "version": 1,
            "exported_at": _now(),
            "complete": status is None,
            "filters": {} if status is None else {"status": status},
        }
        if not p.exists():
            for key in (
                "items",
                "evidence_refs",
                "item_links",
                "events",
                "obligations",
                "assessments",
                "assessment_observations",
            ):
                result[key] = []
        else:
            c = _connect(p, ro=True)
            try:
                schema_status = _domain_schema_status(c)
                if schema_status == "pristine":
                    for key in (
                        "items",
                        "evidence_refs",
                        "item_links",
                        "events",
                        "obligations",
                        "assessments",
                        "assessment_observations",
                    ):
                        result[key] = []
                    names = set()
                elif schema_status == "exact":
                    _verify(c)
                    names = {row[0] for row in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                else:
                    raise RuntimeError("learning schema drift")
                orders = (
                    "item_id",
                    "evidence_id",
                    "source_item_id,derived_item_id,relation",
                    "event_id",
                    "obligation_id",
                    "assessment_id",
                    "assessment_id,observation_id",
                )
                for key, table, order in zip(
                    (
                        "items",
                        "evidence_refs",
                        "item_links",
                        "events",
                        "obligations",
                        "assessments",
                        "assessment_observations",
                    ),
                    TABLES[:-1],
                    orders,
                    strict=True,
                ):
                    if not names:
                        continue
                    if status is None:
                        query = f"SELECT * FROM {table} ORDER BY {order}"
                        parameters = ()
                    elif table == "learning_items":
                        query = f"SELECT * FROM {table} WHERE status=? ORDER BY {order}"
                        parameters = (status,)
                    elif table in {"learning_evidence_refs", "learning_events"}:
                        query = (
                            f"SELECT child.* FROM {table} child JOIN learning_items item "
                            f"ON item.item_id=child.item_id WHERE item.status=? ORDER BY child.{order}"
                        )
                        parameters = (status,)
                    elif table == "learning_item_links":
                        query = (
                            "SELECT link.* FROM learning_item_links link "
                            "JOIN learning_items source ON source.item_id=link.source_item_id "
                            "JOIN learning_items derived ON derived.item_id=link.derived_item_id "
                            "WHERE source.status=? AND derived.status=? "
                            "ORDER BY link.source_item_id,link.derived_item_id,link.relation"
                        )
                        parameters = (status, status)
                    else:
                        query = f"SELECT * FROM {table} ORDER BY {order}"
                        parameters = ()
                    result[key] = [dict(row) for row in c.execute(query, parameters)]
            finally:
                c.close()
        result["content_sha256"] = _sha({k: v for k, v in result.items() if k not in ("exported_at", "content_sha256")})
    elif command == "backup":
        if payload:
            raise ValueError("invalid backup payload")
        p = _db_path()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        dest = Path(f"{p}.learning-backup.{stamp}.sqlite")
        temporary = Path(f"{dest}.tmp-{uuid.uuid4().hex}")
        if not p.exists():
            raise ValueError("learning database is uninitialized")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        s = _connect(p, ro=True)
        d = sqlite3.connect(temporary, isolation_level=None, timeout=5.0)
        try:
            s.execute("BEGIN")
            _verify(s)
            row_counts = {
                table: s.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in TABLES
            }
            s.backup(d)
            if d.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("learning backup integrity check failed")
            _verify(d)
            for table in TABLES:
                destination_count = d.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if row_counts[table] != destination_count:
                    raise RuntimeError("learning backup row count mismatch")
        except Exception:
            d.close()
            temporary.unlink(missing_ok=True)
            raise
        finally:
            s.close()
            d.close()
        _publish_backup(
            temporary, dest, collision_error="learning backup destination already exists"
        )
        result = {"backup_path": str(dest), "integrity_check": "ok", "row_counts": row_counts}
    else:
        raise ValueError("unknown learning command")
    if output_format in ("dict", "json"):
        return result
    if output_format == "markdown":
        return "# Structured Learning\n\n```json\n" + json.dumps(result, indent=2) + "\n```"
    raise AssertionError("unreachable output format")
