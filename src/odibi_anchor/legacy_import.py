"""Explicit, fail-closed import of user-selected Context Workbench state."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

_COPY_NAMES = (".agent_memory.db", "workspace")
_LEGACY_SCHEMA_TABLE = "cw_schema_versions"
_ANCHOR_SCHEMA_TABLE = "anchor_schema_versions"
_LEGACY_V0110_SCHEMAS = {
    "task_adoption": (1, "31108b8a8d0073550228a3f7ce3b1e71ad13a486a8e8483ffb9682a32d0930f9"),
    "memory_lifecycle": (3, "cbbb3e32bdabb280f2212368dcef6770ebb0489f749ecdc1e563b4d928bbbdd7"),
    "memory_promotion": (4, "52d9c0f4d06372a731805225cb696f08ab363c3ec2ac815c1162d85a5863251f"),
    "memory_verifier": (1, "4058c26e360331d60693e5541cb1bfc854e76ffb63e54fdbe495149f5a996aa0"),
    "task_authority": (1, "f1caf8ca47c6ba2b1562630e647cdeba3fd6629ef4a1e43c0379b649dcda9970"),
    "task_execution": (2, "20cbd3cc5d3f4b9a746de84d39321c82797623fe64e8cf1ffea15e9ead7353a7"),
    "structured_learning": (2, "cb34c4a8295935d1fa0032c8595ab22dc4333166b7a4984c74d90539b098f561"),
}


def _supported_schemas() -> dict[str, tuple[int, str]]:
    """Load the runtime's schema authorities rather than duplicating versions."""
    from odibi_anchor.codebase import (
        _adopted_dirty,
        _memory_lifecycle,
        _memory_promotion,
        _memory_verifier,
        _task_authority,
        _task_execution,
        structured_learning_context,
    )

    modules = (
        _adopted_dirty, _memory_lifecycle, _memory_promotion, _memory_verifier,
        _task_authority, _task_execution, structured_learning_context,
    )
    return {
        module.DOMAIN: (
            int(module.SCHEMA_VERSION if hasattr(module, "SCHEMA_VERSION") else module.VERSION),
            module.SCHEMA_SHA256,
        )
        for module in modules
    }


def _directory(value: str | os.PathLike[str], name: str, *, must_exist: bool) -> Path:
    text = os.fspath(value)
    path = Path(text)
    if not text or text.startswith("~") or "\n" in text or "\r" in text or not path.is_absolute():
        raise ValueError(f"{name} must be an explicit absolute, single-line path")
    resolved = path.resolve()
    if must_exist and not resolved.is_dir():
        raise ValueError(f"{name} must be an existing directory")
    return resolved


