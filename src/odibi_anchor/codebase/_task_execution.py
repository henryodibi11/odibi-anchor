"""Durable, integrity-checked terminal projections of Anchor-observed task facts.

This is not an execution replay facility. Records deliberately disclose activity
that Odibi Anchor cannot observe.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOMAIN = "task_execution"
VERSION = 2
FORMAT = "odibi-anchor-terminal-task-v1"
UNOBSERVED = (
    "shell, editor, and filesystem activity outside Odibi Anchor actions",
    "external-provider activity not retained in the Anchor evidence or effect ledger",
    "activity before this task window was accepted",
)
_MAX_ACTION_FACTS = 16
_MAX_LEDGER_FACTS = 8
_MAX_MEMORY_FACTS = 8
_DDL = (
    "CREATE TABLE terminal_task_records (task_window_id TEXT PRIMARY KEY, record_id TEXT NOT NULL UNIQUE, project_id TEXT, problem_id TEXT, spec_id TEXT, work_item_id TEXT, session_id TEXT NOT NULL, terminal_status TEXT NOT NULL CHECK(terminal_status IN ('completed','blocked','failed')), started_at TEXT NOT NULL, ended_at TEXT NOT NULL, start_revision TEXT, end_revision TEXT, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64), created_at TEXT NOT NULL)",
    "CREATE INDEX idx_terminal_task_project ON terminal_task_records(project_id,ended_at,task_window_id)",
    "CREATE TRIGGER terminal_task_records_no_update BEFORE UPDATE ON terminal_task_records BEGIN SELECT RAISE(ABORT,'terminal task records are immutable'); END",
    "CREATE TRIGGER terminal_task_records_no_delete BEFORE DELETE ON terminal_task_records BEGIN SELECT RAISE(ABORT,'terminal task records are immutable'); END",
)
SCHEMA_TEXT = ";\n".join(_DDL) + ";\n"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_TEXT.encode()).hexdigest()
_V1_DDL = _DDL[:2]
V1_SCHEMA_SHA256 = hashlib.sha256((";\n".join(_V1_DDL) + ";\n").encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _bound(value: Any) -> dict[str, Any]:
    """Attach a checksum to one canonical observed fact."""
    payload = value.to_dict() if hasattr(value, "to_dict") else value
    from odibi_anchor._forensic_replay.journal import redact_payload

    payload = redact_payload(payload)
    return {"value": payload, "sha256": hashlib.sha256(_json(payload).encode()).hexdigest()}


def _action_fact(row: Mapping[str, Any]) -> dict[str, Any]:
    """Retain a bounded action projection rather than an unbounded replay payload."""
    fact = {
        key: row[key]
        for key in (
            "action", "passed", "exit_code", "elapsed_ms", "learning_command", "error",
        )
        if key in row
    }
    result = row.get("result")
    if isinstance(result, Mapping):
        for key in ("kind", "status", "summary"):
            if key not in result:
                continue
            value = result.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                fact[f"result_{key}"] = value[:1000] if isinstance(value, str) else value
    elif result is not None:
        fact["result_summary"] = str(result)[:1000]
    return fact


def _fingerprint(value: Any) -> str:
    """Checksum a redacted nested fact without embedding it in the terminal projection."""
    from odibi_anchor._forensic_replay.journal import redact_payload

    if not isinstance(value, Mapping):
        value = {"value": value}
    return hashlib.sha256(_json(redact_payload(value)).encode()).hexdigest()


def _memory_fact(kind: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Connect detailed durable lifecycle rows without copying their full payloads."""
    keys = {
        "selections": ("selection_id", "memory_id", "selected_at"),
        "dispositions": (
            "disposition_id", "selection_id", "disposition", "replacement_memory_id",
            "disposed_at",
        ),
        "applications": ("application_id", "selection_id", "applied_at"),
        "evaluations": ("evaluation_id", "application_id", "outcome", "evaluated_at"),
        "projections": ("projection_id", "learning_item_id", "memory_id", "projected_at"),
    }[kind]
    fact = {key: row.get(key) for key in keys}
    nested = {
        "selections": ("query", "reason"),
        "dispositions": ("reason",),
        "applications": ("context",),
        "evaluations": ("evidence",),
    }.get(kind, ())
    for key in nested:
        fact[f"{key}_sha256"] = _fingerprint(row.get(key, {}))
    if kind == "applications":
        fact["action"] = str(row.get("action", ""))[:1000]
    if kind == "projections":
        # Projection lineage is itself a required terminal fact: obligation,
        # assessment, evidence, project, recurrence, and producing-record links.
        fact["lineage"] = row.get("lineage", {})
    return fact


