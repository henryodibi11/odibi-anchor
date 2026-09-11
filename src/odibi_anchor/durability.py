"""Immutable durable snapshots for a live, local SQLite database.

Durable snapshot bytes are treated as opaque files.  SQLite is opened only on the
live local source or on a local staging copy during restore and inspection.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from odibi_anchor.codebase._migration_backup import logical_digest

_MANIFEST_SUFFIX = ".manifest.json"
_SNAPSHOT_SUFFIX = ".sqlite3"
_FORMAT = "odibi-anchor-durable-snapshot-v1"
_DURABLE_LIVE_PREFIXES = (Path("/Workspace"), Path("/Volumes"), Path("/dbfs"))
_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})\Z")
_OWNER_TABLE = "anchor_authority_identity"


def _next(operation: str, **arguments: Any) -> dict[str, Any]:
    return {"operation": operation, "arguments": arguments}


def _absolute_path(value: str | os.PathLike[str], label: str) -> Path:
    text = os.fspath(value)
    if not isinstance(text, str) or not text:
        raise ValueError(f"{label} must be a non-empty path string")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label} must not contain line breaks")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    _reject_symlinks(path, label)
    return Path(os.path.abspath(path))


def _authority_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("authority_id must be a safe non-empty ID")
    return value


def _reject_symlinks(path: Path, label: str) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symlinks")
        if current == current.parent:
            return
        current = current.parent


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject_overlap(first: Path, second: Path, labels: str) -> None:
    if _is_within(first, second) or _is_within(second, first):
        raise ValueError(f"{labels} must not overlap")


def qualify_paths(
    *,
    source_db: str | os.PathLike[str] | None = None,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str] | None = None,
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Secret-free, read-only qualification of explicit snapshot/restore paths."""
    root = _absolute_path(durable_root, "durable_root")
    authority = _authority_id(authority_id)
    source = _absolute_path(source_db, "source_db") if source_db is not None else None
    destination = _absolute_path(destination_db, "destination_db") if destination_db is not None else None
    if source is None and destination is None:
        raise ValueError("source_db or destination_db is required")
    for path, label in ((source, "source_db"), (destination, "destination_db")):
        if path is not None:
            _reject_overlap(path, root, f"{label} and durable_root")
    if databricks:
        for path, label in ((source, "source_db"), (destination, "destination_db")):
            if path is not None and any(_is_within(path, prefix) for prefix in _DURABLE_LIVE_PREFIXES):
                raise ValueError(f"{label} must be on local compute when databricks=True")
    operation = "snapshot_state" if source is not None else "restore_latest"
    arguments: dict[str, Any] = {"durable_root": str(root)}
    arguments["authority_id"] = authority
    if source is not None:
        arguments["source_db"] = str(source)
        arguments["databricks"] = databricks
    else:
        arguments["destination_db"] = str(destination)
    return {
        "kind": "durability_qualification",
        "qualified": True,
        "source_db": None if source is None else str(source),
        "destination_db": None if destination is None else str(destination),
        "durable_root": str(root),
        "authority_id": authority,
        "databricks": bool(databricks),
        "next_operation": _next(operation, **arguments),
    }