def _schema_versions(database: Path) -> list[dict[str, Any]]:
    if not database.exists():
        return []
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError("legacy database integrity check failed")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if _ANCHOR_SCHEMA_TABLE in tables:
                raise RuntimeError("selected state is already an Odibi Anchor home")
            if _LEGACY_SCHEMA_TABLE not in tables:
                raise RuntimeError("legacy database has no supported schema authority")
            rows = connection.execute(
                f"SELECT domain,version,schema_sha256 FROM {_LEGACY_SCHEMA_TABLE} ORDER BY domain"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("legacy database is incompatible or unreadable") from exc
    versions = [{"domain": row[0], "version": row[1], "schema_sha256": row[2]} for row in rows]
    supported = _supported_schemas()
    if {item["domain"] for item in versions} != set(_LEGACY_V0110_SCHEMAS):
        raise RuntimeError("legacy database does not match the complete v0.11.0 schema set")
    for item in versions:
        authority = supported.get(item["domain"])
        legacy_authority = _LEGACY_V0110_SCHEMAS.get(item["domain"])
        observed = (item["version"], item["schema_sha256"])
        if authority is None or observed != legacy_authority or authority[0] != observed[0]:
            raise RuntimeError("legacy database has a newer or incompatible schema")
    return versions


def _backup_database(source: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite backup and verify it."""
    source_connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        if destination_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("legacy database backup integrity check failed")
    finally:
        destination_connection.close()
        source_connection.close()


def _convert_database(database: Path) -> None:
    """Convert the exact v0.11.0 schema authority to Anchor naming atomically."""
    supported = _supported_schemas()
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"ALTER TABLE {_LEGACY_SCHEMA_TABLE} RENAME TO {_ANCHOR_SCHEMA_TABLE}"
        )
        for domain, (_version, legacy_checksum) in _LEGACY_V0110_SCHEMAS.items():
            anchor_checksum = supported[domain][1]
            if anchor_checksum != legacy_checksum:
                connection.execute(
                    f"UPDATE {_ANCHOR_SCHEMA_TABLE} SET schema_sha256=? "
                    "WHERE domain=? AND schema_sha256=?",
                    (anchor_checksum, domain, legacy_checksum),
                )
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("converted database integrity check failed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def plan_legacy_import(
    source_cw_home: str | os.PathLike[str],
    *,
    anchor_home: str | os.PathLike[str],
) -> dict[str, Any]:
    """Inspect one explicitly selected CW_HOME and return a mutation-free plan."""
    source = _directory(source_cw_home, "source CW_HOME", must_exist=True)
    destination = _directory(anchor_home, "ANCHOR_HOME", must_exist=False)
    if source == destination or source in destination.parents or destination in source.parents:
        raise RuntimeError("source CW_HOME and ANCHOR_HOME must not overlap")
    selected = [name for name in _COPY_NAMES if (source / name).exists()]
    if not selected:
        raise RuntimeError("selected CW_HOME contains no supported legacy state")
    collisions = [name for name in selected if (destination / name).exists()]
    if collisions:
        raise RuntimeError("destination collision: " + ", ".join(collisions))
    versions = _schema_versions(source / ".agent_memory.db")
    identity = hashlib.sha256(json.dumps(
        {"source": str(source), "destination": str(destination), "selected": selected,
         "schemas": versions}, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {"kind": "legacy_import_plan", "read_only": True, "source_cw_home": str(source),
            "anchor_home": str(destination), "selected": selected, "schemas": versions,
            "legacy_version": "0.11.0", "schema_conversion": "cw_to_anchor",
            "plan_id": f"sha256:{identity}", "applicable": True}


def apply_legacy_import(plan: dict[str, Any]) -> dict[str, Any]:
    """Back up selected source state, then copy it additively into ANCHOR_HOME."""
    if not isinstance(plan, dict) or plan.get("kind") != "legacy_import_plan":
        raise ValueError("apply requires a legacy_import_plan")
    current = plan_legacy_import(plan["source_cw_home"], anchor_home=plan["anchor_home"])
    if current != plan:
        raise RuntimeError("legacy import plan is stale")
    source = Path(plan["source_cw_home"])
    destination = Path(plan["anchor_home"])
    destination.mkdir(parents=True, exist_ok=True)
    backup = destination / "legacy-import-backups" / plan["plan_id"].split(":", 1)[1]
    if backup.exists():
        raise RuntimeError("legacy import backup collision")
    backup.mkdir(parents=True)
    attempted: list[str] = []
    try:
        for name in plan["selected"]:
            attempted.append(name)
            source_path = source / name
            backup_path = backup / name
            target_path = destination / name
            if name == ".agent_memory.db":
                _backup_database(source_path, backup_path)
                shutil.copy2(backup_path, target_path)
                _convert_database(target_path)
            elif source_path.is_dir():
                shutil.copytree(source_path, backup_path)
                shutil.copytree(backup_path, target_path)
            else:
                shutil.copy2(source_path, backup_path)
                shutil.copy2(backup_path, target_path)
    except Exception:
        for name in attempted:
            path = destination / name
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        raise
    database = destination / ".agent_memory.db"
    database_sha256 = hashlib.sha256(database.read_bytes()).hexdigest() if database.is_file() else None
    return {"kind": "legacy_import_result", "status": "applied", "backup": str(backup),
            "copied": attempted, "database_sha256": database_sha256,
            "schema_conversion": plan["schema_conversion"], "plan_id": plan["plan_id"]}


__all__ = ["apply_legacy_import", "plan_legacy_import"]
