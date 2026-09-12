"""Task-aware memory retrieval and compatible lifecycle subcommands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from fnmatch import fnmatch
from numbers import Real
from pathlib import Path
from typing import Any

_MEMORY_HELP_HINT = 'Run anchor("help", "memory") for lifecycle schemas and examples.'


def _validate_json_object(value: Any, name: str, *, nonempty: bool = False) -> None:
    """Reject malformed lifecycle payloads before database-backed resolution."""
    if not isinstance(value, Mapping) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ValueError(f"{name} must be a {qualifier}JSON object. {_MEMORY_HELP_HINT}")

_SEED_WITHDRAWAL_TABLE_DDL = (
    "CREATE TABLE memory_seed_withdrawal_events (event_id TEXT PRIMARY KEY,"
    "manifest_sha256 TEXT NOT NULL,seed_id TEXT NOT NULL,source TEXT NOT NULL,"
    "prior_status TEXT NOT NULL,reason TEXT NOT NULL,withdrawn_at TEXT NOT NULL,"
    "UNIQUE(manifest_sha256,seed_id))"
)
_SEED_WITHDRAWAL_UPDATE_TRIGGER_DDL = (
    "CREATE TRIGGER memory_seed_withdrawal_events_no_update BEFORE UPDATE ON "
    "memory_seed_withdrawal_events BEGIN SELECT RAISE(ABORT,'memory seed withdrawal events are immutable'); END"
)
_SEED_WITHDRAWAL_DELETE_TRIGGER_DDL = (
    "CREATE TRIGGER memory_seed_withdrawal_events_no_delete BEFORE DELETE ON "
    "memory_seed_withdrawal_events BEGIN SELECT RAISE(ABORT,'memory seed withdrawal events are immutable'); END"
)
_SEED_RECONCILE_TABLE_DDL = (
    "CREATE TABLE memory_seed_reconciliation_events (event_id TEXT PRIMARY KEY,"
    "manifest_sha256 TEXT NOT NULL,seed_id TEXT NOT NULL,replacement_id TEXT NOT NULL,"
    "source TEXT NOT NULL,prior_status TEXT NOT NULL,prior_confidence REAL NOT NULL,"
    "prior_content_sha256 TEXT NOT NULL,reason TEXT NOT NULL,reconciled_at TEXT NOT NULL,"
    "UNIQUE(manifest_sha256,seed_id))"
)
_SEED_RECONCILE_UPDATE_TRIGGER_DDL = (
    "CREATE TRIGGER memory_seed_reconciliation_events_no_update BEFORE UPDATE ON "
    "memory_seed_reconciliation_events BEGIN SELECT RAISE(ABORT,'memory seed reconciliation events are immutable'); END"
)
_SEED_RECONCILE_DELETE_TRIGGER_DDL = (
    "CREATE TRIGGER memory_seed_reconciliation_events_no_delete BEFORE DELETE ON "
    "memory_seed_reconciliation_events BEGIN SELECT RAISE(ABORT,'memory seed reconciliation events are immutable'); END"
)
_SEED_LOAD_TABLE_DDL = (
    "CREATE TABLE memory_seed_load_events (event_id TEXT PRIMARY KEY,manifest_sha256 TEXT NOT NULL,"
    "entry_count INTEGER NOT NULL CHECK(entry_count>=0),entries_json TEXT NOT NULL,loaded_at TEXT NOT NULL)"
)
_SEED_LOAD_UPDATE_TRIGGER_DDL = (
    "CREATE TRIGGER memory_seed_load_events_no_update BEFORE UPDATE ON memory_seed_load_events "
    "BEGIN SELECT RAISE(ABORT,'memory seed load events are immutable'); END"
)
_SEED_LOAD_DELETE_TRIGGER_DDL = (
    "CREATE TRIGGER memory_seed_load_events_no_delete BEFORE DELETE ON memory_seed_load_events "
    "BEGIN SELECT RAISE(ABORT,'memory seed load events are immutable'); END"
)


#: Reviewed-seed row ids are the canonical manifest id, optionally followed by a
#: version suffix identifying the manifest that introduced a replacement row.
_SEED_ID_PATTERN = r"seed_agent_reliability_\d{2}"
_SEED_ROW_ID_PATTERN = rf"{_SEED_ID_PATTERN}(?:\.v[0-9a-f]{{64}})?"


def _canonical_seed_id(row_id: str) -> str:
    """Return the manifest seed id for a canonical or versioned replacement row."""
    return row_id.split(".v", 1)[0]


def _ensure_seed_withdrawal_schema(connection: sqlite3.Connection, *, create: bool) -> bool:
    """Create or exactly verify the immutable reviewed-seed withdrawal ledger."""
    objects = {
        "memory_seed_withdrawal_events": ("table", _SEED_WITHDRAWAL_TABLE_DDL),
        "memory_seed_withdrawal_events_no_update": ("trigger", _SEED_WITHDRAWAL_UPDATE_TRIGGER_DDL),
        "memory_seed_withdrawal_events_no_delete": ("trigger", _SEED_WITHDRAWAL_DELETE_TRIGGER_DDL),
    }
    rows = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)",
            tuple(objects),
        )
    }
    if not rows and not create:
        return False
    if create:
        connection.execute(_SEED_WITHDRAWAL_TABLE_DDL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
        connection.execute(_SEED_WITHDRAWAL_UPDATE_TRIGGER_DDL.replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1))
        connection.execute(_SEED_WITHDRAWAL_DELETE_TRIGGER_DDL.replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1))
        rows = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)",
                tuple(objects),
            )
        }
    for name, expected in objects.items():
        actual = rows.get(name)
        if actual is None or actual[0] != expected[0]:
            raise RuntimeError("memory seed withdrawal-event schema mismatch")
        normalized = actual[1].replace(" IF NOT EXISTS", "")
        if normalized != expected[1]:
            raise RuntimeError("memory seed withdrawal-event schema mismatch")
    return True


def _ensure_seed_load_schema(connection: sqlite3.Connection, *, create: bool) -> bool:
    """Create or exactly verify the immutable reviewed-seed load ledger."""
    objects = {
        "memory_seed_load_events": ("table", _SEED_LOAD_TABLE_DDL),
        "memory_seed_load_events_no_update": ("trigger", _SEED_LOAD_UPDATE_TRIGGER_DDL),
        "memory_seed_load_events_no_delete": ("trigger", _SEED_LOAD_DELETE_TRIGGER_DDL),
    }
    rows = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)", tuple(objects),
        )
    }
    if not rows and not create:
        return False
    if create:
        for _name, (kind, statement) in objects.items():
            connection.execute(statement.replace(f"CREATE {kind.upper()} ", f"CREATE {kind.upper()} IF NOT EXISTS ", 1))
        rows = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)", tuple(objects),
            )
        }
    for name, expected in objects.items():
        actual = rows.get(name)
        if actual is None or actual[0] != expected[0] or actual[1].replace(" IF NOT EXISTS", "") != expected[1]:
            raise RuntimeError("memory seed load-event schema mismatch")
    return True


def _ensure_seed_reconcile_schema(connection: sqlite3.Connection, *, create: bool) -> bool:
    """Create or exactly verify the immutable reviewed-seed reconciliation ledger."""
    objects = {
        "memory_seed_reconciliation_events": ("table", _SEED_RECONCILE_TABLE_DDL),
        "memory_seed_reconciliation_events_no_update": ("trigger", _SEED_RECONCILE_UPDATE_TRIGGER_DDL),
        "memory_seed_reconciliation_events_no_delete": ("trigger", _SEED_RECONCILE_DELETE_TRIGGER_DDL),
    }
    rows = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)", tuple(objects),
        )
    }
    if not rows and not create:
        return False
    if create:
        for _name, (kind, statement) in objects.items():
            connection.execute(statement.replace(f"CREATE {kind.upper()} ", f"CREATE {kind.upper()} IF NOT EXISTS ", 1))
        rows = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)", tuple(objects),
            )
        }
    for name, expected in objects.items():
        actual = rows.get(name)
        if actual is None or actual[0] != expected[0] or actual[1].replace(" IF NOT EXISTS", "") != expected[1]:
            raise RuntimeError("memory seed reconciliation-event schema mismatch")
    return True


def _db_path(override: str | None = None) -> str:
    if override:
        return override
    from odibi_anchor.codebase._memory_db import _resolve_default_db_path

    return _resolve_default_db_path()


def _task_query(
    task_args: tuple[Any, ...], task_kwargs: dict[str, Any], session_state: Any,
    task_stage: dict[str, Any] | None,
) -> dict[str, Any]:
    profile = getattr(session_state, "active_task_profile", None)
    profile_data = profile.to_dict() if profile and hasattr(profile, "to_dict") else {}
    scope = list((task_stage or {}).get("repository_scope") or [])
    problem = getattr(session_state, "linked_problem", None)
    spec = getattr(session_state, "linked_spec", None)
    tags = {str(tag).lower() for tag in (task_kwargs.get("tags") or [])}
    if problem:
        tags.add(f"problem:{problem}".lower())
    if spec:
        tags.add(f"spec:{spec}".lower())
    return {
        "description": str(task_args[0] if task_args else "").strip(),
        "goal": str(task_kwargs.get("goal") or "").strip(),
        "task_profile": profile_data,
        "problem": problem,
        "spec": spec,
        "project": getattr(session_state, "active_project", None),
        "repository_scope": scope,
        "tags": sorted(tags),
    }


def _persisted_query(query: dict[str, Any]) -> dict[str, Any]:
    """Return bounded retrieval provenance without retaining task prompt text."""
    from odibi_anchor._forensic_replay.journal import redact_payload

    profile = query.get("task_profile") or {}
    bounded = {
        "description_present": bool(query.get("description")),
        "goal_present": bool(query.get("goal")),
        "task_profile": {
            key: profile[key]
            for key in ("work_type", "execution_mode", "task_size")
            if key in profile and isinstance(profile[key], (str, int, float, bool, type(None)))
        },
        "problem": query.get("problem"),
        "spec": query.get("spec"),
        "project": query.get("project"),
        "repository_scope": [str(value)[:512] for value in query.get("repository_scope", [])[:32]],
        "tags": [str(value)[:128] for value in query.get("tags", [])[:32]],
    }
    return redact_payload(bounded)


def _memory_diagnostics(path: str, *, project: str | None = None, **payload: Any) -> dict[str, Any]:
    """Derive an exact read-only memory-effectiveness funnel."""
    if payload:
        raise ValueError(f"unknown memory diagnostics arguments: {sorted(payload)}")
    target = Path(path)
    if not target.is_file():
        return {"kind": "memory_diagnostics", "schema_status": "uninitialized", "counts": {}}
    connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"memories", "memory_selections", "memory_applications", "memory_evaluations", "memory_projections"}
        if not required.issubset(tables):
            return {"kind": "memory_diagnostics", "schema_status": "uninitialized", "counts": {}}
        from odibi_anchor.codebase._memory_lifecycle import (
            DOMAIN,
            SCHEMA_SHA256,
            SCHEMA_VERSION,
            _verify_objects,
        )
        version = connection.execute(
            "SELECT version,schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,),
        ).fetchone()
        if tuple(version or ()) != (SCHEMA_VERSION, SCHEMA_SHA256):
            raise RuntimeError("memory lifecycle schema version/checksum mismatch")
        _verify_objects(connection, version=SCHEMA_VERSION)
        scope = " WHERE m.project IN (?, 'all')" if project else ""
        scoped_and = " AND m.project IN (?, 'all')" if project else ""
        params = (project,) if project else ()
        counts = {
            "memories": connection.execute(f"SELECT count(*) FROM memories m{scope}", params).fetchone()[0],
            "selections": connection.execute(
                f"SELECT count(*) FROM memory_selections s JOIN memories m ON m.id=s.memory_id{scope}", params,
            ).fetchone()[0],
            "applications": connection.execute(
                "SELECT count(*) FROM memory_applications a JOIN memory_selections s ON s.selection_id=a.selection_id "
                f"JOIN memories m ON m.id=s.memory_id{scope}", params,
            ).fetchone()[0],
            "evaluations": connection.execute(
                "SELECT count(*) FROM memory_evaluations e JOIN memory_applications a ON a.application_id=e.application_id "
                "JOIN memory_selections s ON s.selection_id=a.selection_id JOIN memories m ON m.id=s.memory_id"
                f"{scope}", params,
            ).fetchone()[0],
            "projections": connection.execute(
                f"SELECT count(*) FROM memory_projections p JOIN memories m ON m.id=p.memory_id{scope}", params,
            ).fetchone()[0],
            "never_selected": connection.execute(
                f"SELECT count(*) FROM memories m LEFT JOIN memory_selections s ON s.memory_id=m.id WHERE s.selection_id IS NULL{scoped_and}",
                params,
            ).fetchone()[0],
            "selected_never_applied": connection.execute(
                "SELECT count(*) FROM memory_selections s JOIN memories m ON m.id=s.memory_id "
                f"WHERE NOT EXISTS (SELECT 1 FROM memory_applications a WHERE a.selection_id=s.selection_id){scoped_and}",
                params,
            ).fetchone()[0],
        }
        dispositions: dict[str, int] = {}
        if "memory_dispositions" in tables:
            dispositions = dict(connection.execute(
                "SELECT d.disposition,count(*) FROM memory_dispositions d JOIN memory_selections s ON s.selection_id=d.selection_id "
                f"JOIN memories m ON m.id=s.memory_id{scope} GROUP BY d.disposition", params,
            ).fetchall())
        outcomes = dict(connection.execute(
            "SELECT e.outcome,count(*) FROM memory_evaluations e JOIN memory_applications a ON a.application_id=e.application_id "
            "JOIN memory_selections s ON s.selection_id=a.selection_id JOIN memories m ON m.id=s.memory_id"
            f"{scope} GROUP BY e.outcome", params,
        ).fetchall())
        lifecycle = dict(connection.execute(
            f"SELECT m.status,count(*) FROM memories m{scope} GROUP BY m.status", params,
        ).fetchall())
        seed_withdrawals = 0
        seed_loads = 0
        seed_reconciliations = 0
        withdrawn_seed_ids: list[str] = []
        reconciled_seed_ids: list[str] = []
        if _ensure_seed_load_schema(connection, create=False):
            seed_loads = connection.execute("SELECT count(*) FROM memory_seed_load_events").fetchone()[0]
        if _ensure_seed_withdrawal_schema(connection, create=False):
            seed_withdrawals = connection.execute(
                "SELECT count(*) FROM memory_seed_withdrawal_events"
            ).fetchone()[0]
            withdrawn_seed_ids = [row[0] for row in connection.execute(
                "SELECT seed_id FROM memory_seed_withdrawal_events ORDER BY withdrawn_at DESC,seed_id LIMIT 50"
            )]
        if _ensure_seed_reconcile_schema(connection, create=False):
            seed_reconciliations = connection.execute(
                "SELECT count(*) FROM memory_seed_reconciliation_events"
            ).fetchone()[0]
            reconciled_seed_ids = [row[0] for row in connection.execute(
                "SELECT seed_id FROM memory_seed_reconciliation_events "
                "ORDER BY reconciled_at DESC,seed_id LIMIT 50"
            )]
        from odibi_anchor.codebase._memory_promotion import inspect_shadow_promotion
        from odibi_anchor.codebase._memory_verifier import inspect_verifier_runs

        promotion = inspect_shadow_promotion(path, project_id=project or "__unresolved_project__")
        verifier = inspect_verifier_runs(path, project_id=project or "__unresolved_project__")
        promotion_counts = promotion.get("counts", {})
        verifier_counts = verifier.get("counts", {})
        durable_facts = {
            # Shadow decisions are durable evidence, not promotion authority.
            # Historical status/count columns remain state rather than proof
            # that a promotion event occurred.
            "promotion_events": promotion_counts.get("events", 0),
            "promotion_shadow_decisions": promotion_counts.get("decisions", 0),
            "promotion_attestations": promotion_counts.get("attestations", 0),
            "promotion_human_receipts": promotion_counts.get("human_receipts", 0),
            "promotion_verifier_runs": verifier_counts.get("runs", 0),
            "promoted_memories": promotion_counts.get("effective_active", 0),
            "rejected_memories": lifecycle.get("rejected", 0),
            "quarantined_memories": lifecycle.get("quarantined", 0),
            "stale_memories": lifecycle.get("stale", 0),
            "superseded_memories": lifecycle.get("superseded", 0),
            "reviewed_seed_loads": seed_loads,
            "reviewed_seed_withdrawals": seed_withdrawals,
            "reviewed_seed_reconciliations": seed_reconciliations,
        }
        def rate(numerator: int, denominator: int) -> dict[str, Any]:
            return {"numerator": numerator, "denominator": denominator,
                    "rate": numerator / denominator if denominator else None}
        disposed = sum(dispositions.values())
        applied_selections = connection.execute(
            "SELECT count(DISTINCT s.selection_id) FROM memory_selections s "
            "JOIN memory_applications a ON a.selection_id=s.selection_id "
            f"JOIN memories m ON m.id=s.memory_id{scope}", params,
        ).fetchone()[0]
        applied_dispositions_with_application = connection.execute(
            "SELECT count(DISTINCT d.selection_id) FROM memory_dispositions d "
            "JOIN memory_applications a ON a.selection_id=d.selection_id "
            "JOIN memory_selections s ON s.selection_id=d.selection_id "
            f"JOIN memories m ON m.id=s.memory_id WHERE d.disposition='applied'{scoped_and}",
            params,
        ).fetchone()[0] if "memory_dispositions" in tables else 0
        id_scope = " AND m.project IN (?, 'all')" if project else ""
        bounded_ids = {
            "never_selected": [row[0] for row in connection.execute(
                "SELECT m.id FROM memories m WHERE NOT EXISTS "
                f"(SELECT 1 FROM memory_selections s WHERE s.memory_id=m.id){id_scope} ORDER BY m.id LIMIT 50",
                params,
            )],
            "selected_never_applied": [row[0] for row in connection.execute(
                "SELECT s.selection_id FROM memory_selections s JOIN memories m ON m.id=s.memory_id "
                "WHERE NOT EXISTS (SELECT 1 FROM memory_applications a "
                f"WHERE a.selection_id=s.selection_id){id_scope} ORDER BY s.selection_id LIMIT 50",
                params,
            )],
            "quarantined": [row[0] for row in connection.execute(
                f"SELECT m.id FROM memories m WHERE m.status='quarantined'{id_scope} ORDER BY m.id LIMIT 50", params,
            )],
            "superseded": [row[0] for row in connection.execute(
                f"SELECT m.id FROM memories m WHERE m.status='superseded'{id_scope} ORDER BY m.id LIMIT 50", params,
            )],
            "rejected": [row[0] for row in connection.execute(
                f"SELECT m.id FROM memories m WHERE m.status='rejected'{id_scope} ORDER BY m.id LIMIT 50", params,
            )],
            "stale": [row[0] for row in connection.execute(
                f"SELECT m.id FROM memories m WHERE m.status='stale'{id_scope} ORDER BY m.id LIMIT 50", params,
            )],
            "promoted": promotion.get("effective_active_memory_ids", []),
            "withdrawn_reviewed_seeds": withdrawn_seed_ids,
            "reconciled_reviewed_seeds": reconciled_seed_ids,
        }
        eligible_learning = 0
        eligible_projections = 0
        learning_tables = {
            "learning_items", "learning_evidence_refs", "learning_assessments",
            "learning_assessment_observations",
        }
        if learning_tables.issubset(tables):
            learning_scope = (
                " AND json_extract(i.project_refs,'$[0]')=?"
                if project else ""
            )
            cohort = (
                " FROM learning_items i "
                "JOIN learning_assessment_observations ao ON ao.observation_id=i.item_id "
                "JOIN learning_assessments la ON la.assessment_id=ao.assessment_id "
                "WHERE la.outcome='observations_recorded' AND i.kind='observation' "
                "AND i.observation_type IN ('reusable_practice','near_miss','evidence_gap') "
                "AND i.applicability_scope='project_local' AND json_array_length(i.project_refs)=1 "
                "AND EXISTS (SELECT 1 FROM learning_evidence_refs e WHERE e.item_id=i.item_id)"
                + learning_scope
            )
            eligible_learning = connection.execute(
                "SELECT count(DISTINCT i.item_id)" + cohort, params,
            ).fetchone()[0]
            eligible_projections = connection.execute(
                "SELECT count(DISTINCT i.item_id)" + cohort
                + " AND EXISTS (SELECT 1 FROM memory_projections p WHERE p.learning_item_id=i.item_id)",
                params,
            ).fetchone()[0]
        counts["eligible_learning_candidates"] = eligible_learning
        counts["eligible_learning_projections"] = eligible_projections
        return {
            "kind": "memory_diagnostics", "schema_status": "ready", "project": project,
            "counts": {
                **counts, "dispositions": dispositions, "outcomes": outcomes,
                "lifecycle": lifecycle, "durable_facts": durable_facts,
            },
            "bounded_ids": bounded_ids,
            "promotion_capability": {
                "status": "machine_and_owner_promotion_enabled",
                "reason": "verifier_attestations_or_governed_owner_receipts",
                "policy_version": promotion["policy_version"],
                "policy_sha256": promotion["policy_sha256"],
                "automatic_candidate_activation_enabled": promotion.get(
                    "automatic_candidate_activation_enabled", False,
                ),
                "automatic_active_confirmation_enabled": promotion.get(
                    "automatic_active_confirmation_enabled", False,
                ),
                "authenticated_human_provider_available": promotion.get(
                    "authenticated_human_provider_available", False,
                ),
                "authenticated_human_provider_configured": promotion.get(
                    "authenticated_human_provider_configured", False,
                ),
                "human_owner_provider_available": promotion.get(
                    "human_owner_provider_available", False,
                ),
                "human_owner_provider_configured": promotion.get(
                    "human_owner_provider_configured", False,
                ),
                "human_owner_provider_transport": promotion.get(
                    "human_owner_provider_transport",
                ),
                "human_owner_provider_assurance": promotion.get(
                    "human_owner_provider_assurance",
                ),
            },
            "promotion_shadow": promotion,
            "promotion_verifier": verifier,
            "rates": {
                "retrieval_to_disposition": rate(disposed, counts["selections"]),
                "selection_to_application": rate(applied_selections, counts["selections"]),
                "disposition_to_application": rate(
                    applied_dispositions_with_application, dispositions.get("applied", 0),
                ),
                "application_to_evaluation": rate(counts["evaluations"], counts["applications"]),
                "eligible_learning_to_projection": rate(eligible_projections, eligible_learning),
            },
        }
    finally:
        connection.close()


def _validate_seed_manifest(manifest: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Validate checksum, evidence provenance, and candidate-only seed contracts.

    ``manifest_sha256`` is an **integrity** check, not an **authenticity** one. It is
    carried inside the manifest and verified against the manifest's own remaining
    content, so it detects accidental corruption or partial edits -- it does not prove
    who wrote the file, and an editor who recomputes it produces a self-consistent
    manifest. Authenticity comes only from the fact that this resource ships inside the
    installed distribution and that no caller can supply a manifest: ``_memory_seed``
    accepts no manifest argument.

    Every trust-bearing property is therefore re-validated here rather than inferred
    from the checksum: candidate-only ``status``, absent ``human_review``, public
    provenance, commit shape, and a confidence ceiling. A tampered but
    self-consistently re-checksummed manifest still cannot create authority
    (WI-2026-0019).
    """
    from odibi_anchor.codebase._memory_db import CANDIDATE_CONFIDENCE_CEILING

    manifest = dict(manifest)
    expected = manifest.pop("manifest_sha256")
    actual = hashlib.sha256(json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    if expected != actual:
        raise RuntimeError("memory seed manifest checksum mismatch")
    entries = manifest.get("entries")
    withdrawn = manifest.get("withdrawn")
    if (
        set(manifest) != {"version", "name", "trust_domain", "withdrawn", "entries"}
        or manifest.get("version") != 1
        or manifest.get("trust_domain") != "public-repository"
        or not isinstance(entries, list)
        or not isinstance(withdrawn, list)
    ):
        raise RuntimeError("unsupported memory seed manifest")
    entry_keys = {
        "id", "project", "type", "content", "related_files", "tags", "confidence",
        "status", "human_review", "evidence", "content_sha256",
    }
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            raise RuntimeError("invalid memory seed entry schema")
        if (
            not re.fullmatch(r"seed_agent_reliability_\d{2}", str(entry["id"]))
            or entry["id"] in seen
            or entry["project"] != "all"
            or entry["status"] != "candidate"
            or entry["human_review"] is not None
            or not isinstance(entry["related_files"], list)
            or not isinstance(entry["tags"], list)
            or not isinstance(entry["content"], str)
            or not entry["content"].strip()
        ):
            raise RuntimeError("invalid memory seed lifecycle or trust scope")
        # Reviewed seeds are advisory candidates like every other ingress route, so
        # they get no privileged ranking. A separately governed reviewed-seed
        # confidence policy is required before this ceiling may be raised.
        if (
            isinstance(entry["confidence"], bool)
            or not isinstance(entry["confidence"], Real)
            or not 0.0 <= float(entry["confidence"]) <= CANDIDATE_CONFIDENCE_CEILING
        ):
            raise RuntimeError(f"invalid seed confidence ceiling: {entry['id']}")
        seen.add(entry["id"])
        body = {key: entry[key] for key in entry if key != "content_sha256"}
        digest = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        if entry.get("content_sha256") != digest:
            raise RuntimeError(f"memory seed checksum mismatch: {entry.get('id')}")
        evidence = entry["evidence"]
        if (
            not isinstance(evidence, dict)
            or set(evidence) not in (
                {"trust_domain", "repository", "commit", "references", "limitation"},
                {"trust_domain", "repository", "commits", "references", "limitation"},
            )
            or evidence["trust_domain"] != "public-repository"
            or evidence["repository"] != "https://github.com/henryodibi11/odibi-anchor"
            or not isinstance(evidence["references"], list)
            or not evidence["references"]
            or not all(isinstance(item, str) and item.strip() for item in evidence["references"])
        ):
            raise RuntimeError(f"invalid seed evidence provenance: {entry['id']}")
        commits = evidence.get("commits", [evidence.get("commit")])
        if not commits or not all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{40}", item) for item in commits):
            raise RuntimeError(f"invalid seed commit provenance: {entry['id']}")
    withdrawn_ids = set()
    for item in withdrawn:
        if (
            not isinstance(item, dict) or set(item) != {"id", "reason"}
            or not re.fullmatch(r"seed_agent_reliability_\d{2}", str(item["id"]))
            or not isinstance(item["reason"], str) or not item["reason"].strip()
            or item["id"] in seen or item["id"] in withdrawn_ids
        ):
            raise RuntimeError("invalid withdrawn seed provenance")
        withdrawn_ids.add(item["id"])
    return expected, entries


def _memory_seed(path: str, **payload: Any) -> dict[str, Any]:
    """Inspect or idempotently load the packaged, checksum-bound seed manifest."""
    command = str(payload.pop("command", "inspect")).lower()
    if payload or command not in {"inspect", "load"}:
        raise ValueError("memory seed command must be inspect or load")
    resource = Path(__file__).resolve().parents[1] / "seeds" / "agent_reliability.json"
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    expected, entries = _validate_seed_manifest(manifest)
    withdrawn = manifest["withdrawn"]
    result = {"kind": "memory_seed", "command": command, "manifest_sha256": expected,
              "entry_count": len(entries), "entries": entries, "withdrawn": withdrawn}
    if command == "inspect":
        return result
    from odibi_anchor.codebase._memory_db import get_db
    connection = get_db(path)
    now = datetime.now(UTC).isoformat()
    loaded = []
    reconciled = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_seed_load_schema(connection, create=True)
        _ensure_seed_withdrawal_schema(connection, create=True)
        _ensure_seed_reconcile_schema(connection, create=True)
        declared_ids = {entry["id"] for entry in entries}
        explicit_reasons = {entry["id"]: entry["reason"] for entry in withdrawn}
        managed_rows = connection.execute(
            "SELECT id,source,status FROM memories WHERE source LIKE 'reviewed-seed:%' ORDER BY id"
        ).fetchall()
        for row_id, source, prior_status in managed_rows:
            expected_source = "reviewed-seed:" + row_id
            if not re.fullmatch(_SEED_ROW_ID_PATTERN, row_id) or source != expected_source:
                raise ValueError(f"conflicting reviewed seed source: {row_id}")
            # A versioned replacement belongs to its canonical manifest entry, so a
            # declared entry never withdraws its own replacement rows.
            seed_id = _canonical_seed_id(row_id)
            if seed_id in declared_ids:
                continue
            reason = explicit_reasons.get(seed_id, "reviewed seed is absent from the authoritative packaged manifest")
            event_id = "seedwithdraw_" + hashlib.sha256(
                f"{expected}:{row_id}".encode()
            ).hexdigest()
            event = (event_id, expected, row_id, source, prior_status, reason, now)
            connection.execute(
                "INSERT OR IGNORE INTO memory_seed_withdrawal_events VALUES(?,?,?,?,?,?,?)", event,
            )
            persisted = connection.execute(
                "SELECT event_id,manifest_sha256,seed_id,source,prior_status,reason "
                "FROM memory_seed_withdrawal_events WHERE event_id=?", (event_id,),
            ).fetchone()
            if (
                persisted is None
                or tuple(persisted[:4]) != event[:4]
                or persisted[5] != reason
            ):
                raise RuntimeError(f"conflicting reviewed seed withdrawal event: {row_id}")
            connection.execute(
                "UPDATE memories SET status='quarantined' WHERE id=? AND source=? AND status!='quarantined'",
                (row_id, source),
            )
            reconciled.append({
                "id": row_id, "action": "withdrawn", "event_id": event_id,
                "prior_status": persisted[4], "status": "quarantined", "reason": reason,
            })
        for entry in entries:
            existing = connection.execute(
                "SELECT project,type,content,related_files,tags,source,confidence,status,evidence "
                "FROM memories WHERE id=?", (entry["id"],),
            ).fetchone()
            evidence_payload = dict(entry["evidence"])
            if entry.get("human_review"):
                evidence_payload["human_review"] = entry["human_review"]
            evidence = json.dumps(evidence_payload, sort_keys=True)
            tags = json.dumps(entry["tags"])
            related_files = json.dumps(entry.get("related_files", []))
            expected_row = (
                entry["project"], entry["type"], entry["content"], related_files,
                tags, "reviewed-seed:" + entry["id"], entry["confidence"], entry["status"], evidence,
            )
            replacement_id = f"{entry['id']}.v{expected}"
            replacement_source = "reviewed-seed:" + replacement_id
            this_event = connection.execute(
                "SELECT event_id,replacement_id,source,prior_status,prior_confidence,"
                "prior_content_sha256 FROM memory_seed_reconciliation_events "
                "WHERE manifest_sha256=? AND seed_id=?", (expected, entry["id"]),
            ).fetchone()
            if this_event is not None:
                # Restart/retry safe: this manifest already reconciled this seed, so
                # verify the complete replacement and search projection rather than
                # trusting the immutable event or writing anything again.
                replacement_evidence = json.dumps({
                    **evidence_payload,
                    "supersedes": {
                        "seed_id": entry["id"],
                        "manifest_sha256": expected,
                        "prior_status": this_event[3],
                        "prior_confidence": this_event[4],
                        "prior_content_sha256": this_event[5],
                        "authority": "none: historical values are evidence, not lifecycle authority",
                    },
                }, sort_keys=True)
                replacement = connection.execute(
                    "SELECT project,type,content,related_files,tags,source,confidence,status,evidence "
                    "FROM memories WHERE id=?", (this_event[1],),
                ).fetchone()
                fts_replacements = connection.execute(
                    "SELECT content,tags FROM memory_fts WHERE id=?", (this_event[1],),
                ).fetchall()
                if (
                    this_event[1] != replacement_id
                    or tuple(replacement or ()) != (
                        entry["project"], entry["type"], entry["content"], related_files,
                        tags, replacement_source, entry["confidence"], entry["status"],
                        replacement_evidence,
                    )
                    or [tuple(row) for row in fts_replacements] != [(entry["content"], tags)]
                ):
                    raise RuntimeError(f"conflicting reviewed seed replacement: {entry['id']}")
                loaded.append({
                    "id": entry["id"], "action": "reconciled", "replacement_id": this_event[1],
                    "event_id": this_event[0],
                })
                continue
            if existing:
                if tuple(existing) == expected_row:
                    loaded.append({"id": entry["id"], "action": "unchanged"})
                    continue
                fields = (
                    "project", "type", "content", "related_files", "tags", "source",
                    "confidence", "status", "evidence",
                )
                divergent = {
                    name
                    for name, was, now_expected in zip(
                        fields, tuple(existing), expected_row, strict=True
                    )
                    if was != now_expected
                }
                prior_events = connection.execute(
                    "SELECT count(*) FROM memory_seed_reconciliation_events WHERE seed_id=?",
                    (entry["id"],),
                ).fetchone()[0]
                superseded_lineage = existing[7] == "quarantined" and prior_events > 0
                if divergent != {"confidence"} and not superseded_lineage:
                    # Any divergence beyond the confidence ceiling is still a hard
                    # conflict: reconciliation is not a licence to rewrite content.
                    raise ValueError(f"conflicting reviewed seed: {entry['id']}")
                prior_content_sha256 = hashlib.sha256(existing[2].encode()).hexdigest()
                replacement_evidence = json.dumps({
                    **evidence_payload,
                    "supersedes": {
                        "seed_id": entry["id"],
                        "manifest_sha256": expected,
                        "prior_status": existing[7],
                        "prior_confidence": existing[6],
                        "prior_content_sha256": prior_content_sha256,
                        "authority": "none: historical values are evidence, not lifecycle authority",
                    },
                }, sort_keys=True)
                # Append the versioned replacement first so lineage always resolves.
                connection.execute(
                    "INSERT OR IGNORE INTO memories(id,project,type,content,related_files,tags,source,confidence,status,evidence,created,last_used) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (replacement_id, entry["project"], entry["type"], entry["content"],
                     related_files, tags, replacement_source, entry["confidence"],
                     entry["status"], replacement_evidence, now, now),
                )
                # INSERT OR IGNORE is silent when the id is already taken, so confirm the
                # replacement really carries this manifest's values before an event claims
                # it. Otherwise a pre-existing row at that id would be adopted blindly.
                stored_replacement = connection.execute(
                    "SELECT project,type,content,related_files,tags,source,confidence,status,evidence "
                    "FROM memories WHERE id=?",
                    (replacement_id,),
                ).fetchone()
                stored_fts = connection.execute(
                    "SELECT content,tags FROM memory_fts WHERE id=?", (replacement_id,),
                ).fetchall()
                if (
                    tuple(stored_replacement or ()) != (
                        entry["project"], entry["type"], entry["content"], related_files,
                        tags, replacement_source, entry["confidence"], entry["status"],
                        replacement_evidence,
                    )
                    or [tuple(row) for row in stored_fts] != [(entry["content"], tags)]
                ):
                    raise RuntimeError(
                        f"conflicting reviewed seed replacement: {replacement_id}"
                    )
                reason = (
                    "reviewed seed confidence exceeded the candidate ceiling; superseded by a "
                    "versioned candidate replacement"
                    if divergent == {"confidence"} else
                    "canonical reviewed seed row was already superseded by an earlier reconciliation"
                )
                event_id = "seedreconcile_" + hashlib.sha256(
                    f"{expected}:{entry['id']}:{prior_content_sha256}".encode()
                ).hexdigest()
                event = (
                    event_id, expected, entry["id"], replacement_id, existing[5],
                    existing[7], existing[6], prior_content_sha256, reason, now,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO memory_seed_reconciliation_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                    event,
                )
                persisted = connection.execute(
                    "SELECT event_id,manifest_sha256,seed_id,replacement_id,source,prior_status,"
                    "prior_confidence,prior_content_sha256,reason "
                    "FROM memory_seed_reconciliation_events WHERE event_id=?", (event_id,),
                ).fetchone()
                if persisted is None or tuple(persisted) != event[:9]:
                    raise RuntimeError(f"conflicting reviewed seed reconciliation event: {entry['id']}")
                # Withdraw the superseded row by changing lifecycle state only. The
                # immutable event preserves its prior status and confidence as evidence.
                connection.execute(
                    "UPDATE memories SET status='quarantined' WHERE id=? AND source=? AND status!='quarantined'",
                    (entry["id"], existing[5]),
                )
                loaded.append({
                    "id": entry["id"], "action": "reconciled", "replacement_id": replacement_id,
                    "event_id": event_id, "prior_status": existing[7],
                    "prior_confidence": existing[6], "status": "quarantined", "reason": reason,
                })
                continue
            connection.execute(
                "INSERT INTO memories(id,project,type,content,related_files,tags,source,confidence,status,evidence,created,last_used) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry["id"], entry["project"], entry["type"], entry["content"],
                 related_files, tags, "reviewed-seed:" + entry["id"],
                 entry["confidence"], entry["status"], evidence, now, now),
            )
            loaded.append({"id": entry["id"], "action": "inserted"})
        event_id = "seedload_" + hashlib.sha256(expected.encode()).hexdigest()
        event_entries = json.dumps(
            [{"id": entry["id"], "content_sha256": entry["content_sha256"], "status": entry["status"]}
             for entry in entries],
            sort_keys=True, separators=(",", ":"),
        )
        connection.execute(
            "INSERT OR IGNORE INTO memory_seed_load_events VALUES(?,?,?,?,?)",
            (event_id, expected, len(entries), event_entries, now),
        )
        persisted_load = connection.execute(
            "SELECT event_id,manifest_sha256,entry_count,entries_json FROM memory_seed_load_events "
            "WHERE event_id=?", (event_id,),
        ).fetchone()
        if persisted_load is None or tuple(persisted_load) != (
            event_id, expected, len(entries), event_entries,
        ):
            raise RuntimeError("conflicting memory seed load event")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        **result, "loaded": loaded, "reconciled_withdrawals": reconciled,
        "seed_load_event_id": event_id,
    }


