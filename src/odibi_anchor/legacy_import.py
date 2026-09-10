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
            if "anchor_schema_versions" not in tables:
                return []
            rows = connection.execute(
                "SELECT domain,version,schema_sha256 FROM anchor_schema_versions ORDER BY domain"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("legacy database is incompatible or unreadable") from exc
    versions = [{"domain": row[0], "version": row[1], "schema_sha256": row[2]} for row in rows]
    supported = _supported_schemas()
    for item in versions:
        authority = supported.get(item["domain"])
        version = item["version"]
        checksum = item["schema_sha256"]
        if (authority is None or type(version) is not int or version < 1
                or version > authority[0] or not isinstance(checksum, str)
                or len(checksum) != 64
                or (version == authority[0] and checksum != authority[1])):
            raise RuntimeError("legacy database has a newer or incompatible schema")
    return versions


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
            if source_path.is_dir():
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
    return {"kind": "legacy_import_result", "status": "applied", "backup": str(backup),
            "copied": attempted, "plan_id": plan["plan_id"]}


__all__ = ["apply_legacy_import", "plan_legacy_import"]
