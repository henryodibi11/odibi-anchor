"""Provider-neutral, local-only managed work-item artifacts.

The Markdown file is the source of truth.  This module deliberately knows nothing
about provider clients: an approval authorizes a caller to perform a bounded write,
and ``record_publish`` only attests to identifiers returned by that caller.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

_OPERATIONS = frozenset({"create_tasks", "update_tasks", "add_comment"})
_IMPLEMENTATION_DISPOSITIONS = frozenset(
    {"implemented", "deferred", "rejected", "superseded", "evaluation_only", "unknown"}
)
_TRIGGER_STATUSES = frozenset({"unmet", "met", "retired"})
_TRIGGER_FIELDS = frozenset({"id", "condition", "evidence_required", "minimum_count", "status"})
_MAX_TRIGGER_TEXT = 4096
_MATERIAL = (
    "title",
    "outcome",
    "context",
    "scope",
    "non_goals",
    "acceptance_criteria",
    "implementation_notes",
    "validation_notes",
    "risks",
    "dependencies",
    "problem_record",
    "specification",
    "pr_url",
    "publication_plan",
    "implementation_disposition",
    "reopening_triggers",
)
_TEXT_FIELDS = frozenset(_MATERIAL) - {
    "acceptance_criteria",
    "publication_plan",
    "implementation_disposition",
    "reopening_triggers",
}
# The canonical frontmatter contract, named once so the managed-record schema
# registry can derive from it rather than restating it and drifting.
REQUIRED_META = frozenset({
    "work_item_id",
    "status",
    "created_at",
    "updated_at",
    "revision",
    "approvals",
    "provider_bindings",
    "publication_receipts",
    "revisions",
})
# Planning metadata lives outside _MATERIAL so adding it never changes an existing
# fingerprint; files without these keys read as empty lists.
OPTIONAL_META = frozenset(
    {"implementation_disposition", "reopening_triggers", "depends_on", "reviews", "plan_approvals"}
)
_CLOSED_STATUSES = frozenset({"completed", "cancelled"})
_REVIEW_VERDICTS = frozenset({"approve", "changes_requested", "reject"})
_FINDING_STATUSES = frozenset({"open", "resolved"})
_MAX_FINDINGS = 64
# rigor -> (approving reviews with distinct angles, adversarial angle required)
_PLAN_RIGOR = {"direct": (1, False), "compact": (2, False), "full": (4, True)}
_PENDING_APPROVAL_STATUSES = frozenset({"pending", "approval_required", "expired"})

_SECTION_NAMES = (
    "Outcome",
    "Context",
    "Scope",
    "Non-goals",
    "Acceptance criteria",
    "Implementation notes",
    "Validation notes",
    "Risks",
    "Dependencies",
    "Problem Record",
    "Specification",
    "PR",
    "Publication plan",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _directory(artifact_root: str | Path) -> Path:
    root = Path(artifact_root).resolve()
    candidate = root / "work_items"
    if candidate.is_symlink() or (candidate.exists() and candidate.resolve(strict=True) != candidate.absolute()):
        raise ValueError("work_items directory must not be a symlink or reparse point")
    directory = candidate.resolve()
    if directory.parent != root:
        raise ValueError("work_items directory escapes artifact root")
    if directory.exists() and not directory.is_dir():
        raise ValueError("work_items path must be a directory")
    return directory


def _path(artifact_root: str | Path, work_item_id: str) -> Path:
    value = str(work_item_id).strip().upper()
    if re.fullmatch(r"WI-\d{4}-\d{4}", value) is None:
        raise ValueError("work item ID must match WI-YYYY-NNNN")
    return _directory(artifact_root) / f"{value}.md"


def _next_id(directory: Path) -> str:
    year = datetime.now(timezone.utc).year
    found = []
    for path in directory.glob(f"WI-{year}-????.md"):
        match = re.fullmatch(rf"WI-{year}-(\d{{4}})\.md", path.name)
        if match:
            found.append(int(match.group(1)))
    sequence = max(found, default=0) + 1
    if sequence > 9999:
        raise ValueError(f"work item sequence exhausted for {year}")
    return f"WI-{year}-{sequence:04d}"


def _string(value: Any, name: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} must be nonempty")
    return value


def _one_line(value: Any, name: str, *, required: bool = False) -> str:
    result = _string(value, name, required=required)
    if "\n" in result or "\r" in result:
        raise ValueError(f"{name} must be one line")
    return result


def _material_text(value: Any, name: str, *, required: bool = False) -> str:
    result = (
        _one_line(value, name, required=required)
        if name == "title"
        else _string(
            value,
            name,
            required=required,
        )
    )
    if any(re.search(rf"(?m)^## {re.escape(heading)}\s*$", result) for heading in _SECTION_NAMES):
        raise ValueError(f"{name} contains a reserved work-item heading")
    return result


def _provider(value: Any) -> str:
    result = _one_line(value, "provider", required=True).lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", result) is None:
        raise ValueError("provider must be a canonical identifier")
    return result


def _provider_url(value: Any) -> str:
    result = _one_line(value, "provider_url", required=True)
    parsed = urlsplit(result)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("provider_url must be an absolute HTTPS URL")
    return result


def _reject_unknown(values: dict[str, Any], allowed: set[str], command: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown {command} fields: " + ", ".join(sorted(unknown)))


def _operations(value: Any, *, required: bool = True) -> list[str]:
    if not isinstance(value, (list, tuple, set)) or isinstance(value, (str, bytes)):
        raise ValueError("operations must be a list of allowed publication operations")
    result = sorted(set(value)) if all(isinstance(item, str) for item in value) else []
    invalid = set(result) - _OPERATIONS
    if invalid or (required and not result):
        allowed = ", ".join(sorted(_OPERATIONS))
        raise ValueError(f"operations must contain only: {allowed}")
    return result


def _normalize_material(record: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "acceptance_criteria": [],
        "publication_plan": [],
        "implementation_disposition": "unknown",
        "reopening_triggers": [],
    }
    return {key: record.get(key, defaults.get(key, "")) for key in _MATERIAL}


def _fingerprint(record: dict[str, Any]) -> str:
    payload = json.dumps(_normalize_material(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render(record: dict[str, Any]) -> str:
    meta = {
        key: record[key]
        for key in (
            "work_item_id",
            "status",
            "created_at",
            "updated_at",
            "revision",
            "implementation_disposition",
            "reopening_triggers",
            "depends_on",
            "approvals",
            "provider_bindings",
            "publication_receipts",
            "revisions",
            "reviews",
            "plan_approvals",
        )
    }
    lines = (
        ["---"]
        + [f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}" for key, value in meta.items()]
        + ["---", ""]
    )
    lines += [f"# {record['work_item_id']}: {record['title']}", ""]
    headings = (
        (_SECTION_NAMES[0], "outcome"),
        (_SECTION_NAMES[1], "context"),
        (_SECTION_NAMES[2], "scope"),
        ("Non-goals", "non_goals"),
        ("Acceptance criteria", "acceptance_criteria"),
        ("Implementation notes", "implementation_notes"),
        ("Validation notes", "validation_notes"),
        ("Risks", "risks"),
        ("Dependencies", "dependencies"),
        ("Problem Record", "problem_record"),
        ("Specification", "specification"),
        ("PR", "pr_url"),
        ("Publication plan", "publication_plan"),
    )
    for heading, key in headings:
        value = record[key]
        body = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else value
        lines += [f"## {heading}", "", body or "[Not captured]", ""]
    return "\n".join(lines)


def _parse(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"Invalid work-item frontmatter: {path}")
    front, body = text[4:].split("\n---\n", 1)
    record: dict[str, Any] = {}
    try:
        for line in front.splitlines():
            key, value = line.split(":", 1)
            if key in record:
                raise ValueError(f"duplicate work-item frontmatter key: {key}")
            record[key] = json.loads(value.strip())
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid work-item frontmatter: {path}") from exc
    required_meta = set(REQUIRED_META)
    optional_meta = set(OPTIONAL_META)
    if not required_meta.issubset(record):
        missing = sorted(required_meta - set(record))
        raise ValueError(
            f"Missing work-item frontmatter: {missing}. "
            f"{path.name} was not produced by the managed action; create records with "
            'anchor("work_item", "create", title="...", outcome="...") rather than '
            "writing the file directly, or add the missing fields."
        )
    if not set(record).issubset(required_meta | optional_meta):
        raise ValueError(f"Unknown work-item frontmatter: {sorted(set(record) - required_meta - optional_meta)}")
    record["implementation_disposition"] = _implementation_disposition(
        record.get("implementation_disposition", "unknown")
    )
    record["reopening_triggers"] = _reopening_triggers(record.get("reopening_triggers", []))
    if record["status"] not in {"draft", "published", "changes_pending", "completed", "cancelled"}:
        raise ValueError("Invalid work-item status")
    if type(record["revision"]) is not int or record["revision"] < 1:
        raise ValueError("Invalid work-item revision")
    if any(
        not isinstance(record[key], list)
        for key in (
            "approvals",
            "provider_bindings",
            "publication_receipts",
            "revisions",
        )
    ):
        raise ValueError("Invalid work-item ledger metadata")
    record["depends_on"] = _depends_on(record.get("depends_on", []), str(record["work_item_id"]))
    for key in ("reviews", "plan_approvals"):
        record[key] = record.get(key, [])
        if not isinstance(record[key], list) or not all(isinstance(entry, dict) for entry in record[key]):
            raise ValueError("Invalid work-item ledger metadata")
    title_match = re.search(r"(?m)^# (WI-\d{4}-\d{4}): (.*)$", body)
    if title_match is None:
        raise ValueError(f"Invalid work-item body: {path}")
    if title_match.group(1) != record["work_item_id"]:
        raise ValueError("work item body ID does not match frontmatter")
    record["title"] = title_match.group(2)
    headings = [
        ("Outcome", "outcome"),
        ("Context", "context"),
        ("Scope", "scope"),
        ("Non-goals", "non_goals"),
        ("Acceptance criteria", "acceptance_criteria"),
        ("Implementation notes", "implementation_notes"),
        ("Validation notes", "validation_notes"),
        ("Risks", "risks"),
        ("Dependencies", "dependencies"),
        ("Problem Record", "problem_record"),
        ("Specification", "specification"),
        ("PR", "pr_url"),
        ("Publication plan", "publication_plan"),
    ]
    for index, (heading, key) in enumerate(headings):
        if len(re.findall(rf"(?m)^## {re.escape(heading)}\s*$", body)) != 1:
            raise ValueError(f"Work item must contain exactly one {heading} section")
        next_heading = headings[index + 1][0] if index + 1 < len(headings) else None
        pattern = (
            rf"(?ms)^## {re.escape(heading)}\s*\n\n(.*?)(?=\n## {re.escape(next_heading)}\s*$|\Z)"
            if next_heading
            else rf"(?ms)^## {re.escape(heading)}\s*\n\n(.*)\Z"
        )
        match = re.search(pattern, body)
        value = match.group(1).strip() if match else ""
        if value == "[Not captured]":
            value = ""
        if key in {"acceptance_criteria", "publication_plan"}:
            try:
                value = json.loads(value) if value else []
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid {key} section: {path}") from exc
        record[key] = value
    if _path(path.parent.parent, record.get("work_item_id", "")) != path.resolve():
        raise ValueError("work item ID does not match artifact path")
    return record


def _write(path: Path, record: dict[str, Any], *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _directory(path.parent.parent)
    if path.is_symlink() or (path.exists() and path.resolve(strict=True) != path.absolute()):
        raise ValueError("work item destination must not be a symlink or reparse point")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
        ) as handle:
            temporary = handle.name
            handle.write(_render(record))
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            try:
                os.link(temporary, path)
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EPERM):
                    raise
                # Filesystem lacks or prohibits hard links (e.g. Databricks workspace FS).
                # Fall back to check-then-rename — safe under Anchor single-writer.
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(
                        f"work item already exists: {path}"
                    ) from exc
                os.rename(temporary, path)
            else:
                os.unlink(temporary)
            temporary = None
        else:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _plan_approval_current(approval: dict[str, Any], item: dict[str, Any], fingerprint: str) -> bool:
    # A plan approval binds the item's material fingerprint and its dependencies.
    return approval.get("fingerprint") == fingerprint and approval.get("depends_on") == item["depends_on"]


def _plan_approval_state(item: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    current = [a for a in item["plan_approvals"] if _plan_approval_current(a, item, fingerprint)]
    return {
        "approved": bool(current),
        "approval_id": current[-1].get("approval_id") if current else None,
        "stale_approval_ids": [
            a.get("approval_id") for a in item["plan_approvals"] if not _plan_approval_current(a, item, fingerprint)
        ],
    }


def _context(item: dict[str, Any], path: Path, **extra: Any) -> dict[str, Any]:
    fingerprint = _fingerprint(item)
    result = {
        "kind": "work_item_context",
        "work_item_id": item["work_item_id"],
        "title": item["title"],
        "status": item["status"],
        "implementation_disposition": item["implementation_disposition"],
        "reopening_triggers": deepcopy(item["reopening_triggers"]),
        "fingerprint": fingerprint,
        "depends_on": list(item["depends_on"]),
        "reviews": [
            {**deepcopy(review), "stale": review.get("fingerprint") != fingerprint} for review in item["reviews"]
        ],
        "plan_approvals": [
            {**deepcopy(approval), "stale": not _plan_approval_current(approval, item, fingerprint)}
            for approval in item["plan_approvals"]
        ],
        "plan_approval": _plan_approval_state(item, fingerprint),
        "artifact_path": str(path),
        "write_performed": False,
    }
    result.update(extra)
    return result


def render_work_item_result(selector: str, result: dict[str, Any]) -> str:
    """Deferred, transport-neutral renderer for dict-first dispatchers."""
    if selector in {"", "list", "status"}:
        items = result.get("work_items", [])
        return "\n".join(
            ["# Work items", ""]
            + ([
                f"- **{x['work_item_id']}** — {x['status']} / "
                f"{x['implementation_disposition']} — {x['title']}"
                for x in items
            ] or ["No work items."])
        )
    if selector == "next":
        ready = [
            f"- **{x['work_item_id']}** \u2014 {x['title']} "
            f"(plan {'approved' if x['plan_approval']['approved'] else 'not approved'})"
            for x in result.get("ready", [])
        ]
        blocked = [
            f"- **{x['work_item_id']}** \u2014 waiting on "
            + ", ".join(x["unmet_dependencies"] + [f"{d} (unknown)" for d in x["unknown_dependencies"]])
            for x in result.get("blocked", [])
        ]
        return "\n".join(
            ["# Next work items", ""] + (ready or ["No ready work items."]) + ["", "## Blocked", ""]
            + (blocked or ["None."])
        )
    if selector == "request_plan_approval":
        lines = [
            f"# Plan approval: {result.get('status', '')}",
            "",
            f"**Plan SHA-256:** `{result.get('plan_sha256', '')}`",
        ]
        lines += [f"- {x['work_item_id']}: `{x['approval_id']}`" for x in result.get("plan_approvals", [])]
        lines += [f"- {x['purpose']}: `{x['operation']}`" for x in result.get("next_operations", [])]
        return "\n".join(lines)
    return "\n".join(
        [
            f"# {result['work_item_id']}: {result.get('title', '')}",
            "",
            f"**Status:** {result.get('status', '')}",
            f"**Implementation disposition:** {result.get('implementation_disposition', '')}",
            f"**Fingerprint:** `{result.get('fingerprint', '')}`",
            f"**Artifact:** `{result.get('artifact_path', '')}`",
        ]
    )


def work_item_action(
    artifact_root: str | Path,
    *args: Any,
    work_item_id: str | None = None,
    output_format: str = "markdown",
    renderer: Callable[[str, dict[str, Any]], str] | None = None,
    **changes: Any,
) -> dict[str, Any] | str:
    """Execute a local managed work-item lifecycle command."""
    if output_format not in {"dict", "markdown"}:
        raise ValueError("output_format must be 'dict' or 'markdown'")
    command = str(args[0]).strip().lower() if args else "list"
    item_id = str(args[1]) if len(args) > 1 else work_item_id
    directory = _directory(artifact_root)
    if command in {"", "list", "status"}:
        items: list[dict[str, Any]] = []
        malformed: list[dict[str, Any]] = []
        if directory.is_dir():
            for path in sorted(directory.glob("WI-????-????.md")):
                try:
                    record = _parse(path)
                except ValueError as exc:
                    malformed.append(
                        getattr(exc, "context", None)
                        or {"path": str(path), "reason": str(exc)}
                    )
                    continue
                items.append(_context(record, path))
        next_actions = []
        if malformed:
            next_actions.append(
                f"Repair {len(malformed)} malformed record(s): see malformed_records "
                "for the missing fields and the managed call that creates a valid one."
            )
        result: dict[str, Any] = {
            "kind": "work_item_list_context",
            "work_items": items,
            "count": len(items),
            "malformed_records": malformed,
            "malformed_count": len(malformed),
            "suggested_next_actions": next_actions,
            "write_performed": False,
        }
    elif command == "next":
        if changes or item_id:
            raise ValueError("next does not accept arguments")
        result = _next(directory)
    elif command == "request_plan_approval":
        if item_id:
            raise ValueError("request_plan_approval takes work_items=[...], not a work item ID")
        result = _request_plan_approval(artifact_root, changes)
    elif command == "create":
        title = _material_text(changes.pop("title", ""), "title", required=True)
        outcome = _material_text(changes.pop("outcome", ""), "outcome", required=True)
        depends_on = _depends_on(changes.pop("depends_on", []))
        unknown = set(changes) - (set(_MATERIAL) - {"title", "outcome"})
        if unknown:
            raise ValueError("unknown work-item fields: " + ", ".join(sorted(unknown)))
        directory.mkdir(parents=True, exist_ok=True)
        while True:
            new_id, now = _next_id(directory), _now()
            record: dict[str, Any] = {key: "" for key in _MATERIAL}
            record.update(
                {
                    "work_item_id": new_id,
                    "title": title,
                    "outcome": outcome,
                    "status": "draft",
                    "created_at": now,
                    "updated_at": now,
                    "revision": 1,
                    "implementation_disposition": "unknown",
                    "reopening_triggers": [],
                    "depends_on": _depends_on(depends_on, new_id),
                    "approvals": [],
                    "provider_bindings": [],
                    "publication_receipts": [],
                    "revisions": [{"revision": 1, "time": now, "change": "Work item created"}],
                    "reviews": [],
                    "plan_approvals": [],
                }
            )
            record["acceptance_criteria"], record["publication_plan"] = [], []
            _apply_material(record, changes)
            path = _path(artifact_root, new_id)
            try:
                _write(path, record, create_only=True)
                break
            except FileExistsError:
                continue
        result = _context(record, path, created=True, write_performed=True)
    else:
        if not item_id:
            raise ValueError(f"{command} requires a work item ID")
        path = _path(artifact_root, item_id)
        if not path.is_file():
            raise FileNotFoundError(f"Work item does not exist: {item_id}")
        record = _parse(path)
        if command == "show":
            if changes:
                raise ValueError("show does not accept changes")
            result = _context(record, path, record=record)
        elif command == "update":
            if record["status"] in _CLOSED_STATUSES:
                raise ValueError(f"{record['status']} work items cannot be updated")
            candidate = deepcopy(record)
            if "depends_on" in changes:
                candidate["depends_on"] = _depends_on(changes.pop("depends_on"), record["work_item_id"])
            _apply_material(candidate, changes)
            material_changed = _normalize_material(candidate) != _normalize_material(record)
            if not material_changed and candidate["depends_on"] == record["depends_on"]:
                result = _context(record, path, updated=False)
            else:
                if material_changed and candidate["provider_bindings"]:
                    candidate["status"] = "changes_pending"
                _revise(candidate, str(changes.get("change_note", "Work item updated")))
                _write(path, candidate)
                result = _context(candidate, path, updated=True, write_performed=True)
        elif command == "preview":
            _reject_unknown(changes, {"provider", "operations"}, "preview")
            provider = _provider(changes.get("provider"))
            operations = _operations(changes.get("operations"))
            result = _context(
                record,
                path,
                provider=provider,
                operations=operations,
                preview={"provider": provider, "operations": operations, "fingerprint": _fingerprint(record)},
            )
        elif command == "approve":
            _reject_unknown(
                changes,
                {"provider", "operations", "expected_fingerprint", "approver", "source"},
                "approval",
            )
            provider = _provider(changes.get("provider"))
            if record["status"] in {"completed", "cancelled"}:
                raise ValueError("closed work items cannot be approved")
            operations = _operations(changes.get("operations"))
            expected = _string(changes.get("expected_fingerprint"), "expected_fingerprint", required=True)
            if expected != _fingerprint(record):
                raise ValueError("expected fingerprint does not match current draft")
            approver = _one_line(changes.get("approver"), "approver", required=True)
            source = _one_line(changes.get("source"), "source", required=True)
            approval = {
                "approval_id": "WIA-" + uuid4().hex,
                "provider": provider,
                "operations": operations,
                "fingerprint": expected,
                "approver": approver,
                "source": source,
                "approved_at": _now(),
                "consumed_at": None,
            }
            candidate = deepcopy(record)
            candidate["approvals"].append(approval)
            _revise(candidate, f"Publication approved for {provider}")
            _write(path, candidate)
            result = _context(
                candidate, path, approval=approval, approval_id=approval["approval_id"], write_performed=True
            )
        elif command == "review":
            result = _review(record, path, changes)
        elif command == "record_publish":
            result = _record_publish(record, path, changes)
        elif command == "close":
            _reject_unknown(changes, {"outcome", "implementation_disposition", "reopening_triggers"}, "close")
            outcome = _string(changes.get("outcome"), "outcome", required=True).lower()
            if outcome not in {"completed", "cancelled"}:
                raise ValueError("close outcome must be completed or cancelled")
            candidate = deepcopy(record)
            if "implementation_disposition" in changes:
                candidate["implementation_disposition"] = _implementation_disposition(
                    changes["implementation_disposition"]
                )
            if "reopening_triggers" in changes:
                candidate["reopening_triggers"] = _reopening_triggers(changes["reopening_triggers"])
            if candidate["implementation_disposition"] == "unknown":
                raise ValueError("close requires a non-unknown implementation_disposition")
            candidate["status"] = outcome
            _revise(candidate, f"Work item closed as {outcome}")
            _write(path, candidate)
            result = _context(candidate, path, closed=True, write_performed=True)
        else:
            raise ValueError(
                "Unknown work_item sub-command. Valid: list, next, create, show, update, review, "
                "request_plan_approval, preview, approve, record_publish, close"
            )
    if output_format == "dict":
        return result
    return (renderer or render_work_item_result)(command, result)


def _apply_material(record: dict[str, Any], changes: dict[str, Any]) -> None:
    allowed = set(_MATERIAL) | {"change_note"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError("unknown work-item fields: " + ", ".join(sorted(unknown)))
    for key, value in changes.items():
        if key == "change_note":
            continue
        if key in _TEXT_FIELDS:
            record[key] = _material_text(value, key)
        elif key == "acceptance_criteria":
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise ValueError("acceptance_criteria must be a list of nonempty strings")
            record[key] = [x.strip() for x in value]
        elif key == "publication_plan":
            if not isinstance(value, (list, dict)):
                raise ValueError("publication_plan must be a list or dict")
            record[key] = deepcopy(value)
        elif key == "implementation_disposition":
            record[key] = _implementation_disposition(value)
        elif key == "reopening_triggers":
            record[key] = _reopening_triggers(value)


def _implementation_disposition(value: Any) -> str:
    result = _one_line(value, "implementation_disposition", required=True)
    if result not in _IMPLEMENTATION_DISPOSITIONS:
        raise ValueError("invalid implementation_disposition")
    return result


def _bounded_trigger_text(value: Any, name: str) -> str:
    result = _string(value, name, required=True)
    if len(result) > _MAX_TRIGGER_TEXT:
        raise ValueError(f"{name} must contain at most {_MAX_TRIGGER_TEXT} characters")
    return result


def _reopening_triggers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("reopening_triggers must be a list")
    if len(value) > 16:
        raise ValueError("reopening_triggers must contain at most 16 triggers")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, trigger in enumerate(value):
        name = f"reopening_triggers[{index}]"
        if not isinstance(trigger, dict) or set(trigger) != _TRIGGER_FIELDS:
            raise ValueError(f"{name} must contain exactly: " + ", ".join(sorted(_TRIGGER_FIELDS)))
        trigger_id = _one_line(trigger["id"], f"{name}.id", required=True)
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", trigger_id) is None:
            raise ValueError(f"{name}.id has invalid format")
        if trigger_id in ids:
            raise ValueError("reopening trigger IDs must be unique")
        ids.add(trigger_id)
        evidence = trigger["evidence_required"]
        if not isinstance(evidence, list) or len(evidence) > 16:
            raise ValueError(f"{name}.evidence_required must be a list of at most 16 strings")
        evidence_result = [
            _bounded_trigger_text(item, f"{name}.evidence_required[{evidence_index}]")
            for evidence_index, item in enumerate(evidence)
        ]
        minimum_count = trigger["minimum_count"]
        if type(minimum_count) is not int or not 1 <= minimum_count <= 1000:
            raise ValueError(f"{name}.minimum_count must be an integer from 1 through 1000")
        status = _one_line(trigger["status"], f"{name}.status", required=True)
        if status not in _TRIGGER_STATUSES:
            raise ValueError(f"{name}.status must be unmet, met, or retired")
        result.append(
            {
                "id": trigger_id,
                "condition": _bounded_trigger_text(trigger["condition"], f"{name}.condition"),
                "evidence_required": evidence_result,
                "minimum_count": minimum_count,
                "status": status,
            }
        )
    return result


def _revise(record: dict[str, Any], change: str) -> None:
    record["revision"] = int(record["revision"]) + 1
    record["updated_at"] = _now()
    record["revisions"].append({"revision": record["revision"], "time": record["updated_at"], "change": change})


def _record_publish(record: dict[str, Any], path: Path, values: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(
        values,
        {"approval_id", "provider", "provider_id", "provider_url", "performed_operations", "recorded_by"},
        "publication receipt",
    )
    approval_id = _one_line(values.get("approval_id"), "approval_id", required=True)
    provider_id = _one_line(values.get("provider_id"), "provider_id", required=True)
    provider_url = _provider_url(values.get("provider_url"))
    recorded_by = _one_line(values.get("recorded_by"), "recorded_by", required=True)
    operations = _operations(values.get("performed_operations"))
    if record["status"] in {"completed", "cancelled"}:
        raise ValueError("closed work items cannot record publication")
    approvals = [a for a in record["approvals"] if a.get("approval_id") == approval_id]
    if len(approvals) != 1:
        raise ValueError("approval ID does not exist")
    approval = approvals[0]
    provider = _provider(values.get("provider", approval["provider"]))
    if provider != approval["provider"]:
        raise ValueError("provider does not match approval")
    if approval.get("consumed_at"):
        raise ValueError("approval has already been consumed")
    if approval["fingerprint"] != _fingerprint(record):
        raise ValueError("approval fingerprint is stale")
    if not set(operations).issubset(approval["operations"]):
        raise ValueError("performed operations exceed approval scope")
    observed = _now()
    candidate = deepcopy(record)
    target = next(a for a in candidate["approvals"] if a["approval_id"] == approval_id)
    target["consumed_at"] = observed
    candidate["status"] = "published"
    binding = {"provider": provider, "provider_id": provider_id, "provider_url": provider_url}
    candidate["provider_bindings"] = [b for b in candidate["provider_bindings"] if b.get("provider") != provider] + [
        binding
    ]
    receipt = {
        **binding,
        "approval_id": approval_id,
        "performed_operations": operations,
        "fingerprint": approval["fingerprint"],
        "recorded_by": recorded_by,
        "recorded_at": observed,
        "approver": approval["approver"],
        "approval_source": approval["source"],
    }
    candidate["publication_receipts"].append(receipt)
    _revise(candidate, f"Publication recorded for {provider}")
    _write(path, candidate)
    attestation = {
        "id": f"work-item:{candidate['work_item_id']}:{provider}:{provider_id}",
        "kind": "work-item",
        "status": "pass",
        "source": provider_url,
        "observed_at": observed,
        "provenance": {
            "work_item_id": candidate["work_item_id"],
            **binding,
            "approval_id": approval_id,
            "approver": approval["approver"],
            "attestor": approval["approver"],
            "approval_source": approval["source"],
            "fingerprint": approval["fingerprint"],
            "operations": operations,
            "recorded_by": recorded_by,
        },
    }
    return _context(
        candidate,
        path,
        receipt=receipt,
        binding=binding,
        attestation=attestation,
        accepted_attestations=[attestation],
        write_performed=True,
    )


def _depends_on(value: Any, own_id: str | None = None) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("depends_on must be a list of work item IDs")
    result: list[str] = []
    for entry in value:
        item = entry.strip().upper() if isinstance(entry, str) else ""
        if re.fullmatch(r"WI-\d{4}-\d{4}", item) is None:
            raise ValueError("depends_on entries must match WI-YYYY-NNNN")
        if own_id is not None and item == own_id.strip().upper():
            raise ValueError("a work item cannot depend on itself")
        if item in result:
            raise ValueError("depends_on entries must be unique")
        result.append(item)
    return sorted(result)


def _findings(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > _MAX_FINDINGS:
        raise ValueError(f"findings must be a list of at most {_MAX_FINDINGS} findings")
    result = []
    for index, finding in enumerate(value):
        name = f"findings[{index}]"
        if not isinstance(finding, dict) or set(finding) != {"summary", "status"}:
            raise ValueError(f"{name} must contain exactly: status, summary")
        status = _one_line(finding["status"], f"{name}.status", required=True)
        if status not in _FINDING_STATUSES:
            raise ValueError(f"{name}.status must be open or resolved")
        result.append({"summary": _bounded_trigger_text(finding["summary"], f"{name}.summary"), "status": status})
    return result


def _review(record: dict[str, Any], path: Path, values: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(values, {"angle", "verdict", "findings", "reviewer_ref", "expected_fingerprint"}, "review")
    if record["status"] in _CLOSED_STATUSES:
        raise ValueError("closed work items cannot be reviewed")
    angle = _one_line(values.get("angle"), "angle", required=True)
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", angle) is None or len(angle) > 64:
        raise ValueError("angle must be a canonical lowercase kebab-case identifier")
    verdict = _one_line(values.get("verdict"), "verdict", required=True)
    if verdict not in _REVIEW_VERDICTS:
        raise ValueError("verdict must be approve, changes_requested, or reject")
    findings = _findings(values.get("findings", []))
    reviewer_ref = _one_line(values.get("reviewer_ref"), "reviewer_ref", required=True)
    expected = _string(values.get("expected_fingerprint"), "expected_fingerprint", required=True)
    if expected != _fingerprint(record):
        raise ValueError("expected fingerprint does not match the current work item")
    review = {
        "review_id": "WIR-" + uuid4().hex,
        "angle": angle,
        "verdict": verdict,
        "findings": findings,
        "reviewer_ref": reviewer_ref,
        "fingerprint": expected,
        "recorded_at": _now(),
    }
    candidate = deepcopy(record)
    candidate["reviews"].append(review)
    _revise(candidate, f"Review recorded: {angle} {verdict}")
    _write(path, candidate)
    return _context(candidate, path, review=review, review_id=review["review_id"], write_performed=True)


def _next(directory: Path) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    malformed: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("WI-????-????.md")):
            try:
                records[path.stem] = _parse(path)
            except ValueError as exc:
                malformed.append({"path": str(path), "reason": str(exc)})
    ready, blocked = [], []
    for work_item_id, record in sorted(records.items()):
        if record["status"] in _CLOSED_STATUSES:
            continue
        fingerprint = _fingerprint(record)
        entry = {
            "work_item_id": work_item_id,
            "title": record["title"],
            "status": record["status"],
            "fingerprint": fingerprint,
            "depends_on": list(record["depends_on"]),
            "plan_approval": _plan_approval_state(record, fingerprint),
        }
        unmet = [d for d in record["depends_on"] if d in records and records[d]["status"] != "completed"]
        unknown = [d for d in record["depends_on"] if d not in records]
        if unmet or unknown:
            blocked.append({**entry, "unmet_dependencies": unmet, "unknown_dependencies": unknown})
        else:
            ready.append(entry)
    return {
        "kind": "work_item_next_context",
        "ready": ready,
        "blocked": blocked,
        "malformed_records": malformed,
        "write_performed": False,
    }


def _plan_readiness_problems(
    artifact_root: str | Path, items: list[tuple[str, dict[str, Any] | None]], rigor: str
) -> list[str]:
    problems: list[str] = []
    position = {work_item_id: index for index, (work_item_id, _) in enumerate(items)}
    required, adversarial = _PLAN_RIGOR[rigor]
    for work_item_id, record in items:
        if record is None:
            problems.append(f"{work_item_id}: work item does not exist")
            continue
        if record["status"] in _CLOSED_STATUSES:
            problems.append(f"{work_item_id}: status is {record['status']}; only open work items can be approved")
        for dependency in record["depends_on"]:
            if dependency in position:
                if position[dependency] > position[work_item_id]:
                    problems.append(f"{work_item_id}: dependency {dependency} must come earlier in work_items")
                continue
            dependency_path = _path(artifact_root, dependency)
            try:
                status = _parse(dependency_path)["status"] if dependency_path.is_file() else None
            except ValueError as exc:
                problems.append(f"{work_item_id}: dependency {dependency} is malformed: {exc}")
                continue
            if status is None:
                problems.append(f"{work_item_id}: dependency {dependency} does not exist")
            elif status != "completed":
                problems.append(
                    f"{work_item_id}: dependency {dependency} is {status}; include it in work_items or complete it"
                )
        fingerprint = _fingerprint(record)
        current = [review for review in record["reviews"] if review.get("fingerprint") == fingerprint]
        for index, review in enumerate(current):
            label = f"review {review.get('review_id')} ({review.get('angle')})"
            for finding in review.get("findings", []):
                if finding.get("status") == "open":
                    problems.append(f"{work_item_id}: {label} has an open finding: {finding.get('summary')}")
            superseded = any(
                later.get("angle") == review.get("angle") and later.get("verdict") == "approve"
                for later in current[index + 1:]
            )
            if review.get("verdict") != "approve" and not superseded:
                problems.append(
                    f"{work_item_id}: {label} verdict is {review.get('verdict')} "
                    "with no later approving review from the same angle"
                )
        angles = {review.get("angle") for review in current if review.get("verdict") == "approve"}
        if len(angles) < required:
            stale = len(record["reviews"]) - len(current)
            problems.append(
                f"{work_item_id}: {rigor} rigor needs {required} approving current review(s) with distinct "
                f"angles; found {len(angles)}" + (f" ({', '.join(sorted(angles))})" if angles else "")
                + (f"; {stale} stale review(s) ignored" if stale else "")
            )
        if adversarial and "adversarial" not in angles:
            problems.append(f"{work_item_id}: {rigor} rigor needs an approving current adversarial review")
    return problems


def _request_plan_approval(artifact_root: str | Path, values: dict[str, Any]) -> dict[str, Any]:
    passthrough = ("provider", "in_session_approval", "wait_seconds", "timeout_minutes")
    _reject_unknown(values, {"work_items", "spec", "rigor", *passthrough}, "plan approval")
    rigor = _one_line(values.get("rigor"), "rigor", required=True)
    if rigor not in _PLAN_RIGOR:
        raise ValueError("rigor must be direct, compact, or full")
    requested = values.get("work_items")
    if not isinstance(requested, list) or not requested or not all(isinstance(x, str) for x in requested):
        raise ValueError("work_items must be a nonempty list of work item IDs")
    paths = [_path(artifact_root, value) for value in requested]
    ids = [path.stem for path in paths]
    if len(set(ids)) != len(ids):
        raise ValueError("work_items must not repeat a work item")
    items = [(work_item_id, _parse(path) if path.is_file() else None) for work_item_id, path in zip(ids, paths, strict=True)]
    problems = _plan_readiness_problems(artifact_root, items, rigor)
    spec: dict[str, str] | None = None
    if values.get("spec") is not None:
        from odibi_anchor._dispatcher._spec import spec_path

        spec_file = spec_path(Path(artifact_root).resolve() / "specs", _one_line(values["spec"], "spec", required=True))
        if spec_file.is_file():
            spec = {"name": spec_file.name[: -len("_SPEC.md")], "sha256": hashlib.sha256(spec_file.read_bytes()).hexdigest()}
        else:
            problems.append(f"spec {spec_file.name} does not exist")
    if problems:
        raise ValueError("work items are not ready for plan approval:\n- " + "\n- ".join(problems))
    subject = {
        "kind": "anchor.work_item_plan",
        "rigor": rigor,
        "spec": spec,
        "work_items": [
            {"work_item_id": work_item_id, "fingerprint": _fingerprint(record), "depends_on": list(record["depends_on"])}
            for work_item_id, record in items
            if record is not None
        ],
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    from odibi_anchor.owner_approval import request_owner_approval

    prompt = (
        f"Approve the plan of {len(ids)} work item(s) ({', '.join(ids)}) at {rigor} rigor"
        + (f" with spec {spec['name']}" if spec else "") + "."
    )
    outcome = request_owner_approval(
        operation="work_item.plan_approval",
        subject=subject,
        prompt=prompt,
        **{key: values[key] for key in passthrough if values.get(key) is not None},
    )
    status = outcome.get("status")
    result: dict[str, Any] = {
        "kind": "work_item_plan_approval",
        "status": status,
        "plan_sha256": plan_sha256,
        "subject": subject,
        "owner_approval": outcome,
        "plan_approvals": [],
        "next_operations": [],
        "write_performed": False,
    }
    if status in _PENDING_APPROVAL_STATUSES:
        call = (
            f'anchor("work_item", "request_plan_approval", work_items={json.dumps(ids)}, rigor="{rigor}"'
            + (f', spec="{spec["name"]}"' if spec else "")
        )
        reply = outcome.get("approval_response") or f"APPROVE {outcome.get('challenge_sha256', '')}"
        result["next_operations"] = [
            {"purpose": "check_again", "operation": call + ")"},
            {
                "purpose": "complete_in_session",
                "operation": call + f', provider="chat_in_session", in_session_approval={json.dumps(reply)})',
                "note": "only after the owner typed exactly this reply in the session",
            },
        ]
        return result
    if status == "declined":
        return result
    if status != "approved":
        raise ValueError(f"unexpected owner approval status: {status!r}")
    current = [(work_item_id, path, _parse(path)) for work_item_id, path in zip(ids, paths, strict=True)]
    for (work_item_id, _, record), bound in zip(current, subject["work_items"], strict=True):
        if _fingerprint(record) != bound["fingerprint"] or record["depends_on"] != bound["depends_on"]:
            raise ValueError(f"{work_item_id} changed while approval was pending; request plan approval again")
    approval_id = "WIP-" + uuid4().hex
    approved_at = _now()
    for work_item_id, path, record in current:
        approval = {
            "approval_id": approval_id,
            "plan_sha256": plan_sha256,
            "fingerprint": _fingerprint(record),
            "depends_on": list(record["depends_on"]),
            "rigor": rigor,
            "owner": outcome.get("owner"),
            "assurance": outcome.get("assurance"),
            "authority_ref": outcome.get("authority_ref"),
            "approved_at": approved_at,
        }
        candidate = deepcopy(record)
        candidate["plan_approvals"].append(approval)
        _revise(candidate, f"Plan approved: {approval_id}")
        _write(path, candidate)
        result["plan_approvals"].append({"work_item_id": work_item_id, **approval})
    result["write_performed"] = True
    result["managed_writes"] = [{"path": str(path), "kind": "work_item"} for _, path, _ in current]
    return result
