"""Fail-closed evidence-backed promotion for project-local memories.

Immutable machine attestations, governed owner-presence receipts, and promotion
events are the authority.  The ``memories.status`` column is only their atomic
retrieval projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DOMAIN = "memory_promotion"
SCHEMA_VERSION = 4
POLICY_VERSION = "verified-memory-promotion-v9"
POLICY = {
    "automatic_candidate_activation_enabled": True,
    "automatic_active_confirmation_enabled": True,
    "eligible_claim_classes": [
        "assessed_structured_learning_pytest_v1",
        "assessed_structured_learning_python_call_v1",
    ],
    "machine_claim_representations": ["PytestResultV1", "PythonCallResultV1"],
    "human_owner_provider_available": True,
    "human_authority_provider": "owner-presence-input-v2",
    "human_owner_transports": [
        "databricks-in-session-owner-assertion",
        "local-windows-owner-presence",
        "slack",
    ],
    "human_activation_receipts": 1,
    "human_confirmation_receipts": 1,
    "require_separate_human_confirmation": True,
    "human_owner_reactivation_from_quarantine_enabled": True,
    "minimum_activation_attestations": 1,
    "minimum_confirmation_attestations": 2,
    "require_distinct_confirmation_tasks": True,
    "require_distinct_confirmation_source_snapshots": True,
    "require_distinct_verifier_task": True,
    "require_current_source_snapshot": True,
    "require_no_contradictions": True,
    "legacy_terminal_records_are_authority": False,
}
POLICY_SHA256 = hashlib.sha256(
    json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
_LEGACY_POLICY_SHA256 = {
    "verified-memory-promotion-v8": (
        "47b59bc54cd49f1b5e9d017bfe4af6089171ad800e0487424170869afc317aab"
    ),
    "verified-memory-promotion-v7": (
        "a9ab28ee06037dbf57ccda7760a60b7b6d8e5224f4d3ae5dbf7a058a1557d0bf"
    ),
    "verified-memory-promotion-v6": (
        "91fdd32dbf61f45cdb7325ead551a06e110fb4276c2e48d39ca684eafbc54e01"
    ),
}

_V3_DDL = (
    "CREATE TABLE memory_promotion_attestations (attestation_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,claim_sha256 TEXT NOT NULL CHECK(length(claim_sha256)=64),project_id TEXT NOT NULL,trust_domain TEXT NOT NULL,terminal_record_id TEXT NOT NULL REFERENCES terminal_task_records(record_id) ON DELETE RESTRICT,terminal_record_sha256 TEXT NOT NULL CHECK(length(terminal_record_sha256)=64),check_command_sha256 TEXT NOT NULL CHECK(length(check_command_sha256)=64),check_result_sha256 TEXT NOT NULL CHECK(length(check_result_sha256)=64),artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),source_snapshot_sha256 TEXT NOT NULL CHECK(length(source_snapshot_sha256)=64),verifier_provider TEXT NOT NULL,verifier_version TEXT NOT NULL,derivation_id TEXT NOT NULL,evidence_identity TEXT NOT NULL UNIQUE,contradiction_status TEXT NOT NULL CHECK(contradiction_status IN ('clear','contradicted','unavailable')),payload_json TEXT NOT NULL,attested_at TEXT NOT NULL)",
    "CREATE INDEX idx_memory_promotion_attestations_memory ON memory_promotion_attestations(memory_id,attested_at,attestation_id)",
    "CREATE TRIGGER memory_promotion_attestations_no_update BEFORE UPDATE ON memory_promotion_attestations BEGIN SELECT RAISE(ABORT,'memory promotion attestations are immutable'); END",
    "CREATE TRIGGER memory_promotion_attestations_no_delete BEFORE DELETE ON memory_promotion_attestations BEGIN SELECT RAISE(ABORT,'memory promotion attestations are immutable'); END",
    "CREATE TABLE memory_promotion_decisions (decision_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,claim_sha256 TEXT NOT NULL CHECK(length(claim_sha256)=64),project_id TEXT NOT NULL,trust_domain TEXT NOT NULL,policy_version TEXT NOT NULL,policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256)=64),mode TEXT NOT NULL CHECK(mode='shadow'),outcome TEXT NOT NULL CHECK(outcome IN ('rejected','unavailable')),reason TEXT NOT NULL,attestation_ids_json TEXT NOT NULL,nomination_ids_json TEXT NOT NULL,decision_json TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,decided_at TEXT NOT NULL)",
    "CREATE INDEX idx_memory_promotion_decisions_memory ON memory_promotion_decisions(memory_id,decided_at,decision_id)",
    "CREATE TRIGGER memory_promotion_decisions_no_update BEFORE UPDATE ON memory_promotion_decisions BEGIN SELECT RAISE(ABORT,'memory promotion decisions are immutable'); END",
    "CREATE TRIGGER memory_promotion_decisions_no_delete BEFORE DELETE ON memory_promotion_decisions BEGIN SELECT RAISE(ABORT,'memory promotion decisions are immutable'); END",
    "CREATE TABLE memory_promotion_events (event_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,claim_sha256 TEXT NOT NULL CHECK(length(claim_sha256)=64),project_id TEXT NOT NULL,trust_domain TEXT NOT NULL,event_type TEXT NOT NULL CHECK(event_type IN ('activation','confirmation','withdrawal')),prior_event_id TEXT REFERENCES memory_promotion_events(event_id) ON DELETE RESTRICT,target_status TEXT NOT NULL CHECK(target_status IN ('active','confirmed','candidate','quarantined')),policy_version TEXT NOT NULL,policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256)=64),attestation_ids_json TEXT NOT NULL,event_json TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,CHECK((event_type='activation' AND target_status='active') OR (event_type='confirmation' AND prior_event_id IS NOT NULL AND target_status='confirmed') OR (event_type='withdrawal' AND prior_event_id IS NOT NULL AND target_status IN ('candidate','quarantined'))))",
    "CREATE INDEX idx_memory_promotion_events_memory ON memory_promotion_events(memory_id,created_at,event_id)",
    "CREATE TRIGGER memory_promotion_events_no_update BEFORE UPDATE ON memory_promotion_events BEGIN SELECT RAISE(ABORT,'memory promotion events are immutable'); END",
    "CREATE TRIGGER memory_promotion_events_no_delete BEFORE DELETE ON memory_promotion_events BEGIN SELECT RAISE(ABORT,'memory promotion events are immutable'); END",
)
_HUMAN_RECEIPT_DDL = (
    "CREATE TABLE memory_human_authority_receipts (receipt_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,claim_sha256 TEXT NOT NULL CHECK(length(claim_sha256)=64),project_id TEXT NOT NULL,trust_domain TEXT NOT NULL,transition TEXT NOT NULL CHECK(transition IN ('activation','confirmation')),target_status TEXT NOT NULL CHECK(target_status IN ('active','confirmed')),prior_event_id TEXT REFERENCES memory_promotion_events(event_id) ON DELETE RESTRICT,challenge_sha256 TEXT NOT NULL UNIQUE CHECK(length(challenge_sha256)=64),owner_user_id TEXT NOT NULL,transport TEXT NOT NULL,request_id TEXT NOT NULL UNIQUE,response_message_id TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,CHECK((transition='activation' AND target_status='active') OR (transition='confirmation' AND prior_event_id IS NOT NULL AND target_status='confirmed')))",
    "CREATE INDEX idx_memory_human_authority_receipts_memory ON memory_human_authority_receipts(memory_id,created_at,receipt_id)",
    "CREATE TRIGGER memory_human_authority_receipts_no_update BEFORE UPDATE ON memory_human_authority_receipts BEGIN SELECT RAISE(ABORT,'memory human authority receipts are immutable'); END",
    "CREATE TRIGGER memory_human_authority_receipts_no_delete BEFORE DELETE ON memory_human_authority_receipts BEGIN SELECT RAISE(ABORT,'memory human authority receipts are immutable'); END",
)
_DDL = (*_V3_DDL, *_HUMAN_RECEIPT_DDL)
SCHEMA_TEXT = ";\n".join(_DDL) + ";\n"
SCHEMA_SHA256 = hashlib.sha256(SCHEMA_TEXT.encode("utf-8")).hexdigest()
V3_SCHEMA_SHA256 = hashlib.sha256((";\n".join(_V3_DDL) + ";\n").encode("utf-8")).hexdigest()
_V1_DDL = _V3_DDL[:8]
V1_SCHEMA_SHA256 = hashlib.sha256((";\n".join(_V1_DDL) + ";\n").encode("utf-8")).hexdigest()
_V2_EVENT_DDL = (
    "CREATE TABLE memory_promotion_events (event_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE RESTRICT,claim_sha256 TEXT NOT NULL CHECK(length(claim_sha256)=64),project_id TEXT NOT NULL,trust_domain TEXT NOT NULL,event_type TEXT NOT NULL CHECK(event_type IN ('activation','withdrawal')),prior_event_id TEXT REFERENCES memory_promotion_events(event_id) ON DELETE RESTRICT,target_status TEXT NOT NULL CHECK(target_status IN ('active','candidate','quarantined')),policy_version TEXT NOT NULL,policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256)=64),attestation_ids_json TEXT NOT NULL,event_json TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,CHECK((event_type='activation' AND target_status='active') OR (event_type='withdrawal' AND prior_event_id IS NOT NULL AND target_status IN ('candidate','quarantined'))))",
    *_V3_DDL[-3:],
)
_V2_DDL = (*_V1_DDL, *_V2_EVENT_DDL)
V2_SCHEMA_SHA256 = hashlib.sha256((";\n".join(_V2_DDL) + ";\n").encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def _connection(path: str | Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    from odibi_anchor.codebase._sqlite_contention import connect_shared_memory

    resolved = Path(path).expanduser().resolve()
    if not read_only:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    target = f"{resolved.as_uri()}?mode=ro" if read_only else str(resolved)
    connection = connect_shared_memory(
        target,
        owner_key_kind="project_id,trust_domain",
        uri=read_only,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("memory promotion foreign keys disabled")
    if not read_only:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
    try:
        yield connection
    finally:
        connection.close()


def _expected_objects(*, version: int = SCHEMA_VERSION) -> list[tuple[Any, ...]]:
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
        statements = (
            _V1_DDL if version == 1 else _V2_DDL if version == 2
            else _V3_DDL if version == 3 else _DDL
        )
        for statement in statements:
            expected.execute(statement)
        names = [
            row[0]
            for row in expected.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'memory_promotion_%' "
                "OR name LIKE 'idx_memory_promotion_%' "
                "OR name LIKE 'memory_human_authority_%' "
                "OR name LIKE 'idx_memory_human_authority_%' ORDER BY name"
            )
        ]
        placeholders = ",".join("?" for _ in names)
        return [
            tuple(row)
            for row in expected.execute(
                f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({placeholders}) "
                "ORDER BY type,name",
                names,
            )
        ]
    finally:
        expected.close()


def _verify_schema(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    expected_sha256 = (
        V1_SCHEMA_SHA256 if version == 1 else V2_SCHEMA_SHA256 if version == 2
        else V3_SCHEMA_SHA256 if version == 3
        else SCHEMA_SHA256
    )
    schema_row = connection.execute(
        "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
    ).fetchone()
    if tuple(schema_row or ()) != (version, expected_sha256):
        raise RuntimeError("memory promotion schema version/checksum mismatch")
    expected = _expected_objects(version=version)
    names = [row[1] for row in expected]
    placeholders = ",".join("?" for _ in names)
    actual = [
        tuple(row)
        for row in connection.execute(
            f"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name IN ({placeholders}) "
            "ORDER BY type,name",
            names,
        )
    ]
    owned_tables = [
        "memory_promotion_attestations", "memory_promotion_decisions", "memory_promotion_events",
    ]
    if version >= 4:
        owned_tables.append("memory_human_authority_receipts")
    table_placeholders = ",".join("?" for _ in owned_tables)
    owned = {
        row[0]
        for row in connection.execute(
            f"SELECT name FROM sqlite_master WHERE tbl_name IN ({table_placeholders}) "
            "AND sql IS NOT NULL",
            owned_tables,
        )
    }
    if actual != expected or owned != set(names):
        raise RuntimeError("memory promotion schema checksum mismatch")


def initialize_schema(path: str | Path) -> dict[str, Any]:
    """Create, migrate, or exactly verify the additive promotion schema."""
    with _connection(path) as connection:
        foreign_keys_disabled = False
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS anchor_schema_versions (domain TEXT PRIMARY KEY, "
                "version INTEGER NOT NULL CHECK(version>0),schema_sha256 TEXT NOT NULL "
                "CHECK(length(schema_sha256)=64),applied_at TEXT NOT NULL)"
            )
            version = connection.execute(
                "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
            ).fetchone()
            supported_v1 = version is not None and tuple(version) == (1, V1_SCHEMA_SHA256)
            supported_v2 = version is not None and tuple(version) == (2, V2_SCHEMA_SHA256)
            supported_v3 = version is not None and tuple(version) == (3, V3_SCHEMA_SHA256)
            if (
                version is not None
                and not supported_v1
                and not supported_v2
                and not supported_v3
                and tuple(version) != (SCHEMA_VERSION, SCHEMA_SHA256)
            ):
                raise RuntimeError("memory promotion schema version/checksum mismatch")
            if version is None:
                for statement in _DDL:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO anchor_schema_versions VALUES(?,?,?,?)",
                    (DOMAIN, SCHEMA_VERSION, SCHEMA_SHA256, _now()),
                )
            elif supported_v1 or supported_v2 or supported_v3:
                prior_version = 1 if supported_v1 else 2 if supported_v2 else 3
                _verify_schema(connection, version=prior_version)
                connection.commit()
                from odibi_anchor.codebase._migration_backup import ensure_migration_backup

                ensure_migration_backup(
                    path, tag=f"memory-promotion-v{SCHEMA_VERSION}",
                    error_prefix="memory promotion migration",
                )
                if supported_v2:
                    connection.execute("PRAGMA foreign_keys=OFF")
                    foreign_keys_disabled = True
                connection.execute("BEGIN IMMEDIATE")
                _verify_schema(connection, version=prior_version)
                if supported_v1:
                    for statement in _V3_DDL[-4:]:
                        connection.execute(statement)
                elif supported_v2:
                    connection.execute("DROP TRIGGER memory_promotion_events_no_delete")
                    connection.execute("DROP TRIGGER memory_promotion_events_no_update")
                    connection.execute("DROP INDEX idx_memory_promotion_events_memory")
                    connection.execute(
                        "ALTER TABLE memory_promotion_events RENAME TO memory_promotion_events_v2"
                    )
                    for statement in _V3_DDL[-4:]:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO memory_promotion_events SELECT * FROM "
                        "memory_promotion_events_v2 ORDER BY created_at,event_id"
                    )
                    connection.execute("DROP TABLE memory_promotion_events_v2")
                for statement in _HUMAN_RECEIPT_DDL:
                    connection.execute(statement)
                connection.execute(
                    "UPDATE anchor_schema_versions SET version=?,schema_sha256=?,applied_at=? "
                    "WHERE domain=?",
                    (SCHEMA_VERSION, SCHEMA_SHA256, _now(), DOMAIN),
                )
            _verify_schema(connection)
            if foreign_keys_disabled and connection.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("memory promotion migration introduced foreign key violations")
            connection.commit()
            if foreign_keys_disabled:
                connection.execute("PRAGMA foreign_keys=ON")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise RuntimeError("memory promotion migration failed to restore foreign keys")
        except Exception:
            connection.rollback()
            raise
    return {"domain": DOMAIN, "version": SCHEMA_VERSION, "schema_sha256": SCHEMA_SHA256}


def _verified_attestation(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    if row["payload_json"] != _json(payload):
        raise RuntimeError("memory promotion attestation is not canonical JSON")
    indexed = {
        "attestation_id": row["attestation_id"],
        "memory_id": row["memory_id"],
        "claim_sha256": row["claim_sha256"],
        "project_id": row["project_id"],
        "trust_domain": row["trust_domain"],
        "terminal_record_id": row["terminal_record_id"],
        "terminal_record_sha256": row["terminal_record_sha256"],
        "check_command_sha256": row["check_command_sha256"],
        "check_result_sha256": row["check_result_sha256"],
        "artifact_sha256": row["artifact_sha256"],
        "source_snapshot_sha256": row["source_snapshot_sha256"],
        "verifier_provider": row["verifier_provider"],
        "verifier_version": row["verifier_version"],
        "derivation_id": row["derivation_id"],
        "evidence_identity": row["evidence_identity"],
        "contradiction_status": row["contradiction_status"],
        "attested_at": row["attested_at"],
    }
    if any(payload.get(key) != value for key, value in indexed.items()):
        raise RuntimeError("memory promotion attestation indexed column mismatch")
    identity = {
        "run_id": payload.get("run_id"),
        "terminal_record_id": payload.get("terminal_record_id"),
        "terminal_record_sha256": payload.get("terminal_record_sha256"),
        "evidence_identity": payload.get("evidence_identity"),
    }
    if (
        payload.get("format") != "odibi-anchor-memory-promotion-attestation-v1"
        or payload.get("authority_mutation") != "none"
        or payload.get("attestation_id") != "mpa_" + _sha256(identity)
    ):
        raise RuntimeError("memory promotion attestation derived identity mismatch")
    return payload


def _verified_human_receipt(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    if row["payload_json"] != _json(payload):
        raise RuntimeError("memory human authority receipt is not canonical JSON")
    indexed = {
        "receipt_id": row["receipt_id"],
        "memory_id": row["memory_id"],
        "claim_sha256": row["claim_sha256"],
        "project_id": row["project_id"],
        "trust_domain": row["trust_domain"],
        "transition": row["transition"],
        "target_status": row["target_status"],
        "prior_event_id": row["prior_event_id"],
        "challenge_sha256": row["challenge_sha256"],
        "owner_user_id": row["owner_user_id"],
        "transport": row["transport"],
        "request_id": row["request_id"],
        "response_message_id": row["response_message_id"],
        "created_at": row["created_at"],
    }
    if any(payload.get(key) != value for key, value in indexed.items()):
        raise RuntimeError("memory human authority receipt indexed column mismatch")
    subject = payload.get("subject")
    owner = payload.get("owner")
    request = payload.get("request")
    policy_version = payload.get("policy_version")
    policy_sha256 = payload.get("policy_sha256")
    expected_policy_sha256 = (
        POLICY_SHA256 if policy_version == POLICY_VERSION
        else _LEGACY_POLICY_SHA256.get(policy_version)
    )
    if (
        payload.get("format") != "odibi-anchor-memory-human-authority-receipt-v1"
        or not isinstance(subject, dict)
        or row["challenge_sha256"] != _sha256(subject)
        or subject.get("memory_id") != row["memory_id"]
        or subject.get("claim_sha256") != row["claim_sha256"]
        or subject.get("project_id") != row["project_id"]
        or subject.get("trust_domain") != row["trust_domain"]
        or subject.get("transition") != row["transition"]
        or subject.get("target_status") != row["target_status"]
        or subject.get("prior_event_id") != row["prior_event_id"]
        or subject.get("policy_version") != payload.get("policy_version")
        or subject.get("policy_sha256") != payload.get("policy_sha256")
        or expected_policy_sha256 is None
        or policy_sha256 != expected_policy_sha256
        or not isinstance(owner, dict)
        or owner.get("user_id") != row["owner_user_id"]
        or owner.get("transport") != row["transport"]
        or set(owner) != {"user_id", "transport", "transport_ref_sha256"}
        or not isinstance(request, dict)
        or request.get("request_id") != row["request_id"]
        or request.get("response_message_id") != row["response_message_id"]
    ):
        raise RuntimeError("memory human authority receipt derived identity mismatch")
    if policy_version == POLICY_VERSION:
        expected_assurance = {
            "databricks-in-session-owner-assertion": (
                "lower_assurance_single_user_databricks_in_session_assertion"
            ),
            "local-windows-owner-presence": "interactive_local_windows_account_presence",
            "slack": "remote_authenticated_slack_identity",
        }.get(row["transport"])
        if payload.get("owner_assurance") != expected_assurance or expected_assurance is None:
            raise RuntimeError("memory human authority receipt provider assurance mismatch")
    identity = {
        "challenge_sha256": row["challenge_sha256"],
        "request_id": row["request_id"],
        "response_message_id": row["response_message_id"],
    }
    if row["receipt_id"] != "mhr_" + _sha256(identity):
        raise RuntimeError("memory human authority receipt identity mismatch")
    return payload


def _claim(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return {
            "format": "odibi-anchor-memory-claim-v1",
            "memory_id": row["id"],
            "project_id": row["project"],
            "type": row["type"],
            "content": row["content"],
            "related_files": json.loads(row["related_files"]),
            "tags": json.loads(row["tags"]),
            "source": row["source"],
            "evidence": json.loads(row["evidence"]),
        }
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("memory claim contains invalid JSON") from exc


def _activation_enabled() -> bool:
    return os.environ.get("ANCHOR_MEMORY_CANDIDATE_ACTIVATION", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _confirmation_enabled() -> bool:
    return os.environ.get("ANCHOR_MEMORY_ACTIVE_CONFIRMATION", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _verified_event(
    row: sqlite3.Row, connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    payload = json.loads(row["event_json"])
    if row["event_json"] != _json(payload):
        raise RuntimeError("memory promotion event is not canonical JSON")
    indexed = {
        "event_id": row["event_id"],
        "memory_id": row["memory_id"],
        "claim_sha256": row["claim_sha256"],
        "project_id": row["project_id"],
        "trust_domain": row["trust_domain"],
        "event_type": row["event_type"],
        "prior_event_id": row["prior_event_id"],
        "target_status": row["target_status"],
        "policy_version": row["policy_version"],
        "policy_sha256": row["policy_sha256"],
        "attestation_ids": json.loads(row["attestation_ids_json"]),
        "created_at": row["created_at"],
    }
    if any(payload.get(key) != value for key, value in indexed.items()):
        raise RuntimeError("memory promotion event indexed column mismatch")
    identity = {
        "memory_id": row["memory_id"],
        "claim_sha256": row["claim_sha256"],
        "event_type": row["event_type"],
        "prior_event_id": row["prior_event_id"],
        "target_status": row["target_status"],
        "policy_sha256": row["policy_sha256"],
        "attestation_ids": indexed["attestation_ids"],
    }
    if row["policy_version"] in {
        "verified-memory-promotion-v3", "verified-memory-promotion-v4",
        "verified-memory-promotion-v5", "verified-memory-promotion-v6",
        "verified-memory-promotion-v7", POLICY_VERSION,
    }:
        identity.update({
            "authority_lane": payload.get("authority_lane"),
            "actor_provider": payload.get("actor_provider"),
        })
        allowed_lanes = {"machine_verifier", "safety_withdrawal"}
        allowed_providers = {
            "odibi-anchor-pytest", "odibi-anchor-python-call",
            "odibi-anchor-runtime",
        }
        if row["policy_version"] in {
            "verified-memory-promotion-v6", "verified-memory-promotion-v7", POLICY_VERSION,
        }:
            allowed_lanes.add("human_owner")
            allowed_providers.add("odibi-anchor-human-input")
        if identity["authority_lane"] not in allowed_lanes:
            raise RuntimeError("memory promotion event authority lane mismatch")
        if identity["actor_provider"] not in allowed_providers:
            raise RuntimeError("memory promotion event actor/provider mismatch")
    idempotency_key = _sha256(identity)
    if (
        payload.get("format") != "odibi-anchor-memory-promotion-event-v1"
        or row["idempotency_key"] != idempotency_key
        or row["event_id"] != "mpe_" + idempotency_key
    ):
        raise RuntimeError("memory promotion event derived identity mismatch")
    if connection is not None and row["prior_event_id"] is not None:
        prior = connection.execute(
            "SELECT memory_id,claim_sha256,project_id,trust_domain,created_at "
            "FROM memory_promotion_events WHERE event_id=?",
            (row["prior_event_id"],),
        ).fetchone()
        if (
            prior is None
            or tuple(prior[:4]) != (
                row["memory_id"], row["claim_sha256"], row["project_id"], row["trust_domain"],
            )
            or prior["created_at"] > row["created_at"]
        ):
            raise RuntimeError("memory promotion event lineage mismatch")
    return payload


def _latest_event(connection: sqlite3.Connection, memory_id: str) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM memory_promotion_events WHERE memory_id=? "
        "ORDER BY created_at DESC,event_id DESC LIMIT 1",
        (memory_id,),
    ).fetchone()
    if row is not None:
        _verified_event(row, connection)
    return row


def _append_event(
    connection: sqlite3.Connection, *, memory_id: str, claim_sha256: str,
    project_id: str, event_type: str, prior_event_id: str | None,
    target_status: str, attestation_ids: list[str],
    authority_lane: str | None = None, actor_provider: str | None = None,
) -> tuple[dict[str, Any], bool]:
    if event_type == "withdrawal":
        resolved_lane = "safety_withdrawal"
        resolved_provider = "odibi-anchor-runtime"
    elif authority_lane == "human_owner":
        if actor_provider != "odibi-anchor-human-input" or len(attestation_ids) != 1:
            raise RuntimeError("human promotion event requires one governed owner receipt")
        receipt = connection.execute(
            "SELECT * FROM memory_human_authority_receipts WHERE receipt_id=?",
            (attestation_ids[0],),
        ).fetchone()
        if receipt is None:
            raise RuntimeError("human promotion event receipt is unavailable")
        verified_receipt = _verified_human_receipt(receipt)
        if (
            verified_receipt["memory_id"] != memory_id
            or verified_receipt["claim_sha256"] != claim_sha256
            or verified_receipt["project_id"] != project_id
            or verified_receipt["transition"] != event_type
            or verified_receipt["target_status"] != target_status
            or verified_receipt["prior_event_id"] != prior_event_id
        ):
            raise RuntimeError("human promotion event diverges from owner receipt")
        resolved_lane = "human_owner"
        resolved_provider = "odibi-anchor-human-input"
    else:
        placeholders = ",".join("?" for _ in attestation_ids)
        providers = {
            row[0] for row in connection.execute(
                f"SELECT verifier_provider FROM memory_promotion_attestations "
                f"WHERE attestation_id IN ({placeholders})",
                attestation_ids,
            )
        } if attestation_ids else set()
        schema_version = connection.execute(
            "SELECT version FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
        ).fetchone()
        if not providers and tuple(schema_version or ()) in {(1,), (2,)}:
            providers = {"odibi-anchor-pytest"}
        if len(providers) != 1:
            raise RuntimeError("promotion event requires one consistent verifier provider")
        resolved_lane = "machine_verifier"
        resolved_provider = providers.pop()
    identity = {
        "memory_id": memory_id,
        "claim_sha256": claim_sha256,
        "event_type": event_type,
        "prior_event_id": prior_event_id,
        "target_status": target_status,
        "policy_sha256": POLICY_SHA256,
        "attestation_ids": attestation_ids,
        "authority_lane": resolved_lane,
        "actor_provider": resolved_provider,
    }
    idempotency_key = _sha256(identity)
    event_id = "mpe_" + idempotency_key
    existing = connection.execute(
        "SELECT * FROM memory_promotion_events WHERE event_id=?", (event_id,),
    ).fetchone()
    if existing is not None:
        return _verified_event(existing, connection), False
    created_at = _now()
    if prior_event_id is not None:
        prior_created = connection.execute(
            "SELECT created_at FROM memory_promotion_events WHERE event_id=?", (prior_event_id,),
        ).fetchone()
        if prior_created is None:
            raise RuntimeError("promotion event prior authority is unavailable")
        if created_at <= prior_created[0]:
            created_at = (
                datetime.fromisoformat(prior_created[0].replace("Z", "+00:00"))
                + timedelta(microseconds=1)
            ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    payload = {
        "format": "odibi-anchor-memory-promotion-event-v1",
        "event_id": event_id,
        **identity,
        "project_id": project_id,
        "trust_domain": project_id,
        "policy_version": POLICY_VERSION,
        "created_at": created_at,
    }
    raw = _json(payload)
    connection.execute(
        "INSERT INTO memory_promotion_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id, memory_id, claim_sha256, project_id, project_id, event_type,
            prior_event_id, target_status, POLICY_VERSION, POLICY_SHA256,
            _json(attestation_ids), raw, idempotency_key, created_at,
        ),
    )
    persisted = connection.execute(
        "SELECT * FROM memory_promotion_events WHERE event_id=?", (event_id,),
    ).fetchone()
    if persisted is None:
        raise RuntimeError("memory promotion event was not persisted")
    return _verified_event(persisted, connection), True


def _activate_candidate(
    connection: sqlite3.Connection, *, memory_id: str, project_id: str,
) -> dict[str, Any]:
    """Atomically derive one activation event and its retrieval projection."""
    memory = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if memory is None:
        raise ValueError("memory entry not found")
    if memory["project"] != project_id:
        raise ValueError("memory belongs to a different project/trust boundary")
    claim_sha256 = _sha256(_claim(memory))
    latest = _latest_event(connection, memory_id)
    if memory["status"] in {"active", "confirmed"}:
        expected_event = "activation" if memory["status"] == "active" else "confirmation"
        if latest is None or latest["event_type"] != expected_event:
            raise RuntimeError("promoted memory has no effective promotion event authority")
        return {
            "status": memory["status"], "event": _verified_event(latest, connection),
            "write_performed": False,
        }
    if latest is not None and latest["event_type"] == "activation":
        if latest["claim_sha256"] != claim_sha256:
            raise RuntimeError("memory activation projection or claim diverged from authority")
        if memory["status"] in {"quarantined", "retired", "stale", "rejected", "superseded"}:
            return {
                "status": "externally_deactivated",
                "event": _verified_event(latest, connection),
                "write_performed": False,
            }
        if memory["status"] != "candidate":
            raise RuntimeError("memory activation projection diverged from authority")
        connection.execute("UPDATE memories SET status='active' WHERE id=?", (memory_id,))
        return {
            "status": "projection_repaired", "event": _verified_event(latest, connection),
            "write_performed": True,
        }
    if memory["status"] != "candidate":
        if latest is not None and latest["event_type"] == "withdrawal":
            return {
                "status": "withdrawn", "event": _verified_event(latest, connection),
                "write_performed": False,
            }
        raise ValueError("candidate activation requires candidate lifecycle state")
    from odibi_anchor.codebase._memory_verifier import _contradiction_status

    if _contradiction_status(connection, memory_id) != "clear":
        raise ValueError("candidate activation requires clear current contradiction status")
    rows = connection.execute(
        "SELECT * FROM memory_promotion_attestations WHERE memory_id=? AND claim_sha256=? "
        "AND project_id=? ORDER BY attested_at,attestation_id",
        (memory_id, claim_sha256, project_id),
    ).fetchall()
    attestations = [_verified_attestation(row) for row in rows]
    if len(attestations) < POLICY["minimum_activation_attestations"]:
        raise ValueError("candidate activation requires a verifier-bound attestation")
    if any(item["contradiction_status"] != "clear" for item in attestations):
        raise ValueError("candidate activation rejects contradicted attestation evidence")
    attestation_ids = [item["attestation_id"] for item in attestations]
    if (
        latest is not None
        and latest["event_type"] == "withdrawal"
        and json.loads(latest["attestation_ids_json"]) == attestation_ids
    ):
        return {
            "status": "withdrawn", "event": _verified_event(latest, connection),
            "write_performed": False,
        }
    event, created = _append_event(
        connection, memory_id=memory_id, claim_sha256=claim_sha256,
        project_id=project_id, event_type="activation",
        prior_event_id=(latest["event_id"] if latest is not None else None),
        target_status="active", attestation_ids=attestation_ids,
    )
    connection.execute(
        "UPDATE memories SET status='active' WHERE id=? AND status='candidate'",
        (memory_id,),
    )
    projected = connection.execute("SELECT status FROM memories WHERE id=?", (memory_id,)).fetchone()
    if projected is None or projected[0] != "active":
        raise RuntimeError("memory activation projection failed")
    return {"status": "activated", "event": event, "write_performed": created}


def _confirm_active(
    connection: sqlite3.Connection, *, memory_id: str, project_id: str,
) -> dict[str, Any]:
    """Confirm an active memory only from stronger, distinct verifier attestations."""
    memory = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if memory is None or memory["project"] != project_id:
        raise ValueError("memory belongs to a different project/trust boundary")
    latest = _latest_event(connection, memory_id)
    claim_sha256 = _sha256(_claim(memory))
    if memory["status"] == "confirmed":
        if latest is None or latest["event_type"] != "confirmation":
            raise RuntimeError("confirmed memory has no effective confirmation event authority")
        return {
            "status": "confirmed", "event": _verified_event(latest, connection),
            "write_performed": False,
        }
    if memory["status"] != "active" or latest is None or latest["event_type"] != "activation":
        return {
            "status": "ineligible", "reason": "memory_is_not_effectively_active",
            "write_performed": False,
        }
    if latest["claim_sha256"] != claim_sha256:
        raise RuntimeError("memory activation projection or claim diverged from authority")
    from odibi_anchor.codebase._memory_verifier import _contradiction_status

    if _contradiction_status(connection, memory_id) != "clear":
        return {
            "status": "pending", "reason": "current_contradiction_not_clear",
            "write_performed": False,
        }
    rows = connection.execute(
        "SELECT * FROM memory_promotion_attestations WHERE memory_id=? AND claim_sha256=? "
        "AND project_id=? ORDER BY attested_at,attestation_id",
        (memory_id, claim_sha256, project_id),
    ).fetchall()
    attestations = [_verified_attestation(row) for row in rows]
    terminal_ids = {item["terminal_record_id"] for item in attestations}
    source_snapshots = {item["source_snapshot_sha256"] for item in attestations}
    if (
        len(attestations) < POLICY["minimum_confirmation_attestations"]
        or len(terminal_ids) < POLICY["minimum_confirmation_attestations"]
        or len(source_snapshots) < POLICY["minimum_confirmation_attestations"]
    ):
        return {
            "status": "pending", "reason": "insufficient_distinct_verifier_evidence",
            "write_performed": False,
        }
    if any(item["contradiction_status"] != "clear" for item in attestations):
        return {
            "status": "pending", "reason": "attestation_contradiction_not_clear",
            "write_performed": False,
        }
    attestation_ids = [item["attestation_id"] for item in attestations]
    event, created = _append_event(
        connection, memory_id=memory_id, claim_sha256=claim_sha256,
        project_id=project_id, event_type="confirmation",
        prior_event_id=latest["event_id"], target_status="confirmed",
        attestation_ids=attestation_ids,
    )
    connection.execute(
        "UPDATE memories SET status='confirmed' WHERE id=? AND status='active'", (memory_id,),
    )
    projected = connection.execute("SELECT status FROM memories WHERE id=?", (memory_id,)).fetchone()
    if projected is None or projected[0] != "confirmed":
        raise RuntimeError("memory confirmation projection failed")
    return {"status": "confirmed", "event": event, "write_performed": created}


def _owner_subject(
    connection: sqlite3.Connection, *, memory_id: str, project_id: str, transition: str,
) -> tuple[dict[str, Any], sqlite3.Row]:
    memory = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if memory is None or memory["project"] != project_id:
        raise ValueError("memory belongs to a different project/trust boundary")
    claim_sha256 = _sha256(_claim(memory))
    latest = _latest_event(connection, memory_id)
    if transition == "activation":
        if memory["status"] not in {"candidate", "quarantined"}:
            raise ValueError("owner activation requires candidate or quarantined lifecycle state")
        if latest is not None and latest["event_type"] != "withdrawal":
            raise ValueError("owner activation requires no effective promotion authority")
        if (
            memory["status"] == "quarantined"
            and latest is not None
            and latest["target_status"] != "quarantined"
        ):
            raise ValueError("owner reactivation requires an effective quarantine withdrawal")
        target_status = "active"
    elif transition == "confirmation":
        if (
            memory["status"] != "active"
            or latest is None
            or latest["event_type"] != "activation"
            or latest["claim_sha256"] != claim_sha256
        ):
            raise ValueError("owner confirmation requires effective active lifecycle state")
        target_status = "confirmed"
    else:
        raise ValueError("owner promotion transition must be activation or confirmation")
    subject = {
        "format": "odibi-anchor-memory-human-authority-subject-v1",
        "memory_id": memory_id,
        "claim_sha256": claim_sha256,
        "project_id": project_id,
        "trust_domain": project_id,
        "memory_type": memory["type"],
        "content": memory["content"],
        "source": memory["source"],
        "current_status": memory["status"],
        "transition": transition,
        "target_status": target_status,
        "prior_event_id": latest["event_id"] if latest is not None else None,
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
    }
    return subject, memory


def _existing_owner_transition(
    connection: sqlite3.Connection, *, memory_id: str, project_id: str, transition: str,
) -> dict[str, Any] | None:
    memory = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if memory is None or memory["project"] != project_id:
        raise ValueError("memory belongs to a different project/trust boundary")
    expected_status = "active" if transition == "activation" else "confirmed"
    latest = _latest_event(connection, memory_id)
    if memory["status"] != expected_status or latest is None or latest["event_type"] != transition:
        return None
    event = _verified_event(latest, connection)
    if event.get("authority_lane") != "human_owner" or len(event["attestation_ids"]) != 1:
        return None
    receipt = connection.execute(
        "SELECT * FROM memory_human_authority_receipts WHERE receipt_id=?",
        (event["attestation_ids"][0],),
    ).fetchone()
    if receipt is None:
        raise RuntimeError("human promotion event has no owner receipt")
    return {
        "kind": "memory_human_authority",
        "status": "existing",
        "transition": transition,
        "target_status": expected_status,
        "receipt": _verified_human_receipt(receipt),
        "event": event,
        "write_performed": False,
    }


def request_owner_promotion(
    path: str | Path, *, memory_id: str, project_id: str, transition: str,
    timeout_minutes: float = 60, provider: str | None = None,
    in_session_approval: str | None = None,
) -> dict[str, Any]:
    """Request exact owner authority through the selected human-presence provider."""
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise ValueError("memory_id must be a non-empty string")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    if transition not in {"activation", "confirmation"}:
        raise ValueError("owner promotion transition must be activation or confirmation")
    from odibi_anchor.human_input_owner import (
        databricks_in_session_preparation_available,
        select_owner_approval_provider,
    )

    prepare_databricks = (
        in_session_approval is None
        and databricks_in_session_preparation_available(provider=provider)
    )
    selected_provider = None if prepare_databricks else select_owner_approval_provider(
        provider=provider, in_session_approval=in_session_approval,
    )
    initialize_schema(path)
    with _connection(path, read_only=True) as connection:
        _verify_schema(connection)
        existing = _existing_owner_transition(
            connection, memory_id=memory_id, project_id=project_id, transition=transition,
        )
        if existing is not None:
            return existing
        subject, _memory = _owner_subject(
            connection, memory_id=memory_id, project_id=project_id, transition=transition,
        )
    if transition == "activation" and not _activation_enabled():
        raise ValueError("candidate activation is disabled")
    if transition == "confirmation" and not _confirmation_enabled():
        raise ValueError("active confirmation is disabled")
    challenge = _sha256(subject)
    expected_response = f"APPROVE {challenge}"
    request_message = (
        "Odibi Anchor memory authority request. Review the exact project-local claim and "
        f"transition below.\n\nProject: {project_id}\nTransition: {transition}\n"
        f"Type: {subject['memory_type']}\nSource: {subject['source']}\n"
        f"Claim: {subject['content']}\n\nReply exactly: {expected_response}"
    )
    if prepare_databricks:
        return {
            "kind": "memory_human_authority",
            "status": "approval_required",
            "transition": transition,
            "target_status": subject["target_status"],
            "challenge_sha256": challenge,
            "approval_response": expected_response,
            "approval_prompt": request_message,
            "provider": "databricks_in_session",
            "owner_assurance": (
                "lower_assurance_single_user_databricks_in_session_assertion"
            ),
            "warning": (
                "No owner identity was authenticated and no authority was granted. The owner must "
                "personally send the exact approval_response in this Genie Code session before "
                "the command is rerun with provider='databricks_in_session' and "
                "in_session_approval."
            ),
            "write_performed": False,
        }
    assert selected_provider is not None
    from odibi_anchor.human_input import request_human_input_record

    request = request_human_input_record(
        request_message,
        timeout_minutes=timeout_minutes,
        transport=selected_provider.transport,
    )
    if (
        request.response != expected_response
        or request.response_user_id != selected_provider.expected_owner_id
        or request.transport != selected_provider.transport.name
        or not request.response_message_id
        or not request.request_id
        or not request.transport
    ):
        raise ValueError("owner promotion response text, identity, or provider differs")
    created_at = _now()
    receipt_identity = {
        "challenge_sha256": challenge,
        "request_id": request.request_id,
        "response_message_id": request.response_message_id,
    }
    receipt_id = "mhr_" + _sha256(receipt_identity)
    receipt = {
        "format": "odibi-anchor-memory-human-authority-receipt-v1",
        "receipt_id": receipt_id,
        "memory_id": memory_id,
        "claim_sha256": subject["claim_sha256"],
        "project_id": project_id,
        "trust_domain": project_id,
        "transition": transition,
        "target_status": subject["target_status"],
        "prior_event_id": subject["prior_event_id"],
        "challenge_sha256": challenge,
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
        "subject": subject,
        "owner_user_id": request.response_user_id,
        "transport": request.transport,
        "owner_assurance": selected_provider.assurance,
        "request_id": request.request_id,
        "response_message_id": request.response_message_id,
        "owner": {
            "user_id": request.response_user_id,
            "transport": request.transport,
            "transport_ref_sha256": hashlib.sha256(
                (request.transport_ref or "").encode("utf-8")
            ).hexdigest(),
        },
        "request": {
            "request_id": request.request_id,
            "message_sha256": hashlib.sha256(request.message.encode("utf-8")).hexdigest(),
            "response_sha256": hashlib.sha256(request.response.encode("utf-8")).hexdigest(),
            "response_message_id": request.response_message_id,
            "created_at": request.created_at,
            "delivered_at": request.delivered_at,
            "responded_at": request.responded_at,
        },
        "created_at": created_at,
    }
    with _connection(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _verify_schema(connection)
            current_subject, _memory = _owner_subject(
                connection, memory_id=memory_id, project_id=project_id, transition=transition,
            )
            if current_subject != subject or _sha256(current_subject) != challenge:
                raise ValueError("memory claim or lifecycle changed after owner approval")
            existing_receipt = connection.execute(
                "SELECT * FROM memory_human_authority_receipts WHERE challenge_sha256=?",
                (challenge,),
            ).fetchone()
            if existing_receipt is not None:
                retained = _verified_human_receipt(existing_receipt)
                if retained != receipt:
                    raise RuntimeError("a different owner receipt already exists for challenge")
            else:
                connection.execute(
                    "INSERT INTO memory_human_authority_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id, memory_id, subject["claim_sha256"], project_id, project_id,
                        transition, subject["target_status"], subject["prior_event_id"], challenge,
                        request.response_user_id, request.transport, request.request_id,
                        request.response_message_id, _json(receipt), created_at,
                    ),
                )
            event, event_created = _append_event(
                connection, memory_id=memory_id, claim_sha256=subject["claim_sha256"],
                project_id=project_id, event_type=transition,
                prior_event_id=subject["prior_event_id"], target_status=subject["target_status"],
                attestation_ids=[receipt_id], authority_lane="human_owner",
                actor_provider="odibi-anchor-human-input",
            )
            changed = connection.execute(
                "UPDATE memories SET status=? WHERE id=? AND status=?",
                (subject["target_status"], memory_id, subject["current_status"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError("owner promotion lifecycle projection failed")
            persisted = connection.execute(
                "SELECT * FROM memory_human_authority_receipts WHERE receipt_id=?", (receipt_id,),
            ).fetchone()
            if persisted is None or _verified_human_receipt(persisted) != receipt:
                raise RuntimeError("owner promotion receipt was not persisted exactly")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "kind": "memory_human_authority",
        "status": "recorded",
        "transition": transition,
        "target_status": subject["target_status"],
        "receipt": receipt,
        "event": event,
        "write_performed": existing_receipt is None or event_created,
    }


def withdraw_memory_in_transaction(
    connection: sqlite3.Connection, *, memory_id: str, project_id: str,
    final_status: str, superseded_by: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Lower retrieval state, appending a withdrawal when promotion authority exists."""
    if final_status not in {"candidate", "quarantined", "rejected", "retired", "superseded"}:
        raise ValueError("invalid lowered memory lifecycle state")
    memory = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if memory is None or memory["project"] != project_id:
        raise ValueError("memory belongs to a different project/trust boundary")
    latest = None
    created = False
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_promotion_events'"
    )}
    if tables:
        _verify_schema(connection)
        latest = _latest_event(connection, memory_id)
    if memory["status"] in {"active", "confirmed"}:
        if latest is None or latest["event_type"] not in {"activation", "confirmation"}:
            raise RuntimeError("promoted memory has no effective promotion event authority")
        claim_sha256 = _sha256(_claim(memory))
        if latest["claim_sha256"] != claim_sha256:
            raise RuntimeError("memory activation projection or claim diverged from authority")
        event, created = _append_event(
            connection, memory_id=memory_id, claim_sha256=claim_sha256,
            project_id=project_id, event_type="withdrawal",
            prior_event_id=latest["event_id"],
            target_status="candidate" if final_status == "candidate" else "quarantined",
            attestation_ids=json.loads(latest["attestation_ids_json"]),
        )
    elif latest is not None and latest["event_type"] in {"activation", "confirmation"}:
        raise RuntimeError("memory lifecycle projection diverged from promotion authority")
    elif latest is not None and latest["event_type"] == "withdrawal":
        event = _verified_event(latest, connection)
    else:
        event = None
    connection.execute(
        "UPDATE memories SET status=?,superseded_by=? WHERE id=?",
        (final_status, superseded_by, memory_id),
    )
    return event, created


