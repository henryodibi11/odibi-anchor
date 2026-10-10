"""Immutable durable snapshots for live SQLite and managed project artifacts.

SQLite is opened only on the live local source or a local staging copy. Artifact bundles
are created and safely inspected only on local compute; durable transport treats both as bytes.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

# Restore sub-phase timing reuses the bootstrap recorder; outside a bootstrap these are no-ops.
from odibi_anchor._bootstrap_phases import elapsed_ms as _elapsed_ms
from odibi_anchor._bootstrap_phases import phase as _phase
from odibi_anchor._bootstrap_phases import record_phase as _record_phase
from odibi_anchor._recovery import attach_recovery
from odibi_anchor.codebase._migration_backup import logical_digest

_MANIFEST_SUFFIX = ".manifest.json"
_INDEX_SUFFIX = ".index.json"
_SNAPSHOT_SUFFIX = ".sqlite3"
_ARTIFACTS_SUFFIX = ".artifacts.tar"
_FORMAT_V1 = "odibi-anchor-durable-snapshot-v1"
_FORMAT_V2 = "odibi-anchor-durable-snapshot-v2"
_INDEX_FORMAT = "odibi-anchor-durable-snapshot-index-v1"
_SUPPORTED_FORMATS = frozenset({_FORMAT_V1, _FORMAT_V2})
_DURABLE_LIVE_PREFIXES = (Path("/Workspace"), Path("/Volumes"), Path("/dbfs"))
_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})\Z")
_OWNER_TABLE = "anchor_authority_identity"
_MAX_ARTIFACT_MEMBERS = 100_000
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
# Lineage evidence lives beside snapshots/, so readers that only scan snapshots/ ignore it.
_AUTHORITY_MARKER = "AUTHORITY.json"
_AUTHORITY_FORMAT = "odibi-anchor-durable-authority-v1"
_RESTORE_OWNER = ".odibi-anchor-restore-owner.json"
_RESTORE_OWNER_FORMAT = "odibi-anchor-restore-owner-v1"
_RESTORE_PENDING = _RESTORE_OWNER + ".pending"
_RESTORE_RECORD_KEYS = frozenset({
    "authority_id", "created_at", "database", "databricks", "destination_artifacts",
    "destination_db", "device", "durable_root", "format", "inode", "manifest_sha256", "nonce",
    "snapshot_id",
})


class DurableSnapshotUnavailable(RuntimeError):
    """First use: the durable root is reachable and holds no authority marker or snapshot."""

    classification = "no_lineage"


def _next(operation: str, **arguments: Any) -> dict[str, Any]:
    return {"operation": operation, "arguments": arguments}


def _absolute_path(
    value: str | os.PathLike[str],
    label: str,
    *,
    inspect_filesystem: bool = True,
) -> Path:
    text = os.fspath(value)
    if not isinstance(text, str) or not text:
        raise ValueError(f"{label} must be a non-empty path string")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label} must not contain line breaks")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if inspect_filesystem:
        _reject_symlinks(path, label)
    canonical = os.path.abspath(path)
    if os.name != "nt" and canonical.startswith("//"):
        canonical = "/" + canonical.lstrip("/")
    return Path(canonical)


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
    source_artifacts: str | os.PathLike[str] | None = None,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str] | None = None,
    destination_artifacts: str | os.PathLike[str] | None = None,
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Secret-free, read-only qualification of explicit snapshot/restore paths."""
    root = _absolute_path(
        durable_root,
        "durable_root",
        inspect_filesystem=not databricks,
    )
    authority = _authority_id(authority_id)
    source = _absolute_path(source_db, "source_db") if source_db is not None else None
    artifacts_source = (
        _absolute_path(source_artifacts, "source_artifacts")
        if source_artifacts is not None
        else None
    )
    destination = _absolute_path(destination_db, "destination_db") if destination_db is not None else None
    artifacts_destination = (
        _absolute_path(destination_artifacts, "destination_artifacts")
        if destination_artifacts is not None
        else None
    )
    if source is None and destination is None:
        raise ValueError("source_db or destination_db is required")
    if source is None and artifacts_source is not None:
        raise ValueError("source_artifacts requires source_db")
    if destination is None and artifacts_destination is not None:
        raise ValueError("destination_artifacts requires destination_db")
    for path, label in (
        (source, "source_db"),
        (artifacts_source, "source_artifacts"),
        (destination, "destination_db"),
        (artifacts_destination, "destination_artifacts"),
    ):
        if path is not None:
            _reject_overlap(path, root, f"{label} and durable_root")
    if source is not None and artifacts_source is not None:
        _reject_overlap(source, artifacts_source, "source_db and source_artifacts")
    if destination is not None and artifacts_destination is not None:
        _reject_overlap(destination, artifacts_destination, "destination_db and destination_artifacts")
    if databricks:
        if not _is_within(root, Path("/Volumes")):
            raise ValueError("durable_root must be a Unity Catalog Volume when databricks=True")
        for path, label in (
            (source, "source_db"),
            (artifacts_source, "source_artifacts"),
            (destination, "destination_db"),
            (artifacts_destination, "destination_artifacts"),
        ):
            if path is not None and any(_is_within(path, prefix) for prefix in _DURABLE_LIVE_PREFIXES):
                raise ValueError(f"{label} must be on local compute when databricks=True")
    operation = "snapshot_state" if source is not None else "restore_latest"
    arguments: dict[str, Any] = {"durable_root": str(root)}
    arguments["authority_id"] = authority
    if source is not None:
        arguments["source_db"] = str(source)
        if artifacts_source is not None:
            arguments["source_artifacts"] = str(artifacts_source)
    else:
        arguments["destination_db"] = str(destination)
        if artifacts_destination is not None:
            arguments["destination_artifacts"] = str(artifacts_destination)
    arguments["databricks"] = databricks
    return {
        "kind": "durability_qualification",
        "qualified": True,
        "source_db": None if source is None else str(source),
        "source_artifacts": None if artifacts_source is None else str(artifacts_source),
        "destination_db": None if destination is None else str(destination),
        "destination_artifacts": (
            None if artifacts_destination is None else str(artifacts_destination)
        ),
        "durable_root": str(root),
        "authority_id": authority,
        "databricks": bool(databricks),
        "next_operation": _next(operation, **arguments),
    }


def qualify_durability(
    *,
    source_db: str | os.PathLike[str] | None = None,
    source_artifacts: str | os.PathLike[str] | None = None,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str] | None = None,
    destination_artifacts: str | os.PathLike[str] | None = None,
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Publicly named qualification entry point; performs no filesystem mutation."""
    return qualify_paths(
        source_db=source_db,
        source_artifacts=source_artifacts,
        durable_root=durable_root,
        destination_db=destination_db,
        destination_artifacts=destination_artifacts,
        authority_id=authority_id,
        databricks=databricks,
    )


def ensure_database_authority(
    database: str | os.PathLike[str],
    *,
    authority_id: str,
    trust_domain: str,
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
        from odibi_anchor.codebase._authority_relocation import load_relocations
        from odibi_anchor.codebase._workflow import _events, _schema
        from odibi_anchor.codebase._workflow_artifact_restore import load_restores

        load_relocations(connection)
        load_restores(connection)
        tables = {row[1] for row in objects if row[0] == "table"}
        workflow_version = (
            connection.execute("SELECT 1 FROM anchor_schema_versions WHERE domain='workflow'").fetchone()
            if "anchor_schema_versions" in tables else None
        )
        if "workflow_events" in tables or workflow_version:
            _schema(connection)
            connection.row_factory = sqlite3.Row
            for identifier, in connection.execute("SELECT DISTINCT workflow_id FROM workflow_events"):
                first = connection.execute(
                    "SELECT event_json FROM workflow_events WHERE workflow_id=? ORDER BY generation LIMIT 1",
                    (identifier,),
                ).fetchone()
                _events(connection, identifier, json.loads(first[0])["state"]["owner"])
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


def _durable_root_unavailable(root: Path, message: str, *, databricks: bool, observed: str) -> FileNotFoundError:
    return attach_recovery(
        FileNotFoundError(message),
        error_code="durable_root_unavailable",
        context={
            "classification": "durable_root_unavailable",
            "durable_root": str(root),
            "databricks": databricks,
            "observed": observed,
        },
    )


def _identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    return metadata.st_dev, metadata.st_ino


def _destination_conflict(path: Path, message: str, *, observed: str) -> FileExistsError:
    return attach_recovery(
        FileExistsError(message),
        error_code="restore_destination_conflict",
        context={
            "classification": "restore_destination_conflict",
            "destination": str(path),
            "observed": observed,
        },
    )


def _read_restore_record(destination: Path) -> dict[str, Any] | None:
    """Return a well-formed ownership record, or None when none is present or readable."""
    marker = destination / _RESTORE_OWNER
    try:
        if not stat.S_ISREG(os.lstat(marker).st_mode):
            return None
        raw = marker.read_bytes()
    except OSError:
        return None
    return _parse_restore_record(raw)


def _parse_restore_record(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or raw != _canonical_bytes(value)
        or set(value) != _RESTORE_RECORD_KEYS
        or value["format"] != _RESTORE_OWNER_FORMAT
    ):
        return None
    return value


def _restore_owned(destination: Path, record: Mapping[str, Any]) -> bool:
    """The exact directory this restore created still carries this invocation's marker."""
    try:
        metadata = os.lstat(destination)
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == (record["device"], record["inode"])
        and _read_restore_record(destination) == record
    )


