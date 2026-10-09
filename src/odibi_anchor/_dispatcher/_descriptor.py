"""Strict integrity parsing and atomic writes for managed ``PROJECT.md`` descriptors.

The descriptor carries route authority in a small flat frontmatter block. Parsing is
read-only and never repairs or rewrites a damaged descriptor. Rules:

- The first line must be ``---``; otherwise the frontmatter is missing.
- The block ends at the next ``---`` line; an unterminated block is malformed.
- Blank and ``#`` comment lines are ignored. Other unindented lines must be
  ``key: value``; duplicate keys are malformed.
- Indented lines are tolerated as nested content of a non-route field only.
- ``id``, ``project_type`` and ``target_root`` must be present and non-empty.
  ``project_type`` must be ``managed`` or ``referenced``; ``id`` must equal the
  managed directory name. Route values may use one matching pair of quotes.
- Lines split on ``\n`` only (a trailing ``\r`` is ignored); a UTF-8 BOM is preserved.
- An unreadable descriptor is reported as ``unreadable``; it is never treated as absent.

YAML lists, ``...`` terminators and duplicate keys are rejected rather than reinterpreted.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

DESCRIPTOR_NAME = "PROJECT.md"
ROUTE_FIELDS = ("id", "project_type", "target_root")
_PROJECT_TYPES = frozenset({"managed", "referenced"})
_QUOTES = "\"'"
_IS_WINDOWS = os.name == "nt"

IntegrityStatus = Literal[
    "intact", "missing_frontmatter", "malformed_frontmatter", "missing_fields", "unreadable"
]


@dataclass(frozen=True)
class DescriptorIntegrity:
    """Typed, read-only observation of one descriptor's route integrity."""

    path: str
    status: IntegrityStatus
    sha256: str | None
    fields: dict[str, str] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    detail: str | None = None
    text: str = field(default="", repr=False)

    @property
    def intact(self) -> bool:
        """Return whether every route field is present and well formed."""
        return self.status == "intact"


def parse_descriptor_text(
    text: str, *, path: str, sha256: str | None, expected_id: str | None = None,
) -> DescriptorIntegrity:
    """Classify descriptor text without consulting or changing the filesystem."""

    def result(status: IntegrityStatus, detail: str | None = None, **extra: Any) -> DescriptorIntegrity:
        return DescriptorIntegrity(path, status, sha256, detail=detail, text=text, **extra)

    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines[0].lstrip("\ufeff").strip() != "---":
        return result("missing_frontmatter", "first line is not the '---' frontmatter delimiter")
    values: dict[str, str] = {}
    previous_key: str | None = None
    terminated = False
    for number, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if stripped == "---":
            terminated = True
            break
        if not stripped or stripped.startswith("#"):
            continue
        if line[0] in " \t":
            if previous_key in ROUTE_FIELDS:
                return result(
                    "malformed_frontmatter",
                    f"line {number} nests content under route field {previous_key!r}",
                )
            continue
        key, separator, raw = line.partition(":")
        key = key.strip()
        if not separator or not key:
            return result("malformed_frontmatter", f"line {number} is not a flat 'key: value' field")
        if key in values:
            return result("malformed_frontmatter", f"line {number} duplicates field {key!r}")
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        if key in ROUTE_FIELDS and (value[:1] in _QUOTES or value[-1:] in _QUOTES) and value:
            return result("malformed_frontmatter", f"line {number} has ambiguous quoting for {key!r}")
        values[key] = value
        previous_key = key
    if not terminated:
        return result("malformed_frontmatter", "frontmatter is not terminated by a '---' line")
    missing = tuple(name for name in ROUTE_FIELDS if not values.get(name))
    if missing:
        return result(
            "missing_fields", "required route fields are missing or empty",
            fields=values, missing_fields=missing,
        )
    if values["project_type"] not in _PROJECT_TYPES:
        return result(
            "malformed_frontmatter",
            f"project_type {values['project_type']!r} is not 'managed' or 'referenced'",
            fields=values,
        )
    if expected_id is not None and values["id"] != expected_id:
        return result(
            "malformed_frontmatter",
            f"id {values['id']!r} does not match the managed project directory {expected_id!r}",
            fields=values,
        )
    return result("intact", fields=values)


