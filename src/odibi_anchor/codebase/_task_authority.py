"""Immutable accepted-task authority and restart-safe rebinding.

Forensic and terminal records prove that activity occurred, but neither contains the
complete policy state needed to continue an open task.  This domain stores that state at
acceptance and appends lifecycle events without rewriting the accepted record.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DOMAIN = "task_authority"
VERSION = 1
FORMAT = "odibi-anchor-accepted-task-v1"
_DDL = (
    "CREATE TABLE accepted_task_records (task_window_id TEXT PRIMARY KEY, record_id TEXT NOT NULL UNIQUE, project_id TEXT, target_root TEXT NOT NULL, execution_mode TEXT NOT NULL, accepted_at TEXT NOT NULL, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64), created_at TEXT NOT NULL)",
    "CREATE INDEX idx_accepted_task_identity ON accepted_task_records(project_id,target_root,accepted_at,task_window_id)",
    "CREATE TRIGGER accepted_task_records_no_update BEFORE UPDATE ON accepted_task_records BEGIN SELECT RAISE(ABORT,'accepted task records are immutable'); END",
    "CREATE TRIGGER accepted_task_records_no_delete BEFORE DELETE ON accepted_task_records BEGIN SELECT RAISE(ABORT,'accepted task records are immutable'); END",
    "CREATE TABLE accepted_task_events (event_id TEXT PRIMARY KEY, task_window_id TEXT NOT NULL REFERENCES accepted_task_records(task_window_id), event_type TEXT NOT NULL CHECK(event_type IN ('rebound','closed')), event_json TEXT NOT NULL, event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64), created_at TEXT NOT NULL, UNIQUE(task_window_id,event_type))",
    "CREATE INDEX idx_accepted_task_events_window ON accepted_task_events(task_window_id,created_at,event_id)",
    "CREATE TRIGGER accepted_task_events_no_update BEFORE UPDATE ON accepted_task_events BEGIN SELECT RAISE(ABORT,'accepted task events are immutable'); END",
    "CREATE TRIGGER accepted_task_events_no_delete BEFORE DELETE ON accepted_task_events BEGIN SELECT RAISE(ABORT,'accepted task events are immutable'); END",
)
SCHEMA_TEXT = ";\n".join(_DDL) + ";\n"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_TEXT.encode()).hexdigest()


class TaskAuthorityUnavailable(RuntimeError):
    """The requested durable task authority cannot be safely rebound."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_path(value: str | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    return os.path.normcase(str(Path(value).expanduser().resolve()))


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
    """Create or exactly verify the additive accepted-task authority domain."""
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS anchor_schema_versions (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64), applied_at TEXT NOT NULL)"
        )
        row = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
        ).fetchone()
        if row and tuple(row) != (VERSION, SCHEMA_SHA256):
            raise RuntimeError("task authority schema version/checksum mismatch")
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
        raise RuntimeError("task authority schema version/checksum mismatch")
    expected = sqlite3.connect(":memory:")
    try:
        for statement in _DDL:
            expected.execute(statement)
        expected_rows = expected.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE (tbl_name='accepted_task_records' OR tbl_name='accepted_task_events') AND sql IS NOT NULL ORDER BY type,name",
        ).fetchall()
    finally:
        expected.close()
    actual_rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE (tbl_name='accepted_task_records' OR tbl_name='accepted_task_events') AND sql IS NOT NULL ORDER BY type,name",
    ).fetchall()
    if [tuple(row) for row in actual_rows] != expected_rows:
        raise RuntimeError("task authority schema checksum mismatch")


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"$bytes_b64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_encode(item) for item in value]
    if hasattr(value, "to_dict"):
        return _encode(value.to_dict())
    if is_dataclass(value):
        return {
            item.name: _encode(getattr(value, item.name))
            for item in fields(value)
            if item.name != "identity_provider"
        }
    raise TypeError(f"accepted task authority cannot encode {type(value).__name__}")