def qualify_durability(
    *,
    source_db: str | os.PathLike[str] | None = None,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str] | None = None,
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Publicly named qualification entry point; performs no filesystem mutation."""
    return qualify_paths(
        source_db=source_db,
        durable_root=durable_root,
        destination_db=destination_db,
        authority_id=authority_id,
        databricks=databricks,
    )


def ensure_database_authority(
    database: str | os.PathLike[str], *, authority_id: str, trust_domain: str,
    initialize: bool = False,
) -> dict[str, Any]:
    """Bind or verify the one authority that owns a live SQLite store."""
    path = _absolute_path(database, "database")
    authority = _authority_id(authority_id)
    if trust_domain != "work":
        raise ValueError("trust_domain must be 'work'")
    if not path.exists() and not initialize:
        raise FileNotFoundError(f"database is not a file: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"database parent is not a directory: {path.parent}")
    connection = sqlite3.connect(path)
    try:
        exists = connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name=?",
            (_OWNER_TABLE,),
        ).fetchone()[0]
        if not exists:
            if not initialize:
                raise RuntimeError(
                    "live database has no authority identity; back it up and explicitly adopt it "
                    "with ensure_database_authority(..., initialize=True) before preparation"
                )
            connection.execute(
                f"CREATE TABLE {_OWNER_TABLE} (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "authority_id TEXT NOT NULL,trust_domain TEXT NOT NULL,created_at TEXT NOT NULL)"
            )
            connection.execute(
                f"INSERT INTO {_OWNER_TABLE} VALUES (1,?,?,?)",
                (authority, trust_domain, datetime.now(UTC).isoformat().replace("+00:00", "Z")),
            )
            connection.commit()
            status = "initialized"
        else:
            row = connection.execute(
                f"SELECT authority_id,trust_domain FROM {_OWNER_TABLE} WHERE singleton=1"
            ).fetchone()
            if row != (authority, trust_domain):
                raise RuntimeError("live database authority identity conflicts with requested authority")
            status = "verified"
    finally:
        connection.close()
    return {
        "status": status,
        "database": str(path),
        "authority_id": authority,
        "trust_domain": trust_domain,
    }


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_only_uri(path: Path) -> str:
    return path.as_uri() + "?mode=ro&immutable=1"


def _inspect_local_database(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(_read_only_uri(path), uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        ).fetchall()
        schema = [[str(row[0]), str(row[1]), str(row[2]), row[3]] for row in objects]
        schema_sha256 = hashlib.sha256(_canonical_bytes({"schema": schema})).hexdigest()
        return {
            "integrity_check": integrity,
            "logical_digest": logical_digest(path),
            "schema": {
                "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
                "object_count": len(schema),
                "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
                "schema_sha256": schema_sha256,
                "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            },
        }
    finally:
        connection.close()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EINVAL, errno.ENOSYS, errno.EPERM):
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOSYS, errno.EPERM):
                raise
    finally:
        os.close(descriptor)


def _publish_file_exclusive(source: Path, destination: Path) -> None:
    """Publish bytes without overwrite; leave a partial reservation on copy failure."""
    try:
        with source.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.link(source, destination)
    except OSError as exc:
        if isinstance(exc, FileExistsError):
            raise
        if exc.errno not in (errno.EXDEV, errno.ENOSYS, errno.EPERM, errno.EACCES):
            raise
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    _fsync_directory(destination.parent)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid canonical manifest: {path.name}") from exc
    if not isinstance(value, dict) or raw != _canonical_bytes(value):
        raise RuntimeError(f"non-canonical manifest: {path.name}")
    checksum = value.get("manifest_sha256")
    body = {key: item for key, item in value.items() if key != "manifest_sha256"}
    if checksum != hashlib.sha256(_canonical_bytes(body)).hexdigest():
        raise RuntimeError(f"manifest checksum mismatch: {path.name}")
    if value.get("format") != _FORMAT:
        raise RuntimeError(f"unsupported manifest: {path.name}")
    return value


def _validated_manifests(root: Path) -> list[dict[str, Any]]:
    manifests = sorted(root.glob(f"*{_MANIFEST_SUFFIX}"), key=lambda item: item.name)
    snapshots = sorted(root.glob(f"*{_SNAPSHOT_SUFFIX}"), key=lambda item: item.name)
    validated: list[dict[str, Any]] = []
    referenced_snapshots: set[str] = set()
    for path in manifests:
        manifest = _load_manifest(path)
        snapshot_id = path.name.removesuffix(_MANIFEST_SUFFIX)
        content_sha256 = manifest.get("sha256")
        if not isinstance(content_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
            raise RuntimeError(f"manifest identity mismatch: {path.name}")
        snapshot_file = manifest.get("snapshot_file")
        expected_file = f"{content_sha256}{_SNAPSHOT_SUFFIX}"
        if manifest.get("snapshot_id") != snapshot_id or snapshot_file != expected_file:
            raise RuntimeError(f"manifest identity mismatch: {path.name}")
        snapshot = root / expected_file
        if not snapshot.is_file():
            raise RuntimeError("incomplete durable snapshot publication")
        if _sha256(snapshot) != content_sha256:
            raise RuntimeError(f"snapshot hash mismatch: {expected_file}")
        referenced_snapshots.add(expected_file)
        validated.append(manifest)
    present_snapshots = {path.name for path in snapshots}
    if present_snapshots != referenced_snapshots:
        raise RuntimeError("incomplete durable snapshot publication")
    return validated


def list_snapshots(*, durable_root: str | os.PathLike[str], authority_id: str) -> dict[str, Any]:
    """Deterministically list only complete, valid canonical publications."""
    root = _absolute_path(durable_root, "durable_root")
    authority = _authority_id(authority_id)
    _reject_symlinks(root, "durable_root")
    if not root.is_dir():
        raise FileNotFoundError(f"durable_root is not a directory: {root}")
    snapshot_root = root / authority / "snapshots"
    if not snapshot_root.is_dir():
        raise FileNotFoundError(f"authority snapshot root is not a directory: {snapshot_root}")
    manifests = _validated_manifests(snapshot_root)
    entries = [
        {
            "created_at": item["created_at"],
            "logical_digest": item["logical_digest"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "snapshot_id": item["snapshot_id"],
        }
        for item in manifests
    ]
    entries.sort(key=lambda item: (item["created_at"], item["snapshot_id"]))
    return {
        "kind": "durable_snapshot_listing",
        "durable_root": str(root),
        "authority_id": authority,
        "snapshots": entries,
        "next_operation": _next(
            "restore_latest",
            durable_root=str(root),
            authority_id=authority,
            destination_db="<absolute-local-path>",
        ),
    }


def snapshot_state(
    *,
    source_db: str | os.PathLike[str],
    durable_root: str | os.PathLike[str],
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Back up a live local database and publish immutable durable bytes and manifest."""
    qualified = qualify_paths(
        source_db=source_db,
        durable_root=durable_root,
        authority_id=authority_id,
        databricks=databricks,
    )
    source = Path(qualified["source_db"])
    root = Path(qualified["durable_root"])
    snapshot_root = root / qualified["authority_id"] / "snapshots"
    if not source.is_file():
        raise FileNotFoundError(f"source_db is not a file: {source}")
    if not root.is_dir():
        raise FileNotFoundError(f"durable_root is not a directory: {root}")
    authority = ensure_database_authority(
        source, authority_id=qualified["authority_id"], trust_domain="work", initialize=True,
    )
    snapshot_root.mkdir(parents=True, exist_ok=True)
    _reject_symlinks(snapshot_root, "authority snapshot root")
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-snapshot-") as temporary_directory:
        staged = Path(temporary_directory) / "snapshot.sqlite3"
        source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        destination_connection = sqlite3.connect(staged)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        inspection = _inspect_local_database(staged)
        if inspection["integrity_check"] != "ok":
            raise RuntimeError("staged snapshot failed SQLite integrity check")
        snapshot_sha256 = _sha256(staged)
        snapshot_path = snapshot_root / f"{snapshot_sha256}{_SNAPSHOT_SUFFIX}"
        manifests = _validated_manifests(snapshot_root)
        if manifests:
            latest_created = max(str(item["created_at"]) for item in manifests)
            latest = [item for item in manifests if item["created_at"] == latest_created]
            if len(latest) != 1:
                raise RuntimeError("ambiguous latest durable snapshot")
            existing = latest[0]
        else:
            existing = None
        if existing is not None and existing["sha256"] == snapshot_sha256:
            manifest_path = snapshot_root / f"{existing['snapshot_id']}{_MANIFEST_SUFFIX}"
            return {
                "kind": "durable_snapshot",
                "action": "reused",
                "manifest": existing,
                "manifest_path": str(manifest_path),
                "snapshot_path": str(snapshot_path),
                "authority": authority,
                "next_operation": _next(
                    "restore_latest",
                    durable_root=str(root),
                    authority_id=qualified["authority_id"],
                    destination_db="<absolute-local-path>",
                ),
            }
        created = datetime.now(UTC)
        if existing is not None:
            latest = datetime.fromisoformat(existing["created_at"].replace("Z", "+00:00"))
            if created <= latest:
                created = latest + timedelta(microseconds=1)
        created_at = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
        checkpoint = re.sub(r"[^0-9TZ]", "", created_at)
        snapshot_id = f"{checkpoint}-{snapshot_sha256[:16]}"
        manifest_path = snapshot_root / f"{snapshot_id}{_MANIFEST_SUFFIX}"
        body = {
            "created_at": created_at,
            "authority_id": qualified["authority_id"],
            "format": _FORMAT,
            "integrity_check": "ok",
            "logical_digest": inspection["logical_digest"],
            "schema": inspection["schema"],
            "sha256": snapshot_sha256,
            "size_bytes": staged.stat().st_size,
            "snapshot_file": snapshot_path.name,
            "snapshot_id": snapshot_id,
        }
        manifest = dict(body)
        manifest["manifest_sha256"] = hashlib.sha256(_canonical_bytes(body)).hexdigest()
        staged_manifest = Path(temporary_directory) / "manifest.json"
        staged_manifest.write_bytes(_canonical_bytes(manifest))
        snapshot_created = not snapshot_path.exists()
        if snapshot_created:
            _publish_file_exclusive(staged, snapshot_path)
        elif _sha256(snapshot_path) != snapshot_sha256:
            raise RuntimeError("durable snapshot content collision")
        try:
            # The canonical manifest is the commit marker. Roll back this
            # attempt's uncommitted snapshot if marker publication fails.
            _publish_file_exclusive(staged_manifest, manifest_path)
        except Exception:
            if snapshot_created:
                snapshot_path.unlink(missing_ok=True)
            raise
    return {
        "kind": "durable_snapshot",
        "action": "created",
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "snapshot_path": str(snapshot_path),
        "authority": authority,
        "next_operation": _next(
            "restore_latest",
            durable_root=str(root),
            authority_id=qualified["authority_id"],
            destination_db="<absolute-local-path>",
        ),
    }