def _project_memory_records(
    records: Mapping[str, list[dict[str, Any]]], missing: list[str],
) -> dict[str, list[dict[str, Any]]]:
    projected: dict[str, list[dict[str, Any]]] = {}
    for kind in ("selections", "dispositions", "applications", "evaluations", "projections"):
        rows = records.get(kind, [])
        if len(rows) > _MAX_MEMORY_FACTS:
            missing.append(
                f"{len(rows) - _MAX_MEMORY_FACTS} earlier {kind} facts omitted by terminal projection bound"
            )
        projected[kind] = [
            _memory_fact(kind, row) for row in rows[-_MAX_MEMORY_FACTS:]
        ]
    return projected


def build_terminal_projection(
    *, session_state: Any, session_timings: list[dict[str, Any]],
    files_changed: set[str], terminal_status: str, assessment: Mapping[str, Any] | None = None,
    unavailable: list[str] | None = None, memory_db: str | Path,
) -> dict[str, Any]:
    """Build a bounded terminal record solely from Anchor-retained task facts."""
    if terminal_status not in {"completed", "blocked", "failed"}:
        raise ValueError("invalid terminal status")
    epoch = session_state.task_verification_epoch
    if not isinstance(epoch, int):
        raise RuntimeError("terminal projection requires an accepted task epoch")
    window = session_timings[epoch:]
    baseline = session_state.task_repository_baseline
    missing = list(unavailable or [])
    start_revision = getattr(baseline, "task_start_head_sha", None)
    started_at = getattr(baseline, "captured_at", None)
    end_revision = None
    branch = getattr(baseline, "branch", None)
    repository_scope = None
    if baseline is None:
        missing.append("task-start repository revision was not captured")
    else:
        try:
            from odibi_anchor._repository_snapshot import capture_task_change_scope
            repository_scope = capture_task_change_scope(
                baseline, session_state.task_repository_write_fingerprints,
            )
            provenance = getattr(repository_scope, "provenance", {})
            end_revision = provenance.get("target_current_sha") or provenance.get("host_head_current")
            start_revision = start_revision or provenance.get("host_head_start")
            branch = getattr(repository_scope, "branch", branch)
        except (OSError, RuntimeError, ValueError) as exc:
            missing.append(f"task-end repository revision unavailable: {type(exc).__name__}")
    if end_revision is None:
        missing.append("task-end repository revision was not observed")
    if branch is None:
        missing.append("repository branch was not observed")
    evidence_items = session_state.evidence_ledger
    if len(evidence_items) > _MAX_LEDGER_FACTS:
        missing.append(
            f"{len(evidence_items) - _MAX_LEDGER_FACTS} earlier evidence facts omitted by terminal projection bound"
        )
    evidence = [_bound(item) for item in evidence_items[-_MAX_LEDGER_FACTS:]]
    artifacts = []
    artifact_items = session_state.managed_artifact_ledger
    if len(artifact_items) > _MAX_LEDGER_FACTS:
        missing.append(
            f"{len(artifact_items) - _MAX_LEDGER_FACTS} earlier managed artifact facts omitted by terminal projection bound"
        )
    for item in artifact_items[-_MAX_LEDGER_FACTS:]:
        bounded = _bound(item)
        value = bounded["value"]
        candidate = value.get("path") if isinstance(value, dict) else None
        if candidate:
            path = Path(candidate)
            if not path.is_absolute() and session_state.artifact_root:
                path = Path(session_state.artifact_root) / path
            try:
                bounded["content_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                bounded["content_sha256"] = None
                missing.append(f"managed artifact bytes unavailable: {candidate}")
        artifacts.append(bounded)
    from odibi_anchor.codebase._memory_lifecycle import task_memory_records
    memory = _project_memory_records(
        task_memory_records(memory_db, task_window_id=session_state.task_window_id), missing,
    )
    if len(window) > _MAX_ACTION_FACTS:
        missing.append(
            f"{len(window) - _MAX_ACTION_FACTS} earlier Anchor action facts omitted by terminal projection bound"
        )
    action_facts = [_bound(_action_fact(row)) for row in window[-_MAX_ACTION_FACTS:]]
    checks = {
        action: [
            {"observed_action_sha256": fact["sha256"]}
            for fact in action_facts if fact["value"].get("action") == action
        ]
        for action in ("preflight", "test", "review", "gate", "learning")
    }
    for action, facts in checks.items():
        if not facts:
            missing.append(f"{action} check was not observed")
    ended_at = _now()
    repository = {
        "start_revision": start_revision, "end_revision": end_revision,
        "branch": branch,
        "changed_paths": sorted(getattr(repository_scope, "changed_paths", files_changed)),
    }
    if getattr(baseline, "authority_kind", None) == "adopted":
        repository.update({
            "baseline_authority": "adopted",
            "adoption_provenance": dict(getattr(baseline, "adoption_provenance", {}) or {}),
        })
    record = {
        "format": FORMAT,
        "identities": {
            "project_id": session_state.active_project,
            "problem_id": session_state.linked_problem,
            "spec_id": session_state.linked_spec,
            "work_item_id": session_state.linked_work_item,
            "task_window_id": session_state.task_window_id,
            "session_id": session_state.session_id,
        },
        "started_at": started_at or ended_at,
        "ended_at": ended_at,
        "repository": repository,
        "observed_actions": action_facts,
        "evidence": evidence,
        "managed_artifacts": artifacts,
        "checks": checks,
        "learning_assessment": _bound(dict(assessment)) if assessment else None,
        "memory": memory,
        "terminal": {
            "status": terminal_status,
            "basis": (
                "gate_and_learning_committed" if terminal_status == "completed" and files_changed
                else "no_change_assessment_committed" if terminal_status == "completed"
                else "safe_stop"
            ),
            "reason": session_state.terminal_reason,
            "ended_at": ended_at,
        },
        "coverage": {
            "observed": [
                "Anchor-dispatched action summaries in the accepted task window",
                "Anchor evidence and managed-artifact ledgers retained in this process",
                "durable semantic-memory lifecycle facts for this task window",
            ],
            "unobserved": list(UNOBSERVED),
            "unavailable": sorted(set(missing)),
            "replay_claim": "none",
        },
    }
    # Persistence applies the same redaction contract. Validate it here so every
    # supported terminal path receives one aggregate-safe deterministic payload.
    from odibi_anchor._forensic_replay.journal import redact_payload
    return redact_payload(record)


def _connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

    resolved = Path(path).expanduser()
    if not read_only:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    target = f"file:{resolved}?mode=ro" if read_only else str(resolved)
    connection = connect_shared_memory(
        target, owner_key_kind="task_window_id", uri=read_only, isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if not read_only:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def initialize_schema(path: str | Path) -> dict[str, Any]:
    """Create or verify the additive terminal-task domain."""
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS anchor_schema_versions (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64), applied_at TEXT NOT NULL)"
        )
        row = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)
        ).fetchone()
        supported_v1 = row and tuple(row) == (1, V1_SCHEMA_SHA256)
        if row and not supported_v1 and tuple(row) != (VERSION, SCHEMA_SHA256):
            raise RuntimeError("task execution schema version/checksum mismatch")
        if row is None:
            for statement in _DDL:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                (DOMAIN, VERSION, SCHEMA_SHA256, _now()),
            )
        elif supported_v1:
            _verify_schema(connection, version=1)
            connection.commit()
            _verified_migration_backup(path)
            connection.execute("BEGIN IMMEDIATE")
            _verify_schema(connection, version=1)
            _migrate_v1_records(connection)
            for statement in _DDL[2:]:
                connection.execute(statement)
            connection.execute(
                "UPDATE anchor_schema_versions SET version=?,schema_sha256=?,applied_at=? WHERE domain=?",
                (VERSION, SCHEMA_SHA256, _now(), DOMAIN),
            )
        _verify_schema(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"domain": DOMAIN, "version": VERSION, "schema_sha256": SCHEMA_SHA256}


