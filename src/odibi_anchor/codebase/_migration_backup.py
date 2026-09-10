"""Content-addressed pre-migration backups that can never permanently block startup.

A fixed backup filename makes the first migration unrepeatable: once
``<db>.pre-<tag>.bak`` exists, every later migration of a *different* database at the
same path fails forever with "backup already exists", which deadlocks schema
initialization (and therefore learning closure).  See WI-2026-0019.

The backup name here embeds a digest of the source database's **logical** content, so:

* distinct pre-migration contents get distinct filenames and can never collide;
* re-running a migration over identical content is idempotent and reuses the backup;
* a same-name backup whose logical content does not match the source fails closed;
* no existing file is ever overwritten, moved, or deleted.

The digest is taken over ``sqlite3.iterdump`` output rather than raw file bytes because
``Connection.backup`` reproduces logical content, not a byte-identical page image.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

_IS_WINDOWS = os.name == "nt"

#: Characters of the hex digest embedded in a backup filename.
DIGEST_PREFIX_LENGTH = 16


class _PublishCollision(RuntimeError):
    """Another process published the same content address first."""


def _read_only_uri(db_path: str | Path) -> str:
    """Return a correctly encoded read-only SQLite URI for one existing file.

    ``Path.as_uri`` percent-encodes URI metacharacters. Interpolating a raw path
    instead lets ``#`` start a fragment and ``?`` start a query, which silently
    truncates the path, drops ``mode=ro``, and can open or create a *different*
    file -- yielding a "verified" backup of the wrong database.
    """
    return Path(db_path).resolve().as_uri() + "?mode=ro"


def logical_digest(db_path: str | Path) -> str:
    """Return a sha256 over the database's logical schema and rows.

    Two databases with identical schema and identical row content produce the same
    digest even when their page images differ, which is exactly the relationship
    between a source database and its ``Connection.backup`` copy.
    """
    digest = hashlib.sha256()
    connection = sqlite3.connect(_read_only_uri(db_path), uri=True)
    try:
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        connection.close()
    return digest.hexdigest()


def _integrity_check(db_path: str | Path) -> str:
    """Run SQLite's full integrity check without making the database writable."""
    connection = sqlite3.connect(_read_only_uri(db_path), uri=True)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def migration_backup_path(db_path: str | Path, *, tag: str, digest: str) -> Path:
    """Return the content-addressed backup path for one source digest."""
    if not tag or "/" in tag or "\\" in tag:
        raise ValueError("migration backup tag must be a simple name")
    return Path(f"{Path(db_path)}.pre-{tag}.{digest[:DIGEST_PREFIX_LENGTH]}.bak")


def _publish_without_overwrite(temporary: Path, destination: Path, *, collision_error: str) -> None:
    """Durably publish a verified backup, never replacing an existing destination."""
    try:
        # Windows' _commit rejects read-only descriptors, so fsync through a
        # writable handle while preserving fatal file-durability failures.
        with open(temporary, "r+b") as backup_file:
            os.fsync(backup_file.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise _PublishCollision(collision_error) from exc
        except OSError as exc:
            if exc.errno not in (errno.ENOSYS, errno.EPERM):
                raise
            # Filesystem lacks or prohibits hard links (e.g. Databricks workspace FS).
            # Reserve the destination atomically, then copy into the file we alone
            # created. A check-then-rename fallback would overwrite a concurrently
            # published destination on POSIX.
            try:
                destination_fd = os.open(
                    destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
                )
            except FileExistsError as collision:
                raise _PublishCollision(collision_error) from collision
            destination_identity = os.fstat(destination_fd)
            try:
                with open(temporary, "rb") as source_file, os.fdopen(
                    destination_fd, "wb",
                ) as destination_file:
                    shutil.copyfileobj(source_file, destination_file)
                    destination_file.flush()
                    os.fsync(destination_file.fileno())
            except Exception:
                try:
                    current_identity = destination.stat()
                except FileNotFoundError:
                    pass
                else:
                    if (
                        current_identity.st_dev == destination_identity.st_dev
                        and current_identity.st_ino == destination_identity.st_ino
                    ):
                        destination.unlink()
                raise
    finally:
        temporary.unlink(missing_ok=True)

    # Opening a directory descriptor is unsupported on Windows. File fsync above
    # remains mandatory there; POSIX additionally persists the directory entry.
    if not _IS_WINDOWS:
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def ensure_migration_backup(
    db_path: str | Path, *, tag: str, error_prefix: str,
) -> dict[str, Any]:
    """Guarantee a verified pre-migration backup of ``db_path`` exists.

    Returns a dict with ``action`` (``"created"`` or ``"reused"``), ``backup_path``,
    ``source_digest``, and ``integrity_check``.

    Raises ``RuntimeError`` without mutating anything when a backup already occupies the
    content-addressed name but does not hold the source's logical content.
    """
    source_path = Path(db_path).expanduser()
    source_digest = logical_digest(source_path)
    destination = migration_backup_path(source_path, tag=tag, digest=source_digest)

    if destination.exists():
        # Same content address: reuse it only if it really holds this content.
        try:
            existing_digest = logical_digest(destination)
            existing_integrity = _integrity_check(destination)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"{error_prefix} backup is not a readable database: {destination.name}"
            ) from exc
        if existing_digest != source_digest or existing_integrity != "ok":
            raise RuntimeError(
                f"{error_prefix} backup content mismatch for {destination.name}"
            )
        return {
            "action": "reused",
            "backup_path": str(destination),
            "source_digest": source_digest,
            "integrity_check": existing_integrity,
        }

    # The staging name is unique per attempt. A shared ".tmp" name would let one
    # crashed run's leftover file block every later migration forever -- the same
    # permanent dead-end this module exists to remove. An orphaned staging file is
    # inert and inspectable; a deadlock is not, so uniqueness wins.
    temporary = Path(f"{destination}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        reserved_fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:  # pragma: no cover - unique name collision
        raise RuntimeError(f"{error_prefix} backup staging file exists: {temporary.name}") from exc
    os.close(reserved_fd)

    destination_connection = sqlite3.connect(temporary, isolation_level=None, timeout=5.0)
    source_connection = None
    try:
        source_connection = sqlite3.connect(_read_only_uri(source_path), uri=True)
        source_connection.backup(destination_connection)
        if destination_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{error_prefix} backup failed")
    except Exception:
        destination_connection.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if source_connection is not None:
            source_connection.close()
        destination_connection.close()

    # Bind the published filename to the snapshot actually copied. Without this
    # check, a writer between the initial digest and Connection.backup could put
    # different logical content behind the earlier content address.
    try:
        copied_digest = logical_digest(temporary)
        if copied_digest != source_digest:
            raise RuntimeError(f"{error_prefix} source changed while backup was created")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    try:
        _publish_without_overwrite(
            temporary, destination, collision_error=f"{error_prefix} backup already exists"
        )
    except _PublishCollision as collision:
        try:
            winner_digest = logical_digest(destination)
            winner_integrity = _integrity_check(destination)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"{error_prefix} backup is not a readable database: {destination.name}"
            ) from exc
        if winner_digest != source_digest or winner_integrity != "ok":
            raise RuntimeError(
                f"{error_prefix} backup content mismatch for {destination.name}"
            ) from collision
        return {
            "action": "reused",
            "backup_path": str(destination),
            "source_digest": source_digest,
            "integrity_check": winner_integrity,
        }
    return {
        "action": "created",
        "backup_path": str(destination),
        "source_digest": source_digest,
        "integrity_check": "ok",
    }
