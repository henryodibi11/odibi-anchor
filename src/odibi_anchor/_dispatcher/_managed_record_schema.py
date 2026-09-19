"""Canonical frontmatter contracts for managed artifact directories.

Managed directories are plain filesystem directories, so nothing stops a direct
`open(path, "w")` from writing a record with the wrong frontmatter. Such a file
used to parse without complaint, persist through snapshot, and only fail later as
a raw `KeyError` when a managed action read it back — separating the error surface
from the cause by an arbitrary number of sessions (issue #15).

Record types are registered at one of two enforcement levels, because they do not
fail the same way:

  read_enforced — problems and work items. Their readers index frontmatter
  directly, so a missing field becomes a failure anyway; raising at parse puts
  that failure next to the bad file instead of sessions later.

  advisory — specs and decisions. Spec readers use `fm.get(...)` with defaults and
  tolerate a missing block entirely, and decisions have no managed reader or
  writer at all. Neither can produce the failure above, so they are reported at
  snapshot and never enforced at read; enforcing would newly reject files that
  work today.

Required field sets are derived from each type's own writer rather than restated,
so the contract cannot drift away from what the canonical path produces. The
decision entry is deliberately contentless: with no writer there is no canonical
shape, so only a missing frontmatter block is reported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

#: A record type whose contract is enforced when the record is read. A violation
#: raises, because the managed action cannot produce a correct result without it.
ENFORCEMENT_READ = "read_enforced"
#: A record type that is only reported at snapshot. Its readers tolerate missing
#: fields, so enforcing would newly reject files that work today.
ENFORCEMENT_ADVISORY = "advisory"


class ManagedRecordSchema(NamedTuple):
    """One managed record type's directory, filenames and frontmatter contract."""

    kind: str
    directory: str
    pattern: str
    required_fields: frozenset[str]
    identifier_field: str | None
    create_call: str | None
    enforcement: str = ENFORCEMENT_READ


def _problem_required_fields() -> frozenset[str]:
    from odibi_anchor._dispatcher._problem import _empty_record

    template = _empty_record("PRB-0000-0000", "project", "title", "full")
    return frozenset(template["meta"])


def _work_item_required_fields() -> frozenset[str]:
    # _work_item states its own required set for parsing; reuse it verbatim.
    from odibi_anchor._dispatcher import _work_item

    return frozenset(_work_item.REQUIRED_META)


def _spec_scaffold_fields() -> frozenset[str]:
    """Derive the spec frontmatter shape from the scaffold that writes it."""
    from odibi_anchor._dispatcher._spec import _SCAFFOLD_TEMPLATE

    meta = _read_frontmatter_text(_SCAFFOLD_TEMPLATE.format(title="T"))
    return frozenset(meta or ())


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
        # Advisory only. Spec readers use `fm.get(...)` with defaults and
        # `_parse_frontmatter` returns None rather than raising, so a spec with
        # missing fields degrades instead of failing. Enforcing at read would
        # newly reject hand-written and pre-scaffold specs that work today.
        "spec": ManagedRecordSchema(
            kind="spec",
            directory="specs",
            pattern="*_SPEC.md",
            required_fields=_spec_scaffold_fields(),
            identifier_field=None,
            create_call='anchor("spec", "create", name="FEATURE_NAME")',
            enforcement=ENFORCEMENT_ADVISORY,
        ),
        # Advisory, and deliberately contentless: decisions have no managed
        # reader or writer, so there is no canonical shape to compare against.
        # An empty required set means only a missing frontmatter block is
        # reported — inventing fields here would enforce a contract nothing
        # produces.
        "decision": ManagedRecordSchema(
            kind="decision",
            directory="decisions",
            pattern="*.md",
            required_fields=frozenset(),
            identifier_field=None,
            create_call=None,
            enforcement=ENFORCEMENT_ADVISORY,
        ),
    }


def read_enforced_schemas() -> dict[str, ManagedRecordSchema]:
    """Only the types whose contract a reader may enforce."""
    return {
        kind: schema
        for kind, schema in managed_record_schemas().items()
        if schema.enforcement == ENFORCEMENT_READ
    }


def missing_required_fields(meta: dict[str, Any], schema: ManagedRecordSchema) -> list[str]:
    """Return the canonical fields absent from this record's frontmatter."""
    return sorted(schema.required_fields - set(meta))


def repair_guidance(
    path: Path | str,
    schema: ManagedRecordSchema,
    missing: list[str],
    *,
    frontmatter_missing: bool = False,
) -> dict[str, Any]:
    """Describe one malformed record and the exact call that produces a valid one."""
    if frontmatter_missing:
        reason = f"{Path(path).name} has no readable {schema.kind} frontmatter block"
    else:
        reason = (
            f"{Path(path).name} is missing canonical {schema.kind} frontmatter: "
            f"{', '.join(missing)}"
        )
    if schema.create_call:
        repair = (
            f"Create the record through its managed action — {schema.create_call} — "
            "rather than writing the file directly, or add the missing frontmatter "
            "fields to match the canonical form."
        )
    else:
        # Decisions have no managed writer, so there is no call to point at.
        repair = (
            f"Add a frontmatter block to this {schema.kind} record. There is no "
            f"managed {schema.kind} action, so its shape is a project convention "
            "rather than an enforced contract."
        )
    return {
        "kind": "managed_record_schema_violation",
        "record_kind": schema.kind,
        "enforcement": schema.enforcement,
        "advisory": schema.enforcement == ENFORCEMENT_ADVISORY,
        "path": str(path),
        "missing_fields": list(missing),
        "required_fields": sorted(schema.required_fields),
        "reason": reason,
        "repair": repair,
    }


def _read_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse frontmatter keys leniently. None means the block is absent or unusable.

    Deliberately permissive: this exists to describe broken files, so it must not
    raise on the very input it is meant to report.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A record that is not even decodable is still a record to report.
        return None
    return _read_frontmatter_text(text)


def _read_frontmatter_text(text: str) -> dict[str, Any] | None:
    """Parse a frontmatter block out of already-loaded text."""
    import json

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
                    repair_guidance(
                        path, schema, sorted(schema.required_fields),
                        frontmatter_missing=True,
                    ),
                )
                continue
            missing = missing_required_fields(meta, schema)
            if missing:
                findings.append(repair_guidance(path, schema, missing))
    return findings