def withdraw_candidate_activation(
    path: str | Path, *, memory_id: str, project_id: str, quarantine: bool = False,
) -> dict[str, Any]:
    """Append a safe rollback event and atomically lower the retrieval projection."""
    if type(quarantine) is not bool:
        raise TypeError("quarantine must be a bool")
    initialize_schema(path)
    target_status = "quarantined" if quarantine else "candidate"
    with _connection(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _verify_schema(connection)
            latest = _latest_event(connection, memory_id)
            if (
                latest is not None
                and latest["event_type"] == "withdrawal"
                and latest["target_status"] != target_status
            ):
                raise ValueError("activation already withdrawn to a different lifecycle state")
            event, created = withdraw_memory_in_transaction(
                connection, memory_id=memory_id, project_id=project_id,
                final_status=target_status,
            )
            if event is None:
                raise ValueError("memory has no promotion event to withdraw")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "kind": "memory_promotion_withdrawal",
        "status": "recorded" if created else "existing",
        "event": event, "write_performed": created,
    }


def _legacy_nominations(
    connection: sqlite3.Connection, *, memory_id: str, project_id: str,
) -> list[dict[str, str]]:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {
        "memory_selections", "memory_applications", "memory_evaluations", "terminal_task_records",
    }
    if not required.issubset(tables):
        return []
    rows = connection.execute(
        "SELECT e.evaluation_id,e.outcome,t.record_id,t.record_sha256,t.task_window_id "
        "FROM memory_evaluations e JOIN memory_applications a ON a.application_id=e.application_id "
        "JOIN memory_selections s ON s.selection_id=a.selection_id "
        "JOIN terminal_task_records t ON t.task_window_id=a.task_window_id "
        "WHERE s.memory_id=? AND t.project_id=? "
        "ORDER BY t.ended_at,t.record_id,e.evaluation_id",
        (memory_id, project_id),
    ).fetchall()
    return [
        {
            "evaluation_id": row["evaluation_id"],
            "outcome": row["outcome"],
            "terminal_record_id": row["record_id"],
            "terminal_record_sha256": row["record_sha256"],
            "task_window_id": row["task_window_id"],
            "authority": "legacy_unbound_nomination",
        }
        for row in rows
    ]