def _restore_operations(record: Mapping[str, Any], *, include_resume: bool = True) -> list[dict[str, Any]]:
    arguments = {
        key: record[key]
        for key in ("durable_root", "authority_id", "destination_db", "destination_artifacts", "databricks")
    }
    flags = [
        "--durable-root", record["durable_root"], "--authority", record["authority_id"],
        "--database", record["destination_db"], "--artifacts", record["destination_artifacts"],
    ] + (["--databricks"] if record["databricks"] else [])
    operations = [
        {
            "operation": "resume_restore",
            "arguments": arguments,
            "copy_ready": shlex.join(["anchor", "state", "resume", *flags]),
            "python": "odibi_anchor.durability.resume_restore(**arguments)",
            "reason": "re-verify the owned tree against the same snapshot manifest, then publish the database",
            "requires_owner": False,
            "retry_safety": "verifies ownership and snapshot identity before writing; refuses on mismatch",
        },
        {
            "operation": "abandon_restore",
            "arguments": arguments,
            "copy_ready": shlex.join(["anchor", "state", "abandon", *flags]),
            "python": "odibi_anchor.durability.abandon_restore(**arguments)",
            "reason": "move the owned incomplete tree to a preserved quarantine path; nothing is deleted",
            "requires_owner": False,
            "retry_safety": "moves only the verified owned tree; refuses on mismatch",
        },
    ]
    return operations if include_resume else operations[1:]


def _restore_incomplete(
    record: Mapping[str, Any], *, observed: str, include_resume: bool = True,
) -> RuntimeError:
    return attach_recovery(
        RuntimeError(
            "restore is incomplete: managed artifacts at "
            f"{record['destination_artifacts']} belong to an unfinished restore of snapshot "
            f"{record['snapshot_id']} for database {record['destination_db']} ({observed}); "
            "resume or abandon that restore"
        ),
        error_code="restore_incomplete",
        context={
            "classification": "restore_incomplete",
            "observed": observed,
            "owner_marker": str(Path(record["destination_artifacts"]) / _RESTORE_OWNER),
            **{
                key: record[key]
                for key in (
                    "snapshot_id", "manifest_sha256", "durable_root", "authority_id",
                    "destination_db", "destination_artifacts", "databricks",
                )
            },
        },
        next_operations=_restore_operations(record, include_resume=include_resume),
    )


def _incomplete_restore_error(destination_artifacts: Path) -> RuntimeError | None:
    """Describe a verified unfinished restore at this destination, if one is present."""
    record = _read_restore_record(destination_artifacts)
    if record is None or not _restore_owned(destination_artifacts, record):
        return None
    if _restore_database_published(record):
        return _restore_incomplete(
            record, observed="the database was published but the ownership marker was not removed",
        )
    return _restore_incomplete(record, observed="an incomplete restore record is present")


def _restore_database_published(record: Mapping[str, Any]) -> bool:
    """The destination database holds exactly the staged bytes this restore published.

    Content, not inode identity, so a copy-fallback publication is recognized and a reused
    inode cannot be mistaken for it.
    """
    if record["database"] is None:
        return False
    database = Path(record["destination_db"])
    try:
        if not stat.S_ISREG(os.lstat(database).st_mode):
            return False
        return _sha256(database) == record["database"]
    except OSError:
        return False


def _record_database_intent(destination: Path, record: dict[str, Any], staged: Path) -> dict[str, Any]:
    """Atomically extend the owned record with the staged database SHA-256 before publishing it."""
    if not _restore_owned(destination, record):
        raise RuntimeError("restore destination ownership changed before database publication")
    updated = {**record, "database": _sha256(staged)}
    pending = destination / _RESTORE_PENDING
    descriptor = os.open(
        pending, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_canonical_bytes(updated))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, destination / _RESTORE_OWNER)
    _fsync_directory(destination)
    return updated


