"""Contained atomic persistence for canonical operational evidence."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ._contract import canonical_json, utc_now
from ._redaction import redact

_USE_DIRECTORY_FD = os.name != "nt"


@dataclass(frozen=True)
class Artifact:
    path: Path
    relative_path: str
    sha256: str


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or (
        hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT")
        and getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_private_posix_root(root: Path) -> None:
    """Exclude untrusted concurrent writers; same-user mutation remains out of scope."""
    if os.name != "nt" and root.stat().st_mode & 0o022:
        raise ValueError("artifact root must not be writable by group or other users")


def _safe_directory(root: Path, relative: Path) -> Path:
    root = root.resolve(strict=True)
    current = root
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise ValueError("invalid artifact path")
        current = current / part
        if current.exists():
            if _is_link_or_reparse(current):
                raise ValueError("artifact path contains a link or reparse point")
            if not current.is_dir():
                raise ValueError("artifact parent is not a directory")
        else:
            current.mkdir()
    current.resolve(strict=True).relative_to(root)
    return current


def read_artifact(path: str | os.PathLike[str], artifact_root: str | os.PathLike[str]) -> str:
    """Read one contained artifact through a no-follow descriptor where supported."""
    root = Path(artifact_root).resolve(strict=True)
    _assert_private_posix_root(root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=True)
    relative = resolved.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if _is_link_or_reparse(current):
            raise ValueError("artifact path contains a link or reparse point")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        named = candidate.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("artifact changed while it was opened")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def persist_artifact(payload: Any, artifact_root: str | os.PathLike[str], *,
                     kind: Literal["incidents", "tables"] = "incidents", subject: str | None = None) -> Artifact:
    """Redact again and atomically create one immutable canonical JSON artifact."""
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(root):
        raise ValueError("artifact root is a link or reparse point")
    _assert_private_posix_root(root)
    relative = Path(".odibi-anchor/evidence") / kind
    if kind == "tables":
        if not subject:
            raise ValueError("table artifacts require subject")
        slug = "".join(char.lower() if char.isalnum() else "-" for char in subject).strip("-")[:48] or "subject"
        relative /= f"{slug}-{hashlib.sha256(subject.encode()).hexdigest()[:12]}"
    directory = _safe_directory(root, relative)
    cleaned, summary = redact(payload)  # mandatory persistence-boundary pass
    body = {"payload": cleaned, "redaction": summary}
    payload_digest = hashlib.sha256(canonical_json(body)).hexdigest()
    document = {**body, "payload_sha256": payload_digest}
    data = canonical_json(document) + b"\n"
    stamp = utc_now().replace(":", "").replace("-", "")
    prefix = "INC-" if kind == "incidents" else ""
    name = f"{prefix}{stamp}-{secrets.token_hex(6)}.json"
    target = directory / name
    temporary = directory / f".{name}.{secrets.token_hex(8)}.tmp"
    directory_fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if _USE_DIRECTORY_FD:
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            fd = os.open(temporary.name, flags, 0o600, dir_fd=directory_fd)
        else:
            fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link is create-only: an existing target can never be replaced. POSIX
        # publication stays relative to the already-opened no-follow directory.
        if directory_fd < 0:
            stable = _safe_directory(root, relative)
            if stable.resolve(strict=True) != directory.resolve(strict=True):
                raise ValueError("artifact directory changed before publication")
        try:
            if directory_fd >= 0:
                os.link(temporary.name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                        follow_symlinks=False)
            else:
                os.link(temporary, target, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in (errno.ENOSYS, errno.EPERM):
                raise
            # Filesystem lacks or prohibits hard links (e.g. Databricks workspace FS).
            # Fall back to check-then-rename within the private artifact root.
            try:
                if directory_fd >= 0:
                    os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
                else:
                    target.lstat()
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"artifact already exists: {target}") from exc
            if directory_fd >= 0:
                os.rename(temporary.name, target.name, src_dir_fd=directory_fd,
                          dst_dir_fd=directory_fd)
            else:
                os.rename(temporary, target)
        try:
            if directory_fd >= 0:
                os.fsync(directory_fd)
            else:
                sync_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(sync_fd)
                finally:
                    os.close(sync_fd)
        except OSError:  # directory fsync is unavailable on some Windows filesystems
            pass
    finally:
        if directory_fd >= 0:
            with suppress(FileNotFoundError):
                os.unlink(temporary.name, dir_fd=directory_fd)
            os.close(directory_fd)
        else:
            temporary.unlink(missing_ok=True)
    return Artifact(target, target.relative_to(root.resolve()).as_posix(), payload_digest)