def _verified_migration_backup(path: str | Path) -> Path:
    """Create or reuse a content-addressed pre-v2 backup without overwriting one."""
    from odibi_anchor.codebase._migration_backup import ensure_migration_backup

    result = ensure_migration_backup(
        Path(path).expanduser(),
        tag="task-execution-v2",
        error_prefix="task execution migration",
    )
    return Path(result["backup_path"])


def _verify_schema(connection: sqlite3.Connection, *, version: int = VERSION) -> None:
    expected_hash = V1_SCHEMA_SHA256 if version == 1 else SCHEMA_SHA256
    row = connection.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)
    ).fetchone()
    if tuple(row or ()) != (version, expected_hash):
        raise RuntimeError("task execution schema version/checksum mismatch")
    statements = _V1_DDL if version == 1 else _DDL
    expected = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            expected.execute(statement)
        names = [row[0] for row in expected.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name='terminal_task_records' AND sql IS NOT NULL"
        )]
        placeholders = ",".join("?" for _ in names)
        expected_rows = expected.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({placeholders}) "
            "ORDER BY type,name", names,
        ).fetchall()
    finally:
        expected.close()
    actual = connection.execute(
        f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({placeholders}) "
        "ORDER BY type,name", names,
    ).fetchall()
    actual_names = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name='terminal_task_records' AND sql IS NOT NULL"
    )}
    if [tuple(row) for row in actual] != expected_rows or actual_names != set(names):
        raise RuntimeError("task execution schema checksum mismatch")


