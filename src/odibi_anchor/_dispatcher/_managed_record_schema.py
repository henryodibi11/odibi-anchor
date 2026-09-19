"""Canonical frontmatter contracts for managed artifact directories.

Managed directories are plain filesystem directories, so nothing stops a direct
`open(path, "w")` from writing a record with the wrong frontmatter. Such a file
used to parse without complaint, persist through snapshot, and only fail later as
a raw `KeyError` when a managed action read it back — separating the error surface
from the cause by an arbitrary number of sessions (issue #15).

Only record types with canonical frontmatter appear here. Specs are written from a
free-form scaffold and decisions have no managed action, so neither has a contract
to check; listing them would invent one.

Required field sets are derived from each type's own writer rather than restated,
so the contract cannot drift away from what the canonical path produces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple


class ManagedRecordSchema(NamedTuple):
    """One managed record type's directory, filenames and frontmatter contract."""

    kind: str
    directory: str
    pattern: str
    required_fields: frozenset[str]
    identifier_field: str
    create_call: str


def _problem_required_fields() -> frozenset[str]:
    from odibi_anchor._dispatcher._problem import _empty_record

    template = _empty_record("PRB-0000-0000", "project", "title", "full")
    return frozenset(template["meta"])


def _work_item_required_fields() -> frozenset[str]:
    # _work_item states its own required set for parsing; reuse it verbatim.
    from odibi_anchor._dispatcher import _work_item

    return frozenset(_work_item.REQUIRED_META)


def managed_record_schemas() -> dict[str, ManagedRecordSchema]:
    """Build the registry. Constructed on call so the writers stay the source."""
    return {
        "problem": ManagedRecordSchema(
            kind="problem",
            directory="problems",
            pattern="PRB-*.md",
            required_fields=_problem_required_fields(),
            identifier_field="problem_id",
            create_call='anchor("problem", "create", title="...", rigor_level=...)',
        ),
        "work_item": ManagedRecordSchema(
            kind="work_item",
            directory="work_items",
            pattern="WI-*.md",
            required_fields=_work_item_required_fields(),
            identifier_field="work_item_id",
            create_call='anchor("work_item", "create", title="...", outcome="...")',
        ),
    }


def missing_required_fields(meta: dict[str, Any], schema: ManagedRecordSchema) -> list[str]:
    """Return the canonical fields absent from this record's frontmatter."""
    return sorted(schema.required_fields - set(meta))


def repair_guidance(
    path: Path | str, schema: ManagedRecordSchema, missing: list[str],
) -> dict[str, Any]:
    """Describe one malformed record and the exact call that produces a valid one."""
    return {
        "kind": "managed_record_schema_violation",
        "record_kind": schema.kind,
        "path": str(path),
        "missing_fields": list(missing),
        "required_fields": sorted(schema.required_fields),
        "reason": (
            f"{Path(path).name} is missing canonical {schema.kind} frontmatter: "
            f"{', '.join(missing)}"
        ),
        "repair": (
            f"Create the record through its managed action — {schema.create_call} — "
            "rather than writing the file directly, or add the missing frontmatter "
            "fields to match the canonical form."
        ),
    }


def _read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse frontmatter keys leniently. None means the block is absent or unusable.

    Deliberately permissive: this exists to describe broken files, so it must not
    raise on the very input it is meant to report.
    """
    import json

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A record that is not even decodable is still a record to report.
        return None
    if not text.startswith("---\n"):
        return None
    _, _, rest = text.partition("---\n")
    frontmatter, separator, _ = rest.partition("\n---\n")
    if not separator:
        return None
    meta: dict[str, Any] = {}
    for line in frontmatter.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        try:
            meta[key.strip()] = json.loads(value.strip())
        except (ValueError, json.JSONDecodeError):
            meta[key.strip()] = value.strip()
    return meta


def scan_managed_records(artifact_root: Path | str) -> list[dict[str, Any]]:
    """Report every malformed managed record under an artifact root.

    Reporting only. Callers such as snapshot persist regardless: a preservation
    step that refuses malformed input would make the damaged file unsnapshottable
    and leave the operator unable to checkpoint the work it belongs to.
    """
    root = Path(artifact_root)
    findings: list[dict[str, Any]] = []
    for schema in managed_record_schemas().values():
        directory = root / schema.directory
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(schema.pattern)):
            meta = _read_frontmatter(path)
            if meta is None:
                findings.append(
                    repair_guidance(path, schema, sorted(schema.required_fields)),
                )
                continue
            missing = missing_required_fields(meta, schema)
            if missing:
                findings.append(repair_guidance(path, schema, missing))
    return findings