def build_task_memory_context(
    root: str | Path, task_args: tuple[Any, ...], task_kwargs: dict[str, Any],
    *, session_state: Any, task_stage: dict[str, Any] | None = None,
    db_path: str | None = None, limit: int = 5,
) -> dict[str, Any]:
    """Retrieve and persist bounded selections for one accepted task."""
    from odibi_anchor.codebase._memory_db import get_db, query_memories
    from odibi_anchor.codebase._memory_lifecycle import record_selections
    from odibi_anchor.codebase.memory_context import _score_entries, _tokenize_text

    path = _db_path(db_path)
    get_db(path)
    query = _task_query(task_args, task_kwargs, session_state, task_stage)
    query_text = " ".join(
        value for value in (query["description"], query["goal"], query["problem"], query["spec"])
        if value
    )
    profile = query["task_profile"]
    task_type = str(profile.get("work_type") or profile.get("execution_mode") or "")
    # An unresolved project must not widen retrieval to every private project.
    # Only explicitly global records are eligible until project identity exists.
    retrieval_project = query["project"] or "all"
    candidates = query_memories(
        path, project=retrieval_project, status=["candidate", "active", "confirmed"],
        limit=None,
    )
    ranked = _score_entries(
        candidates, files_involved=query["repository_scope"], tags=query["tags"],
        error_text="", task_type=task_type, query=query_text,
    )
    query_tokens = _tokenize_text(query_text) - {
        "a", "an", "and", "as", "at", "be", "before", "by", "for", "from",
        "in", "into", "is", "it", "of", "on", "or", "that", "the", "this",
        "to", "with", "add", "change", "code", "ensure", "fix", "implement",
        "review", "source", "task", "test", "tests", "update", "use", "work",
    }
    pending = []
    for entry in ranked:
        entry_tags = {str(tag).lower() for tag in entry.get("tags", [])}
        entry_tokens = _tokenize_text(str(entry.get("content", "")))
        matched_files = sorted({
            scoped for scoped in query["repository_scope"]
            for pattern in entry.get("related_files", [])
            if fnmatch(scoped, pattern) or fnmatch(scoped, f"**/{pattern}")
        })
        matched_query_terms = sorted(query_tokens & entry_tokens)[:12]
        matched_tags = sorted(set(query["tags"]) & entry_tags)
        task_type_match = bool(task_type and task_type.lower() in entry_tags)
        # Ranking alone is not selection authority. Generic one-token overlap
        # must not create disposition obligations for an unrelated task.
        if not (
            matched_files or matched_tags or task_type_match
            or len(matched_query_terms) >= 2
        ):
            continue
        reason = {
            "matched_query_terms": matched_query_terms,
            "matched_tags": matched_tags,
            "matched_files": matched_files[:12],
            "task_type_match": task_type_match,
            "ranker": "semantic-memory-v1",
        }
        pending.append({"memory_id": entry["id"], "reason": reason, "entry": entry})
        if len(pending) == limit:
            break
    persisted_query = _persisted_query(query)
    recorded_selections = record_selections(
        path,
        task_window_id=session_state.task_window_id,
        query=persisted_query,
        selections=[{"memory_id": item["memory_id"], "reason": item["reason"]} for item in pending],
    )
    selections = []
    for item, recorded in zip(pending, recorded_selections, strict=True):
        entry = item["entry"]
        reason = item["reason"]
        content = str(entry.get("content", ""))
        selections.append({
            "selection_id": recorded["selection_id"],
            "memory_id": entry["id"],
            "type": entry.get("type"),
            "content": content[:500],
            "content_truncated": len(content) > 500,
            "project": entry.get("project"),
            "status": entry.get("status"),
            "confidence": entry.get("confidence"),
            "source": entry.get("source"),
            "reason": reason,
        })
    session_state.memory_selections = [item["selection_id"] for item in selections]
    return {
        "kind": "task_memory_context",
        "task_window_id": session_state.task_window_id,
        "query": persisted_query,
        "selections": selections,
        "selection_count": len(selections),
        "bounded_limit": limit,
        "retrieval_is_application": False,
        "authority": "advisory",
        "scope_semantics": {
            "project_local": "eligible only inside its exact managed project",
            "all": "eligible across projects when relevant; not selected for every task",
        },
    }


