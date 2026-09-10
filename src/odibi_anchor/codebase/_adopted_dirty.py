"""Fail-closed owner approval and atomic provenance for dirty task adoption."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOMAIN = "task_adoption"
VERSION = 1
FORMAT = "odibi-anchor-dirty-adoption-v1"
_DDL = (
    "CREATE TABLE task_touched_paths (task_window_id TEXT NOT NULL REFERENCES accepted_task_records(task_window_id), path TEXT NOT NULL, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64), created_at TEXT NOT NULL, PRIMARY KEY(task_window_id,path))",
    "CREATE TRIGGER task_touched_paths_no_update BEFORE UPDATE ON task_touched_paths BEGIN SELECT RAISE(ABORT,'task touched paths are immutable'); END",
    "CREATE TRIGGER task_touched_paths_no_delete BEFORE DELETE ON task_touched_paths BEGIN SELECT RAISE(ABORT,'task touched paths are immutable'); END",
    "CREATE TABLE dirty_adoption_approvals (approval_id TEXT PRIMARY KEY, challenge_sha256 TEXT NOT NULL UNIQUE CHECK(length(challenge_sha256)=64), prior_task_window_id TEXT NOT NULL REFERENCES accepted_task_records(task_window_id), approval_json TEXT NOT NULL, approval_sha256 TEXT NOT NULL CHECK(length(approval_sha256)=64), created_at TEXT NOT NULL)",
    "CREATE TRIGGER dirty_adoption_approvals_no_update BEFORE UPDATE ON dirty_adoption_approvals BEGIN SELECT RAISE(ABORT,'dirty adoption approvals are immutable'); END",
    "CREATE TRIGGER dirty_adoption_approvals_no_delete BEFORE DELETE ON dirty_adoption_approvals BEGIN SELECT RAISE(ABORT,'dirty adoption approvals are immutable'); END",
    "CREATE TABLE dirty_adoption_events (adoption_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE REFERENCES dirty_adoption_approvals(approval_id), prior_task_window_id TEXT NOT NULL REFERENCES accepted_task_records(task_window_id), task_window_id TEXT NOT NULL UNIQUE REFERENCES accepted_task_records(task_window_id), event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64), created_at TEXT NOT NULL)",
    "CREATE TRIGGER dirty_adoption_events_no_update BEFORE UPDATE ON dirty_adoption_events BEGIN SELECT RAISE(ABORT,'dirty adoption events are immutable'); END",
    "CREATE TRIGGER dirty_adoption_events_no_delete BEFORE DELETE ON dirty_adoption_events BEGIN SELECT RAISE(ABORT,'dirty adoption events are immutable'); END",
    "CREATE TABLE dirty_adoption_refusal_events (refusal_id TEXT PRIMARY KEY, operation TEXT NOT NULL CHECK(operation IN ('request','prepare','commit')), approval_id TEXT, prior_task_window_id TEXT, precondition TEXT NOT NULL, event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64), created_at TEXT NOT NULL)",
    "CREATE INDEX idx_dirty_adoption_refusals_precondition ON dirty_adoption_refusal_events(precondition,created_at,refusal_id)",
    "CREATE TRIGGER dirty_adoption_refusal_events_no_update BEFORE UPDATE ON dirty_adoption_refusal_events BEGIN SELECT RAISE(ABORT,'dirty adoption refusal events are immutable'); END",
    "CREATE TRIGGER dirty_adoption_refusal_events_no_delete BEFORE DELETE ON dirty_adoption_refusal_events BEGIN SELECT RAISE(ABORT,'dirty adoption refusal events are immutable'); END",
    "CREATE TABLE dirty_adoption_withdrawal_events (withdrawal_id TEXT PRIMARY KEY, adoption_id TEXT NOT NULL UNIQUE REFERENCES dirty_adoption_events(adoption_id), event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64), created_at TEXT NOT NULL)",
    "CREATE TRIGGER dirty_adoption_withdrawal_events_no_update BEFORE UPDATE ON dirty_adoption_withdrawal_events BEGIN SELECT RAISE(ABORT,'dirty adoption withdrawal events are immutable'); END",
    "CREATE TRIGGER dirty_adoption_withdrawal_events_no_delete BEFORE DELETE ON dirty_adoption_withdrawal_events BEGIN SELECT RAISE(ABORT,'dirty adoption withdrawal events are immutable'); END",
)
_TABLES = (
    "task_touched_paths", "dirty_adoption_approvals", "dirty_adoption_events",
    "dirty_adoption_refusal_events", "dirty_adoption_withdrawal_events",
)
SCHEMA_TEXT = ";\n".join(_DDL) + ";\n"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_TEXT.encode()).hexdigest()


class AdoptionUnavailable(RuntimeError):
    """A named dirty-adoption precondition was not satisfied."""

    def __init__(self, precondition: str, detail: str):
        self.precondition = precondition
        self.detail = detail
        super().__init__(f"adopted_dirty precondition failed: {precondition}: {detail}")


def _fail(precondition: str, detail: str) -> None:
    raise AdoptionUnavailable(precondition, detail)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

    resolved = Path(path).expanduser().resolve()
    if not read_only:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    target = resolved.as_uri() + "?mode=ro" if read_only else str(resolved)
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
    """Create or exactly verify the additive dirty-adoption domain."""
    from odibi_anchor.codebase._task_authority import initialize_schema as initialize_authority

    initialize_authority(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
        ).fetchone()
        if row and tuple(row) != (VERSION, SCHEMA_SHA256):
            raise RuntimeError("task adoption schema version/checksum mismatch")
        if row is None:
            for statement in _DDL:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                (DOMAIN, VERSION, SCHEMA_SHA256, _now()),
            )
        _verify_schema(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"domain": DOMAIN, "version": VERSION, "schema_sha256": SCHEMA_SHA256}


def _verify_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
    ).fetchone()
    if tuple(row or ()) != (VERSION, SCHEMA_SHA256):
        raise RuntimeError("task adoption schema version/checksum mismatch")
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute("PRAGMA foreign_keys=OFF")
        expected.execute("CREATE TABLE accepted_task_records(task_window_id TEXT PRIMARY KEY)")
        for statement in _DDL:
            expected.execute(statement)
        placeholders = ",".join("?" for _ in _TABLES)
        expected_rows = expected.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ({placeholders}) "
            "AND sql IS NOT NULL ORDER BY type,name", _TABLES,
        ).fetchall()
    finally:
        expected.close()
    actual_rows = connection.execute(
        f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ({placeholders}) "
        "AND sql IS NOT NULL ORDER BY type,name", _TABLES,
    ).fetchall()
    if [tuple(item) for item in actual_rows] != expected_rows:
        raise RuntimeError("task adoption schema checksum mismatch")


def _verified_json(row: sqlite3.Row, json_key: str, digest_key: str) -> dict[str, Any]:
    raw = row[json_key]
    value = json.loads(raw)
    if raw != _canonical(value) or hashlib.sha256(raw.encode()).hexdigest() != row[digest_key]:
        _fail("immutable evidence", "stored record integrity mismatch")
    return value


def _load_prior(connection: sqlite3.Connection, task_window_id: str) -> tuple[dict[str, Any], str]:
    from odibi_anchor.codebase._task_authority import _load_verified_record, _verify_schema

    _verify_schema(connection)
    row = connection.execute(
        "SELECT * FROM accepted_task_records WHERE task_window_id=?", (task_window_id,),
    ).fetchone()
    if row is None:
        _fail("recoverable prior authority", "accepted task record is unavailable")
    closed = connection.execute(
        "SELECT 1 FROM accepted_task_events WHERE task_window_id=? AND event_type='closed'",
        (task_window_id,),
    ).fetchone()
    if closed is not None:
        _fail("recoverable prior authority", "accepted task is already closed")
    record = _load_verified_record(row)
    if record["task"]["profile"]["execution_mode"] != "source_change":
        _fail("recoverable prior authority", "prior task is not a source-change task")
    return record, row["record_sha256"]


def record_touched_path(path: str | Path, *, task_window_id: str, touched_path: str) -> dict[str, Any]:
    """Append one task-scoped touched path after accepted authority exists."""
    initialize_schema(path)
    normalized = touched_path.replace("\\", "/").strip("/")
    if not normalized or normalized.startswith("../") or "/../" in f"/{normalized}/":
        raise ValueError("touched_path must be a repository-relative path")
    created_at = _now()
    record = {
        "format": FORMAT, "kind": "touched_path", "task_window_id": task_window_id,
        "path": normalized, "created_at": created_at,
    }
    payload = _canonical(record)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_schema(connection)
        if connection.execute(
            "SELECT 1 FROM accepted_task_records WHERE task_window_id=?", (task_window_id,),
        ).fetchone() is None:
            connection.rollback()
            return {
                "task_window_id": task_window_id, "path": normalized,
                "created": False, "status": "unavailable",
            }
        existing = connection.execute(
            "SELECT record_json,record_sha256 FROM task_touched_paths "
            "WHERE task_window_id=? AND path=?",
            (task_window_id, normalized),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO task_touched_paths VALUES(?,?,?,?,?)",
                (task_window_id, normalized, payload, digest, created_at),
            )
        else:
            retained = _verify_touch_row(existing)
            if retained["task_window_id"] != task_window_id or retained["path"] != normalized:
                raise RuntimeError("task touched path idempotency conflict")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "task_window_id": task_window_id, "path": normalized,
        "created": existing is None, "status": "recorded",
    }


def _allowed_paths(connection: sqlite3.Connection, record: dict[str, Any]) -> tuple[str, ...]:
    declared = tuple(str(item).strip("/") for item in record["task"]["repository_scope"])
    touched = tuple(
        row["path"] for row in connection.execute(
            "SELECT path FROM task_touched_paths WHERE task_window_id=? ORDER BY path",
            (record["identity"]["task_window_id"],),
        )
    )
    return tuple(sorted(set(declared) | set(touched)))


def _inside_scope(path: str, scopes: tuple[str, ...]) -> bool:
    return any(
        scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/")
        for scope in scopes
    )


def _verify_approval_row(row: sqlite3.Row) -> dict[str, Any]:
    approval = _verified_json(row, "approval_json", "approval_sha256")
    request = approval.get("request")
    if not isinstance(request, dict):
        _fail("immutable evidence", "approval request evidence is invalid")
    expected_id = "ada_" + hashlib.sha256(
        f"{approval.get('challenge_sha256')}:{request.get('request_id')}:"
        f"{request.get('response_message_id')}".encode()
    ).hexdigest()
    duplicated = {
        "approval_id": approval.get("approval_id"),
        "challenge_sha256": approval.get("challenge_sha256"),
        "prior_task_window_id": approval.get("subject", {}).get("prior_authority", {}).get(
            "task_window_id"
        ),
        "created_at": approval.get("created_at"),
    }
    if expected_id != row["approval_id"] or any(row[key] != value for key, value in duplicated.items()):
        _fail("immutable evidence", "approval indexed column mismatch")
    if request.get("response_sha256") != hashlib.sha256(
        f"APPROVE {row['challenge_sha256']}".encode()
    ).hexdigest():
        _fail("immutable evidence", "approval response does not bind its challenge")
    return approval


def _verify_event_row(row: sqlite3.Row) -> dict[str, Any]:
    event = _verified_json(row, "event_json", "event_sha256")
    expected_id = "ade_" + hashlib.sha256(row["approval_id"].encode()).hexdigest()
    duplicated = {
        "adoption_id": event.get("adoption_id"),
        "approval_id": event.get("approval_id"),
        "prior_task_window_id": event.get("prior_task_window_id"),
        "task_window_id": event.get("task_window_id"),
        "created_at": event.get("created_at"),
    }
    if expected_id != row["adoption_id"] or any(row[key] != value for key, value in duplicated.items()):
        _fail("immutable evidence", "adoption indexed column mismatch")
    subject = event.get("subject")
    if not isinstance(subject, dict) or event.get("challenge_sha256") != hashlib.sha256(
        _canonical(subject).encode()
    ).hexdigest():
        _fail("immutable evidence", "adoption challenge does not bind its subject")
    return event


def _verify_touch_row(row: sqlite3.Row) -> dict[str, Any]:
    record = _verified_json(row, "record_json", "record_sha256")
    if any(
        row[key] != record.get(key)
        for key in ("task_window_id", "path", "created_at")
    ):
        _fail("immutable evidence", "touched-path indexed column mismatch")
    return record


def _verify_refusal_row(row: sqlite3.Row) -> dict[str, Any]:
    event = _verified_json(row, "event_json", "event_sha256")
    duplicated = {
        "refusal_id": event.get("refusal_id"),
        "operation": event.get("operation"),
        "approval_id": event.get("approval_id"),
        "prior_task_window_id": event.get("prior_task_window_id"),
        "precondition": event.get("precondition"),
        "created_at": event.get("created_at"),
    }
    if any(row[key] != value for key, value in duplicated.items()):
        _fail("immutable evidence", "refusal indexed column mismatch")
    return event


def _verify_withdrawal_row(row: sqlite3.Row) -> dict[str, Any]:
    event = _verified_json(row, "event_json", "event_sha256")
    expected_id = "adw_" + hashlib.sha256(row["adoption_id"].encode()).hexdigest()
    duplicated = {
        "withdrawal_id": event.get("withdrawal_id"),
        "adoption_id": event.get("adoption_id"),
        "created_at": event.get("created_at"),
    }
    if expected_id != row["withdrawal_id"] or any(
        row[key] != value for key, value in duplicated.items()
    ):
        _fail("immutable evidence", "withdrawal indexed column mismatch")
    return event


def record_adoption_refusal(
    path: str | Path, error: AdoptionUnavailable, *, operation: str,
    approval_id: str | None = None, prior_task_window_id: str | None = None,
) -> dict[str, Any]:
    """Append aggregate-safe refusal telemetry without granting task authority."""
    if operation not in {"request", "prepare", "commit"}:
        raise ValueError("invalid adoption refusal operation")
    initialize_schema(path)
    created_at = _now()
    refusal_id = "adr_" + uuid.uuid4().hex
    from odibi_anchor._forensic_replay.journal import redact_payload

    event = redact_payload({
        "format": FORMAT, "kind": "refused", "refusal_id": refusal_id,
        "operation": operation, "approval_id": approval_id,
        "prior_task_window_id": prior_task_window_id,
        "precondition": error.precondition, "detail": error.detail[:1000],
        "created_at": created_at,
    })
    payload = _canonical(event)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_schema(connection)
        connection.execute(
            "INSERT INTO dirty_adoption_refusal_events VALUES(?,?,?,?,?,?,?,?)",
            (
                refusal_id, operation, approval_id, prior_task_window_id,
                error.precondition, payload, digest, created_at,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"refusal_id": refusal_id, "precondition": error.precondition}


def withdraw_adoption(
    path: str | Path, *, adoption_id: str, reason: str, actor_ref: str,
) -> dict[str, Any]:
    """Append one withdrawal while retaining the immutable adoption provenance."""
    if not adoption_id.strip() or not reason.strip() or not actor_ref.strip():
        raise ValueError("adoption_id, reason, and actor_ref must be non-empty")
    initialize_schema(path)
    withdrawal_id = "adw_" + hashlib.sha256(adoption_id.encode()).hexdigest()
    created_at = _now()
    event = {
        "format": FORMAT, "kind": "withdrawn", "withdrawal_id": withdrawal_id,
        "adoption_id": adoption_id,
        "reason_sha256": hashlib.sha256(reason.strip().encode()).hexdigest(),
        "actor_ref_sha256": hashlib.sha256(actor_ref.strip().encode()).hexdigest(),
        "created_at": created_at,
    }
    payload = _canonical(event)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_schema(connection)
        if connection.execute(
            "SELECT 1 FROM dirty_adoption_events WHERE adoption_id=?", (adoption_id,),
        ).fetchone() is None:
            _fail("withdrawable adoption", "adoption record is unavailable")
        existing = connection.execute(
            "SELECT * FROM dirty_adoption_withdrawal_events WHERE adoption_id=?", (adoption_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO dirty_adoption_withdrawal_events VALUES(?,?,?,?,?)",
                (withdrawal_id, adoption_id, payload, digest, created_at),
            )
        else:
            retained = _verify_withdrawal_row(existing)
            if any(
                retained[key] != event[key]
                for key in ("reason_sha256", "actor_ref_sha256")
            ):
                _fail("withdrawable adoption", "a different withdrawal already exists")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"withdrawal_id": withdrawal_id, "adoption_id": adoption_id, "created": existing is None}


def adoption_status(path: str | Path, *, adoption_id: str) -> dict[str, Any]:
    """Read one integrity-checked adoption and its optional withdrawal."""
    connection = _connect(path, read_only=True)
    try:
        _verify_schema(connection)
        row = connection.execute(
            "SELECT * FROM dirty_adoption_events WHERE adoption_id=?", (adoption_id,),
        ).fetchone()
        if row is None:
            _fail("immutable provenance", "adoption event is unavailable")
        _verify_event_row(row)
        withdrawal = connection.execute(
            "SELECT * FROM dirty_adoption_withdrawal_events WHERE adoption_id=?", (adoption_id,),
        ).fetchone()
        if withdrawal is not None:
            _verify_withdrawal_row(withdrawal)
        return {
            "adoption_id": adoption_id,
            "status": "withdrawn" if withdrawal is not None else "active",
            "withdrawal_id": withdrawal["withdrawal_id"] if withdrawal is not None else None,
        }
    finally:
        connection.close()


def _subject(
    connection: sqlite3.Connection, *, prior_task_window_id: str, project_id: str,
    target_root: str | Path, artifact_root: str | Path, trust_domain: str,
) -> tuple[dict[str, Any], Any, str]:
    from odibi_anchor._repository_snapshot import (
        TaskRepositoryBaseline,
        capture_task_change_scope,
    )
    from odibi_anchor.codebase._task_authority import _restore_baseline

    record, record_sha256 = _load_prior(connection, prior_task_window_id)
    identity = record["identity"]
    canonical_target = os.path.normcase(str(Path(target_root).expanduser().resolve()))
    canonical_artifact = os.path.normcase(str(Path(artifact_root).expanduser().resolve()))
    if identity.get("project_id") != project_id:
        _fail("same domain", "project identity differs")
    if identity.get("target_root") != canonical_target or identity.get("artifact_root") != canonical_artifact:
        _fail("same domain", "target or artifact root differs")
    if not trust_domain or identity.get("trust_domain") != trust_domain:
        _fail("same domain", "explicit trust domain is missing or differs")
    if identity.get("repository_provider_id") is not None:
        _fail("same domain", "adopted_dirty currently requires canonical local Git")
    baseline = _restore_baseline(record["repository_baseline"], None)
    if not isinstance(baseline, TaskRepositoryBaseline):
        _fail("recoverable prior authority", "prior baseline is not a born local Git baseline")
    snapshot = capture_task_change_scope(baseline)
    mutable = tuple(sorted(set(snapshot.staged_paths) | set(snapshot.unstaged_paths) | set(snapshot.untracked_paths)))
    if not mutable:
        _fail("exact worktree fingerprint", "worktree has no staged, unstaged, or untracked paths")
    allowed = _allowed_paths(connection, record)
    outside = tuple(item for item in snapshot.changed_paths if not _inside_scope(item, allowed))
    if outside:
        _fail("every path in prior scope", f"outside prior scope or touched records: {outside[0]}")
    subject = {
        "format": FORMAT,
        "prior_authority": {
            "task_window_id": prior_task_window_id,
            "record_id": record["record_id"],
            "record_sha256": record_sha256,
            "accepted_at": record["accepted_at"],
            "execution_mode": "source_change",
        },
        "domain": {
            "project_id": project_id, "trust_domain": trust_domain,
            "target_root": canonical_target, "artifact_root": canonical_artifact,
            "repository_provider_id": None,
        },
        "repository": {
            "branch": snapshot.branch, "head_sha": snapshot.head_sha,
            "configured_target_ref": snapshot.configured_target_ref,
            "target_sha": snapshot.target_sha, "merge_base_sha": snapshot.merge_base_sha,
            "staged_paths": list(snapshot.staged_paths),
            "unstaged_paths": list(snapshot.unstaged_paths),
            "untracked_paths": list(snapshot.untracked_paths),
            "changed_paths": list(snapshot.changed_paths),
            "index_fingerprint": snapshot.provenance["index_fingerprint"],
            "worktree_fingerprint": snapshot.provenance["worktree_fingerprint"],
        },
        "scope_evidence": {"allowed_paths": list(allowed)},
    }
    challenge = hashlib.sha256(_canonical(subject).encode()).hexdigest()
    return subject, baseline, challenge


def request_adoption_approval(
    path: str | Path, *, prior_task_window_id: str, project_id: str,
    target_root: str | Path, artifact_root: str | Path, trust_domain: str,
    expected_owner_id: str, timeout_minutes: float = 60,
    state_path: str | os.PathLike[str] | None = None, transport: Any = None,
) -> dict[str, Any]:
    """Request and persist authenticated owner approval for one exact dirty tree."""
    if os.environ.get("ANCHOR_DISABLE_ADOPTED_DIRTY") == "1":
        _fail("kill switch", "dirty adoption is disabled")
    if not expected_owner_id:
        _fail("explicit owner authority", "configured owner identity is unavailable")
    initialize_schema(path)
    connection = _connect(path, read_only=True)
    try:
        _verify_schema(connection)
        subject, _, challenge = _subject(
            connection, prior_task_window_id=prior_task_window_id, project_id=project_id,
            target_root=target_root, artifact_root=artifact_root, trust_domain=trust_domain,
        )
    finally:
        connection.close()
    from odibi_anchor.human_input import request_human_input_record

    expected_response = f"APPROVE {challenge}"
    request = request_human_input_record(
        "Odibi Anchor dirty-adoption request. Verify the exact task and fingerprint, then "
        f"reply exactly: {expected_response}",
        timeout_minutes=timeout_minutes, state_path=state_path, transport=transport,
    )
    if request.response != expected_response or request.response_user_id != expected_owner_id:
        _fail("explicit owner authority", "response text or authenticated owner differs")
    created_at = _now()
    approval_id = "ada_" + hashlib.sha256(
        f"{challenge}:{request.request_id}:{request.response_message_id}".encode()
    ).hexdigest()
    approval = {
        "format": FORMAT, "kind": "owner_approval", "approval_id": approval_id,
        "challenge_sha256": challenge, "subject": subject,
        "owner": {
            "user_id": request.response_user_id, "transport": request.transport,
            "transport_ref_sha256": hashlib.sha256((request.transport_ref or "").encode()).hexdigest(),
        },
        "request": {
            "request_id": request.request_id,
            "message_sha256": hashlib.sha256(request.message.encode()).hexdigest(),
            "response_sha256": hashlib.sha256(request.response.encode()).hexdigest(),
            "response_message_id": request.response_message_id,
            "created_at": request.created_at, "delivered_at": request.delivered_at,
            "responded_at": request.responded_at,
        },
        "created_at": created_at,
    }
    payload = _canonical(approval)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_schema(connection)
        existing = connection.execute(
            "SELECT * FROM dirty_adoption_approvals WHERE challenge_sha256=?", (challenge,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO dirty_adoption_approvals VALUES(?,?,?,?,?,?)",
                (approval_id, challenge, prior_task_window_id, payload, digest, created_at),
            )
        else:
            retained = _verify_approval_row(existing)
            if retained != approval:
                _fail("explicit owner authority", "a different approval already exists for challenge")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"approval_id": approval_id, "challenge_sha256": challenge, "created": existing is None}


def prepare_adoption(
    path: str | Path, *, approval_id: str, project_id: str, target_root: str | Path,
    artifact_root: str | Path, trust_domain: str,
) -> dict[str, Any]:
    """Verify an unconsumed approval and return a staged adopted baseline."""
    if os.environ.get("ANCHOR_DISABLE_ADOPTED_DIRTY") == "1":
        _fail("kill switch", "dirty adoption is disabled")
    initialize_schema(path)
    connection = _connect(path, read_only=True)
    try:
        _verify_schema(connection)
        row = connection.execute(
            "SELECT * FROM dirty_adoption_approvals WHERE approval_id=?", (approval_id,),
        ).fetchone()
        if row is None:
            _fail("explicit owner authority", "approval record is unavailable")
        if connection.execute(
            "SELECT 1 FROM dirty_adoption_events WHERE approval_id=?", (approval_id,),
        ).fetchone():
            _fail("no replay", "approval was already consumed")
        approval = _verify_approval_row(row)
        subject, baseline, challenge = _subject(
            connection, prior_task_window_id=row["prior_task_window_id"], project_id=project_id,
            target_root=target_root, artifact_root=artifact_root, trust_domain=trust_domain,
        )
        if challenge != row["challenge_sha256"] or approval.get("subject") != subject:
            _fail("exact worktree fingerprint", "current repository state differs from approval")
    finally:
        connection.close()
    provenance = {
        "authority_kind": "adopted", "approval_id": approval_id,
        "adoption_id": "ade_" + hashlib.sha256(approval_id.encode()).hexdigest(),
        "challenge_sha256": challenge, "prior_task_window_id": row["prior_task_window_id"],
        "changed_paths": subject["repository"]["changed_paths"],
        "index_fingerprint": subject["repository"]["index_fingerprint"],
        "worktree_fingerprint": subject["repository"]["worktree_fingerprint"],
    }
    return {
        "approval_id": approval_id, "prior_task_window_id": row["prior_task_window_id"],
        "challenge_sha256": challenge, "subject": subject,
        "baseline": replace(
            baseline, authority_kind="adopted", adoption_provenance=provenance,
        ),
    }


def commit_prepared_adoption(
    connection: sqlite3.Connection, *, prepared: dict[str, Any], task_window_id: str,
) -> dict[str, Any]:
    """Revalidate and atomically consume an approval inside task persistence."""
    _verify_schema(connection)
    approval_id = prepared["approval_id"]
    row = connection.execute(
        "SELECT * FROM dirty_adoption_approvals WHERE approval_id=?", (approval_id,),
    ).fetchone()
    if row is None:
        _fail("explicit owner authority", "approval record is unavailable")
    approval = _verify_approval_row(row)
    subject = prepared["subject"]
    existing = connection.execute(
        "SELECT * FROM dirty_adoption_events WHERE approval_id=?", (approval_id,),
    ).fetchone()
    if existing is not None:
        event = _verify_event_row(existing)
        if (
            existing["task_window_id"] != task_window_id
            or event.get("subject") != subject
            or event.get("prior_task_window_id") != prepared["prior_task_window_id"]
        ):
            _fail("no replay", "approval was consumed by a different adoption")
        return {
            "adoption_id": existing["adoption_id"], "approval_id": approval_id,
            "event_sha256": existing["event_sha256"],
        }
    current, _, challenge = _subject(
        connection, prior_task_window_id=prepared["prior_task_window_id"],
        project_id=subject["domain"]["project_id"], target_root=subject["domain"]["target_root"],
        artifact_root=subject["domain"]["artifact_root"],
        trust_domain=subject["domain"]["trust_domain"],
    )
    if current != subject or challenge != approval["challenge_sha256"]:
        _fail("drift after fingerprinting", "repository changed before adoption committed")
    created_at = _now()
    adoption_id = "ade_" + hashlib.sha256(approval_id.encode()).hexdigest()
    event = {
        "format": FORMAT, "kind": "adopted", "adoption_id": adoption_id,
        "approval_id": approval_id, "prior_task_window_id": prepared["prior_task_window_id"],
        "task_window_id": task_window_id, "challenge_sha256": challenge,
        "subject": subject, "owner": approval["owner"], "created_at": created_at,
    }
    payload = _canonical(event)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    try:
        connection.execute(
            "INSERT INTO dirty_adoption_events VALUES(?,?,?,?,?,?,?)",
            (
                adoption_id, approval_id, prepared["prior_task_window_id"], task_window_id,
                payload, digest, created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        _fail("no replay", f"approval or adoption was already consumed ({type(exc).__name__})")
    return {"adoption_id": adoption_id, "approval_id": approval_id, "event_sha256": digest}


def inspect_adoptions(path: str | Path) -> dict[str, Any]:
    """Return counts derived from integrity-checked immutable adoption records."""
    target = Path(path).expanduser()
    if not target.is_file():
        return {
            "schema_status": "uninitialized", "approvals": 0, "adoptions": 0,
            "active_adoptions": 0, "refused_adoptions": 0, "refusals_by_precondition": {},
            "withdrawals": 0, "touched_paths": 0,
        }
    connection = _connect(target, read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "dirty_adoption_approvals" not in tables:
            return {
                "schema_status": "uninitialized", "approvals": 0, "adoptions": 0,
                "active_adoptions": 0, "refused_adoptions": 0,
                "refusals_by_precondition": {}, "withdrawals": 0, "touched_paths": 0,
            }
        _verify_schema(connection)
        approvals = connection.execute("SELECT * FROM dirty_adoption_approvals").fetchall()
        events = connection.execute("SELECT * FROM dirty_adoption_events").fetchall()
        touches = connection.execute("SELECT * FROM task_touched_paths").fetchall()
        refusals = connection.execute("SELECT * FROM dirty_adoption_refusal_events").fetchall()
        withdrawals = connection.execute(
            "SELECT * FROM dirty_adoption_withdrawal_events"
        ).fetchall()
        for row in approvals:
            _verify_approval_row(row)
        for row in events:
            _verify_event_row(row)
        for row in touches:
            _verify_touch_row(row)
        refusal_counts: dict[str, int] = {}
        for row in refusals:
            event = _verify_refusal_row(row)
            refusal_counts[event["precondition"]] = refusal_counts.get(event["precondition"], 0) + 1
        for row in withdrawals:
            _verify_withdrawal_row(row)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            _fail("immutable evidence", "adoption foreign-key integrity mismatch")
        return {
            "schema_status": "ready", "approvals": len(approvals),
            "adoptions": len(events), "active_adoptions": len(events) - len(withdrawals),
            "refused_adoptions": len(refusals),
            "refusals_by_precondition": dict(sorted(refusal_counts.items())),
            "withdrawals": len(withdrawals), "touched_paths": len(touches),
        }
    finally:
        connection.close()
