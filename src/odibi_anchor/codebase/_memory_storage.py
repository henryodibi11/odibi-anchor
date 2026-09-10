"""Read-only memory-store diagnostics and non-destructive migration planning."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from odibi_anchor._runtime_paths import resolve_runtime_paths


def _canonical(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _selected_profile(config: Mapping[str, Any], environment: Mapping[str, str]) -> tuple[str | None, Mapping[str, Any]]:
    profiles = config.get("environment", {}).get("profiles", {})
    requested = environment.get("ANCHOR_PROFILE")
    if requested:
        if requested not in profiles:
            raise ValueError(
                f"Unknown ANCHOR_PROFILE {requested!r}; expected one of {sorted(profiles)}"
            )
        return requested, profiles[requested]
    if environment.get("DATABRICKS_RUNTIME_VERSION") and "databricks" in profiles:
        return "databricks", profiles["databricks"]
    if environment.get("CI") and "ci" in profiles:
        return "ci", profiles["ci"]
    for name, candidate in profiles.items():
        if candidate.get("platform") == "local" and candidate.get("home") and Path(candidate["home"]).is_dir():
            return name, candidate
    return None, {}


def resolve_memory_storage(
    *,
    environment: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the canonical store using runtime precedence, without filesystem writes.

    Only allow-listed metadata is returned; environment/config values unrelated to
    routing are never copied into the result.
    """
    env = os.environ if environment is None else environment
    if config is None:
        try:
            from odibi_anchor._dispatcher._boot import _load_config

            cfg = _load_config()
        except (ImportError, OSError, ValueError):  # pragma: no cover - standalone package fallback
            cfg = {}
    else:
        cfg = config
    profile_name, profile = _selected_profile(cfg, env)
    runtime = resolve_runtime_paths(profile.get("anchor_root"), environment=env)
    if env.get("ANCHOR_MEMORY_DB"):
        raw, source = env["ANCHOR_MEMORY_DB"], "environment:ANCHOR_MEMORY_DB"
    elif profile.get("memory_db"):
        raw, source = profile["memory_db"], "profile:memory_db"
    else:
        raw, source = runtime.anchor_home / ".agent_memory.db", "default:anchor_home"
    trust_domain = profile.get("trust_domain") or env.get("ANCHOR_TRUST_DOMAIN") or "default"
    return {
        "path": str(_canonical(raw)),
        "source": source,
        "explicit": not source.startswith("default:"),
        "profile": profile_name or "default",
        "trust_domain": str(trust_domain),
    }


def _roots(
    *,
    source_roots: Sequence[str | os.PathLike[str]] = (),
    target_roots: Sequence[str | os.PathLike[str]] = (),
    artifact_roots: Sequence[str | os.PathLike[str]] = (),
    resource_roots: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, list[Path]]:
    return {
        "source": [_canonical(p) for p in source_roots],
        "target": [_canonical(p) for p in target_roots],
        "artifact": [_canonical(p) for p in artifact_roots],
        "resource": [_canonical(p) for p in resource_roots],
    }