def evaluate_shadow_promotion(
    path: str | Path, *, memory_id: str, project_id: str,
) -> dict[str, Any]:
    """Append a fail-closed decision using only storage-derived evidence.

    Caller-authored attestations, verifier labels, claim digests, and policy data
    are intentionally not accepted.  Existing terminal records and evaluations
    are retained only as unbound nominations because they do not prove that a
    check evaluated this exact claim.
    """
    if os.environ.get("ANCHOR_MEMORY_PROMOTION_SHADOW", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return {
            "kind": "memory_promotion_shadow",
            "status": "disabled",
            "reason": "shadow_kill_switch",
            "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256,
            "write_performed": False,
        }
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise ValueError("memory_id must be a non-empty string")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    initialize_schema(path)
    with _connection(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _verify_schema(connection)
            row = connection.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                raise ValueError("memory entry not found")
            if row["project"] != project_id:
                raise ValueError("memory belongs to a different project/trust boundary")
            claim = _claim(row)
            claim_sha256 = _sha256(claim)
            nominations = _legacy_nominations(
                connection, memory_id=memory_id, project_id=project_id,
            )
            nomination_ids = [
                _sha256(
                    {
                        "evaluation_id": item["evaluation_id"],
                        "terminal_record_id": item["terminal_record_id"],
                        "terminal_record_sha256": item["terminal_record_sha256"],
                    }
                )
                for item in nominations
            ]
            attestations = connection.execute(
                "SELECT attestation_id FROM memory_promotion_attestations "
                "WHERE memory_id=? AND claim_sha256=? AND project_id=? "
                "ORDER BY attested_at,attestation_id",
                (memory_id, claim_sha256, project_id),
            ).fetchall()
            attestation_ids = [item[0] for item in attestations]
            if row["status"] not in {"candidate", "active"}:
                reason = "ineligible_memory_lifecycle"
            elif not attestation_ids:
                reason = "no_verifier_bound_attestations"
            elif row["status"] == "active":
                latest = _latest_event(connection, memory_id)
                if latest is None or latest["event_type"] != "activation":
                    raise RuntimeError("active memory has no effective promotion event authority")
                reason = "activation_event_exists"
            elif not _activation_enabled():
                reason = "candidate_activation_kill_switch"
            else:
                reason = "activation_finalization_pending"
            outcome = "rejected" if row["status"] == "candidate" else "unavailable"
            decision = {
                "format": "odibi-anchor-memory-promotion-decision-v1",
                "memory_id": memory_id,
                "claim_sha256": claim_sha256,
                "project_id": project_id,
                "trust_domain": project_id,
                "policy_version": POLICY_VERSION,
                "policy_sha256": POLICY_SHA256,
                "mode": "shadow",
                "outcome": outcome,
                "reason": reason,
                "attestation_ids": attestation_ids,
                "nomination_ids": nomination_ids,
                "legacy_nominations": nominations,
                "authority_mutation": "none",
            }
            idempotency_key = _sha256(
                {
                    "memory_id": memory_id,
                    "claim_sha256": claim_sha256,
                    "policy_sha256": POLICY_SHA256,
                    "attestation_ids": attestation_ids,
                    "nomination_ids": nomination_ids,
                    "reason": reason,
                }
            )
            decision_id = "mpd_" + idempotency_key
            payload = _json(decision)
            attestation_json = _json(attestation_ids)
            nomination_json = _json(nomination_ids)
            connection.execute(
                "INSERT OR IGNORE INTO memory_promotion_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, memory_id, claim_sha256, project_id, project_id,
                    POLICY_VERSION, POLICY_SHA256, "shadow", outcome, reason,
                    attestation_json, nomination_json, payload, idempotency_key, _now(),
                ),
            )
            persisted = connection.execute(
                "SELECT * FROM memory_promotion_decisions WHERE decision_id=?", (decision_id,),
            ).fetchone()
            expected = {
                "memory_id": memory_id,
                "claim_sha256": claim_sha256,
                "project_id": project_id,
                "trust_domain": project_id,
                "policy_version": POLICY_VERSION,
                "policy_sha256": POLICY_SHA256,
                "mode": "shadow",
                "outcome": outcome,
                "reason": reason,
                "attestation_ids_json": attestation_json,
                "nomination_ids_json": nomination_json,
                "decision_json": payload,
                "idempotency_key": idempotency_key,
            }
            if persisted is None or any(persisted[key] != value for key, value in expected.items()):
                raise RuntimeError("conflicting memory promotion decision")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "kind": "memory_promotion_shadow",
        "status": "recorded",
        "decision_id": decision_id,
        "decision": decision,
        "write_performed": True,
    }


