"""Strict integrity parsing and atomic writes for managed ``PROJECT.md`` descriptors.

The descriptor carries route authority in a small flat frontmatter block. Parsing is
read-only and never repairs or rewrites a damaged descriptor. Rules:

- The first line must be ``---``; otherwise the frontmatter is missing.
- The block ends at the next ``---`` line; an unterminated block is malformed.
- Blank and ``#`` comment lines are ignored. Other unindented lines must be
  ``key: value``; duplicate keys are malformed.
- Indented lines and unindented ``- item`` list lines are tolerated as nested content
  of a non-route field only.
- ``target_root`` must be present and non-empty. ``id`` and ``project_type`` are optional
  as in 0.3.23: a missing ``id`` defaults to the directory name and a missing
  ``project_type`` is derived from target versus artifact root; both are reported in
  ``defaulted_fields``. Present values must be valid.
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
_NO_REPAIR_SENTENCE = (
    "The supported repair needs portfolio authority for the target (`anchor portfolio "
    "repair-descriptor`), which this call did not have; stop and ask the project owner. "
    "Do not replace or hand-edit PROJECT.md."
)

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
    defaulted_fields: tuple[str, ...] = ()
    text: str = field(default="", repr=False)

    @property
    def intact(self) -> bool:
        """Return whether every route field is present and well formed."""
        return self.status == "intact"


def parse_descriptor_text(
    text: str, *, path: str, sha256: str | None, expected_id: str | None = None,
) -> DescriptorIntegrity:
    """Classify descriptor text without changing the filesystem.

    Deriving a missing ``project_type`` may read path metadata (symlink resolution).
    """

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
        # Indented lines and unindented YAML list items ("- value") continue the previous
        # non-route field; 0.3.23 ignored both, so they must keep parsing.
        if line[0] in " \t" or stripped == "-" or stripped.startswith("- "):
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
    # target_root is mandatory: its absence caused the #29 artifact-root fallback. id and
    # project_type never change routing; 0.3.23 defaulted them, so descriptors without them
    # keep booting with explicit, reported defaults.
    missing = tuple(
        name for name in ROUTE_FIELDS
        if (name == "target_root" or name in values) and not values.get(name)
    )
    if missing:
        return result(
            "missing_fields", "required route fields are missing or empty",
            fields=values, missing_fields=missing,
        )
    defaulted: list[str] = []
    artifact_root = Path(path).parent
    if "id" not in values:
        values["id"] = expected_id if expected_id is not None else artifact_root.name
        defaulted.append("id")
    if "project_type" not in values:
        configured = Path(values["target_root"])
        target = configured if configured.is_absolute() else artifact_root / configured
        same = _same_location(target, artifact_root)
        values["project_type"] = "managed" if same else "referenced"
        defaulted.append("project_type")
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
    return result("intact", fields=values, defaulted_fields=tuple(defaulted))


def _same_location(target: Path, artifact_root: Path) -> bool:
    """Compare locations for project_type derivation without ever raising.

    Resolution can fail (for example a symlink loop raises RuntimeError on Python 3.11 and
    3.12); fall back to a lexical comparison of absolute, normalized paths. Routing never reads
    project_type, so the fallback only affects the reported label.
    """
    try:
        return os.path.normcase(str(target.resolve(strict=False))) == os.path.normcase(
            str(artifact_root.resolve(strict=False))
        )
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.normpath(os.path.abspath(target))) == os.path.normcase(
            os.path.normpath(os.path.abspath(artifact_root))
        )


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
    absent = [key for key in updates if key not in replaced]
    if any(key not in integrity.defaulted_fields for key in absent):
        raise ValueError("descriptor route fields changed while rendering the update")
    if absent:
        # A defaulted field has no line yet: insert it before the closing delimiter.
        closing = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
        # Follow the frontmatter's line-ending style, not only the closing line's: a closing
        # delimiter without a trailing newline carries no ending of its own.
        crlf = any(line.endswith("\r") for line in lines[: closing + 1])
        ending = "\r" if crlf else ""
        lines[closing:closing] = [f"{key}: {updates[key]}{ending}" for key in absent]
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


def descriptor_route_claims(text: str) -> dict[str, list[str]]:
    """Leniently read flat route-field values from a possibly damaged frontmatter block.

    Used only to refuse a repair that would contradict what the damaged descriptor still
    claims; it never supplies a value to write.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines[0].lstrip("\ufeff").strip() != "---":
        return {}
    claims: dict[str, list[str]] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[0] in " \t":
            continue
        key, separator, raw = line.partition(":")
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        if separator and key.strip() in ROUTE_FIELDS and value:
            claims.setdefault(key.strip(), []).append(value)
    return claims