def _migrate_v1_records(connection: sqlite3.Connection) -> None:
    """Bind legacy canonical payloads to their deterministic indexed record IDs."""
    for row in connection.execute("SELECT * FROM terminal_task_records").fetchall():
        raw_payload = row["record_json"]
        payload = json.loads(raw_payload)
        if raw_payload != _json(payload):
            raise RuntimeError("legacy terminal task record is not canonical JSON")
        if hashlib.sha256(raw_payload.encode()).hexdigest() != row["record_sha256"]:
            raise RuntimeError("legacy terminal task record checksum mismatch")
        identities, terminal, repository = _validate_record(payload)
        expected_id = "ttr_" + hashlib.sha256(str(identities["task_window_id"]).encode()).hexdigest()
        duplicated = {
            "record_id": expected_id,
            "task_window_id": identities.get("task_window_id"),
            "project_id": identities.get("project_id"),
            "problem_id": identities.get("problem_id"),
            "spec_id": identities.get("spec_id"),
            "work_item_id": identities.get("work_item_id"),
            "session_id": identities.get("session_id"),
            "terminal_status": terminal.get("status"),
            "started_at": payload.get("started_at") or terminal.get("started_at"),
            "ended_at": payload.get("ended_at") or terminal.get("ended_at"),
            "start_revision": repository.get("start_revision"),
            "end_revision": repository.get("end_revision"),
        }
        if any(row[column] != value for column, value in duplicated.items()):
            raise RuntimeError("legacy terminal task record indexed column mismatch")
        if payload.get("record_id") not in {None, expected_id}:
            raise RuntimeError("legacy terminal task record record_id mismatch")
        if payload.get("record_id") is None:
            payload["record_id"] = expected_id
            migrated = _json(payload)
            connection.execute(
                "UPDATE terminal_task_records SET record_json=?,record_sha256=? WHERE task_window_id=?",
                (migrated, hashlib.sha256(migrated.encode()).hexdigest(), row["task_window_id"]),
            )