def _baseline_payload(baseline: Any, repository_provider: Any) -> dict[str, Any] | None:
    if baseline is None:
        return None
    kind = type(baseline).__name__
    supported = {
        "TaskRepositoryBaseline", "UnbornTaskRepositoryBaseline",
        "DatabricksGitFolderTaskBaseline",
    }
    if kind not in supported:
        raise TypeError(f"unsupported task repository baseline: {kind}")
    payload = _encode(baseline)
    if kind == "TaskRepositoryBaseline" and payload.get("authority_kind") == "clean":
        payload.pop("authority_kind")
        payload.pop("adoption_provenance")
    if kind == "DatabricksGitFolderTaskBaseline":
        payload["identity_provider_id"] = getattr(repository_provider, "provider_id", None)
    return {"kind": kind, "value": payload}


def build_accepted_task_record(
    *, session_state: Any, task_stage: dict[str, Any], task_result: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete immutable authority needed to resume one accepted task."""
    profile = session_state.active_task_profile
    if profile is None or not session_state.task_window_id:
        raise RuntimeError("accepted task authority requires a materialized task profile and window")
    target_root = _canonical_path(session_state.target_root or session_state.artifact_root)
    if target_root is None:
        raise RuntimeError("accepted task authority requires a target root")
    verification = task_result.get("verification")
    acceptance_criteria = task_result.get("acceptance_criteria")
    if acceptance_criteria is None and isinstance(verification, dict):
        acceptance_criteria = verification.get("acceptance_criteria")
    accepted_at = _now()
    record = {
        "format": FORMAT,
        "accepted_at": accepted_at,
        "identity": {
            "task_window_id": session_state.task_window_id,
            "session_id": session_state.session_id,
            "project_id": session_state.active_project,
            "anchor_home": _canonical_path(session_state.anchor_home),
            "project_root": _canonical_path(session_state.project_root),
            "artifact_root": _canonical_path(session_state.artifact_root),
            "target_root": target_root,
            "repository_provider_id": getattr(session_state.repository_provider, "provider_id", None),
            "trust_domain": task_stage.get("trust_domain"),
        },
        "task": {
            "mode": session_state.active_task_mode,
            "goal": session_state.task_goal,
            "tags": list(session_state.task_tags),
            "profile": profile.to_dict(),
            "assurance_plan": _encode(session_state.active_assurance_plan),
            "bps_kernel": _encode(session_state.bps_kernel),
            "referenced_facts": list(session_state.referenced_facts),
            "linked_problem": session_state.linked_problem,
            "linked_spec": session_state.linked_spec,
            "linked_work_item": session_state.linked_work_item,
            "persisted_spec_name": session_state.persisted_spec_name,
            "active_spec": _encode(session_state.active_spec),
            "explicit_problem_requested": session_state.explicit_problem_requested,
            "explicit_spec_requested": session_state.explicit_spec_requested,
            "explicit_pr_draft_requested": session_state.explicit_pr_draft_requested,
            "phase_count": session_state.phase_count,
            "current_phase": session_state.current_phase,
            "repository_scope": list(task_stage.get("repository_scope") or ()),
            "handoff_context": _encode(getattr(session_state, "task_handoff_context", {})),
        },
        "repository_baseline": _baseline_payload(
            session_state.task_repository_baseline, session_state.repository_provider,
        ),
        "baseline_qualification": _encode(
            getattr(session_state, "task_repository_baseline_qualification", None)
        ),
        "obligations": {
            "acceptance_criteria": list(acceptance_criteria or ()),
            "required_skills": _encode(task_result.get("required_skills") or ()),
            "assurance": _encode(task_result.get("assurance")),
        },
        "initial_ledgers": {
            "evidence": _encode(session_state.evidence_ledger),
            "managed_artifacts": _encode(session_state.managed_artifact_ledger),
            "guidance_attestations": _encode(session_state.guidance_attestations),
            "observed_effects": list(session_state.observed_effects),
            "intended_pr_paths": list(session_state.intended_pr_paths),
            "repository_write_fingerprints": dict(session_state.task_repository_write_fingerprints),
        },
    }
    return record


def _validate_record(record: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(record, dict) or record.get("format") != FORMAT:
        raise ValueError("unsupported accepted task record format")
    identity, task, obligations = (
        record.get("identity"), record.get("task"), record.get("obligations"),
    )
    if not all(isinstance(value, dict) for value in (identity, task, obligations)):
        raise ValueError("accepted task record sections are incomplete")
    required_identity = {"task_window_id", "session_id", "artifact_root", "target_root"}
    if any(not identity.get(key) for key in required_identity):
        raise ValueError("accepted task identity is incomplete")
    if not isinstance(task.get("profile"), dict) or not task.get("profile", {}).get("execution_mode"):
        raise ValueError("accepted task profile is incomplete")
    if not isinstance(obligations.get("acceptance_criteria"), list):
        raise ValueError("accepted task acceptance criteria are incomplete")
    if not isinstance(obligations.get("required_skills"), list):
        raise ValueError("accepted task skill obligations are incomplete")
    return identity, task, obligations


def persist_accepted_task(
    path: str | Path, *, session_state: Any, task_stage: dict[str, Any],
    task_result: dict[str, Any], supersede_task_window_id: str | None = None,
    prepared_adoption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one accepted task before its acceptance is returned to the caller."""
    session_state.trust_domain = task_stage.get("trust_domain")
    record = build_accepted_task_record(
        session_state=session_state, task_stage=task_stage, task_result=task_result,
    )
    identity, task, _ = _validate_record(record)
    window = str(identity["task_window_id"])
    record_id = "atr_" + hashlib.sha256(window.encode()).hexdigest()
    record["record_id"] = record_id
    initialize_schema(path)
    if prepared_adoption is not None:
        from odibi_anchor.codebase._adopted_dirty import initialize_schema as initialize_adoption

        initialize_adoption(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_schema(connection)
        existing = connection.execute(
            "SELECT * FROM accepted_task_records WHERE task_window_id=?", (window,),
        ).fetchone()
        if existing is not None:
            existing_record = _load_verified_record(existing)
            record["accepted_at"] = existing_record["accepted_at"]
        payload = _canonical(record)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if existing is not None and existing["record_sha256"] != digest:
            raise RuntimeError("accepted task authority idempotency conflict")
        if existing is None:
            connection.execute(
                "INSERT INTO accepted_task_records VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    window, record_id, identity.get("project_id"), identity["target_root"],
                    task["profile"]["execution_mode"], record["accepted_at"], payload,
                    digest, record["accepted_at"],
                ),
            )
        adoption = None
        if prepared_adoption is not None:
            from odibi_anchor.codebase._adopted_dirty import commit_prepared_adoption

            adoption = commit_prepared_adoption(
                connection, prepared=prepared_adoption, task_window_id=window,
            )
        superseded = None
        if supersede_task_window_id and supersede_task_window_id != window:
            prior = connection.execute(
                "SELECT 1 FROM accepted_task_records WHERE task_window_id=?",
                (supersede_task_window_id,),
            ).fetchone()
            prior_closed = connection.execute(
                "SELECT 1 FROM accepted_task_events WHERE task_window_id=? AND event_type='closed'",
                (supersede_task_window_id,),
            ).fetchone()
            if prior is not None and prior_closed is None:
                event_id, event_payload, event_digest, event_created_at = _event_record(
                    supersede_task_window_id, "closed", {"terminal_status": "superseded"},
                )
                connection.execute(
                    "INSERT INTO accepted_task_events VALUES(?,?,?,?,?,?)",
                    (
                        event_id, supersede_task_window_id, "closed", event_payload,
                        event_digest, event_created_at,
                    ),
                )
                superseded = supersede_task_window_id
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if prepared_adoption is not None:
            from odibi_anchor.codebase._adopted_dirty import (
                AdoptionUnavailable,
                record_adoption_refusal,
            )

            if isinstance(exc, AdoptionUnavailable):
                connection.close()
                record_adoption_refusal(
                    path, exc, operation="commit",
                    approval_id=prepared_adoption.get("approval_id"),
                    prior_task_window_id=prepared_adoption.get("prior_task_window_id"),
                )
        raise
    finally:
        connection.close()
    return {
        "record_id": record_id, "task_window_id": window, "record_sha256": digest,
        "superseded_task_window_id": superseded, "adoption": adoption,
    }


def _event_record(
    task_window_id: str, event_type: str, details: dict[str, Any],
) -> tuple[str, str, str, str]:
    event_id = "ate_" + hashlib.sha256(f"{task_window_id}:{event_type}".encode()).hexdigest()
    created_at = _now()
    event = {
        "event_id": event_id, "task_window_id": task_window_id,
        "event_type": event_type, "details": details, "created_at": created_at,
    }
    payload = _canonical(event)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return event_id, payload, digest, created_at


def _append_event(path: str | Path, task_window_id: str, event_type: str, details: dict[str, Any]) -> dict[str, Any]:
    initialize_schema(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_schema(connection)
        existing = connection.execute(
            "SELECT event_id,event_json,event_sha256,created_at FROM accepted_task_events "
            "WHERE task_window_id=? AND event_type=?",
            (task_window_id, event_type),
        ).fetchone()
        if existing is None:
            event_id, payload, digest, created_at = _event_record(task_window_id, event_type, details)
            connection.execute(
                "INSERT INTO accepted_task_events VALUES(?,?,?,?,?,?)",
                (event_id, task_window_id, event_type, payload, digest, created_at),
            )
        else:
            event_id, payload, digest, created_at = existing
            decoded = json.loads(payload)
            if (
                _canonical(decoded) != payload
                or
                hashlib.sha256(payload.encode()).hexdigest() != digest
                or decoded != {
                    "event_id": event_id, "task_window_id": task_window_id,
                    "event_type": event_type, "details": details, "created_at": created_at,
                }
            ):
                raise RuntimeError(f"accepted task {event_type} event idempotency conflict")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"event_id": event_id, "event_sha256": digest, "created": existing is None}


def close_accepted_task(path: str | Path, *, task_window_id: str, terminal_status: str) -> dict[str, Any]:
    """Append the terminal closure of an accepted task without rewriting authority."""
    if terminal_status not in {"completed", "blocked", "failed", "superseded"}:
        raise ValueError("invalid accepted task terminal status")
    initialize_schema(path)
    connection = _connect(path, read_only=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM accepted_task_records WHERE task_window_id=?", (task_window_id,),
        ).fetchone()
    finally:
        connection.close()
    if exists is None:
        return {"status": "unavailable", "task_window_id": task_window_id, "created": False}
    result = _append_event(
        path, task_window_id, "closed", {"terminal_status": terminal_status},
    )
    result.update(
        status="closed" if result["created"] else "already_closed",
        task_window_id=task_window_id,
    )
    return result


def _load_verified_record(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["record_json"]
    record = json.loads(raw)
    if raw != _canonical(record):
        raise TaskAuthorityUnavailable("accepted task record is not canonical JSON")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if digest != row["record_sha256"]:
        raise TaskAuthorityUnavailable("accepted task record checksum mismatch")
    try:
        identity, task, _ = _validate_record(record)
    except ValueError as exc:
        raise TaskAuthorityUnavailable(f"accepted task record is incomplete: {exc}") from exc
    duplicated = (
        row["record_id"], row["task_window_id"], row["project_id"], row["target_root"],
        row["execution_mode"], row["accepted_at"], row["created_at"],
    )
    expected = (
        record.get("record_id"), identity.get("task_window_id"), identity.get("project_id"),
        identity.get("target_root"), task.get("profile", {}).get("execution_mode"),
        record.get("accepted_at"), record.get("accepted_at"),
    )
    if duplicated != expected:
        raise TaskAuthorityUnavailable("accepted task indexed identity mismatch")
    deterministic_id = "atr_" + hashlib.sha256(str(identity["task_window_id"]).encode()).hexdigest()
    if record.get("record_id") != deterministic_id:
        raise TaskAuthorityUnavailable("accepted task record id mismatch")
    return record


def _restore_baseline(value: dict[str, Any] | None, repository_provider: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        raise TaskAuthorityUnavailable("accepted task repository baseline is incomplete")
    kind, payload = value["kind"], value["value"]
    if not isinstance(payload, dict):
        raise TaskAuthorityUnavailable("accepted task repository baseline payload is invalid")
    payload = dict(payload)
    from odibi_anchor._repository_snapshot import (
        DatabricksGitFolderIdentity,
        DatabricksGitFolderTaskBaseline,
        ScopedFilePreimage,
        TaskRepositoryBaseline,
        UnbornTaskRepositoryBaseline,
        _provider_identity,
        capture_task_change_scope,
    )
    if kind == "TaskRepositoryBaseline":
        baseline = TaskRepositoryBaseline(**payload)
        capture_task_change_scope(baseline)
        return baseline
    if kind == "UnbornTaskRepositoryBaseline":
        baseline = UnbornTaskRepositoryBaseline(**payload)
        capture_task_change_scope(baseline)
        return baseline
    if kind != "DatabricksGitFolderTaskBaseline":
        raise TaskAuthorityUnavailable("accepted task repository baseline kind is unsupported")
    if repository_provider is None:
        raise TaskAuthorityUnavailable("accepted task repository provider is unavailable")
    if payload.pop("identity_provider_id", None) != getattr(repository_provider, "provider_id", None):
        raise TaskAuthorityUnavailable("accepted task repository provider identity mismatch")
    identity = DatabricksGitFolderIdentity(**payload.pop("identity"))
    preimages = tuple(
        ScopedFilePreimage(
            item["path"], item["sha256"], item["size"],
            base64.b64decode(item["content"]["$bytes_b64"], validate=True),
        )
        for item in payload.pop("preimages")
    )
    baseline = DatabricksGitFolderTaskBaseline(
        identity=identity, preimages=preimages, identity_provider=repository_provider,
        repository_scope=tuple(payload.pop("repository_scope")),
        directory_scopes=tuple(payload.pop("directory_scopes")), **payload,
    )
    current = _provider_identity(repository_provider, baseline.target_worktree)
    if current != identity:
        raise TaskAuthorityUnavailable("accepted task repository identity mismatch")
    return baseline


def _restore_state(record: dict[str, Any], session_state: Any) -> None:
    identity, task, _ = _validate_record(record)
    from odibi_anchor.assurance import AssurancePlan
    from odibi_anchor.planning._task_policy import (
        BpsKernel,
        EvidenceEntry,
        ManagedArtifactEntry,
    )
    from odibi_anchor.planning._task_profile import TaskProfile

    baseline = _restore_baseline(record.get("repository_baseline"), session_state.repository_provider)
    qualification = record.get("baseline_qualification")
    if qualification is not None:
        if not isinstance(qualification, dict):
            raise TaskAuthorityUnavailable("accepted task baseline qualification is invalid")
        from odibi_anchor._dispatcher._baseline_qualification import (
            BaselineQualification,
            project_baseline_qualification,
        )
        try:
            qualification = BaselineQualification(
                **{**qualification, "command_identity": tuple(qualification.get("command_identity", ()))},
            )
        except (TypeError, ValueError) as exc:
            raise TaskAuthorityUnavailable("accepted task baseline qualification is invalid") from exc
        projection = project_baseline_qualification(
            qualification, baseline, task_window_id=identity["task_window_id"],
        )
        if projection["status"] == "stale":
            raise TaskAuthorityUnavailable("accepted task baseline qualification is stale")
    ledgers = record.get("initial_ledgers")
    if not isinstance(ledgers, dict):
        raise TaskAuthorityUnavailable("accepted task initial ledgers are incomplete")
    session_state.task_window_id = identity["task_window_id"]
    session_state.active_task_mode = task.get("mode")
    session_state.task_goal = task.get("goal")
    session_state.task_tags = list(task.get("tags") or ())
    session_state.active_task_profile = TaskProfile.from_dict(task["profile"])
    session_state.active_assurance_plan = (
        AssurancePlan.from_dict(task["assurance_plan"]) if task.get("assurance_plan") else None
    )
    session_state.bps_kernel = BpsKernel.from_dict(task["bps_kernel"])
    session_state.referenced_facts = tuple(task.get("referenced_facts") or ())
    session_state.linked_problem = task.get("linked_problem")
    session_state.linked_spec = task.get("linked_spec")
    session_state.linked_work_item = task.get("linked_work_item")
    session_state.persisted_spec_name = task.get("persisted_spec_name")
    session_state.spec_persisted = bool(
        session_state.linked_spec
        and session_state.persisted_spec_name == session_state.linked_spec
    )
    session_state.active_spec = task.get("active_spec")
    session_state.explicit_problem_requested = task.get("explicit_problem_requested", False)
    session_state.explicit_spec_requested = task.get("explicit_spec_requested", False)
    session_state.explicit_pr_draft_requested = task.get("explicit_pr_draft_requested")
    session_state.phase_count = task.get("phase_count", 1)
    session_state.current_phase = task.get("current_phase", 1)
    session_state.task_handoff_context = dict(task.get("handoff_context") or {})
    session_state.active_problem = session_state.linked_problem
    session_state.task_repository_baseline = baseline
    session_state.task_repository_baseline_qualification = qualification
    session_state.task_repository_write_fingerprints = dict(
        ledgers.get("repository_write_fingerprints") or {},
    )
    session_state.evidence_ledger = [EvidenceEntry.from_dict(item) for item in ledgers.get("evidence", ())]
    session_state.managed_artifact_ledger = [
        ManagedArtifactEntry.from_dict(item) for item in ledgers.get("managed_artifacts", ())
    ]
    session_state.guidance_attestations = [
        EvidenceEntry.from_dict(item) for item in ledgers.get("guidance_attestations", ())
    ]
    session_state.observed_effects = list(ledgers.get("observed_effects") or ())
    session_state.intended_pr_paths = tuple(ledgers.get("intended_pr_paths") or ())
    session_state.task_verification_epoch = 0
    session_state.learning_obligation_id = None
    session_state.latest_closed_obligation_id = None
    session_state.terminal_status = None
    session_state.terminal_basis = None
    session_state.terminal_reason = None


def rebind_latest_open_task(
    path: str | Path, *, session_state: Any,
) -> dict[str, Any]:
    """Restore the single open authority matching every current owner identity."""
    target = Path(path).expanduser()
    if not target.is_file():
        raise TaskAuthorityUnavailable("accepted task authority is unavailable")
    connection = _connect(target, read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accepted_task_records" not in tables:
            raise TaskAuthorityUnavailable("accepted task authority is unavailable")
        _verify_schema(connection)
        project = session_state.active_project
        target_root = _canonical_path(session_state.target_root or session_state.artifact_root)
        rows = connection.execute(
            "SELECT r.* FROM accepted_task_records r WHERE r.project_id IS ? AND r.target_root=? AND NOT EXISTS (SELECT 1 FROM accepted_task_events e WHERE e.task_window_id=r.task_window_id AND e.event_type='closed') ORDER BY r.accepted_at,r.task_window_id",
            (project, target_root),
        ).fetchall()
        records = [_load_verified_record(row) for row in rows]
    except TaskAuthorityUnavailable:
        raise
    except (KeyError, TypeError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise TaskAuthorityUnavailable("accepted task authority storage is inconsistent") from exc
    finally:
        connection.close()
    current_identity = {
        "project_id": session_state.active_project,
        "anchor_home": _canonical_path(session_state.anchor_home),
        "project_root": _canonical_path(session_state.project_root),
        "artifact_root": _canonical_path(session_state.artifact_root),
        "target_root": _canonical_path(session_state.target_root or session_state.artifact_root),
        "repository_provider_id": getattr(session_state.repository_provider, "provider_id", None),
        "trust_domain": getattr(session_state, "trust_domain", None),
    }
    matches = [
        candidate for candidate in records
        if all(candidate["identity"].get(key) == current for key, current in current_identity.items())
    ]
    if not matches:
        raise TaskAuthorityUnavailable(
            "no open accepted task matches the exact project, Anchor home, project root, "
            "artifact root, target, repository provider, and trust domain"
        )
    if len(matches) > 1:
        bounded = sorted(item["identity"]["task_window_id"] for item in matches)[:10]
        raise TaskAuthorityUnavailable(
            "multiple open accepted tasks match the exact owner; close or explicitly adopt one: "
            + ", ".join(bounded)
        )
    record = matches[0]
    identity = record["identity"]
    prior_state = vars(session_state).copy()
    try:
        _restore_state(record, session_state)
        event = _append_event(
            path, identity["task_window_id"], "rebound",
            {"accepted_record_sha256": hashlib.sha256(_canonical(record).encode()).hexdigest()},
        )
    except TaskAuthorityUnavailable:
        vars(session_state).clear()
        vars(session_state).update(prior_state)
        raise
    except Exception as exc:
        vars(session_state).clear()
        vars(session_state).update(prior_state)
        raise TaskAuthorityUnavailable("accepted task authority could not be restored") from exc
    result = {
        "status": "rebound", "task_window_id": identity["task_window_id"],
        "record_id": record["record_id"], "rebind_event_id": event["event_id"],
        "event_created": event["created"], "obligations": record["obligations"],
        "repository_scope": record["task"]["repository_scope"],
    }
    result["diagnostics"] = inspect_task_authority(path)
    return result


def inspect_task_authority(path: str | Path) -> dict[str, Any]:
    """Return diagnostics derived only from verified durable records and events."""
    target = Path(path).expanduser()
    if not target.is_file():
        return {"schema_status": "uninitialized", "durable_windows": 0, "rebindings": 0, "closures": 0}
    connection = _connect(target, read_only=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accepted_task_records" not in tables:
            return {"schema_status": "uninitialized", "durable_windows": 0, "rebindings": 0, "closures": 0}
        _verify_schema(connection)
        rows = connection.execute(
            "SELECT task_window_id,record_id,project_id,target_root,execution_mode,accepted_at,record_json,record_sha256,created_at FROM accepted_task_records ORDER BY accepted_at,task_window_id"
        ).fetchall()
        for row in rows:
            _load_verified_record(row)
        events = connection.execute(
            "SELECT event_id,task_window_id,event_type,event_json,event_sha256,created_at "
            "FROM accepted_task_events"
        ).fetchall()
        for row in events:
            raw = row["event_json"]
            event = json.loads(raw)
            deterministic_id = "ate_" + hashlib.sha256(
                f"{row['task_window_id']}:{row['event_type']}".encode()
            ).hexdigest()
            duplicated = (
                event.get("event_id"), event.get("task_window_id"), event.get("event_type"),
                event.get("created_at"),
            )
            if (
                raw != _canonical(event)
                or hashlib.sha256(raw.encode()).hexdigest() != row["event_sha256"]
                or duplicated != (
                    row["event_id"], row["task_window_id"], row["event_type"], row["created_at"],
                )
                or row["event_id"] != deterministic_id
            ):
                raise TaskAuthorityUnavailable("accepted task event integrity mismatch")
        return {
            "schema_status": "ready", "durable_windows": len(rows),
            "rebindings": sum(row["event_type"] == "rebound" for row in events),
            "closures": sum(row["event_type"] == "closed" for row in events),
        }
    finally:
        connection.close()
