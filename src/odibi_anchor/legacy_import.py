"""Explicit, fail-closed import of user-selected Context Workbench state."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

_COPY_NAMES = ("workspace",)
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


def _path_exists(path: Path) -> bool:
    """Return true for regular entries and dangling symlinks."""
    return path.exists() or path.is_symlink()


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


def _open_task_count(database: Path) -> int:
    """Return exact open legacy task count without loading untrusted payloads."""
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "accepted_task_records" not in tables:
            return 0
        if "accepted_task_events" not in tables:
            raise RuntimeError("legacy task authority tables are incomplete")
        return int(connection.execute(
            "SELECT COUNT(*) FROM accepted_task_records r WHERE NOT EXISTS "
            "(SELECT 1 FROM accepted_task_events e "
            "WHERE e.task_window_id=r.task_window_id AND e.event_type='closed')"
        ).fetchone()[0])
    finally:
        connection.close()


def _descriptor_fields(descriptor: Path) -> dict[str, str]:
    """Parse the flat routing authority without accepting ambiguous YAML."""
    try:
        lines = descriptor.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"legacy project descriptor is unreadable: {descriptor.parent.name}") from exc
    if not lines or lines[0].strip() != "---":
        raise RuntimeError(f"legacy project descriptor is incomplete: {descriptor.parent.name}")
    values: dict[str, str] = {}
    terminated = False
    for line in lines[1:]:
        if line.strip() == "---":
            terminated = True
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise RuntimeError(f"legacy project descriptor is ambiguous: {descriptor.parent.name}")
        key, value = line.split(":", 1)
        key = key.strip()
        if key in values:
            raise RuntimeError(
                f"legacy project descriptor has duplicate field {key!r}: {descriptor.parent.name}"
            )
        raw_value = value.strip()
        runtime_value = raw_value.strip("\"'")
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in "\"'":
            raw_value = raw_value[1:-1]
        if raw_value != runtime_value:
            raise RuntimeError(
                f"legacy project descriptor has ambiguous quoting: {descriptor.parent.name}"
            )
        values[key] = raw_value
    if not terminated or not {"id", "project_type", "target_root"}.issubset(values):
        raise RuntimeError(f"legacy project descriptor is incomplete: {descriptor.parent.name}")
    if values["id"] != descriptor.parent.name:
        raise RuntimeError(f"legacy project descriptor has an ambiguous ID: {descriptor.parent.name}")
    if values["project_type"] not in {"managed", "referenced"}:
        raise RuntimeError(
            f"legacy project descriptor has unsupported project_type: {descriptor.parent.name}"
        )
    if not Path(values["target_root"]).is_absolute():
        raise RuntimeError(f"legacy project descriptor has an ambiguous target: {descriptor.parent.name}")
    return values


def _project_rewrites(source: Path, destination: Path) -> list[dict[str, str]]:
    """Validate descriptors and plan exact self-target rewrites for managed projects."""
    workspace = source / "workspace"
    if workspace.is_symlink():
        raise RuntimeError("legacy workspace is a symlink and cannot be imported safely")
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("legacy workspace contains a symlink and cannot be imported safely")
    projects = workspace / "projects"
    rewrites: list[dict[str, str]] = []
    if not projects.is_dir():
        return rewrites
    for descriptor in sorted(projects.glob("*/PROJECT.md")):
        fields = _descriptor_fields(descriptor)
        if fields["project_type"] != "managed":
            continue
        old_target = Path(fields["target_root"]).resolve()
        expected = descriptor.parent.resolve()
        if old_target != expected:
            raise RuntimeError(
                f"managed legacy project has an ambiguous target: {descriptor.parent.name}"
            )
        new_target = destination / "workspace" / "projects" / descriptor.parent.name
        rewrites.append({
            "project_id": descriptor.parent.name,
            "descriptor_sha256": hashlib.sha256(descriptor.read_bytes()).hexdigest(),
            "old_target": str(old_target),
            "new_target": str(new_target.resolve()),
        })
    return rewrites


def _workspace_digest(workspace: Path) -> str:
    """Hash a canonical typed manifest of every workspace entry."""
    if workspace.is_symlink() or not workspace.is_dir():
        raise RuntimeError("legacy workspace must be a regular directory")
    manifest: list[dict[str, str]] = []
    for path in sorted(workspace.rglob("*"), key=lambda item: item.relative_to(workspace).as_posix()):
        relative = path.relative_to(workspace).as_posix()
        if path.is_symlink():
            raise RuntimeError("legacy workspace contains a symlink and cannot be imported safely")
        if path.is_dir():
            manifest.append({"path": relative, "type": "directory"})
        elif path.is_file():
            manifest.append({
                "path": relative,
                "type": "file",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        else:
            raise RuntimeError("legacy workspace contains an unsupported filesystem entry")
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    database = source / ".agent_memory.db"
    if database.is_symlink():
        raise RuntimeError("legacy database is a symlink and cannot be imported safely")
    selected = [name for name in _COPY_NAMES if (source / name).exists()]
    if not selected:
        raise RuntimeError("selected CW_HOME contains no supported legacy state")
    collisions = [name for name in selected if _path_exists(destination / name)]
    if collisions:
        raise RuntimeError("destination collision: " + ", ".join(collisions))
    versions = _schema_versions(database)
    open_tasks = _open_task_count(database)
    if open_tasks:
        raise RuntimeError(
            f"legacy database contains {open_tasks} open task(s); close them before import"
        )
    rewrites = _project_rewrites(source, destination)
    workspace_sha256 = _workspace_digest(source / "workspace")
    identity = hashlib.sha256(json.dumps(
        {"source": str(source), "destination": str(destination), "selected": selected,
         "schemas": versions, "project_rewrites": rewrites,
         "workspace_sha256": workspace_sha256},
        sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {"kind": "legacy_import_plan", "read_only": True, "source_cw_home": str(source),
            "anchor_home": str(destination), "selected": selected, "schemas": versions,
            "legacy_version": "0.11.0", "project_rewrites": rewrites,
            "workspace_sha256": workspace_sha256,
            "open_tasks": 0, "live_history_imported": False,
            "history_disposition": "backup_only",
            "activation_exclusions": ["projects/*/continuity"],
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
    backup_root = destination / "legacy-import-backups"
    if backup_root.is_symlink():
        raise RuntimeError("legacy import backup destination is a symlink")
    backup = backup_root / plan["plan_id"].split(":", 1)[1]
    if _path_exists(backup):
        raise RuntimeError("legacy import backup collision")
    backup.mkdir(parents=True)
    source_database = source / ".agent_memory.db"
    backup_database = backup / ".agent_memory.db"
    _backup_database(source_database, backup_database)
    copied: list[str] = []
    created_targets: list[Path] = []
    try:
        for name in plan["selected"]:
            source_path = source / name
            backup_path = backup / name
            if source_path.is_dir():
                shutil.copytree(source_path, backup_path)
            else:
                shutil.copy2(source_path, backup_path)
        if _schema_versions(backup_database) != plan["schemas"] or _open_task_count(backup_database):
            raise RuntimeError("legacy database changed while its backup was being created")
        if _workspace_digest(backup / "workspace") != plan["workspace_sha256"]:
            raise RuntimeError("legacy workspace changed while its backup was being created")
        for name in plan["selected"]:
            backup_path = backup / name
            target_path = destination / name
            if _path_exists(target_path):
                raise RuntimeError(f"destination collision: {name}")
            if backup_path.is_dir():
                target_path.mkdir()
                created_targets.append(target_path)
                shutil.copytree(backup_path, target_path, dirs_exist_ok=True)
            else:
                target_path.touch(exist_ok=False)
                created_targets.append(target_path)
                shutil.copy2(backup_path, target_path)
            copied.append(name)
        for continuity in (destination / "workspace" / "projects").glob("*/continuity"):
            shutil.rmtree(continuity)
        for rewrite in plan["project_rewrites"]:
            descriptor = destination / "workspace" / "projects" / rewrite["project_id"] / "PROJECT.md"
            if hashlib.sha256(descriptor.read_bytes()).hexdigest() != rewrite["descriptor_sha256"]:
                raise RuntimeError("legacy managed-project target changed during import")
            lines = descriptor.read_text(encoding="utf-8").splitlines(keepends=True)
            indexes = [index for index, line in enumerate(lines) if line.split(":", 1)[0].strip() == "target_root"]
            if len(indexes) != 1:
                raise RuntimeError("legacy managed-project target changed during import")
            ending = "\n" if lines[indexes[0]].endswith("\n") else ""
            lines[indexes[0]] = f"target_root: {rewrite['new_target']}{ending}"
            descriptor.write_text("".join(lines), encoding="utf-8")
    except Exception:
        for path in reversed(created_targets):
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        raise
    database_sha256 = hashlib.sha256(backup_database.read_bytes()).hexdigest()
    return {"kind": "legacy_import_result", "status": "applied", "backup": str(backup),
            "copied": copied, "legacy_database_backup_sha256": database_sha256,
            "live_history_imported": False, "history_disposition": "verified_backup_only",
            "activation_exclusions": plan["activation_exclusions"],
            "project_rewrites": plan["project_rewrites"], "plan_id": plan["plan_id"]}


__all__ = ["apply_legacy_import", "plan_legacy_import"]