def render_descriptor_repair(text: str, route: dict[str, str]) -> tuple[str, str, str]:
    """Render repaired route frontmatter; return ``(text, mode, preserved_body)``.

    ``route_fields_reconstructed`` rewrites only route lines of a terminated frontmatter
    block, keeping every other frontmatter line and the body after the closing delimiter
    byte for byte. Otherwise, or when that result would still not parse intact,
    ``frontmatter_prepended`` places new route frontmatter above the whole old content.
    The caller must still verify the result with ``parse_descriptor_text``.
    """
    if set(route) != set(ROUTE_FIELDS):
        raise ValueError("descriptor repair requires every route field")
    lines = text.split("\n")
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None,
    )
    if lines[0].lstrip("\ufeff").strip() == "---" and closing is not None:
        rendered: list[str] = [lines[0]]
        written: set[str] = set()
        under_route = False
        for line in lines[1:closing]:
            stripped = line.strip()
            nested = line[:1] in " \t" or stripped == "-" or stripped.startswith("- ")
            if not stripped or stripped.startswith("#") or nested:
                if not (nested and stripped and under_route):
                    rendered.append(line)
                continue
            key = line.partition(":")[0].strip()
            under_route = key in ROUTE_FIELDS
            if under_route and key not in written:
                ending = "\r" if line.endswith("\r") else ""
                rendered.append(f"{key}: {route[key]}{ending}")
                written.add(key)
            elif not under_route:
                rendered.append(line)
        rendered.extend(f"{key}: {route[key]}" for key in ROUTE_FIELDS if key not in written)
        candidate = "\n".join(rendered + lines[closing:])
        check = parse_descriptor_text(candidate, path="", sha256=None, expected_id=route["id"])
        if check.intact and all(check.fields[key] == route[key] for key in ROUTE_FIELDS):
            return candidate, "route_fields_reconstructed", "\n".join(lines[closing + 1:])
    header = "---\n" + "".join(f"{key}: {route[key]}\n" for key in ROUTE_FIELDS) + "---\n"
    return header + text, "frontmatter_prepended", text


def repair_operations(
    *, config_path: str, host_id: str, project_id: str, expected_sha256: str, approve: bool,
) -> list[dict[str, Any]]:
    """Copy-ready portfolio-authorized repair operations (CLI first, then dispatcher)."""
    import shlex

    from odibi_anchor._recovery import dispatcher_operation

    arguments = {
        "config_path": config_path, "host_id": host_id, "project_id": project_id,
        "expected_sha256": expected_sha256, "approve": approve,
    }
    reason = (
        "write the owner-approved repair: backup, receipt and post-write verification"
        if approve else
        "dry run: show the exact repaired PROJECT.md for owner review; nothing is written"
    )
    command = [
        "anchor", "portfolio", "repair-descriptor", "--config", config_path, "--host", host_id,
        "--project", project_id, "--expected-sha256", expected_sha256,
    ] + (["--approve"] if approve else [])
    return [
        {
            "operation": "repair_descriptor",
            "arguments": arguments,
            "copy_ready": shlex.join(command),
            "python": "odibi_anchor.startup.repair_portfolio_descriptor(**arguments)",
            "reason": reason,
            "requires_owner": True,
            "retry_safety": "refuses unless identity, portfolio target, ownership and the expected sha256 all match",
        },
        dispatcher_operation(
            "project", "repair-descriptor", project_id,
            kwargs={key: arguments[key] for key in ("config_path", "host_id", "expected_sha256", "approve")},
            reason=reason, requires_owner=True,
            retry_safety="refuses unless identity, portfolio target, ownership and the expected sha256 all match",
        ),
    ]


def attach_repair_recovery(exc: ValueError, *, config_path: str, host_id: str) -> ValueError:
    """Name the supported repair on a ``managed_descriptor_damaged`` error with portfolio authority.

    Detection is unchanged; an unreadable descriptor has no hash to bind, so it keeps
    ``supported_repair_available=False``.
    """
    from odibi_anchor._recovery import attach_recovery

    context = dict(exc.context)  # type: ignore[attr-defined]
    context.update(config_path=config_path, host_id=host_id)
    if context.get("descriptor_sha256") is None:
        return attach_recovery(exc, error_code="managed_descriptor_damaged", context=context)
    operations = repair_operations(
        config_path=config_path, host_id=host_id, project_id=context["project_id"],
        expected_sha256=context["descriptor_sha256"], approve=False,
    )
    context["supported_repair_available"] = True
    exc.args = (
        str(exc.args[0]).split(_NO_REPAIR_SENTENCE)[0]
        + "Stop and ask the project owner to review and approve the supported, "
        f"portfolio-authorized repair (dry run first): {operations[0]['copy_ready']}. "
        "Do not replace or hand-edit PROJECT.md.",
    )
    return attach_recovery(
        exc, error_code="managed_descriptor_damaged", context=context, next_operations=operations,
    )


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
            f"rewrite the descriptor. {_NO_REPAIR_SENTENCE}"
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
