"""Operator diagnostics and guarded migrations for distinct-project concurrency."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DOMAINS = ("route", "continuity", "learning", "selection_recovery")
COMMANDS = frozenset({"inspect", "dry_run", "apply", "rollback"})
SELECTION_STATES = (
    "active_pending",
    "terminal_unresolved",
    "abandoned",
    "recovered",
    "semantically_disposed",
    "ambiguous",
)


def _redacted_error(error: Exception) -> dict[str, str]:
    from odibi_anchor._forensic_replay.journal import redact_payload

    return redact_payload(
        {
            "error_type": type(error).__name__,
            "error": str(error)[:500],
        }
    )


def _manual_recovery(domain: str, blocker: str) -> dict[str, Any]:
    return {
        "status": "manual_recovery_required",
        "domain": domain,
        "blocker": blocker,
        "preserve": [
            "the current database and migration backups",
            "continuity records and immutable abandonment/recovery events",
            "the exact diagnostics output and failing runtime version",
        ],
        "next_actions": [
            "Stop new writes for the affected project boundary.",
            "Use a per-project ANCHOR_HOME as temporary containment.",
            "Escalate with the preserved diagnostic packet; do not delete or rewrite history.",
        ],
    }


def _route_status(route_binding: Any) -> dict[str, Any]:
    from odibi_anchor._dispatcher._project import (
        route_binding_diagnostics,
        route_binding_staleness_reason,
    )

    if route_binding is None:
        return {
            "domain": "route",
            "status": "unbound",
            "schema_status": "unavailable",
            "version": None,
            "binding": None,
            "stale_reason": "concurrent-project support requires an exact managed RouteBinding",
            "planned_action": "bind_exact_managed_project",
            "rollback_eligible": False,
            "rollback_blocker": "no immutable RouteBinding is active",
        }
    stale_reason = route_binding_staleness_reason(route_binding)
    return {
        "domain": "route",
        "status": "stale" if stale_reason else "exact",
        "schema_status": "exact",
        "version": route_binding.schema_version,
        "binding": route_binding_diagnostics(route_binding),
        "stale_reason": stale_reason,
        "planned_action": "rebootstrap" if stale_reason else "none",
        "rollback_eligible": False,
        "rollback_blocker": "RouteBinding is immutable for the process lifetime",
    }


def _continuity_status(route_binding: Any, session_state: Any) -> dict[str, Any]:
    from odibi_anchor._dispatcher._session import continuity_migration_status

    if route_binding is None:
        return {
            "domain": "continuity",
            "schema_status": "unavailable_no_exact_route",
            "planned_action": "bind_exact_managed_project",
            "rollback_eligible": False,
        }
    return {
        "domain": "continuity",
        **continuity_migration_status(route_binding, session_state),
    }


def _learning_status(session_state: Any) -> dict[str, Any]:
    from odibi_anchor.codebase.structured_learning_context import (
        active_learning_obligation,
        learning_migration_status,
    )

    status = learning_migration_status()
    project = getattr(session_state, "learning_owner_project_id", None) or getattr(
        session_state, "active_project", None
    )
    task = getattr(session_state, "task_window_id", None)
    owner: dict[str, Any]
    if status.get("schema_status") != "exact":
        owner = {"status": "unavailable_schema_not_exact"}
    elif not project or not task:
        owner = {"status": "unavailable_no_exact_task"}
    else:
        active = active_learning_obligation(project_id=project, task_window_id=task)
        owner = {
            "status": "active" if active else "none_active",
            "project_id": project,
            "task_window_id": task,
            "obligation": active,
        }
    return {"domain": "learning", **status, "owner": owner}


def _selection_status(memory_db: str | Path, session_state: Any) -> dict[str, Any]:
    from odibi_anchor.codebase._memory_lifecycle import (
        RecoveryUnavailable,
        selection_recovery,
        selection_recovery_migration_status,
    )

    migration = selection_recovery_migration_status(memory_db)
    inspection: dict[str, Any] = {
        "status": "unavailable_no_exact_task",
        "states": [],
        "counts": {state: 0 for state in SELECTION_STATES},
        "selected_never_applied": 0,
    }
    required = {
        "project_id": getattr(session_state, "active_project", None),
        "anchor_home": getattr(session_state, "anchor_home", None),
        "project_root": getattr(session_state, "project_root", None),
        "target_root": getattr(session_state, "target_root", None),
        "artifact_root": getattr(session_state, "artifact_root", None),
        "current_task_window_id": getattr(session_state, "task_window_id", None),
    }
    if migration.get("schema_status") == "exact" and all(required.values()):
        try:
            inspection = {
                "status": "available",
                **selection_recovery(
                    memory_db,
                    action="inspect",
                    **required,
                    repository_provider_id=getattr(
                        getattr(session_state, "repository_provider", None),
                        "provider_id",
                        None,
                    ),
                    trust_domain=getattr(session_state, "trust_domain", None),
                ),
            }
        except RecoveryUnavailable:
            inspection = {
                **inspection,
                "status": "unavailable_owner_or_task_not_authoritative",
            }
    return {
        "domain": "selection_recovery",
        **migration,
        "inspection": inspection,
        "state_labels": list(SELECTION_STATES),
        "metric_label": "selected_never_applied",
        "metric_interpretation": "neutral lifecycle total, not a leak count",
    }


def _inspect_domains(
    domains: Sequence[str],
    *,
    route_binding: Any,
    session_state: Any,
    memory_db: str | Path,
) -> list[dict[str, Any]]:
    builders = {
        "route": lambda: _route_status(route_binding),
        "continuity": lambda: _continuity_status(route_binding, session_state),
        "learning": lambda: _learning_status(session_state),
        "selection_recovery": lambda: _selection_status(memory_db, session_state),
    }
    return [builders[domain]() for domain in domains]


def _apply_domain(
    domain: str,
    *,
    route_binding: Any,
    session_state: Any,
    memory_db: str | Path,
) -> dict[str, Any]:
    if domain == "route":
        result = _route_status(route_binding)
        if result["status"] != "exact":
            raise RuntimeError(str(result["stale_reason"]))
        return {**result, "apply_status": "verified_no_migration"}
    if domain == "continuity":
        from odibi_anchor._dispatcher._session import _load_continuity_state

        applied = _load_continuity_state(route_binding, session_state, migrate_legacy=True)
        return {
            **_continuity_status(route_binding, session_state),
            "apply_status": "verified",
            "legacy_migration": applied.get("legacy_migration"),
        }
    if domain == "learning":
        from odibi_anchor.codebase.structured_learning_context import initialize_learning_schema

        applied = initialize_learning_schema()
        return {**_learning_status(session_state), "apply_status": "verified", "apply": applied}
    from odibi_anchor.codebase._memory_lifecycle import initialize_schema

    applied = initialize_schema(memory_db)
    return {
        **_selection_status(memory_db, session_state),
        "apply_status": "verified",
        "apply": applied,
    }


def _rollback_domain(domain: str, *, memory_db: str | Path) -> dict[str, Any]:
    if domain in {"route", "continuity"}:
        return {
            "domain": domain,
            "status": "unavailable",
            "rollback_eligible": False,
            "manual_recovery": _manual_recovery(
                domain,
                "immutable routing and additive continuity evidence are not deleted automatically",
            ),
        }
    if domain == "learning":
        from odibi_anchor.codebase.structured_learning_context import (
            learning_migration_status,
            rollback_learning_obligation_migration,
        )

        status = learning_migration_status()
        if not status.get("rollback_eligible"):
            blocker = str(status.get("rollback_blocker") or "no eligible learning migration")
            return {
                "domain": domain,
                "status": "unavailable",
                **status,
                "manual_recovery": _manual_recovery(domain, blocker),
            }
        return {
            "domain": domain,
            "status": "rolled_back",
            "result": rollback_learning_obligation_migration(),
        }
    from odibi_anchor.codebase._memory_lifecycle import (
        rollback_recovery_schema,
        selection_recovery_migration_status,
    )

    status = selection_recovery_migration_status(memory_db)
    if not status.get("rollback_eligible"):
        blocker = str(status.get("rollback_blocker") or "no eligible selection migration")
        return {
            "domain": domain,
            "status": "unavailable",
            **status,
            "manual_recovery": _manual_recovery(domain, blocker),
        }
    return {
        "domain": domain,
        "status": "rolled_back",
        "result": rollback_recovery_schema(memory_db),
    }


def _render(result: Mapping[str, Any]) -> str:
    lines = [
        "# Concurrent-project operations",
        "",
        f"Command: `{result['command']}`  ",
        "Cross-domain atomicity: `false`  ",
        f"Support status: `{result['support_status']}`",
        "",
    ]
    for domain in result["domains"]:
        status = domain.get("status", domain.get("schema_status", "unknown"))
        lines.append(f"- **{domain['domain']}**: `{status}`")
    lines.extend(["", "Concurrent support applies only to distinct managed projects."])
    return "\n".join(lines)


def concurrency_action(
    *args: Any,
    route_binding: Any,
    session_state: Any,
    memory_db: str | Path,
    command: str | None = None,
    domains: Sequence[str] | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Inspect or operate independent concurrency migration domains.

    Args:
        args: Optional single positional command.
        route_binding: Immutable runtime route authority.
        session_state: Current process-local session and task identity.
        memory_db: Shared SQLite memory database path.
        command: inspect, dry_run, apply, or rollback.
        domains: Ordered subset of route, continuity, learning, and selection_recovery.
        output_format: dict or markdown.

    Returns:
        Bounded per-domain diagnostics with explicit non-atomic semantics.

    Raises:
        ValueError: If the command, domains, or output format is invalid.
    """
    if len(args) > 1 or (args and command is not None):
        raise ValueError("concurrency accepts one positional command or command=, not both")
    selected_command = str(args[0] if args else command or "inspect").strip().lower()
    if selected_command not in COMMANDS:
        raise ValueError("concurrency command must be inspect, dry_run, apply, or rollback")
    if output_format not in {"dict", "markdown"}:
        raise ValueError("output_format must be dict or markdown")
    selected_domains = list(DOMAINS if domains is None else domains)
    if (
        not selected_domains
        or any(not isinstance(domain, str) or domain not in DOMAINS for domain in selected_domains)
        or len(set(selected_domains)) != len(selected_domains)
    ):
        raise ValueError(f"domains must be a unique non-empty subset of {list(DOMAINS)}")
    if selected_command == "rollback" and len(selected_domains) != 1:
        raise ValueError("rollback requires exactly one explicit domain")

    if selected_command in {"inspect", "dry_run"}:
        results = _inspect_domains(
            selected_domains,
            route_binding=route_binding,
            session_state=session_state,
            memory_db=memory_db,
        )
    elif selected_command == "rollback":
        results = [_rollback_domain(selected_domains[0], memory_db=memory_db)]
    else:
        results = []
        for domain in selected_domains:
            try:
                results.append(
                    _apply_domain(
                        domain,
                        route_binding=route_binding,
                        session_state=session_state,
                        memory_db=memory_db,
                    )
                )
            except Exception as exc:
                redacted = _redacted_error(exc)
                results.append(
                    {
                        "domain": domain,
                        "status": "blocked",
                        **redacted,
                        "manual_recovery": _manual_recovery(
                            domain, str(redacted.get("error", "operation failed"))
                        ),
                    }
                )
                break
    blocked = any(
        result.get("status") in {"blocked", "stale", "unbound", "unavailable"}
        or str(result.get("schema_status", "")).startswith(("blocked", "drift", "unavailable"))
        for result in results
    )
    from odibi_anchor.codebase._sqlite_contention import STORE, TIMEOUT_MS

    result: dict[str, Any] = {
        "kind": "concurrent_project_operations",
        "command": selected_command,
        "atomic": False,
        "support_scope": "distinct_managed_projects_only",
        "same_project_writers": "unsupported_fail_closed",
        "support_status": "blocked" if blocked else "release_evidence_required",
        "domains": results,
        "persistence_contention": {
            "store": STORE,
            "timeout_ms": TIMEOUT_MS,
            "failure_type": "PersistenceContentionError",
            "failure_fields": [
                "store",
                "operation",
                "owner_key_kind",
                "duration_ms",
                "timeout_ms",
                "retryable",
            ],
            "cumulative_wait_and_error_counts": "unavailable",
            "operator_action": "retain the bounded error and retry only after owner-safe inspection",
        },
        "release_gate": {
            "required": True,
            "evidence": [
                "WI-2026-0007 two-process scenario matrix",
                "legacy and abandonment/recovery tests",
                "wheel and installed-distribution verification",
                "qualification for each claimed host profile",
            ],
        },
    }
    return _render(result) if output_format == "markdown" else result