def _claim_restore_destination(destination: Path, base: Mapping[str, Any]) -> dict[str, Any]:
    """Create the exact destination exclusively and bind it to a per-invocation nonce."""
    try:
        os.mkdir(destination)
    except FileExistsError as exc:
        raise _destination_conflict(
            destination,
            f"destination_artifacts appeared before this restore could claim it: {destination}",
            observed="exists_at_claim",
        ) from exc
    device, inode = _identity(destination)
    record = {
        **base,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "database": None,
        "device": device,
        "format": _RESTORE_OWNER_FORMAT,
        "inode": inode,
        "nonce": secrets.token_hex(16),
    }
    try:
        descriptor = os.open(
            destination / _RESTORE_OWNER,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        if _identity(destination) == (device, inode):
            with suppress(OSError):
                os.rmdir(destination)
        raise _destination_conflict(
            destination,
            f"destination_artifacts changed before this restore could record ownership: {destination}",
            observed="marker_collision",
        ) from exc
    marker_identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(destination)
        _fsync_directory(destination.parent)
    except BaseException:
        # Release only the marker and directory this invocation just created.
        with suppress(OSError):
            if _identity(destination / _RESTORE_OWNER) == (marker_identity.st_dev, marker_identity.st_ino):
                os.unlink(destination / _RESTORE_OWNER)
            if _identity(destination) == (device, inode):
                os.rmdir(destination)
        raise
    return record


# Restore fill and cleanup act relative to held directory descriptors where the platform
# supports it, so a parent swapped for a symlink or another directory after a check cannot
# redirect the following create, unlink or rmdir. Elsewhere the path-based code runs.
_DIR_FD_CLEANUP = (
    hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
    and {os.open, os.stat, os.unlink, os.rmdir, os.mkdir} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def _open_directory(name: str | Path, *, dir_fd: int | None = None) -> int:
    return os.open(
        name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=dir_fd,
    )


def _fd_identity(name: str, dir_fd: int) -> tuple[int, int]:
    metadata = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _fd_owned(root_fd: int, record: Mapping[str, Any]) -> bool:
    """The held directory is the claimed one and still carries this invocation's marker."""
    metadata = os.fstat(root_fd)
    if (metadata.st_dev, metadata.st_ino) != (record["device"], record["inode"]):
        return False
    try:
        marker = os.open(_RESTORE_OWNER, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(marker).st_mode):
            return False
        with os.fdopen(os.dup(marker), "rb") as stream:
            raw = stream.read()
    finally:
        os.close(marker)
    return _parse_restore_record(raw) == record


def _open_owned_root(destination: Path, record: Mapping[str, Any]) -> int | None:
    try:
        root_fd = _open_directory(destination)
    except OSError:
        return None
    if not _fd_owned(root_fd, record):
        os.close(root_fd)
        return None
    return root_fd


def _fill_owned_destination_fd(
    staged: Path,
    destination: Path,
    record: Mapping[str, Any],
    created: list[tuple[Path, str, tuple[int, int]]],
) -> None:
    root_fd = _open_owned_root(destination, record)
    if root_fd is None:
        raise RuntimeError("restore destination changed during copy: .")
    directories = {Path("."): root_fd}
    expected: set[Path] = set()

    def require_unchanged(relative_directory: Path) -> int:
        # Acting through the held descriptor keeps writes in the claimed tree; the path
        # check still detects a substituted parent so the restore cannot report success.
        held = directories.get(relative_directory)
        if held is None or _identity(destination / relative_directory) != (
            os.fstat(held).st_dev, os.fstat(held).st_ino,
        ):
            raise RuntimeError(f"restore destination changed during copy: {relative_directory.as_posix()}")
        return held

    try:
        for source in sorted(staged.rglob("*"), key=lambda item: item.relative_to(staged).parts):
            relative = source.relative_to(staged)
            expected.add(relative)
            parent_fd = require_unchanged(relative.parent)
            name = relative.name
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if source.is_dir():
                if existing is None:
                    os.mkdir(name, dir_fd=parent_fd)
                    identity = _fd_identity(name, parent_fd)
                    created.append((relative, "directory", identity))
                elif not stat.S_ISDIR(existing.st_mode):
                    raise RuntimeError(f"restored artifact differs from the snapshot: {relative.as_posix()}")
                else:
                    identity = (existing.st_dev, existing.st_ino)
                child = _open_directory(name, dir_fd=parent_fd)
                directories[relative] = child
                if (os.fstat(child).st_dev, os.fstat(child).st_ino) != identity:
                    raise RuntimeError(f"restore destination changed during copy: {relative.as_posix()}")
                continue
            if existing is not None:
                if not stat.S_ISREG(existing.st_mode):
                    raise RuntimeError(f"restored artifact differs from the snapshot: {relative.as_posix()}")
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != _sha256(source):
                    raise RuntimeError(f"restored artifact differs from the snapshot: {relative.as_posix()}")
                continue
            descriptor = os.open(
                name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644, dir_fd=parent_fd,
            )
            metadata = os.fstat(descriptor)
            created.append((relative, "file", (metadata.st_dev, metadata.st_ino)))
            with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        for relative_directory in directories:
            require_unchanged(relative_directory)
    finally:
        for descriptor in directories.values():
            os.close(descriptor)
    for path in destination.rglob("*"):
        relative = path.relative_to(destination)
        if relative not in expected and relative not in (Path(_RESTORE_OWNER), Path(_RESTORE_PENDING)):
            raise RuntimeError(f"restored artifact tree has an unexpected entry: {relative.as_posix()}")


def _release_owned_destination_fd(
    destination: Path,
    record: Mapping[str, Any],
    created: list[tuple[Path, str, tuple[int, int]]],
    *,
    remove_root: bool,
) -> str:
    root_fd = _open_owned_root(destination, record)
    if root_fd is None:
        return "not_owned"
    try:
        for relative, kind, identity in reversed(created):
            opened: list[int] = []
            try:
                parent_fd = root_fd
                for part in relative.parent.parts:
                    parent_fd = _open_directory(part, dir_fd=parent_fd)
                    opened.append(parent_fd)
                if _fd_identity(relative.name, parent_fd) != identity:
                    continue
                if kind == "directory":
                    os.rmdir(relative.name, dir_fd=parent_fd)
                else:
                    os.unlink(relative.name, dir_fd=parent_fd)
            except OSError:
                continue
            finally:
                for descriptor in opened:
                    os.close(descriptor)
        if not remove_root:
            return "retained"
        with os.scandir(root_fd) as entries:
            if any(entry.name != _RESTORE_OWNER for entry in entries):
                return "retained"
        if not _fd_owned(root_fd, record):
            return "not_owned"
        os.unlink(_RESTORE_OWNER, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        parent_fd = _open_directory(destination.parent)
    except OSError:
        return "not_owned"
    try:
        if _fd_identity(destination.name, parent_fd) != (record["device"], record["inode"]):
            return "not_owned"
        os.rmdir(destination.name, dir_fd=parent_fd)
    except OSError:
        return "not_owned"
    finally:
        os.close(parent_fd)
    _fsync_directory(destination.parent)
    return "removed"


def _fill_owned_destination(
    staged: Path,
    destination: Path,
    record: Mapping[str, Any],
    created: list[tuple[Path, str, tuple[int, int]]],
) -> None:
    """Create absent snapshot entries exclusively and require existing ones to match exactly."""
    if _DIR_FD_CLEANUP:
        return _fill_owned_destination_fd(staged, destination, record, created)
    expected: set[Path] = set()
    directories = {Path("."): (record["device"], record["inode"])}
    for source in sorted(staged.rglob("*"), key=lambda item: item.relative_to(staged).parts):
        relative = source.relative_to(staged)
        expected.add(relative)
        target = destination / relative
        # A parent swapped for a symlink or another directory must not redirect writes.
        if _identity(target.parent) != directories.get(relative.parent):
            raise RuntimeError(f"restore destination changed during copy: {relative.parent.as_posix()}")
        try:
            existing = os.lstat(target)
        except FileNotFoundError:
            existing = None
        if source.is_dir():
            if existing is None:
                os.mkdir(target)
                created.append((relative, "directory", _identity(target)))
            elif not stat.S_ISDIR(existing.st_mode):
                raise RuntimeError(f"restored artifact differs from the snapshot: {relative.as_posix()}")
            directories[relative] = _identity(target)
            continue
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or _sha256(target) != _sha256(source):
                raise RuntimeError(f"restored artifact differs from the snapshot: {relative.as_posix()}")
            continue
        descriptor = os.open(
            target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o644,
        )
        metadata = os.fstat(descriptor)
        created.append((relative, "file", (metadata.st_dev, metadata.st_ino)))
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    for path in destination.rglob("*"):
        relative = path.relative_to(destination)
        if relative not in expected and relative not in (Path(_RESTORE_OWNER), Path(_RESTORE_PENDING)):
            raise RuntimeError(f"restored artifact tree has an unexpected entry: {relative.as_posix()}")


def _release_owned_destination(
    destination: Path,
    record: Mapping[str, Any],
    created: list[tuple[Path, str, tuple[int, int]]],
    *,
    remove_root: bool,
) -> str:
    """Remove only entries this invocation created while the owned identity still holds."""
    if _DIR_FD_CLEANUP:
        return _release_owned_destination_fd(destination, record, created, remove_root=remove_root)
    if not _restore_owned(destination, record):
        return "not_owned"
    for relative, kind, identity in reversed(created):
        target = destination / relative
        try:
            if _identity(target) != identity:
                continue
            if kind == "directory":
                os.rmdir(target)
            else:
                os.unlink(target)
        except OSError:
            continue
    if not remove_root:
        return "retained"
    if any(entry.name != _RESTORE_OWNER for entry in os.scandir(destination)):
        return "retained"
    if not _restore_owned(destination, record):
        return "not_owned"
    os.unlink(destination / _RESTORE_OWNER)
    try:
        os.rmdir(destination)
    except OSError:
        return "not_owned"
    _fsync_directory(destination.parent)
    return "removed"


def _safe_artifact_name(name: str) -> PurePosixPath:
    if (
        not name
        or "\x00" in name
        or name.startswith(("/", "\\"))
        or "\\" in name
        or ":" in name
    ):
        raise RuntimeError("invalid artifact archive entry")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise RuntimeError("invalid artifact archive entry")
    return path


def _stage_artifact_bundle(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_dir():
        raise FileNotFoundError(f"source_artifacts is not a directory: {source}")
    _reject_symlinks(source, "source_artifacts")
    if os.path.lexists(source / _RESTORE_OWNER):
        incomplete = _incomplete_restore_error(source)
        if incomplete is not None:
            raise incomplete
        raise ValueError(f"source_artifacts contains a restore ownership marker: {source / _RESTORE_OWNER}")
    directories: list[tuple[str, Path]] = []
    files: list[tuple[str, Path]] = []
    total_bytes = 0
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source).as_posix()
        _safe_artifact_name(relative)
        if path.is_symlink():
            raise ValueError(f"source_artifacts must not contain symlinks: {relative}")
        if path.is_dir():
            directories.append((relative, path))
        elif path.is_file():
            size = path.stat(follow_symlinks=False).st_size
            total_bytes += size
            if total_bytes > _MAX_ARTIFACT_BYTES:
                raise ValueError("source_artifacts exceeds the supported size limit")
            files.append((relative, path))
        else:
            raise ValueError(f"source_artifacts contains a special file: {relative}")
    if len(directories) + len(files) > _MAX_ARTIFACT_MEMBERS:
        raise ValueError("source_artifacts exceeds the supported member limit")
    with tarfile.open(destination, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for relative, _path in directories:
            info = tarfile.TarInfo(relative + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info)
        for relative, path in files:
            info = tarfile.TarInfo(relative)
            info.size = path.stat(follow_symlinks=False).st_size
            info.mode = 0o644
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    return {
        "file": f"{_sha256(destination)}{_ARTIFACTS_SUFFIX}",
        "sha256": _sha256(destination),
        "size_bytes": destination.stat().st_size,
        "file_count": len(files),
        "directory_count": len(directories),
        "content_size_bytes": total_bytes,
    }


def _descriptor_integrity(bundle: Path) -> list[dict[str, Any]]:
    """Report each bundled project's ``PROJECT.md`` route integrity; never blocks a snapshot."""
    from odibi_anchor._dispatcher._descriptor import DESCRIPTOR_NAME, parse_descriptor_text

    summary: list[dict[str, Any]] = []
    with tarfile.open(bundle, mode="r:") as archive:
        for info in archive:
            parts = PurePosixPath(info.name).parts
            if not info.isfile() or len(parts) != 2 or parts[1] != DESCRIPTOR_NAME:
                continue
            stream = archive.extractfile(info)
            data = stream.read() if stream is not None else b""
            digest = hashlib.sha256(data).hexdigest()
            try:
                status = parse_descriptor_text(
                    data.decode("utf-8"), path=info.name, sha256=digest, expected_id=parts[0],
                ).status
            except UnicodeDecodeError:
                status = "malformed_frontmatter"
            summary.append({"project_id": parts[0], "status": status, "sha256": digest})
    return sorted(summary, key=lambda item: item["project_id"])


def _extract_artifact_bundle(bundle: Path, destination: Path) -> dict[str, int]:
    seen: dict[PurePosixPath, str] = {}
    members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    total_bytes = 0
    with tarfile.open(bundle, mode="r:") as archive:
        for info in archive:
            path = _safe_artifact_name(info.name.rstrip("/"))
            kind = "directory" if info.isdir() else "file" if info.isfile() else "special"
            if kind == "special":
                raise RuntimeError("artifact archive contains a special entry")
            if path in seen:
                raise RuntimeError("artifact archive contains duplicate entries")
            for parent in path.parents:
                if parent != PurePosixPath(".") and seen.get(parent) == "file":
                    raise RuntimeError("artifact archive contains a file/directory collision")
            if kind == "file" and any(
                existing != path and path in existing.parents for existing in seen
            ):
                raise RuntimeError("artifact archive contains a file/directory collision")
            seen[path] = kind
            total_bytes += info.size if kind == "file" else 0
            if len(seen) > _MAX_ARTIFACT_MEMBERS or total_bytes > _MAX_ARTIFACT_BYTES:
                raise RuntimeError("artifact archive exceeds supported extraction limits")
            members.append((info, path))
        destination.mkdir()
        for _info, path in sorted(
            (item for item in members if item[0].isdir()),
            key=lambda item: (len(item[1].parts), item[1].as_posix()),
        ):
            (destination.joinpath(*path.parts)).mkdir(parents=True, exist_ok=False)
        for info, path in (item for item in members if item[0].isfile()):
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            incoming = archive.extractfile(info)
            if incoming is None:
                raise RuntimeError("artifact archive file has no content")
            with target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    return {
        "file_count": sum(info.isfile() for info, _path in members),
        "directory_count": sum(info.isdir() for info, _path in members),
        "content_size_bytes": total_bytes,
    }


def _validate_manifest(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid canonical manifest: {name}")
    checksum = value.get("manifest_sha256")
    body = {key: item for key, item in value.items() if key != "manifest_sha256"}
    if checksum != hashlib.sha256(_canonical_bytes(body)).hexdigest():
        raise RuntimeError(f"manifest checksum mismatch: {name}")
    if value.get("format") not in _SUPPORTED_FORMATS:
        raise RuntimeError(f"unsupported manifest: {name}")
    if value["format"] == _FORMAT_V2:
        artifacts = value.get("artifacts")
        if not isinstance(artifacts, dict):
            raise RuntimeError(f"manifest artifacts mismatch: {name}")
        artifacts_sha256 = artifacts.get("sha256")
        if (
            not isinstance(artifacts_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", artifacts_sha256)
            or artifacts.get("file") != f"{artifacts_sha256}{_ARTIFACTS_SUFFIX}"
            or not isinstance(artifacts.get("size_bytes"), int)
            or artifacts["size_bytes"] < 0
            or not isinstance(artifacts.get("file_count"), int)
            or artifacts["file_count"] < 0
            or not isinstance(artifacts.get("directory_count"), int)
            or artifacts["directory_count"] < 0
            or not isinstance(artifacts.get("content_size_bytes"), int)
            or artifacts["content_size_bytes"] < 0
        ):
            raise RuntimeError(f"manifest artifacts mismatch: {name}")
    return value


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid canonical manifest: {path.name}") from exc
    if not isinstance(value, dict) or raw != _canonical_bytes(value):
        raise RuntimeError(f"non-canonical manifest: {path.name}")
    return _validate_manifest(value, path.name)


def _remote_entry_size(entry: Any) -> int | None:
    for attribute in ("file_size", "size"):
        value = getattr(entry, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _validate_manifest_payloads(root: Path, manifest: dict[str, Any]) -> None:
    snapshot_file = manifest["snapshot_file"]
    snapshot = root / snapshot_file
    if not snapshot.is_file():
        raise RuntimeError("incomplete durable snapshot publication")
    if _sha256(snapshot) != manifest["sha256"]:
        raise RuntimeError(f"snapshot hash mismatch: {snapshot_file}")
    if snapshot.stat().st_size != manifest["size_bytes"]:
        raise RuntimeError(f"snapshot size mismatch: {snapshot_file}")
    if manifest["format"] == _FORMAT_V2:
        artifacts = manifest["artifacts"]
        bundle = root / artifacts["file"]
        if not bundle.is_file():
            raise RuntimeError("incomplete durable snapshot publication")
        if bundle.stat().st_size != artifacts["size_bytes"]:
            raise RuntimeError(f"artifact bundle size mismatch: {artifacts['file']}")
        if _sha256(bundle) != artifacts["sha256"]:
            raise RuntimeError(f"artifact bundle hash mismatch: {artifacts['file']}")


def _validate_remote_manifest_references(
    manifest: dict[str, Any],
    remote_entries: Mapping[str, Any],
) -> None:
    references = [(manifest["snapshot_file"], manifest["size_bytes"], "snapshot")]
    if manifest["format"] == _FORMAT_V2:
        references.append((
            manifest["artifacts"]["file"],
            manifest["artifacts"]["size_bytes"],
            "artifact bundle",
        ))
    for name, expected_size, label in references:
        entry = remote_entries.get(name)
        if entry is None:
            raise RuntimeError("incomplete durable snapshot publication")
        actual_size = _remote_entry_size(entry)
        if actual_size is not None and actual_size != expected_size:
            raise RuntimeError(f"{label} size mismatch: {name}")


def _validated_manifests(
    root: Path,
    *,
    remote_entries: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    manifests = sorted(root.glob(f"*{_MANIFEST_SUFFIX}"), key=lambda item: item.name)
    validated: list[dict[str, Any]] = []
    for path in manifests:
        manifest = _load_manifest(path)
        snapshot_id = path.name.removesuffix(_MANIFEST_SUFFIX)
        _validate_manifest_identity(manifest, snapshot_id, path.name)
        if remote_entries is None:
            _validate_manifest_payloads(root, manifest)
        else:
            _validate_remote_manifest_references(manifest, remote_entries)
        validated.append(manifest)
    return validated


def _validate_manifest_identity(
    manifest: dict[str, Any], snapshot_id: str, name: str,
) -> None:
    content_sha256 = manifest.get("sha256")
    if not isinstance(content_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise RuntimeError(f"manifest identity mismatch: {name}")
    if (
        manifest.get("snapshot_id") != snapshot_id
        or manifest.get("snapshot_file") != f"{content_sha256}{_SNAPSHOT_SUFFIX}"
    ):
        raise RuntimeError(f"manifest identity mismatch: {name}")


def _databricks_files_api() -> Any:
    try:
        sdk = importlib.import_module("databricks.sdk")
    except ImportError as exc:
        raise RuntimeError("Databricks durability requires the Databricks SDK available in the runtime") from exc
    return sdk.WorkspaceClient().files


def _is_databricks_not_found(error: Exception) -> bool:
    try:
        errors = importlib.import_module("databricks.sdk.errors")
    except ImportError:
        return False
    return isinstance(error, errors.NotFound)


def _remote_child(parent: str, name: str) -> str:
    return f"{parent.rstrip('/')}/{name}"


def _remote_snapshot_entries(files: Any, remote_root: str) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for entry in files.list_directory_contents(remote_root):
        remote_path = str(entry.path)
        name = remote_path.rsplit("/", 1)[-1]
        if remote_path != _remote_child(remote_root, name):
            raise RuntimeError("invalid Databricks snapshot entry")
        if not name.endswith((
            _MANIFEST_SUFFIX, _INDEX_SUFFIX, _SNAPSHOT_SUFFIX, _ARTIFACTS_SUFFIX,
        )):
            continue
        if not name or "/" in name or "\\" in name:
            raise RuntimeError("invalid Databricks snapshot entry")
        if name in entries:
            raise RuntimeError("duplicate Databricks snapshot entry")
        entries[name] = entry
    return entries


def _download_remote_snapshot_files(
    files: Any,
    remote_root: str,
    local_root: Path,
    *,
    latest_manifest_only: bool,
    download_manifests: bool,
) -> dict[str, Any]:
    local_root.mkdir(exist_ok=True)
    entries = _remote_snapshot_entries(files, remote_root)
    manifests = sorted(name for name in entries if name.endswith(_MANIFEST_SUFFIX))
    if not download_manifests:
        manifests = []
    if latest_manifest_only and manifests:
        manifests = manifests[-1:]
    for name in manifests:
        files.download_to(
            _remote_child(remote_root, name),
            str(local_root / name),
            overwrite=False,
            use_parallel=False,
        )
    return entries


def _load_snapshot_index(
    path: Path,
    *,
    authority: str,
    remote_entries: Mapping[str, Any],
) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid durable snapshot index: {path.name}") from exc
    if not isinstance(value, dict) or raw != _canonical_bytes(value):
        raise RuntimeError(f"non-canonical durable snapshot index: {path.name}")
    checksum = value.get("index_sha256")
    body = {key: item for key, item in value.items() if key != "index_sha256"}
    if checksum != hashlib.sha256(_canonical_bytes(body)).hexdigest():
        raise RuntimeError(f"durable snapshot index checksum mismatch: {path.name}")
    if value.get("format") != _INDEX_FORMAT or value.get("authority_id") != authority:
        raise RuntimeError(f"durable snapshot index authority mismatch: {path.name}")
    entries = value.get("snapshots")
    if not isinstance(entries, list):
        raise RuntimeError(f"durable snapshot index entries mismatch: {path.name}")
    indexed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in entries:
        manifest = _validate_manifest(candidate, path.name)
        snapshot_id = manifest.get("snapshot_id")
        if not isinstance(snapshot_id, str) or snapshot_id in seen:
            raise RuntimeError(f"durable snapshot index entries mismatch: {path.name}")
        seen.add(snapshot_id)
        _validate_manifest_identity(manifest, snapshot_id, path.name)
        manifest_name = f"{snapshot_id}{_MANIFEST_SUFFIX}"
        if manifest_name not in remote_entries:
            continue
        _validate_remote_manifest_references(manifest, remote_entries)
        indexed.append(manifest)
    return indexed


def _indexed_remote_manifests(
    *,
    snapshot_root: Path,
    files: Any,
    publication_root: str,
    remote_entries: Mapping[str, Any],
    authority: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Load one verified index and only canonical manifests not represented by it."""
    index_names = sorted(name for name in remote_entries if name.endswith(_INDEX_SUFFIX))
    indexed: list[dict[str, Any]] = []
    if index_names:
        index_path = _download_remote_file(
            files, publication_root, snapshot_root, index_names[-1],
        )
        indexed = _load_snapshot_index(
            index_path, authority=authority, remote_entries=remote_entries,
        )
    represented = {item["snapshot_id"] for item in indexed}
    missing_names = sorted(
        name
        for name in remote_entries
        if name.endswith(_MANIFEST_SUFFIX)
        and name.removesuffix(_MANIFEST_SUFFIX) not in represented
    )
    for name in missing_names:
        if not (snapshot_root / name).is_file():
            _download_remote_file(files, publication_root, snapshot_root, name)
    missing = _validated_manifests(snapshot_root, remote_entries=remote_entries)
    combined = {item["snapshot_id"]: item for item in indexed}
    combined.update({item["snapshot_id"]: item for item in missing})
    return list(combined.values()), not index_names


def _download_remote_file(files: Any, remote_root: str, local_root: Path, name: str) -> Path:
    destination = local_root / name
    files.download_to(
        _remote_child(remote_root, name),
        str(destination),
        overwrite=False,
        use_parallel=False,
    )
    return destination


@contextmanager
def _snapshot_view(
    root: Path,
    authority: str,
    *,
    databricks: bool,
    create: bool = False,
    latest_manifest_only: bool = False,
    download_manifests: bool = True,
    missing_ok: bool = False,
) -> Iterator[tuple[Path, Any | None, str, Mapping[str, Any] | None]]:
    snapshot_root = root / authority / "snapshots"
    if not databricks:
        if create:
            snapshot_root.mkdir(parents=True, exist_ok=True)
            _reject_symlinks(snapshot_root, "authority snapshot root")
        yield snapshot_root, None, str(snapshot_root), None
        return

    files = _databricks_files_api()
    remote_root = _remote_child(_remote_child(str(root), authority), "snapshots")
    if create:
        files.create_directory(remote_root)
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-durable-view-") as temporary_directory:
        local_root = Path(temporary_directory)
        try:
            remote_entries = _download_remote_snapshot_files(
                files,
                remote_root,
                local_root,
                latest_manifest_only=latest_manifest_only,
                download_manifests=download_manifests,
            )
        except Exception as exc:
            if not _is_databricks_not_found(exc):
                raise
            # NotFound is first use only when the configured Volume root itself is confirmed.
            _confirm_remote_durable_root(files, root)
            if not missing_ok:
                raise FileNotFoundError(f"authority snapshot root is not a directory: {remote_root}") from exc
            remote_entries = {}
        yield local_root, files, remote_root, remote_entries


def _confirm_remote_durable_root(files: Any, root: Path) -> None:
    """Confirm the Unity Catalog Volume itself; a missing subdirectory inside it is first use."""
    volume = Path(*root.parts[:5]) if len(root.parts) > 5 else root
    try:
        files.get_directory_metadata(str(volume))
    except Exception as exc:
        if not _is_databricks_not_found(exc):
            sdk_code = getattr(exc, "error_code", None)
            raise attach_recovery(
                RuntimeError(
                    f"durable_root is unavailable: the Databricks Files API could not confirm Volume {volume} "
                    f"({type(exc).__name__}: {exc})"
                ),
                error_code="durable_root_unavailable",
                context={
                    "classification": "durable_root_unavailable",
                    "durable_root": str(root),
                    "databricks": True,
                    "observed": type(exc).__name__,
                    "sdk_error_code": sdk_code if isinstance(sdk_code, str) else None,
                },
            ) from exc
        raise _durable_root_unavailable(
            root,
            f"durable_root is unavailable: the Databricks Files API reports Volume {volume} not found",
            databricks=True,
            observed="not_found",
        ) from exc


def _authority_marker_present(root: Path, authority: str, *, files: Any | None) -> bool:
    if files is None:
        return os.path.lexists(root / authority / _AUTHORITY_MARKER)
    authority_root = _remote_child(str(root), authority)
    marker = _remote_child(authority_root, _AUTHORITY_MARKER)
    try:
        return any(str(entry.path) == marker for entry in files.list_directory_contents(authority_root))
    except Exception as exc:
        if _is_databricks_not_found(exc):
            return False
        raise


def _ensure_authority_marker(root: Path, authority: str, *, files: Any | None, staging: Path) -> str:
    """Record that this lineage has published a snapshot; identical bytes for every writer."""
    expected = _canonical_bytes({"authority_id": authority, "format": _AUTHORITY_FORMAT})
    staged = staging / _AUTHORITY_MARKER
    staged.write_bytes(expected)
    if files is None:
        marker = root / authority / _AUTHORITY_MARKER
        if not os.path.lexists(marker):
            descriptor, temporary = tempfile.mkstemp(prefix=f".{_AUTHORITY_MARKER}.", dir=marker.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(expected)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, marker)
                except OSError as exc:
                    if isinstance(exc, FileExistsError):
                        raise
                    if exc.errno not in (errno.EXDEV, errno.ENOSYS, errno.EPERM, errno.EACCES):
                        raise
                    # Filesystems without hard links: every writer publishes identical
                    # canonical bytes, so an atomic rename never leaves a partial marker
                    # or replaces it with different content.
                    os.replace(temporary, marker)
                _fsync_directory(marker.parent)
                return "created"
            except FileExistsError:
                pass
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)
        if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != expected:
            raise RuntimeError(f"durable authority marker mismatch: {marker}")
        return "present"
    marker_path = _remote_child(_remote_child(str(root), authority), _AUTHORITY_MARKER)
    if not _authority_marker_present(root, authority, files=files):
        _publish_remote_file(files, staged, marker_path)
        return "created"
    if not _remote_file_matches(files, staged, marker_path):
        raise RuntimeError(f"durable authority marker mismatch: {marker_path}")
    return "present"


def _no_snapshots(root: Path, authority: str, *, files: Any | None, databricks: bool) -> Exception:
    """Classify an empty lineage as first use or as missing lineage; never guess."""
    marker = (
        _remote_child(_remote_child(str(root), authority), _AUTHORITY_MARKER)
        if databricks
        else str(root / authority / _AUTHORITY_MARKER)
    )
    context = {
        "durable_root": str(root),
        "authority_id": authority,
        "databricks": databricks,
        "authority_marker": marker,
    }
    if _authority_marker_present(root, authority, files=files):
        flags = ["--durable-root", str(root), "--authority", authority]
        return attach_recovery(
            RuntimeError(
                f"durable lineage is missing: authority marker {marker} shows snapshots were "
                "published, but no valid snapshots were found; refusing to initialize empty state"
            ),
            error_code="durable_lineage_missing",
            context={**context, "classification": "durable_lineage_missing"},
            next_operations=[{
                "operation": "list_snapshots",
                "arguments": {"durable_root": str(root), "authority_id": authority, "databricks": databricks},
                "copy_ready": shlex.join(
                    ["anchor", "state", "list", *flags] + (["--databricks"] if databricks else [])
                ),
                "reason": "inspect the durable lineage; recovering or replacing it requires owner judgment",
                "requires_owner": True,
                "retry_safety": "read_only",
            }],
        )
    unavailable = DurableSnapshotUnavailable("no valid durable snapshots")
    unavailable.context = {**context, "classification": "no_lineage"}  # type: ignore[attr-defined]
    return unavailable


def _publish_remote_file(files: Any, source: Path, destination: str) -> None:
    try:
        files.upload_from(destination, str(source), overwrite=False, use_parallel=False)
    except Exception:
        if _remote_file_matches(files, source, destination):
            return
        raise
    if _remote_file_matches(files, source, destination):
        return
    raise RuntimeError(f"Databricks upload verification failed: {source.name}")


def _remote_file_matches(files: Any, source: Path, destination: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-upload-check-") as temporary_directory:
        verified = Path(temporary_directory) / source.name
        try:
            files.download_to(destination, str(verified), overwrite=False, use_parallel=False)
        except Exception:
            return False
        return verified.stat().st_size == source.stat().st_size and _sha256(verified) == _sha256(source)


def _retention_policy(
    retention_days: int | None,
    minimum_snapshots: int | None,
) -> tuple[int, int] | None:
    if retention_days is None and minimum_snapshots is None:
        return None
    if retention_days is None or minimum_snapshots is None:
        raise ValueError(
            "retention_days and minimum_snapshots must be configured together"
        )
    for value, label, maximum in (
        (retention_days, "retention_days", 3650),
        (minimum_snapshots, "minimum_snapshots", 1000),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{label} must be an integer from 1 through {maximum}")
    return retention_days, minimum_snapshots


def _delete_publication(
    path: Path,
    *,
    files: Any | None,
    publication_root: str,
) -> None:
    try:
        if files is None:
            path.unlink()
        else:
            files.delete(_remote_child(publication_root, path.name))
    except Exception as exc:
        if isinstance(exc, FileNotFoundError) or _is_databricks_not_found(exc):
            return
        raise


def _apply_retention(
    *,
    snapshot_root: Path,
    files: Any | None,
    publication_root: str,
    manifests: list[dict[str, Any]],
    retention_days: int,
    minimum_snapshots: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove expired manifests, then blobs no retained manifest can reach."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    ordered = sorted(
        manifests,
        key=lambda item: (item["created_at"], item["snapshot_id"]),
        reverse=True,
    )
    expired = [
        item
        for index, item in enumerate(ordered)
        if index >= minimum_snapshots
        and datetime.fromisoformat(
            str(item["created_at"]).replace("Z", "+00:00")
        )
        < cutoff
    ]
    candidate_blobs = {item["snapshot_file"] for item in expired} | {
        item["artifacts"]["file"]
        for item in expired
        if item["format"] == _FORMAT_V2
    }
    for item in expired:
        manifest_path = snapshot_root / f"{item['snapshot_id']}{_MANIFEST_SUFFIX}"
        _delete_publication(
            manifest_path,
            files=files,
            publication_root=publication_root,
        )

    retained = [item for item in ordered if item not in expired]
    removed_blobs: list[str] = []
    referenced = {item["snapshot_file"] for item in retained} | {
        item["artifacts"]["file"]
        for item in retained
        if item["format"] == _FORMAT_V2
    }
    for name in sorted(candidate_blobs - referenced):
        path = snapshot_root / name
        if files is not None or path.is_file():
            _delete_publication(
                path,
                files=files,
                publication_root=publication_root,
            )
            removed_blobs.append(name)
    return (
        {
            "status": "applied",
            "days": retention_days,
            "minimum_snapshots": minimum_snapshots,
            "removed_snapshots": len(expired),
            "removed_blobs": len(removed_blobs),
            "retained_snapshots": len(retained),
        },
        retained,
    )


def _publish_snapshot_index(
    *,
    snapshot_root: Path,
    files: Any,
    publication_root: str,
    authority: str,
    manifests: list[dict[str, Any]],
    prior_index_names: list[str],
    migrated: bool,
) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    body = {
        "authority_id": authority,
        "format": _INDEX_FORMAT,
        "generated_at": generated_at,
        "snapshots": sorted(
            manifests, key=lambda item: (item["created_at"], item["snapshot_id"]),
        ),
    }
    index = dict(body)
    index["index_sha256"] = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    checkpoint = re.sub(r"[^0-9TZ]", "", generated_at)
    name = f"snapshot-index-{checkpoint}-{index['index_sha256'][:16]}{_INDEX_SUFFIX}"
    staged = snapshot_root / name
    staged.write_bytes(_canonical_bytes(index))
    _publish_remote_file(files, staged, _remote_child(publication_root, name))
    for prior in prior_index_names:
        if prior != name:
            _delete_publication(
                snapshot_root / prior,
                files=files,
                publication_root=publication_root,
            )
    return {
        "status": "migrated" if migrated else "updated",
        "file": name,
        "snapshot_count": len(manifests),
    }


def _enforce_retention(
    *,
    snapshot_root: Path,
    files: Any | None,
    publication_root: str,
    manifests: list[dict[str, Any]],
    retention_policy: tuple[int, int],
    authority: str,
    remote_entries: Mapping[str, Any] | None,
    index_migrated: bool,
) -> dict[str, Any]:
    retention, retained = _apply_retention(
        snapshot_root=snapshot_root,
        files=files,
        publication_root=publication_root,
        manifests=manifests,
        retention_days=retention_policy[0],
        minimum_snapshots=retention_policy[1],
    )
    if files is not None:
        assert remote_entries is not None
        retention["index"] = _publish_snapshot_index(
            snapshot_root=snapshot_root,
            files=files,
            publication_root=publication_root,
            authority=authority,
            manifests=retained,
            prior_index_names=sorted(
                name for name in remote_entries if name.endswith(_INDEX_SUFFIX)
            ),
            migrated=index_migrated,
        )
    return retention


def list_snapshots(
    *,
    durable_root: str | os.PathLike[str],
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """List canonical publications, deferring remote payload hashes until use."""
    root = _absolute_path(
        durable_root,
        "durable_root",
        inspect_filesystem=not databricks,
    )
    authority = _authority_id(authority_id)
    if databricks and not _is_within(root, Path("/Volumes")):
        raise ValueError("durable_root must be a Unity Catalog Volume when databricks=True")
    if not databricks:
        _reject_symlinks(root, "durable_root")
        if not root.is_dir():
            raise _durable_root_unavailable(
                root, f"durable_root is not a directory: {root}", databricks=False, observed="not_a_directory",
            )
        snapshot_root = root / authority / "snapshots"
        if not snapshot_root.is_dir():
            raise FileNotFoundError(f"authority snapshot root is not a directory: {snapshot_root}")
    with _snapshot_view(root, authority, databricks=databricks) as (
        snapshot_root, _, _, remote_entries,
    ):
        manifests = _validated_manifests(
            snapshot_root,
            remote_entries=remote_entries,
        )
    entries = [
        {
            "created_at": item["created_at"],
            "format": item["format"],
            "logical_digest": item["logical_digest"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "snapshot_id": item["snapshot_id"],
            "artifacts": item.get("artifacts"),
        }
        for item in manifests
    ]
    entries.sort(key=lambda item: (item["created_at"], item["snapshot_id"]))
    return {
        "kind": "durable_snapshot_listing",
        "durable_root": str(root),
        "authority_id": authority,
        "payload_verification": (
            "deferred_until_restore_or_reuse" if databricks else "verified"
        ),
        "snapshots": entries,
        "next_operation": _next(
            "restore_latest",
            durable_root=str(root),
            authority_id=authority,
            destination_db="<absolute-local-path>",
            destination_artifacts="<absolute-local-projects-path>",
            databricks=databricks,
        ),
    }


def snapshot_state(
    *,
    source_db: str | os.PathLike[str],
    source_artifacts: str | os.PathLike[str] | None = None,
    durable_root: str | os.PathLike[str],
    authority_id: str,
    databricks: bool = False,
    retention_days: int | None = None,
    minimum_snapshots: int | None = None,
) -> dict[str, Any]:
    """Back up live local state and publish immutable bytes plus a commit manifest."""
    retention_policy = _retention_policy(retention_days, minimum_snapshots)
    qualified = qualify_paths(
        source_db=source_db,
        source_artifacts=source_artifacts,
        durable_root=durable_root,
        authority_id=authority_id,
        databricks=databricks,
    )
    source = Path(qualified["source_db"])
    artifacts_source = (
        Path(qualified["source_artifacts"])
        if qualified["source_artifacts"] is not None
        else None
    )
    root = Path(qualified["durable_root"])
    if not source.is_file():
        raise FileNotFoundError(f"source_db is not a file: {source}")
    if not databricks and not root.is_dir():
        raise _durable_root_unavailable(
            root, f"durable_root is not a directory: {root}", databricks=False, observed="not_a_directory",
        )
    authority = ensure_database_authority(
        source,
        authority_id=qualified["authority_id"],
        trust_domain="work",
        initialize=True,
    )
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-snapshot-") as temporary_directory:
        staged = Path(temporary_directory) / "snapshot.sqlite3"
        staged_artifacts = Path(temporary_directory) / "artifacts.tar"
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
        from odibi_anchor.codebase._workflow_artifact_restore import snapshot_proofs

        proofs = snapshot_proofs(staged, artifacts_source) if artifacts_source is not None else []
        artifact_manifest = (
            _stage_artifact_bundle(artifacts_source, staged_artifacts)
            if artifacts_source is not None
            else None
        )
        if artifact_manifest is not None:
            if snapshot_proofs(staged, artifacts_source) != proofs:
                raise RuntimeError("draft artifact baseline changed during snapshot")
            if proofs:
                artifact_manifest["workflow_baselines"] = proofs
        # Additive v2 body key: manifest_sha256 covers it and older readers ignore it.
        descriptor_integrity = (
            _descriptor_integrity(staged_artifacts) if artifact_manifest is not None else None
        )
        with _snapshot_view(
            root,
            qualified["authority_id"],
            databricks=databricks,
            create=True,
            latest_manifest_only=retention_policy is None,
            download_manifests=retention_policy is None,
        ) as (snapshot_root, files, publication_root, remote_entries):
            if databricks:
                assert files is not None
            snapshot_path = snapshot_root / f"{snapshot_sha256}{_SNAPSHOT_SUFFIX}"
            published_snapshot_path = _remote_child(publication_root, snapshot_path.name)
            artifacts_path = (
                snapshot_root / artifact_manifest["file"]
                if artifact_manifest is not None
                else None
            )
            published_artifacts_path = (
                _remote_child(publication_root, artifacts_path.name)
                if artifacts_path is not None
                else None
            )
            if databricks and retention_policy is not None:
                assert files is not None and remote_entries is not None
                manifests, index_migrated = _indexed_remote_manifests(
                    snapshot_root=snapshot_root,
                    files=files,
                    publication_root=publication_root,
                    remote_entries=remote_entries,
                    authority=qualified["authority_id"],
                )
            else:
                manifests = _validated_manifests(
                    snapshot_root,
                    remote_entries=remote_entries,
                )
                index_migrated = False
            if manifests:
                latest_created = max(str(item["created_at"]) for item in manifests)
                latest = [item for item in manifests if item["created_at"] == latest_created]
                if len(latest) != 1:
                    raise RuntimeError("ambiguous latest durable snapshot")
                existing = latest[0]
            else:
                existing = None
            same_checkpoint = (
                existing is not None
                and existing["sha256"] == snapshot_sha256
                and (
                    (
                        artifact_manifest is None
                        and existing["format"] == _FORMAT_V1
                    )
                    or (
                        artifact_manifest is not None
                        and existing["format"] == _FORMAT_V2
                        and existing["artifacts"]["sha256"] == artifact_manifest["sha256"]
                        and existing["artifacts"].get("workflow_baselines", []) == artifact_manifest.get("workflow_baselines", [])
                    )
                )
            )
            if same_checkpoint:
                assert existing is not None
                if databricks:
                    assert files is not None
                    if not _remote_file_matches(files, staged, published_snapshot_path):
                        raise RuntimeError(f"snapshot hash mismatch: {snapshot_path.name}")
                    if (
                        staged_artifacts.is_file()
                        and published_artifacts_path is not None
                        and not _remote_file_matches(
                            files, staged_artifacts, published_artifacts_path,
                        )
                    ):
                        raise RuntimeError(
                            "artifact bundle hash mismatch: "
                            f"{existing['artifacts']['file']}"
                        )
                manifest_path = snapshot_root / f"{existing['snapshot_id']}{_MANIFEST_SUFFIX}"
                published_manifest_path = _remote_child(publication_root, manifest_path.name)
                result = {
                    "kind": "durable_snapshot",
                    "action": "reused",
                    "manifest": existing,
                    "descriptor_integrity": descriptor_integrity,
                    "manifest_path": published_manifest_path,
                    "snapshot_path": published_snapshot_path,
                    "artifacts_path": published_artifacts_path,
                    "authority": authority,
                    "transport": "databricks_files_api" if databricks else "local_filesystem",
                    "next_operation": _next(
                        "restore_latest",
                        durable_root=str(root),
                        authority_id=qualified["authority_id"],
                        destination_db="<absolute-local-path>",
                        destination_artifacts="<absolute-local-projects-path>",
                        databricks=databricks,
                    ),
                }
                result["authority_marker"] = _ensure_authority_marker(
                    root, qualified["authority_id"], files=files, staging=Path(temporary_directory),
                )
                if retention_policy is not None:
                    result["retention"] = _enforce_retention(
                        snapshot_root=snapshot_root,
                        files=files,
                        publication_root=publication_root,
                        manifests=manifests,
                        retention_policy=retention_policy,
                        authority=qualified["authority_id"],
                        remote_entries=remote_entries,
                        index_migrated=index_migrated,
                    )
                return result
            created = datetime.now(UTC)
            if existing is not None:
                latest = datetime.fromisoformat(existing["created_at"].replace("Z", "+00:00"))
                if created <= latest:
                    created = latest + timedelta(microseconds=1)
            created_at = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
            checkpoint = re.sub(r"[^0-9TZ]", "", created_at)
            snapshot_id = f"{checkpoint}-{snapshot_sha256[:16]}"
            manifest_path = snapshot_root / f"{snapshot_id}{_MANIFEST_SUFFIX}"
            published_manifest_path = _remote_child(publication_root, manifest_path.name)
            body = {
                "created_at": created_at,
                "authority_id": qualified["authority_id"],
                "format": _FORMAT_V2 if artifact_manifest is not None else _FORMAT_V1,
                "integrity_check": "ok",
                "logical_digest": inspection["logical_digest"],
                "schema": inspection["schema"],
                "sha256": snapshot_sha256,
                "size_bytes": staged.stat().st_size,
                "snapshot_file": snapshot_path.name,
                "snapshot_id": snapshot_id,
            }
            if artifact_manifest is not None:
                body["artifacts"] = artifact_manifest
                body["descriptor_integrity"] = descriptor_integrity
            manifest = dict(body)
            manifest["manifest_sha256"] = hashlib.sha256(_canonical_bytes(body)).hexdigest()
            staged_manifest = Path(temporary_directory) / "manifest.json"
            staged_manifest.write_bytes(_canonical_bytes(manifest))
            snapshot_created = (
                snapshot_path.name not in remote_entries
                if remote_entries is not None
                else not snapshot_path.exists()
            )
            artifacts_created = artifacts_path is not None and (
                artifacts_path.name not in remote_entries
                if remote_entries is not None
                else not artifacts_path.exists()
            )
            if snapshot_created:
                if databricks:
                    _publish_remote_file(files, staged, published_snapshot_path)
                else:
                    _publish_file_exclusive(staged, snapshot_path)
            elif databricks:
                assert files is not None
                if not _remote_file_matches(files, staged, published_snapshot_path):
                    raise RuntimeError("durable snapshot content collision")
            elif _sha256(snapshot_path) != snapshot_sha256:
                raise RuntimeError("durable snapshot content collision")
            if artifacts_created:
                assert artifacts_path is not None
                assert published_artifacts_path is not None
                if databricks:
                    _publish_remote_file(files, staged_artifacts, published_artifacts_path)
                else:
                    _publish_file_exclusive(staged_artifacts, artifacts_path)
            elif artifacts_path is not None and databricks:
                assert files is not None
                assert published_artifacts_path is not None
                if not _remote_file_matches(
                    files, staged_artifacts, published_artifacts_path,
                ):
                    raise RuntimeError("durable artifact bundle content collision")
            elif artifacts_path is not None:
                assert artifact_manifest is not None
                if _sha256(artifacts_path) != artifact_manifest["sha256"]:
                    raise RuntimeError("durable artifact bundle content collision")
            # The canonical manifest is the logical commit marker. Content-addressed
            # objects are retained on publication failure because they may be shared
            # by another checkpoint or the remote commit outcome may be indeterminate.
            if databricks:
                _publish_remote_file(files, staged_manifest, published_manifest_path)
            else:
                _publish_file_exclusive(staged_manifest, manifest_path)
            result = {
                "kind": "durable_snapshot",
                "action": "created",
                "manifest": manifest,
                "descriptor_integrity": descriptor_integrity,
                "manifest_path": published_manifest_path,
                "snapshot_path": published_snapshot_path,
                "artifacts_path": published_artifacts_path,
                "authority": authority,
                "transport": "databricks_files_api" if databricks else "local_filesystem",
                "next_operation": _next(
                    "restore_latest",
                    durable_root=str(root),
                    authority_id=qualified["authority_id"],
                    destination_db="<absolute-local-path>",
                    destination_artifacts="<absolute-local-projects-path>",
                    databricks=databricks,
                ),
            }
            result["authority_marker"] = _ensure_authority_marker(
                root, qualified["authority_id"], files=files, staging=Path(temporary_directory),
            )
            if retention_policy is not None:
                result["retention"] = _enforce_retention(
                    snapshot_root=snapshot_root,
                    files=files,
                    publication_root=publication_root,
                    manifests=[*manifests, manifest],
                    retention_policy=retention_policy,
                    authority=qualified["authority_id"],
                    remote_entries=remote_entries,
                    index_migrated=index_migrated,
                )
            return result


def restore_latest(
    *,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str],
    destination_artifacts: str | os.PathLike[str] | None = None,
    authority_id: str,
    overwrite: bool = False,
    databricks: bool = False,
) -> dict[str, Any]:
    """Verify the latest snapshot locally and publish only to absent destinations.

    The artifact destination is created exclusively and bound to this invocation before
    any copy; failure cleanup removes only entries this invocation created. A failure after
    the artifacts are complete leaves a ``restore_incomplete`` record for resume or abandon.
    """
    if overwrite:
        raise ValueError("destructive overwrite is not supported")
    qualified = qualify_paths(
        durable_root=durable_root,
        destination_db=destination_db,
        destination_artifacts=destination_artifacts,
        authority_id=authority_id,
        databricks=databricks,
    )
    root = Path(qualified["durable_root"])
    destination = Path(qualified["destination_db"])
    artifacts_destination = (
        Path(qualified["destination_artifacts"])
        if qualified["destination_artifacts"] is not None
        else None
    )
    if not databricks and not root.is_dir():
        raise _durable_root_unavailable(
            root, f"durable_root is not a directory: {root}", databricks=False, observed="not_a_directory",
        )
    if destination.exists():
        raise FileExistsError(f"destination_db already exists: {destination}")
    if artifacts_destination is not None and os.path.lexists(artifacts_destination):
        incomplete = _incomplete_restore_error(artifacts_destination)
        if incomplete is not None:
            raise incomplete
        raise _destination_conflict(
            artifacts_destination,
            f"destination_artifacts already exists: {artifacts_destination}",
            observed="exists_at_preflight",
        )
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"destination parent is not a directory: {destination.parent}")
    listing_started = time.perf_counter()
    with _snapshot_view(
        root,
        qualified["authority_id"],
        databricks=databricks,
        latest_manifest_only=True,
        missing_ok=True,
    ) as (
        snapshot_root,
        files,
        publication_root,
        remote_entries,
    ):
        manifests = (
            _validated_manifests(snapshot_root, remote_entries=remote_entries)
            if snapshot_root.is_dir()
            else []
        )
        _record_phase(
            "restore_listing", outcome="ok", elapsed_ms=_elapsed_ms(listing_started),
            manifests=len(manifests),
        )
        if not manifests:
            raise _no_snapshots(root, qualified["authority_id"], files=files, databricks=databricks)
        latest_created = max(str(item["created_at"]) for item in manifests)
        latest = [item for item in manifests if item["created_at"] == latest_created]
        if len(latest) != 1:
            raise RuntimeError("ambiguous latest durable snapshot")
        return _restore_manifest(
            qualified=qualified,
            manifest=latest[0],
            snapshot_root=snapshot_root,
            files=files,
            publication_root=publication_root,
            record=None,
        )


def _restore_manifest(
    *,
    qualified: Mapping[str, Any],
    manifest: dict[str, Any],
    snapshot_root: Path,
    files: Any | None,
    publication_root: str,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    """Verify one manifest's payloads, fill the owned artifact tree, then publish the database."""
    databricks = bool(qualified["databricks"])
    destination = Path(qualified["destination_db"])
    artifacts_destination = (
        Path(qualified["destination_artifacts"])
        if qualified["destination_artifacts"] is not None
        else None
    )
    if manifest.get("authority_id") != qualified["authority_id"]:
        raise RuntimeError("durable snapshot authority mismatch")
    if manifest["format"] == _FORMAT_V2 and artifacts_destination is None:
        raise ValueError(
            "destination_artifacts is required to restore a v2 durable snapshot"
        )
    if files is not None:
        with _phase("restore_download", transport="databricks_files_api"):
            _download_remote_file(
                files,
                publication_root,
                snapshot_root,
                manifest["snapshot_file"],
            )
            if manifest["format"] == _FORMAT_V2:
                _download_remote_file(
                    files,
                    publication_root,
                    snapshot_root,
                    manifest["artifacts"]["file"],
                )
            _validate_manifest_payloads(snapshot_root, manifest)
    durable_snapshot = snapshot_root / str(manifest["snapshot_file"])
    # Stage on the destination filesystem so hard-link publication is an atomic,
    # no-overwrite directory-entry operation rather than the partial-copy fallback.
    with tempfile.TemporaryDirectory(
        prefix=".odibi-anchor-restore-", dir=destination.parent
    ) as temporary_directory:
        with _phase("restore_verify_database", size_bytes=manifest.get("size_bytes")):
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
                staged,
                authority_id=qualified["authority_id"],
                trust_domain="work",
            )
        artifacts_status: dict[str, Any]
        if manifest["format"] == _FORMAT_V2:
            assert artifacts_destination is not None
            if not artifacts_destination.parent.exists():
                artifacts_destination.parent.mkdir(parents=True)
            _reject_symlinks(artifacts_destination.parent, "destination_artifacts")
            with _phase("restore_extract_artifacts", file_count=manifest["artifacts"].get("file_count")):
                staged_artifacts = Path(temporary_directory) / "artifacts"
                bundle = snapshot_root / manifest["artifacts"]["file"]
                extracted = _extract_artifact_bundle(bundle, staged_artifacts)
                expected = {
                    key: manifest["artifacts"][key]
                    for key in ("file_count", "directory_count", "content_size_bytes")
                }
                if extracted != expected:
                    raise RuntimeError("artifact bundle inventory mismatch")
            from odibi_anchor._dispatcher._session import (
                _relocate_restored_continuity,
            )

            continuity = _relocate_restored_continuity(
                staged_artifacts, artifacts_destination
            )
            if continuity["status"] == "relocated":
                from odibi_anchor.codebase._authority_relocation import append_verified_relocation

                continuity["attestation_id"] = append_verified_relocation(
                    staged, source_home=continuity["source_home"],
                    destination_home=continuity["destination_home"], manifest=manifest,
                )
            fresh = record is None
            if record is None:
                record = _claim_restore_destination(artifacts_destination, {
                    "authority_id": qualified["authority_id"],
                    "databricks": databricks,
                    "destination_artifacts": str(artifacts_destination),
                    "destination_db": str(destination),
                    "durable_root": qualified["durable_root"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "snapshot_id": manifest["snapshot_id"],
                })
            created: list[tuple[Path, str, tuple[int, int]]] = []
            try:
                with _phase("restore_fill_artifacts"):
                    _fill_owned_destination(staged_artifacts, artifacts_destination, record, created)
                    if not _restore_owned(artifacts_destination, record):
                        raise RuntimeError("restore destination ownership changed during copy")
                    from odibi_anchor.codebase._workflow_artifact_restore import append_verified_restores

                    append_verified_restores(staged, projects=artifacts_destination, manifest=manifest)
            except Exception as exc:
                released = _release_owned_destination(
                    artifacts_destination, record, created, remove_root=fresh,
                )
                if released == "removed":
                    raise
                if released == "not_owned":
                    raise _destination_conflict(
                        artifacts_destination,
                        "destination_artifacts identity or ownership marker changed during restore; "
                        f"nothing further was removed: {artifacts_destination}",
                        observed="ownership_lost",
                    ) from exc
                raise _restore_incomplete(
                    record,
                    observed=f"artifact copy or verification failed: {type(exc).__name__}: {exc}",
                    include_resume=False,
                ) from exc
            artifacts_status = {
                "status": "restored",
                "destination": str(artifacts_destination),
                "continuity": continuity,
                **manifest["artifacts"],
            }
        else:
            artifacts_status = {"status": "not_included_legacy_v1"}
        try:
            with _phase("restore_publish"):
                if record is not None:
                    assert artifacts_destination is not None
                    record = _record_database_intent(artifacts_destination, record, staged)
                _publish_file_exclusive(staged, destination)
        except Exception as exc:
            if record is None or artifacts_destination is None:
                raise
            if not _restore_owned(artifacts_destination, record):
                raise _destination_conflict(
                    artifacts_destination,
                    "destination_artifacts identity or ownership marker changed during restore: "
                    f"{artifacts_destination}",
                    observed="ownership_lost",
                ) from exc
            raise _restore_incomplete(
                record, observed=f"database publication failed: {type(exc).__name__}: {exc}",
            ) from exc
    if record is not None:
        assert artifacts_destination is not None
        if not _restore_owned(artifacts_destination, record):
            raise _destination_conflict(
                artifacts_destination,
                "the database was published but destination_artifacts no longer carries this "
                f"restore's ownership marker: {artifacts_destination}",
                observed="ownership_lost_after_publication",
            )
        os.unlink(artifacts_destination / _RESTORE_OWNER)
        _fsync_directory(artifacts_destination)
    authority = ensure_database_authority(
        destination,
        authority_id=qualified["authority_id"],
        trust_domain="work",
    )
    return {
        "kind": "durable_restore",
        "status": "restored",
        "action": "created",
        "classification": "restored",
        "destination_db": str(destination),
        "snapshot_id": manifest["snapshot_id"],
        "sha256": manifest["sha256"],
        "logical_digest": manifest["logical_digest"],
        "restored_sha256": _sha256(destination),
        "restored_logical_digest": logical_digest(destination),
        "integrity_check": "ok",
        "format": manifest["format"],
        "artifacts": artifacts_status,
        "descriptor_integrity": manifest.get("descriptor_integrity"),
        "authority": authority,
        "transport": "databricks_files_api" if databricks else "local_filesystem",
        "next_operation": _next("inspect_restored_state", destination_db=str(destination)),
    }


def _owned_incomplete_restore(qualified: Mapping[str, Any]) -> dict[str, Any]:
    """Return the verified unfinished restore that exactly matches the requested arguments."""
    destination = Path(qualified["destination_artifacts"])
    record = _read_restore_record(destination)
    if record is None or not _restore_owned(destination, record):
        raise _destination_conflict(
            destination,
            f"no incomplete restore owned by Anchor is recorded at destination_artifacts: {destination}",
            observed="no_owned_restore_record",
        )
    mismatched = sorted(
        key
        for key in ("durable_root", "authority_id", "destination_db", "destination_artifacts", "databricks")
        if record[key] != qualified[key]
    )
    if mismatched:
        raise _destination_conflict(
            destination,
            "the incomplete restore record does not match the requested "
            f"{', '.join(mismatched)}: {destination}",
            observed="record_mismatch",
        )
    return record


def _foreign_database_conflict(record: Mapping[str, Any]) -> FileExistsError:
    return _destination_conflict(
        Path(record["destination_db"]),
        "destination_db exists and is not the database this restore published: "
        f"{record['destination_db']}",
        observed="foreign_destination_db",
    )


def _restore_recovery_arguments(
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str],
    destination_artifacts: str | os.PathLike[str],
    authority_id: str,
    databricks: bool,
) -> dict[str, Any]:
    qualified = qualify_paths(
        durable_root=durable_root,
        destination_db=destination_db,
        destination_artifacts=destination_artifacts,
        authority_id=authority_id,
        databricks=databricks,
    )
    if qualified["destination_artifacts"] is None:
        raise ValueError("destination_artifacts is required")
    return qualified


def resume_restore(
    *,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str],
    destination_artifacts: str | os.PathLike[str],
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Finish an unfinished restore by re-verifying it against its recorded snapshot manifest."""
    qualified = _restore_recovery_arguments(
        durable_root, destination_db, destination_artifacts, authority_id, databricks,
    )
    record = _owned_incomplete_restore(qualified)
    destination = Path(qualified["destination_artifacts"])
    if _restore_database_published(record):
        # Only the marker removal remained; the tree was verified before publication.
        os.unlink(destination / _RESTORE_OWNER)
        _fsync_directory(destination)
        return {
            "kind": "durable_restore",
            "status": "restored",
            "action": "finalized",
            "classification": "restored",
            "destination_db": qualified["destination_db"],
            "destination_artifacts": str(destination),
            "snapshot_id": record["snapshot_id"],
            "authority": ensure_database_authority(
                qualified["destination_db"], authority_id=qualified["authority_id"], trust_domain="work",
            ),
            "next_operation": _next("inspect_restored_state", destination_db=qualified["destination_db"]),
        }
    if os.path.lexists(qualified["destination_db"]):
        raise _foreign_database_conflict(record)
    root = Path(qualified["durable_root"])
    name = f"{record['snapshot_id']}{_MANIFEST_SUFFIX}"
    with _snapshot_view(
        root, qualified["authority_id"], databricks=databricks, download_manifests=False,
        missing_ok=True,
    ) as (snapshot_root, files, publication_root, remote_entries):
        # Lost or altered lineage offers only abandon; transport errors propagate for a retry.
        try:
            if remote_entries is not None:
                if name not in remote_entries:
                    raise RuntimeError(f"recorded snapshot manifest is missing: {name}")
                _download_remote_file(files, publication_root, snapshot_root, name)
            elif not (snapshot_root / name).is_file():
                raise RuntimeError(f"recorded snapshot manifest is missing: {name}")
            manifest = _load_manifest(snapshot_root / name)
            _validate_manifest_identity(manifest, record["snapshot_id"], name)
            if remote_entries is None:
                _validate_manifest_payloads(snapshot_root, manifest)
            else:
                _validate_remote_manifest_references(manifest, remote_entries)
            if manifest["manifest_sha256"] != record["manifest_sha256"]:
                raise RuntimeError("recorded snapshot manifest checksum changed")
        except RuntimeError as exc:
            raise _restore_incomplete(
                record,
                observed=f"the recorded snapshot manifest cannot be verified: {type(exc).__name__}: {exc}",
                include_resume=False,
            ) from exc
        result = _restore_manifest(
            qualified=qualified,
            manifest=manifest,
            snapshot_root=snapshot_root,
            files=files,
            publication_root=publication_root,
            record=record,
        )
    result["action"] = "resumed"
    return result


def abandon_restore(
    *,
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str],
    destination_artifacts: str | os.PathLike[str],
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Move an unfinished owned restore to a preserved sibling quarantine; never delete it."""
    qualified = _restore_recovery_arguments(
        durable_root, destination_db, destination_artifacts, authority_id, databricks,
    )
    record = _owned_incomplete_restore(qualified)
    destination = Path(qualified["destination_artifacts"])
    if _restore_database_published(record):
        raise _destination_conflict(
            destination,
            "the restore already published its database; resume finalizes it instead of abandoning: "
            f"{destination}",
            observed="database_published",
        )
    stamp = re.sub(r"[^0-9TZ]", "", datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"))
    quarantine = destination.parent / f"{destination.name}.restore-abandoned-{stamp}-{record['nonce'][:12]}"
    os.mkdir(quarantine)
    preserved = quarantine / destination.name
    if not _restore_owned(destination, record):
        os.rmdir(quarantine)
        raise _destination_conflict(
            destination,
            f"destination_artifacts ownership changed before abandon: {destination}",
            observed="ownership_lost",
        )
    os.rename(destination, preserved)
    _fsync_directory(destination.parent)
    return {
        "kind": "durable_restore_abandoned",
        "action": "quarantined",
        "destination_artifacts": str(destination),
        "destination_db": qualified["destination_db"],
        "quarantine_path": str(preserved),
        "snapshot_id": record["snapshot_id"],
        "next_operation": _next(
            "restore_latest",
            durable_root=qualified["durable_root"],
            authority_id=qualified["authority_id"],
            destination_db=qualified["destination_db"],
            destination_artifacts=str(destination),
            databricks=databricks,
        ),
    }
