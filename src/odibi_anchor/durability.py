"""Immutable durable snapshots for a live, local SQLite database.

Durable snapshot bytes are treated as opaque files.  SQLite is opened only on the
live local source or on a local staging copy during restore and inspection.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
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
    durable_root: str | os.PathLike[str],
    destination_db: str | os.PathLike[str] | None = None,
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
    destination = _absolute_path(destination_db, "destination_db") if destination_db is not None else None
    if source is None and destination is None:
        raise ValueError("source_db or destination_db is required")
    for path, label in ((source, "source_db"), (destination, "destination_db")):
        if path is not None:
            _reject_overlap(path, root, f"{label} and durable_root")
    if databricks:
        if not _is_within(root, Path("/Volumes")):
            raise ValueError("durable_root must be a Unity Catalog Volume when databricks=True")
        for path, label in ((source, "source_db"), (destination, "destination_db")):
            if path is not None and any(_is_within(path, prefix) for prefix in _DURABLE_LIVE_PREFIXES):
                raise ValueError(f"{label} must be on local compute when databricks=True")
    operation = "snapshot_state" if source is not None else "restore_latest"
    arguments: dict[str, Any] = {"durable_root": str(root)}
    arguments["authority_id"] = authority
    if source is not None:
        arguments["source_db"] = str(source)
    else:
        arguments["destination_db"] = str(destination)
    arguments["databricks"] = databricks
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


def _download_remote_snapshot_files(files: Any, remote_root: str, local_root: Path) -> None:
    local_root.mkdir(exist_ok=True)
    for entry in files.list_directory_contents(remote_root):
        remote_path = str(entry.path)
        name = remote_path.rsplit("/", 1)[-1]
        if remote_path != _remote_child(remote_root, name):
            raise RuntimeError("invalid Databricks snapshot entry")
        if not name.endswith((_MANIFEST_SUFFIX, _SNAPSHOT_SUFFIX)):
            continue
        if not name or "/" in name or "\\" in name:
            raise RuntimeError("invalid Databricks snapshot entry")
        files.download_to(remote_path, str(local_root / name), overwrite=False, use_parallel=False)


@contextmanager
def _snapshot_view(
    root: Path,
    authority: str,
    *,
    databricks: bool,
    create: bool = False,
) -> Iterator[tuple[Path, Any | None, str]]:
    snapshot_root = root / authority / "snapshots"
    if not databricks:
        if create:
            snapshot_root.mkdir(parents=True, exist_ok=True)
            _reject_symlinks(snapshot_root, "authority snapshot root")
        yield snapshot_root, None, str(snapshot_root)
        return

    files = _databricks_files_api()
    remote_root = _remote_child(_remote_child(str(root), authority), "snapshots")
    if create:
        files.create_directory(remote_root)
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-durable-view-") as temporary_directory:
        local_root = Path(temporary_directory)
        try:
            _download_remote_snapshot_files(files, remote_root, local_root)
        except Exception as exc:
            if _is_databricks_not_found(exc):
                raise FileNotFoundError(f"authority snapshot root is not a directory: {remote_root}") from exc
            raise
        yield local_root, files, remote_root


def _publish_remote_file(files: Any, source: Path, destination: str) -> None:
    files.upload_from(destination, str(source), overwrite=False, use_parallel=False)
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-upload-check-") as temporary_directory:
        verified = Path(temporary_directory) / source.name
        files.download_to(destination, str(verified), overwrite=False, use_parallel=False)
        if _sha256(verified) != _sha256(source):
            raise RuntimeError(f"Databricks upload verification failed: {source.name}")


def list_snapshots(
    *,
    durable_root: str | os.PathLike[str],
    authority_id: str,
    databricks: bool = False,
) -> dict[str, Any]:
    """Deterministically list only complete, valid canonical publications."""
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
            raise FileNotFoundError(f"durable_root is not a directory: {root}")
        snapshot_root = root / authority / "snapshots"
        if not snapshot_root.is_dir():
            raise FileNotFoundError(f"authority snapshot root is not a directory: {snapshot_root}")
    with _snapshot_view(root, authority, databricks=databricks) as (snapshot_root, _, _):
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
            databricks=databricks,
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
    if not source.is_file():
        raise FileNotFoundError(f"source_db is not a file: {source}")
    if not databricks and not root.is_dir():
        raise FileNotFoundError(f"durable_root is not a directory: {root}")
    authority = ensure_database_authority(
        source,
        authority_id=qualified["authority_id"],
        trust_domain="work",
        initialize=True,
    )
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
        with _snapshot_view(
            root,
            qualified["authority_id"],
            databricks=databricks,
            create=True,
        ) as (snapshot_root, files, publication_root):
            if databricks:
                assert files is not None
            snapshot_path = snapshot_root / f"{snapshot_sha256}{_SNAPSHOT_SUFFIX}"
            published_snapshot_path = _remote_child(publication_root, snapshot_path.name)
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
                published_manifest_path = _remote_child(publication_root, manifest_path.name)
                return {
                    "kind": "durable_snapshot",
                    "action": "reused",
                    "manifest": existing,
                    "manifest_path": published_manifest_path,
                    "snapshot_path": published_snapshot_path,
                    "authority": authority,
                    "transport": "databricks_files_api" if databricks else "local_filesystem",
                    "next_operation": _next(
                        "restore_latest",
                        durable_root=str(root),
                        authority_id=qualified["authority_id"],
                        destination_db="<absolute-local-path>",
                        databricks=databricks,
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
            published_manifest_path = _remote_child(publication_root, manifest_path.name)
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
                if databricks:
                    _publish_remote_file(files, staged, published_snapshot_path)
                else:
                    _publish_file_exclusive(staged, snapshot_path)
            elif _sha256(snapshot_path) != snapshot_sha256:
                raise RuntimeError("durable snapshot content collision")
            try:
                # The canonical manifest is the commit marker. Roll back this
                # attempt's uncommitted snapshot if marker publication fails.
                if databricks:
                    _publish_remote_file(files, staged_manifest, published_manifest_path)
                else:
                    _publish_file_exclusive(staged_manifest, manifest_path)
            except Exception:
                if snapshot_created:
                    if databricks:
                        assert files is not None
                        files.delete(published_snapshot_path)
                    else:
                        snapshot_path.unlink(missing_ok=True)
                raise
            return {
                "kind": "durable_snapshot",
                "action": "created",
                "manifest": manifest,
                "manifest_path": published_manifest_path,
                "snapshot_path": published_snapshot_path,
                "authority": authority,
                "transport": "databricks_files_api" if databricks else "local_filesystem",
                "next_operation": _next(
                    "restore_latest",
                    durable_root=str(root),
                    authority_id=qualified["authority_id"],
                    destination_db="<absolute-local-path>",
                    databricks=databricks,
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
        durable_root=durable_root,
        destination_db=destination_db,
        authority_id=authority_id,
        databricks=databricks,
    )
    root = Path(qualified["durable_root"])
    destination = Path(qualified["destination_db"])
    if not databricks and not root.is_dir():
        raise FileNotFoundError(f"durable_root is not a directory: {root}")
    if destination.exists():
        raise FileExistsError(f"destination_db already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"destination parent is not a directory: {destination.parent}")
    with _snapshot_view(root, qualified["authority_id"], databricks=databricks) as (
        snapshot_root,
        _,
        _,
    ):
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
        with tempfile.TemporaryDirectory(
            prefix=".odibi-anchor-restore-", dir=destination.parent
        ) as temporary_directory:
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
            _publish_file_exclusive(staged, destination)
    authority = ensure_database_authority(
        destination,
        authority_id=qualified["authority_id"],
        trust_domain="work",
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
        "transport": "databricks_files_api" if databricks else "local_filesystem",
        "next_operation": _next("inspect_restored_state", destination_db=str(destination)),
    }