def finalize_verifier_attestation(
    path: str | Path, *, run_id: str, project_id: str,
) -> dict[str, Any]:
    """Bind one successful verifier run to its later immutable terminal record."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("project_id must be a non-empty string")
    from odibi_anchor.codebase._memory_verifier import (
        _verify_schema as verify_verifier_schema,
    )
    from odibi_anchor.codebase._memory_verifier import (
        get_verifier_run,
        verify_current_source_snapshot,
    )
    from odibi_anchor.codebase._task_execution import inspect_terminal_records

    run = get_verifier_run(path, run_id=run_id, project_id=project_id)
    if run["result_state"] != "passed":
        raise ValueError("only a passed installed verifier run can produce an attestation")
    if run["contradiction_status"] != "clear":
        raise ValueError("contradicted or unavailable evidence cannot produce an attestation")
    terminal = inspect_terminal_records(
        path, task_window_id=run["task_window_id"], project_id=project_id,
    )["records"]
    if len(terminal) != 1 or terminal[0]["terminal_status"] != "completed":
        raise ValueError("verifier task has no completed immutable terminal record")
    record = terminal[0]
    if record["end_revision"] != run["source_head_sha"]:
        raise ValueError("verifier terminal revision does not match the tested source snapshot")
    verify_current_source_snapshot(
        path,
        task_window_id=run["task_window_id"],
        expected_sha256=run["source_snapshot_sha256"],
    )
    initialize_schema(path)
    with _connection(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _verify_schema(connection)
            verify_verifier_schema(connection)
            memory = connection.execute(
                "SELECT * FROM memories WHERE id=?", (run["memory_id"],),
            ).fetchone()
            if memory is None or memory["project"] != project_id:
                raise ValueError("memory belongs to a different project/trust boundary")
            claim_sha256 = _sha256(_claim(memory))
            if claim_sha256 != run["claim_sha256"]:
                raise ValueError("memory claim changed after verifier execution")
            conflicts = connection.execute(
                "SELECT result_state,contradiction_status FROM memory_verifier_runs "
                "WHERE evidence_identity=? AND run_id<>?",
                (run["evidence_identity"], run_id),
            ).fetchall()
            if any(
                row["result_state"] != "passed" or row["contradiction_status"] != "clear"
                for row in conflicts
            ):
                raise ValueError("conflicting verifier outcomes exist for this evidence identity")
            existing = connection.execute(
                "SELECT * FROM memory_promotion_attestations WHERE evidence_identity=?",
                (run["evidence_identity"],),
            ).fetchone()
            if existing is not None:
                payload = _verified_attestation(existing)
                if payload["run_id"] != run_id:
                    raise RuntimeError("conflicting memory promotion attestation")
                activation = (
                    _activate_candidate(
                        connection, memory_id=run["memory_id"], project_id=project_id,
                    )
                    if _activation_enabled() else
                    {"status": "disabled", "reason": "candidate_activation_kill_switch",
                     "write_performed": False}
                )
                confirmation = (
                    _confirm_active(
                        connection, memory_id=run["memory_id"], project_id=project_id,
                    )
                    if _confirmation_enabled() else
                    {"status": "disabled", "reason": "active_confirmation_kill_switch",
                     "write_performed": False}
                )
                connection.commit()
                return {
                    "kind": "memory_promotion_attestation",
                    "status": "existing",
                    "attestation": payload,
                    "activation": activation,
                    "confirmation": confirmation,
                }
            attested_at = _now()
            identity = {
                "run_id": run_id,
                "terminal_record_id": record["record_id"],
                "terminal_record_sha256": record["record_sha256"],
                "evidence_identity": run["evidence_identity"],
            }
            attestation_id = "mpa_" + _sha256(identity)
            payload = {
                "format": "odibi-anchor-memory-promotion-attestation-v1",
                "attestation_id": attestation_id,
                "run_id": run_id,
                "memory_id": run["memory_id"],
                "claim_sha256": claim_sha256,
                "project_id": project_id,
                "trust_domain": project_id,
                "terminal_record_id": record["record_id"],
                "terminal_record_sha256": record["record_sha256"],
                "check_command_sha256": run["check_command_sha256"],
                "check_result_sha256": run["check_result_sha256"],
                "artifact_sha256": run["artifact_sha256"],
                "source_snapshot_sha256": run["source_snapshot_sha256"],
                "verifier_provider": run["verifier_provider"],
                "verifier_version": run["verifier_version"],
                "derivation_id": run["derivation_id"],
                "evidence_identity": run["evidence_identity"],
                "contradiction_status": run["contradiction_status"],
                "attested_at": attested_at,
                "authority_mutation": "none",
            }
            raw = _json(payload)
            connection.execute(
                "INSERT INTO memory_promotion_attestations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attestation_id, run["memory_id"], claim_sha256, project_id, project_id,
                    record["record_id"], record["record_sha256"],
                    run["check_command_sha256"], run["check_result_sha256"],
                    run["artifact_sha256"], run["source_snapshot_sha256"],
                    run["verifier_provider"], run["verifier_version"], run["derivation_id"],
                    run["evidence_identity"], run["contradiction_status"], raw, attested_at,
                ),
            )
            activation = (
                _activate_candidate(
                    connection, memory_id=run["memory_id"], project_id=project_id,
                )
                if _activation_enabled() else
                {"status": "disabled", "reason": "candidate_activation_kill_switch",
                 "write_performed": False}
            )
            confirmation = (
                _confirm_active(
                    connection, memory_id=run["memory_id"], project_id=project_id,
                )
                if _confirmation_enabled() else
                {"status": "disabled", "reason": "active_confirmation_kill_switch",
                 "write_performed": False}
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "kind": "memory_promotion_attestation",
        "status": "recorded",
        "attestation": payload,
        "activation": activation,
        "confirmation": confirmation,
    }


def finalize_task_verifier_attestations(
    path: str | Path, *, task_window_id: str, project_id: str,
) -> dict[str, Any] | None:
    """Finalize every immutable verifier run when its task terminal is persisted."""
    target = Path(path).expanduser()
    if not target.is_file():
        return None
    with _connection(target, read_only=True) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "memory_verifier_runs" not in tables:
            return None
        from odibi_anchor.codebase._memory_verifier import _verify_schema as verify_verifier

        verify_verifier(connection)
        run_ids = [
            row[0] for row in connection.execute(
                "SELECT run_id FROM memory_verifier_runs "
                "WHERE task_window_id=? AND project_id=? AND result_state='passed' "
                "AND contradiction_status='clear' ORDER BY completed_at,run_id",
                (task_window_id, project_id),
            )
        ]
    if not run_ids:
        return None
    results = []
    for run_id in run_ids:
        try:
            results.append(
                finalize_verifier_attestation(path, run_id=run_id, project_id=project_id)
            )
        except ValueError as exc:
            results.append({
                "kind": "memory_promotion_attestation",
                "status": "not_attested",
                "run_id": run_id,
                "reason": str(exc),
                "authority_mutation": "none",
            })
    return {
        "kind": "memory_promotion_terminal_finalization",
        "task_window_id": task_window_id,
        "run_ids": run_ids,
        "results": results,
    }


def inspect_shadow_promotion(
    path: str | Path, *, project_id: str, memory_id: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Read exactly verified shadow-promotion diagnostics without initializing storage."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    from odibi_anchor.human_input_owner import owner_approval_provider_status

    provider_status = owner_approval_provider_status()
    provider_diagnostics = {
        "human_owner_provider_available": provider_status["available"],
        "human_owner_provider_configured": provider_status["configured"],
        "human_owner_provider_transport": provider_status["transport"],
        "human_owner_provider_assurance": provider_status["assurance"],
        "authenticated_human_provider_available": (
            provider_status["transport"] == "slack"
        ),
        "authenticated_human_provider_configured": (
            provider_status["transport"] == "slack" and provider_status["configured"]
        ),
    }
    target = Path(path).expanduser()
    if not target.is_file():
        return {
            "schema_status": "uninitialized", "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256, "counts": {}, "attestations": [],
            "human_receipts": [], "decisions": [], "events": [],
            "automatic_candidate_activation_enabled": _activation_enabled(),
            "automatic_active_confirmation_enabled": _confirmation_enabled(),
            **provider_diagnostics,
        }
    with _connection(target, read_only=True) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "memory_promotion_decisions" not in tables:
            return {
                "schema_status": "uninitialized", "policy_version": POLICY_VERSION,
                "policy_sha256": POLICY_SHA256, "counts": {}, "attestations": [],
                "human_receipts": [], "decisions": [], "events": [],
                "automatic_candidate_activation_enabled": _activation_enabled(),
                "automatic_active_confirmation_enabled": _confirmation_enabled(),
                **provider_diagnostics,
            }
        _verify_schema(connection)
        where = ["project_id=?"]
        params: list[Any] = [project_id]
        if memory_id is not None:
            where.append("memory_id=?")
            params.append(memory_id)
        rows = connection.execute(
            "SELECT * FROM memory_promotion_decisions WHERE " + " AND ".join(where)
            + " ORDER BY decided_at DESC,decision_id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        attestation_rows = connection.execute(
            "SELECT * FROM memory_promotion_attestations WHERE " + " AND ".join(where)
            + " ORDER BY attested_at DESC,attestation_id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        attestations = [_verified_attestation(row) for row in attestation_rows]
        receipt_rows = connection.execute(
            "SELECT * FROM memory_human_authority_receipts WHERE " + " AND ".join(where)
            + " ORDER BY created_at DESC,receipt_id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        human_receipts = [_verified_human_receipt(row) for row in receipt_rows]
        event_rows = connection.execute(
            "SELECT * FROM memory_promotion_events WHERE " + " AND ".join(where)
            + " ORDER BY created_at DESC,event_id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        events = [_verified_event(row, connection) for row in event_rows]
        decisions = []
        for row in rows:
            decision = json.loads(row["decision_json"])
            if row["decision_json"] != _json(decision):
                raise RuntimeError("memory promotion decision is not canonical JSON")
            indexed = {
                "memory_id": row["memory_id"],
                "claim_sha256": row["claim_sha256"],
                "project_id": row["project_id"],
                "trust_domain": row["trust_domain"],
                "policy_version": row["policy_version"],
                "policy_sha256": row["policy_sha256"],
                "mode": row["mode"],
                "outcome": row["outcome"],
                "reason": row["reason"],
                "attestation_ids": json.loads(row["attestation_ids_json"]),
                "nomination_ids": json.loads(row["nomination_ids_json"]),
            }
            if any(decision.get(key) != value for key, value in indexed.items()):
                raise RuntimeError("memory promotion decision indexed column mismatch")
            decisions.append({
                "decision_id": row["decision_id"],
                "memory_id": row["memory_id"],
                "outcome": row["outcome"],
                "reason": row["reason"],
                "claim_sha256": row["claim_sha256"],
                "policy_version": row["policy_version"],
                "policy_sha256": row["policy_sha256"],
                "attestation_ids": json.loads(row["attestation_ids_json"]),
                "nomination_ids": json.loads(row["nomination_ids_json"]),
                "decided_at": row["decided_at"],
            })
        counts = dict(
            connection.execute(
                "SELECT outcome,count(*) FROM memory_promotion_decisions WHERE "
                + " AND ".join(where) + " GROUP BY outcome",
                params,
            ).fetchall()
        )
        attestation_count = connection.execute(
            "SELECT count(*) FROM memory_promotion_attestations WHERE " + " AND ".join(where),
            params,
        ).fetchone()[0]
        human_receipt_count = connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts WHERE "
            + " AND ".join(where),
            params,
        ).fetchone()[0]
        event_counts = dict(connection.execute(
            "SELECT event_type,count(*) FROM memory_promotion_events WHERE "
            + " AND ".join(where) + " GROUP BY event_type",
            params,
        ).fetchall())
        authority_lane_counts = dict(connection.execute(
            "SELECT json_extract(event_json,'$.authority_lane'),count(*) "
            "FROM memory_promotion_events WHERE " + " AND ".join(where)
            + " GROUP BY json_extract(event_json,'$.authority_lane')",
            params,
        ).fetchall())
        effective_rows = connection.execute(
            "SELECT e.memory_id,e.event_type,e.target_status,m.status "
            "FROM memory_promotion_events e JOIN memories m ON m.id=e.memory_id "
            "WHERE e.project_id=? AND NOT EXISTS ("
            "SELECT 1 FROM memory_promotion_events later WHERE later.memory_id=e.memory_id "
            "AND (later.created_at>e.created_at OR "
            "(later.created_at=e.created_at AND later.event_id>e.event_id)))",
            (project_id,),
        ).fetchall()
        safe_terminal = {"quarantined", "retired", "stale", "rejected", "superseded"}
        if any(
            row["target_status"] != row["status"]
            and not (
                row["event_type"] in {"activation", "confirmation"}
                and row["status"] in safe_terminal
            )
            for row in effective_rows
        ):
            raise RuntimeError("memory promotion event authority diverged from lifecycle projection")
        effective_active = sum(
            row["event_type"] in {"activation", "confirmation"}
            and row["status"] in {"active", "confirmed"}
            for row in effective_rows
        )
        effective_active_memory_ids = sorted(
            row["memory_id"] for row in effective_rows
            if row["event_type"] in {"activation", "confirmation"}
            and row["status"] in {"active", "confirmed"}
        )[:50]
        return {
            "schema_status": "ready",
            "project_id": project_id,
            "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA256,
            "automatic_candidate_activation_enabled": _activation_enabled(),
            "automatic_active_confirmation_enabled": _confirmation_enabled(),
            **provider_diagnostics,
            "counts": {
                "attestations": attestation_count,
                "human_receipts": human_receipt_count,
                "decisions": sum(counts.values()),
                "outcomes": counts,
                "events": sum(event_counts.values()),
                "event_types": event_counts,
                "authority_lanes": authority_lane_counts,
                "effective_active": effective_active,
                "effective_confirmed": sum(
                    row["event_type"] == "confirmation" and row["status"] == "confirmed"
                    for row in effective_rows
                ),
            },
            "attestations": attestations,
            "human_receipts": human_receipts,
            "decisions": decisions,
            "events": events,
            "effective_active_memory_ids": effective_active_memory_ids,
        }