def inspect_memory_storage(
    *,
    environment: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    source_roots: Sequence[str | os.PathLike[str]] = (),
    target_roots: Sequence[str | os.PathLike[str]] = (),
    artifact_roots: Sequence[str | os.PathLike[str]] = (),
    resource_roots: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """Report store routing, safety and SQLite health without creating or changing it."""
    routing = resolve_memory_storage(environment=environment, config=config)
    path = Path(routing["path"])
    roots = _roots(source_roots=source_roots, target_roots=target_roots,
                   artifact_roots=artifact_roots, resource_roots=resource_roots)
    overlaps = sorted(
        ({"kind": kind, "root": str(root)} for kind, values in roots.items()
         for root in values if _overlap(path, root)),
        key=lambda item: (item["kind"], item["root"]),
    )
    exists, tables, integrity, error = path.is_file(), [], "not_checked", None
    readable = exists and os.access(path, os.R_OK)
    writable = (exists and os.access(path, os.W_OK)) or (not exists and path.parent.is_dir() and os.access(path.parent, os.W_OK))
    if readable:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                tables = sorted(row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ))
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            integrity, error = "unavailable", type(exc).__name__
    fixed = {
        path.with_name(path.name + ".bak"), path.with_name(path.name + ".backup"),
        path.with_suffix(path.suffix + ".bak"),
    }
    candidates = sorted(
        {candidate for candidate in (*fixed, *path.parent.glob(path.name + ".pre-*.bak"))
         if candidate.is_file()},
        key=str,
    )
    backup_details = []
    for candidate in candidates:
        candidate_integrity, candidate_error = "unavailable", None
        try:
            backup_connection = sqlite3.connect(
                f"{candidate.resolve().as_uri()}?mode=ro&immutable=1", uri=True,
            )
            try:
                candidate_integrity = backup_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
            finally:
                backup_connection.close()
        except (OSError, sqlite3.Error) as exc:
            candidate_error = type(exc).__name__
        backup_details.append({
            "path": str(candidate), "integrity": candidate_integrity,
            "usable": candidate_integrity == "ok", "diagnostic_error": candidate_error,
        })
    backup_candidates = [detail["path"] for detail in backup_details]
    return {
        **routing,
        "overlaps": overlaps,
        "safe_location": not overlaps,
        "exists": exists,
        "readable": readable,
        "writable": writable,
        "schema_tables": tables,
        "integrity": integrity,
        "diagnostic_error": error,
        "backups": backup_candidates,
        "backup_present": bool(backup_candidates),
        "backup_details": backup_details,
        "usable_backups": [detail["path"] for detail in backup_details if detail["usable"]],
    }


def plan_memory_migration(
    destination: str | os.PathLike[str],
    *,
    destination_trust_domain: str | None = None,
    allow_trust_domain_crossing: bool = False,
    environment: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    source_roots: Sequence[str | os.PathLike[str]] = (),
    target_roots: Sequence[str | os.PathLike[str]] = (),
    artifact_roots: Sequence[str | os.PathLike[str]] = (),
    resource_roots: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    """Return a deterministic owner-review plan. This function performs no migration."""
    text = os.fspath(destination)
    if not text or text.startswith("~") or not Path(text).is_absolute():
        raise ValueError("destination must be an explicit absolute, non-tilde path")
    diagnostic = inspect_memory_storage(
        environment=environment, config=config, source_roots=source_roots,
        target_roots=target_roots, artifact_roots=artifact_roots, resource_roots=resource_roots,
    )
    source, target = Path(diagnostic["path"]), _canonical(text)
    forbidden = _roots(source_roots=source_roots, target_roots=target_roots,
                       artifact_roots=artifact_roots, resource_roots=resource_roots)
    if target == source:
        raise ValueError("destination must differ from the source database")
    collisions = sorted((kind, str(root)) for kind, values in forbidden.items() for root in values if _overlap(target, root))
    if collisions:
        raise ValueError("destination overlaps a forbidden root")
    destination_domain = destination_trust_domain or diagnostic["trust_domain"]
    crossing = destination_domain != diagnostic["trust_domain"]
    if crossing and not allow_trust_domain_crossing:
        raise ValueError("trust-domain crossing is denied by default")
    return {
        "kind": "memory_storage_migration_plan",
        "source": str(source),
        "destination": str(target),
        "source_trust_domain": diagnostic["trust_domain"],
        "destination_trust_domain": destination_domain,
        "trust_domain_crossing": crossing,
        "execution_authorized": False,
        "preconditions": [
            "obtain storage-owner approval",
            "require source exists, is readable, and passes SQLite integrity_check",
            "require destination does not exist and parent is writable",
        ],
        "steps": [
            "create a timestamped source backup using the SQLite backup API",
            "verify backup integrity and schema tables against the source",
            "copy into a temporary file in the destination directory using the SQLite backup API",
            "fsync the temporary file and destination directory",
            "verify temporary integrity, schema tables, and application read checks",
            "atomically publish the temporary file without overwriting the destination",
            "verify the published database, then separately update explicit routing configuration",
            "retain the source and backup until owner-approved rollback retention expires",
        ],
        "destructive_actions": [],
    }