def memory_action(
    root: str | Path, args: tuple[Any, ...], kwargs: dict[str, Any], *,
    session_state: Any, query_fn: Any, render_fn: Any,
) -> dict[str, Any] | str:
    """Dispatch lifecycle subcommands while preserving ordinary memory queries."""
    selector = str(args[0]).strip().lower() if args else "query"
    if selector not in {
        "apply", "disposition", "evaluate", "diagnostics", "seed",
        "task_record", "replay", "storage", "promotion", "recovery",
    }:
        saved_format = kwargs.get("output_format", "dict")
        query_kwargs = dict(kwargs)
        query_kwargs["output_format"] = "dict"
        query_kwargs.setdefault("surfaced", False)
        requested_project = query_kwargs.get("project")
        active_project = session_state.active_project
        if requested_project is not None and requested_project != (active_project or "all"):
            raise ValueError("memory queries are restricted to the active project/trust boundary")
        query_kwargs["project"] = active_project or "all"
        result = query_fn(root, *args, **query_kwargs)
        return render_fn(result) if saved_format == "markdown" else result

    payload = dict(kwargs)
    payload.pop("output_format", None)
    path = _db_path(payload.pop("db_path", None))
    if selector == "apply":
        from odibi_anchor.codebase._memory_lifecycle import record_application, resolve_task_selection

        unknown = set(payload) - {"selection_id", "memory_id", "action", "context"}
        if unknown:
            raise ValueError(
                f"unknown memory apply arguments: {sorted(unknown)}. "
                + _MEMORY_HELP_HINT
            )
        if "context" in payload:
            _validate_json_object(payload["context"], "context")
        selected = resolve_task_selection(
            path, task_window_id=session_state.task_window_id,
            selection_id=payload.pop("selection_id", None), memory_id=payload.pop("memory_id", None),
        )
        result = record_application(
            path, selection_id=selected["selection_id"],
            task_window_id=session_state.task_window_id,
            action=payload.pop("action", ""), context=payload.pop("context", None),
        )
        session_state.memory_applications.append(result["application_id"])
        return {"kind": "memory_application", **result}
    if selector == "disposition":
        from odibi_anchor.codebase._memory_lifecycle import (
            pending_task_selections,
            record_disposition_effects,
            resolve_task_selection,
        )

        unknown = set(payload) - {
            "selection_id", "memory_id", "disposition", "reason", "replacement_memory_id",
            "action", "context", "all_pending",
        }
        if unknown:
            raise ValueError(
                f"unknown memory disposition arguments: {sorted(unknown)}. "
                + _MEMORY_HELP_HINT
            )
        disposition = payload.pop("disposition", "")
        reason = payload.pop("reason", {})
        _validate_json_object(reason, "reason", nonempty=True)
        if "context" in payload:
            _validate_json_object(payload["context"], "context")
        all_pending = payload.pop("all_pending", False)
        if type(all_pending) is not bool:
            raise TypeError("all_pending must be a bool")
        if all_pending:
            if disposition != "irrelevant" or payload.get("selection_id") or payload.get("memory_id"):
                raise ValueError("all_pending is only valid for explicit irrelevant disposition")
            selections = pending_task_selections(path, task_window_id=session_state.task_window_id)
        else:
            selections = [resolve_task_selection(
                path, task_window_id=session_state.task_window_id,
                selection_id=payload.pop("selection_id", None), memory_id=payload.pop("memory_id", None),
                pending_only=True,
            )]
        results = []
        for selected in selections:
            disposed, applied = record_disposition_effects(
                path, selection_id=selected["selection_id"],
                task_window_id=session_state.task_window_id, disposition=disposition,
                reason=reason, replacement_memory_id=payload.get("replacement_memory_id"),
                action=payload.get("action") if disposition == "applied" else None,
                context=payload.get("context") if disposition == "applied" else None,
            )
            if not hasattr(session_state, "memory_dispositions"):
                session_state.memory_dispositions = []
            session_state.memory_dispositions.append(disposed["disposition_id"])
            if applied is not None:
                session_state.memory_applications.append(applied["application_id"])
            results.append({**disposed, "memory_id": selected["memory_id"], "application": applied})
        return {"kind": "memory_disposition", "results": results, "count": len(results)}
    if selector == "evaluate":
        from odibi_anchor.codebase._memory_lifecycle import evaluate_application_effects

        unknown = set(payload) - {"application_id", "memory_id", "outcome", "evidence"}
        if unknown:
            raise ValueError(
                f"unknown memory evaluate arguments: {sorted(unknown)}. "
                + _MEMORY_HELP_HINT
            )
        evidence = payload.pop("evidence", {})
        _validate_json_object(evidence, "evidence", nonempty=True)
        application_id = payload.pop("application_id", None)
        memory_id = payload.pop("memory_id", None)
        if not application_id:
            import sqlite3
            if not memory_id:
                raise ValueError("evaluate requires application_id or memory_id")
            with sqlite3.connect(path) as connection:
                rows = connection.execute(
                    "SELECT a.application_id FROM memory_applications a "
                    "JOIN memory_selections s ON s.selection_id=a.selection_id "
                    "LEFT JOIN memory_evaluations e ON e.application_id=a.application_id "
                    "WHERE a.task_window_id=? AND s.memory_id=? AND e.evaluation_id IS NULL",
                    (session_state.task_window_id, memory_id),
                ).fetchall()
            if len(rows) != 1:
                raise ValueError("memory evaluation resolution requires exactly one unevaluated application")
            application_id = rows[0][0]
        outcome = payload.pop("outcome", "")
        result = evaluate_application_effects(
            path, application_id=application_id,
            task_window_id=session_state.task_window_id,
            outcome=outcome, evidence=evidence,
        )
        if not hasattr(session_state, "memory_evaluations"):
            session_state.memory_evaluations = []
        session_state.memory_evaluations.append(result["evaluation_id"])
        return {"kind": "memory_evaluation", **result}
    if selector == "diagnostics":
        requested = payload.pop("project", None)
        active = session_state.active_project
        if requested is not None and (active is None or requested != active):
            raise ValueError("memory diagnostics are restricted to the active project/trust boundary")
        return _memory_diagnostics(path, project=active or "__unresolved_project__", **payload)
    if selector == "recovery":
        from odibi_anchor.codebase._memory_lifecycle import selection_recovery

        unknown = set(payload) - {
            "action", "selection_id", "prior_task_window_id", "reason", "evidence", "limit",
        }
        if unknown:
            raise ValueError(f"unknown memory recovery arguments: {sorted(unknown)}")
        required_owner = {
            "project_id": session_state.active_project,
            "anchor_home": session_state.anchor_home,
            "project_root": session_state.project_root,
            "target_root": session_state.target_root,
            "artifact_root": session_state.artifact_root,
            "current_task_window_id": session_state.task_window_id,
        }
        if any(value is None for value in required_owner.values()):
            raise RuntimeError(
                "memory selection recovery requires a current accepted task and exact managed route"
            )
        command = str(payload.pop("action", "inspect")).strip().lower()
        if command != "inspect":
            _validate_json_object(payload.get("reason"), "reason", nonempty=True)
            _validate_json_object(payload.get("evidence"), "evidence", nonempty=True)
        result = selection_recovery(
            path,
            action=command,
            **required_owner,
            repository_provider_id=getattr(
                session_state.repository_provider, "provider_id", None
            ),
            trust_domain=getattr(session_state, "trust_domain", None),
            selection_id=payload.get("selection_id"),
            prior_task_window_id=payload.get("prior_task_window_id"),
            reason=payload.get("reason"),
            evidence=payload.get("evidence"),
            actor_kind="agent",
            actor_ref="dispatcher",
            limit=payload.get("limit", 50),
        )
        return {"kind": "memory_selection_recovery", "action": command, **result}
    if selector == "promotion":
        from odibi_anchor._dispatcher._boot import _ENV
        from odibi_anchor.codebase._memory_promotion import (
            evaluate_shadow_promotion,
            finalize_verifier_attestation,
            inspect_shadow_promotion,
            request_owner_promotion,
            withdraw_candidate_activation,
        )
        from odibi_anchor.codebase._memory_verifier import (
            inspect_verifier_runs,
            run_installed_verifier,
        )

        unknown = set(payload) - {
            "command", "memory_id", "run_id", "limit", "timeout_minutes",
            "provider", "in_session_approval",
        }
        if unknown:
            raise ValueError(
                f"unknown memory promotion arguments: {sorted(unknown)}. "
                'Run anchor("help", "memory") for lifecycle schemas and examples.'
            )
        command = str(payload.pop("command", "inspect")).strip().lower()
        memory_id = payload.pop("memory_id", None)
        run_id = payload.pop("run_id", None)
        project = session_state.active_project
        if not project:
            raise ValueError("memory promotion requires an active project/trust boundary")
        if command == "evaluate":
            if payload or run_id is not None:
                raise ValueError("limit is only valid for memory promotion inspection")
            return evaluate_shadow_promotion(
                path, memory_id=memory_id, project_id=project,
            )
        if command == "verify":
            if payload or run_id is not None:
                raise ValueError("memory promotion verify accepts only memory_id")
            baseline = session_state.task_repository_baseline
            if baseline is None or not session_state.task_window_id or not session_state.target_root:
                raise RuntimeError("memory promotion verify requires accepted source-task authority")
            from odibi_anchor._repository_snapshot import (
                is_databricks_git_folder_baseline,
            )

            if is_databricks_git_folder_baseline(baseline):
                raise ValueError(
                    "memory promotion verification requires canonical local Git history; "
                    "Databricks Git Folder task evidence does not provide it"
                )
            return run_installed_verifier(
                path,
                memory_id=memory_id,
                project_id=project,
                task_window_id=session_state.task_window_id,
                target_root=session_state.target_root,
                configured_target_ref=baseline.configured_target_ref,
            )
        if command == "attest":
            if payload or memory_id is not None:
                raise ValueError("memory promotion attest accepts only run_id")
            return finalize_verifier_attestation(path, run_id=run_id, project_id=project)
        if command in {"request_owner_activation", "request_owner_confirmation"}:
            if run_id is not None or "limit" in payload:
                raise ValueError(
                    "owner promotion accepts only memory_id, command, timeout_minutes, provider, "
                    "and in_session_approval"
                )
            timeout_minutes = payload.pop("timeout_minutes", 60)
            provider = payload.pop("provider", None)
            in_session_approval = payload.pop("in_session_approval", None)
            if payload:
                raise ValueError(
                    "owner promotion accepts only memory_id, command, timeout_minutes, provider, "
                    "and in_session_approval"
                )
            return request_owner_promotion(
                path, memory_id=memory_id, project_id=project,
                transition=(
                    "activation" if command == "request_owner_activation" else "confirmation"
                ),
                timeout_minutes=timeout_minutes, provider=provider,
                in_session_approval=in_session_approval,
                authority_id=_ENV.get("authority_id"),
                trust_domain=_ENV.get("trust_domain"),
            )
        if command in {"withdraw", "quarantine"}:
            if payload or run_id is not None:
                raise ValueError(
                    "memory promotion withdrawal accepts only memory_id and command"
                )
            return withdraw_candidate_activation(
                path, memory_id=memory_id, project_id=project,
                quarantine=command == "quarantine",
                authority_id=_ENV.get("authority_id"),
                trust_domain=_ENV.get("trust_domain"),
            )
        if command == "inspect":
            if run_id is not None:
                raise ValueError("run_id is not valid for memory promotion inspection")
            limit = payload.pop("limit", 50)
            if payload:
                raise ValueError("memory promotion inspect accepts only memory_id and limit")
            return {
                "kind": "memory_promotion_shadow_diagnostics",
                **inspect_shadow_promotion(
                    path, project_id=project, memory_id=memory_id,
                    limit=limit,
                ),
                "verifier": inspect_verifier_runs(
                    path, project_id=project, memory_id=memory_id,
                    limit=limit,
                ),
            }
        raise ValueError(
            "memory promotion command must be inspect, evaluate, verify, attest, withdraw, "
            "quarantine, request_owner_activation, or request_owner_confirmation"
        )
    if selector == "seed":
        return _memory_seed(path, **payload)
    if selector == "task_record":
        unknown = set(payload) - {"task_window_id", "project_id"}
        if unknown:
            raise ValueError(f"unknown task record arguments: {sorted(unknown)}")
        from odibi_anchor.codebase._task_execution import inspect_terminal_records

        requested = payload.get("project_id")
        active = session_state.active_project
        if requested is not None and (active is None or requested != active):
            raise ValueError("terminal task records are restricted to the active project/trust boundary")
        return {
            "kind": "terminal_task_records",
            **inspect_terminal_records(
                path,
                task_window_id=payload.get("task_window_id"),
                project_id=active or "__unresolved_project__",
            ),
        }
    if selector == "replay":
        from odibi_anchor._forensic_replay import build_context_package, inspect, verify
        from odibi_anchor.codebase._task_execution import inspect_terminal_records

        task = str(payload.pop("task_window_id", None) or session_state.task_window_id)
        view = str(payload.pop("view", "context")).lower()
        if payload or view not in {"inspect", "verify", "context"}:
            raise ValueError("memory replay view must be inspect, verify, or context")
        if task != session_state.task_window_id:
            ownership = inspect_terminal_records(path, task_window_id=task)["records"]
            active_project = session_state.active_project
            allowed_projects = {"all"} if active_project is None else {active_project, "all"}
            if len(ownership) != 1 or ownership[0]["project_id"] not in allowed_projects:
                raise ValueError("memory replay is restricted to the active project/trust boundary")
        result = {
            "inspect": lambda: {"events": inspect(path, task)},
            "verify": lambda: verify(path, task),
            "context": lambda: build_context_package(path, task),
        }[view]()
        return {"kind": "memory_replay", "view": view, **result}

    from odibi_anchor.codebase._memory_storage import (
        inspect_memory_storage,
        plan_memory_migration,
    )

    command = str(payload.pop("command", "inspect")).lower()
    from odibi_anchor._dispatcher._boot import _RUNTIME_PATHS

    runtime = _RUNTIME_PATHS
    roots = {
        "source_roots": [runtime.resource_root] if runtime.source_checkout else [],
        "target_roots": [session_state.target_root] if session_state.target_root else [],
        "artifact_roots": [session_state.artifact_root] if session_state.artifact_root else [],
        "resource_roots": [runtime.resource_root],
    }
    storage_environment = {**os.environ, "ANCHOR_MEMORY_DB": path}
    if command == "inspect":
        if payload:
            raise ValueError(f"unknown memory storage arguments: {sorted(payload)}")
        return {
            "kind": "memory_storage",
            **inspect_memory_storage(environment=storage_environment, **roots),
        }
    if command == "plan":
        destination = payload.pop("destination", "")
        return {
            "kind": "memory_storage",
            **plan_memory_migration(
                destination, environment=storage_environment, **payload, **roots,
            ),
        }
    raise ValueError("memory storage command must be inspect or plan")