def _validate_record(complete: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate canonical payload invariants duplicated by indexed columns."""
    if complete.get("format") != FORMAT:
        raise ValueError(f"terminal record format must be {FORMAT}")
    coverage = complete.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("terminal record requires coverage")
    unobserved = coverage.get("unobserved")
    if not isinstance(unobserved, list) or not set(UNOBSERVED).issubset(unobserved):
        raise ValueError("terminal record must disclose permanent unobserved boundaries")
    unavailable = coverage.get("unavailable")
    if not isinstance(unavailable, list):
        raise ValueError("terminal record requires explicit unavailable boundaries")
    if coverage.get("replay_claim") != "none":
        raise ValueError("terminal record replay_claim must be 'none'")
    identities = complete.get("identities")
    terminal = complete.get("terminal")
    repository = complete.get("repository")
    if (
        not isinstance(identities, dict)
        or not isinstance(terminal, dict)
        or not isinstance(repository, dict)
    ):
        raise ValueError("terminal record requires identities, repository, and terminal objects")
    task = str(identities.get("task_window_id") or "").strip()
    session = str(identities.get("session_id") or "").strip()
    status = terminal.get("status")
    if not task or not session or status not in {"completed", "blocked", "failed"}:
        raise ValueError("invalid terminal record identity or status")
    return identities, terminal, repository


def persist_terminal_record(path: str | Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one immutable redacted terminal projection."""
    from odibi_anchor._forensic_replay.journal import redact_payload

    complete = redact_payload(dict(record))
    identities, terminal, repository = _validate_record(complete)
    task = str(identities["task_window_id"]).strip()
    session = str(identities["session_id"]).strip()
    status = terminal["status"]
    record_id = "ttr_" + hashlib.sha256(task.encode()).hexdigest()
    supplied_record_id = complete.get("record_id")
    if supplied_record_id not in {None, record_id}:
        raise ValueError("terminal record_id does not match task identity")
    complete["record_id"] = record_id
    payload = _json(complete)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    initialize_schema(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_schema(connection)
        existing = connection.execute(
            "SELECT record_sha256 FROM terminal_task_records WHERE task_window_id=?", (task,)
        ).fetchone()
        if existing and existing[0] != digest:
            raise ValueError("terminal task record idempotency conflict")
        if existing is None:
            connection.execute(
                "INSERT INTO terminal_task_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task, record_id, identities.get("project_id"), identities.get("problem_id"),
                    identities.get("spec_id"), identities.get("work_item_id"), session, status,
                    complete.get("started_at") or terminal.get("started_at") or _now(),
                    complete.get("ended_at") or terminal.get("ended_at") or _now(),
                    repository.get("start_revision"), repository.get("end_revision"), payload,
                    digest, _now(),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    result = {
        "record_id": record_id, "task_window_id": task,
        "record_sha256": digest, "record": complete,
    }
    project_id = identities.get("project_id")
    if status == "completed" and isinstance(project_id, str) and project_id:
        from odibi_anchor.codebase._memory_promotion import (
            finalize_task_verifier_attestations,
        )

        finalization = finalize_task_verifier_attestations(
            path, task_window_id=task, project_id=project_id,
        )
        if finalization is not None:
            result["memory_promotion_finalization"] = finalization
    return result


def inspect_terminal_records(
    path: str | Path, *, task_window_id: str | None = None, project_id: str | None = None,
) -> dict[str, Any]:
    """Read and verify terminal records without initializing absent storage."""
    target = Path(path).expanduser()
    if not target.is_file():
        return {"schema_status": "uninitialized", "records": []}
    connection = _connect(target, read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "terminal_task_records" not in tables:
            return {"schema_status": "uninitialized", "records": []}
        version = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
        ).fetchone()
        if tuple(version or ()) == (1, V1_SCHEMA_SHA256):
            _verify_schema(connection, version=1)
            schema_status = "legacy_read_only"
        else:
            _verify_schema(connection)
            schema_status = "ready"
        where, params = [], []
        if task_window_id:
            where.append("task_window_id=?")
            params.append(task_window_id)
        if project_id:
            where.append("project_id=?")
            params.append(project_id)
        query = "SELECT * FROM terminal_task_records"
        if where:
            query += " WHERE " + " AND ".join(where)
        rows = connection.execute(query + " ORDER BY ended_at,task_window_id", params).fetchall()
        records = []
        for row in rows:
            raw_payload = row["record_json"]
            payload = json.loads(raw_payload)
            if raw_payload != _json(payload):
                raise RuntimeError("terminal task record is not canonical JSON")
            try:
                identities, terminal, repository = _validate_record(payload)
            except ValueError as exc:
                raise RuntimeError(f"invalid terminal task record: {exc}") from exc
            duplicated = {
                "record_id": payload.get("record_id"),
                "task_window_id": identities.get("task_window_id"),
                "project_id": identities.get("project_id"),
                "problem_id": identities.get("problem_id"),
                "spec_id": identities.get("spec_id"),
                "work_item_id": identities.get("work_item_id"),
                "session_id": identities.get("session_id"),
                "terminal_status": terminal.get("status"),
                "started_at": payload.get("started_at") or terminal.get("started_at"),
                "ended_at": payload.get("ended_at") or terminal.get("ended_at"),
                "start_revision": repository.get("start_revision"),
                "end_revision": repository.get("end_revision"),
            }
            for column, expected_value in duplicated.items():
                if row[column] != expected_value:
                    raise RuntimeError(f"terminal task record indexed column mismatch: {column}")
            digest = hashlib.sha256(raw_payload.encode()).hexdigest()
            if digest != row["record_sha256"]:
                raise RuntimeError("terminal task record checksum mismatch")
            records.append({**dict(row), "record": payload, "valid": True})
        return {"schema_status": schema_status, "records": records}
    finally:
        connection.close()