def restore_latest(
    *,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str],
    authority_id: str,
    overwrite: bool = False,
    databricks: bool = False,
) -> dict[str, Any]:
    """Verify the latest durable snapshot locally and publish only to an absent path."""
    if overwrite:
        raise ValueError("destructive overwrite is not supported")
    qualified = qualify_paths(
        durable_root=durable_root, destination_db=destination_db,
        authority_id=authority_id, databricks=databricks,
    )
    root = Path(qualified["durable_root"])
    destination = Path(qualified["destination_db"])
    snapshot_root = root / qualified["authority_id"] / "snapshots"
    if not root.is_dir():
        raise FileNotFoundError(f"durable_root is not a directory: {root}")
    if destination.exists():
        raise FileExistsError(f"destination_db already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"destination parent is not a directory: {destination.parent}")
    if not snapshot_root.is_dir():
        raise RuntimeError("no valid durable snapshots")
    manifests = _validated_manifests(snapshot_root)
    if not manifests:
        raise RuntimeError("no valid durable snapshots")
    latest_created = max(str(item["created_at"]) for item in manifests)
    latest = [item for item in manifests if item["created_at"] == latest_created]
    if len(latest) != 1:
        raise RuntimeError("ambiguous latest durable snapshot")
    manifest = latest[0]
    if manifest.get("authority_id") != qualified["authority_id"]:
        raise RuntimeError("durable snapshot authority mismatch")
    durable_snapshot = snapshot_root / str(manifest["snapshot_file"])
    # Stage on the destination filesystem so hard-link publication is an atomic,
    # no-overwrite directory-entry operation rather than the partial-copy fallback.
    with tempfile.TemporaryDirectory(prefix=".odibi-anchor-restore-", dir=destination.parent) as temporary_directory:
        staged = Path(temporary_directory) / "restore.sqlite3"
        shutil.copyfile(durable_snapshot, staged)
        if _sha256(staged) != manifest["sha256"]:
            raise RuntimeError("staged restore hash mismatch")
        inspection = _inspect_local_database(staged)
        if inspection["integrity_check"] != "ok":
            raise RuntimeError("staged restore failed SQLite integrity check")
        if inspection["logical_digest"] != manifest["logical_digest"]:
            raise RuntimeError("staged restore logical digest mismatch")
        ensure_database_authority(
            staged, authority_id=qualified["authority_id"], trust_domain="work",
        )
        _publish_file_exclusive(staged, destination)
    authority = ensure_database_authority(
        destination, authority_id=qualified["authority_id"], trust_domain="work",
    )
    return {
        "kind": "durable_restore",
        "action": "created",
        "destination_db": str(destination),
        "snapshot_id": manifest["snapshot_id"],
        "sha256": manifest["sha256"],
        "logical_digest": manifest["logical_digest"],
        "integrity_check": "ok",
        "authority": authority,
        "next_operation": _next("inspect_restored_state", destination_db=str(destination)),
    }
