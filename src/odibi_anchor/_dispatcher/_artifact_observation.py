"""Read-only saved-byte observations over the existing managed artifact registry.

These identities describe a project and saved bytes, not task authority, semantic
quality, authenticated host observations, or a material plan fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from odibi_anchor._dispatcher._descriptor import validate_sha256

OBSERVATION_VERSION = "anchor.artifact_observation/v1"
MAX_PAGE_CHARS = 32_000


def contained_path(root: Path, path: Path) -> Path:
    root, path = root.absolute(), path.absolute()
    if not path.is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("artifact path must be contained and must not traverse symlinks")
    if path.resolve() != path or root.resolve() != root:
        raise ValueError("artifact path must not traverse reparse points")
    return path


def observation(root: Path, path: Path, *, project_id: str | None = None) -> dict[str, Any]:
    """Observe exact bytes without newline translation or filesystem writes."""
    root = root.absolute()
    path = contained_path(root, path)
    data = path.read_bytes()
    relative = path.relative_to(root).as_posix()
    identity = {"project_id": project_id or root.name, "artifact_root": str(root), "relative_path": relative}
    return {
        "schema_version": OBSERVATION_VERSION,
        **identity,
        "artifact_id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        "artifact_path": str(path),
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "write_performed": False,
    }


def saved_content(
    root: Path, path: Path, *, project_id: str | None = None,
    offset: int = 0, limit: int = MAX_PAGE_CHARS, expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Read bounded UTF-8 character pages bound to the whole saved-byte digest."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_PAGE_CHARS:
        raise ValueError(f"offset must be nonnegative and limit must be 1..{MAX_PAGE_CHARS} characters")
    info = observation(root, path, project_id=project_id)
    if offset and expected_sha256 is None:
        raise ValueError("subsequent content pages require expected_sha256 from the first page")
    if expected_sha256 is not None and validate_sha256(expected_sha256, "expected_sha256") != info["content_sha256"]:
        raise ValueError("stale artifact content; read the first page again")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != info["content_sha256"]:
        raise ValueError("artifact changed during read; read again")
    text = data.decode("utf-8")
    if offset > len(text):
        raise ValueError("offset exceeds saved content length")
    end = min(offset + limit, len(text))
    return {
        **info, "content": text[offset:end], "encoding": "utf-8", "offset": offset,
        "limit": limit, "total_chars": len(text), "next_offset": end if end < len(text) else None,
        "complete": offset == 0 and end == len(text),
    }


def check_expected(info: dict[str, Any], *, expected_sha256: Any = None, expected_revision: Any = None,
                   revision: int | None = None) -> None:
    if expected_sha256 is not None and validate_sha256(expected_sha256, "expected_sha256") != info["content_sha256"]:
        raise ValueError("stale artifact: expected_sha256 does not match saved content; show and reconcile before retry")
    if expected_revision is not None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        if expected_revision != revision:
            raise ValueError("stale artifact: expected_revision does not match saved revision; show and reconcile before retry")


def project_artifacts(root: Path, command: str, *, project_id: str | None = None, **values: Any) -> dict[str, Any]:
    """List/read through project status's declared roots, never a second catalogue."""
    from odibi_anchor._dispatcher._managed_record_schema import managed_record_schemas
    from odibi_anchor._dispatcher._project import artifact_contract

    allowed = {"path", "offset", "limit", "expected_sha256"} if command == "read_artifact" else {"offset", "limit"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError("unknown artifact arguments: " + ", ".join(sorted(unknown)))
    roots = artifact_contract(artifact_root=str(root))["artifacts"]
    if command == "read_artifact":
        relative = values.pop("path", None)
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("path must be an exact project-relative artifact path returned by project artifacts")
        first = Path(relative).parts[0]
        entry = next((x for x in roots if x["root_name"] == first), None)
        if entry is None or entry["path_status"] != "available":
            raise ValueError("path is not an available declared managed artifact root")
        return {"kind": "saved_artifact", **saved_content(root, root / relative, project_id=project_id, **values)}
    offset, limit = values.get("offset", 0), values.get("limit", 100)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("artifact list offset must be nonnegative and limit must be 1..200")
    schemas = managed_record_schemas()
    files: list[dict[str, Any]] = []
    unavailable = []
    for entry in roots:
        if entry["path_status"] != "available":
            unavailable.append({"path": entry["path"], "reason": entry["path_reason"]})
            continue
        directory = root / entry["root_name"]
        candidates = [directory] if directory.is_file() else sorted(directory.rglob("*")) if directory.is_dir() else []
        for path in candidates:
            if path.is_dir() and not path.is_symlink():
                continue
            try:
                info = observation(root, path, project_id=project_id)
                info["record_kind"] = next((s.kind for s in schemas.values()
                                             if path.parent == root / s.directory and path.match(s.pattern)), None)
                files.append(info)
            except (OSError, ValueError) as exc:
                unavailable.append({"path": path.relative_to(root).as_posix(), "reason": str(exc)})
    files.sort(key=lambda x: x["relative_path"])
    return {
        "kind": "project_artifacts", "schema_version": OBSERVATION_VERSION,
        "project_id": project_id or root.name, "artifact_root": str(root),
        "artifacts": files[offset:offset + limit], "total": len(files), "offset": offset, "limit": limit,
        "next_offset": offset + limit if offset + limit < len(files) else None,
        "unavailable": unavailable, "write_performed": False,
        "limitations": ["Point-in-time inventory, not a filesystem snapshot or task authority.",
                         "read_artifact supports UTF-8 text; binary artifacts remain discoverable."],
    }