def read_descriptor(project_root: str | Path) -> DescriptorIntegrity:
    """Read and classify ``PROJECT.md``; a present but unreadable file is ``unreadable``."""
    root = Path(project_root)
    path = root / DESCRIPTOR_NAME
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        return DescriptorIntegrity(
            str(path), "unreadable", None, detail=f"descriptor could not be read ({type(exc).__name__})",
        )
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return DescriptorIntegrity(
            str(path), "malformed_frontmatter", digest, detail="descriptor is not valid UTF-8",
        )
    return parse_descriptor_text(text, path=str(path), sha256=digest, expected_id=root.name)


def render_route_update(integrity: DescriptorIntegrity, updates: dict[str, str]) -> str:
    """Replace existing route-field lines in an intact descriptor, preserving all else."""
    if not integrity.intact or not set(updates) <= set(ROUTE_FIELDS):
        raise ValueError("route updates require an intact descriptor and route fields only")
    lines = integrity.text.split("\n")
    replaced: set[str] = set()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            break
        key = line.partition(":")[0].strip()
        if line[:1] not in " \t" and key in updates:
            ending = "\r" if line.endswith("\r") else ""
            lines[index] = f"{key}: {updates[key]}{ending}"
            replaced.add(key)
    if replaced != set(updates):
        raise ValueError("descriptor route fields changed while rendering the update")
    return "\n".join(lines)


def require_route_roundtrip(text: str, *, path: str, project_id: str, target_root: str) -> None:
    """Refuse before writing when rendered route fields would not parse back intact."""
    check = parse_descriptor_text(text, path=path, sha256=None, expected_id=project_id)
    if not check.intact or check.fields.get("target_root") != target_root:
        raise ValueError(
            f"target {target_root!r} cannot be represented in the PROJECT.md route frontmatter "
            f"({check.detail or 'target_root would not round-trip'}); nothing was written"
        )


def write_descriptor_atomic(
    path: str | Path, text: str, *, expected_sha256: str | None,
) -> str:
    """Write through a unique fsynced temporary file and ``os.replace``.

    ``expected_sha256=None`` requires the destination to be absent; otherwise its
    current bytes must hash to the expected value. The check is best-effort optimistic
    concurrency, not a lock. A new file honors the umask; a replaced file keeps its mode.
    Returns the SHA-256 of the written bytes.
    """
    destination = Path(path)
    data = text.encode("utf-8")
    mode: int | None = None
    try:
        current = destination.read_bytes()
        mode = destination.stat().st_mode & 0o777
    except FileNotFoundError:
        current = None
    actual = hashlib.sha256(current).hexdigest() if current is not None else None
    if actual != expected_sha256:
        raise FileExistsError(
            f"descriptor changed before write: expected {expected_sha256 or 'absent'}, "
            f"found {actual or 'absent'}"
        )
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    if not _IS_WINDOWS:
        # The new bytes are already in place; a directory fsync failure is not a write failure.
        with suppress(OSError):
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    return hashlib.sha256(data).hexdigest()


def validate_sha256(value: Any, name: str) -> str:
    """Return a lowercase 64-hex digest or raise a precise ValueError."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def descriptor_damaged_error(
    integrity: DescriptorIntegrity,
    *,
    project_id: str,
    artifact_root: str,
    requested_target: str | None = None,
) -> ValueError:
    """Build the fail-closed ``managed_descriptor_damaged`` error for one descriptor."""
    from odibi_anchor._recovery import attach_recovery

    return attach_recovery(
        ValueError(
            f"Managed project '{project_id}' descriptor is damaged "
            f"(managed_descriptor_damaged, {integrity.status}): {integrity.detail}. "
            "Anchor did not substitute the artifact root for the target and did not "
            "rewrite the descriptor. No supported descriptor repair operation exists in "
            "this version; stop and ask the project owner. Do not replace or hand-edit "
            "PROJECT.md."
        ),
        error_code="managed_descriptor_damaged",
        context={
            "project_id": project_id,
            "descriptor_path": integrity.path,
            "descriptor_sha256": integrity.sha256,
            "integrity_status": integrity.status,
            "missing_fields": list(integrity.missing_fields),
            "detail": integrity.detail,
            "artifact_root": artifact_root,
            "requested_target": requested_target,
            "owner_decision_required": True,
            "supported_repair_available": False,
        },
    )
