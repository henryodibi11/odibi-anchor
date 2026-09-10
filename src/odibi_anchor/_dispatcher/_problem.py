"""Durable seven-stage Problem Records for substantial project work.

Markdown is canonical. Flat frontmatter provides index metadata, while stable
tables keep issues, hypotheses, work, evidence, revisions, and spec links readable
and deterministically parseable without a YAML dependency.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TABLES = {
    "issues": ["ID", "Parent", "Issue", "Status", "Priority", "Rationale"],
    "hypotheses": [
        "ID", "Issue", "Hypothesis", "Expected signal", "Status", "Critical", "Evidence"
    ],
    "workplan": ["Hypothesis", "Analysis", "Owner", "Status"],
    "evidence": [
        "ID", "Captured", "Source", "Observation", "Interpretation",
        "Hypotheses", "Confidence", "Contradicts",
    ],
    "revisions": ["Revision", "Time", "Change"],
    "linked_specs": ["Spec", "Recommendation revision"],
}

_HEADINGS = {
    "issues": "## 2. Build an issue tree",
    "priorities": "## 3. Prioritize branches",
    "hypotheses": "## 4. Create a hypothesis-driven workplan",
    "evidence": "## 5. Conduct analysis and capture evidence",
    "synthesis": "## 6. Synthesize findings",
    "recommendation": "## 7. Communicate the recommendation",
    "revisions": "## Decision and revision log",
    "linked_specs": "## Linked implementation specs",
}


def _now() -> str:
    """Return a UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _cell(value: Any) -> str:
    """Encode a value for one Markdown table cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        value = "yes" if value else "no"
    elif isinstance(value, (list, tuple, set)):
        value = ", ".join(str(item) for item in value)
    return html.escape(str(value), quote=False).replace("|", "&#124;").replace("\n", "<br>")


def _uncell(value: str) -> str:
    """Decode one Markdown table cell."""
    return html.unescape(value.strip().replace("<br>", "\n"))


def _table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    """Render a deterministic Markdown table."""
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def _parse_table(section: str, columns: list[str]) -> list[dict[str, str]]:
    """Parse a table rendered by :func:`_table`."""
    lines = [line for line in section.splitlines() if line.startswith("|")]
    if len(lines) < 2:
        return []
    rows: list[dict[str, str]] = []
    for line in lines[2:]:
        values = [_uncell(value) for value in line.strip().strip("|").split("|")]
        if len(values) == len(columns):
            rows.append(dict(zip(columns, values)))
    return rows


def normalize_problem_rigor(value: int | str) -> str:
    """Validate and canonicalize a durable Problem Record rigor value."""
    rigor = {1: "compact", 2: "full", "1": "compact", "2": "full",
             "compact": "compact", "full": "full"}.get(value)
    if rigor is None:
        raise ValueError("rigor_level must be compact/full or legacy 1/2")
    return rigor


def _empty_record(problem_id: str, project_id: str, title: str, rigor_level: int | str) -> dict[str, Any]:
    """Build a new in-memory Problem Record."""
    rigor = normalize_problem_rigor(rigor_level)
    now = _now()
    return {
        "meta": {
            "problem_id": problem_id,
            "project_id": project_id,
            "title": title.strip(),
            "status": "framing",
            "stage": 1,
            "rigor_level": rigor,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "next_action": "Complete the problem definition and decision needed.",
        },
        "definition": "",
        "decision_needed": "",
        "scope": "",
        "constraints": "",
        "success_measures": "",
        "issues": [],
        "priorities": "",
        "hypotheses": [],
        "workplan": [],
        "evidence": [],
        "synthesis": "",
        "conflicting_evidence": "",
        "uncertainty": "",
        "recommendation": "",
        "recommendation_evidence": "",
        "alternatives": "",
        "risks": "",
        "reversal_conditions": "",
        "revisions": [{"Revision": 1, "Time": now, "Change": "Problem Record created"}],
        "linked_specs": [],
    }


def _render(record: dict[str, Any]) -> str:
    """Render a complete canonical Problem Record."""
    meta = record["meta"]
    frontmatter = ["---"]
    for key in (
        "problem_id", "project_id", "title", "status", "stage", "rigor_level",
        "revision", "created_at", "updated_at", "next_action",
    ):
        frontmatter.append(f"{key}: {json.dumps(meta[key], ensure_ascii=False)}")
    heading_title = str(meta["title"]).replace("\r", " ").replace("\n", " ")
    frontmatter.extend(["---", "", f"# {meta['problem_id']}: {heading_title}", ""])
    sections = [
        "## 1. Define the problem",
        "",
        "### Problem definition",
        record["definition"] or "[Not yet captured]",
        "",
        "### Decision needed",
        record["decision_needed"] or "[Not yet captured]",
        "",
        "### Scope",
        record["scope"] or "[Not yet captured]",
        "",
        "### Constraints",
        record["constraints"] or "[Not yet captured]",
        "",
        "### Success measures",
        record["success_measures"] or "[Not yet captured]",
        "",
        _HEADINGS["issues"], "", _table(_TABLES["issues"], record["issues"]), "",
        _HEADINGS["priorities"], "", record["priorities"] or "[Not yet captured]", "",
        _HEADINGS["hypotheses"], "",
        "### Hypotheses", "", _table(_TABLES["hypotheses"], record["hypotheses"]), "",
        "### Workplan", "", _table(_TABLES["workplan"], record["workplan"]), "",
        _HEADINGS["evidence"], "", _table(_TABLES["evidence"], record["evidence"]), "",
        _HEADINGS["synthesis"], "",
        "### Current synthesis", record["synthesis"] or "[Not yet captured]", "",
        "### Conflicting evidence", record["conflicting_evidence"] or "[None captured]", "",
        "### Material uncertainty", record["uncertainty"] or "[Not yet captured]", "",
        _HEADINGS["recommendation"], "",
        "### Recommendation", record["recommendation"] or "[Not yet captured]", "",
        "### Supporting evidence IDs", record["recommendation_evidence"] or "[Not yet captured]", "",
        "### Alternatives", record["alternatives"] or "[Not yet captured]", "",
        "### Risks", record["risks"] or "[Not yet captured]", "",
        "### Reversal conditions", record["reversal_conditions"] or "[Not yet captured]", "",
        _HEADINGS["revisions"], "", _table(_TABLES["revisions"], record["revisions"]), "",
        _HEADINGS["linked_specs"], "", _table(_TABLES["linked_specs"], record["linked_specs"]), "",
    ]
    return "\n".join(frontmatter + sections)


def _section(text: str, heading: str, next_heading: str | None = None) -> str:
    """Extract content beneath one exact heading."""
    start_match = re.search(rf"(?m)^{re.escape(heading)}\s*$", text)
    if start_match is None:
        return ""
    start = start_match.end()
    if next_heading:
        end_match = re.search(rf"(?m)^{re.escape(next_heading)}\s*$", text[start:])
        end = start + end_match.start() if end_match else len(text)
    else:
        end = len(text)
    return text[start:end].strip()


def _prose(text: str, heading: str, next_heading: str | None) -> str:
    """Extract prose and convert template placeholders back to empty values."""
    value = _section(text, heading, next_heading)
    return "" if value.startswith("[Not yet captured]") or value.startswith("[None captured]") else value


def _parse(path: Path) -> dict[str, Any]:
    """Parse a canonical Problem Record from disk."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"Invalid Problem Record frontmatter: {path}")
    frontmatter, body = text[4:].split("\n---\n", 1)
    meta: dict[str, Any] = {}
    for line in frontmatter.splitlines():
        key, value = line.split(":", 1)
        try:
            meta[key] = json.loads(value.strip())
        except json.JSONDecodeError:
            meta[key] = value.strip()
    meta["rigor_level"] = normalize_problem_rigor(meta.get("rigor_level", "full"))

    issues_section = _section(body, _HEADINGS["issues"], _HEADINGS["priorities"])
    hypotheses_section = _section(body, _HEADINGS["hypotheses"], _HEADINGS["evidence"])
    evidence_section = _section(body, _HEADINGS["evidence"], _HEADINGS["synthesis"])
    revisions_section = _section(body, _HEADINGS["revisions"], _HEADINGS["linked_specs"])
    specs_section = _section(body, _HEADINGS["linked_specs"])
    has_recommendation_evidence = re.search(
        r"(?m)^### Supporting evidence IDs\s*$", body
    ) is not None
    return {
        "meta": meta,
        "definition": _prose(body, "### Problem definition", "### Decision needed"),
        "decision_needed": _prose(body, "### Decision needed", "### Scope"),
        "scope": _prose(body, "### Scope", "### Constraints"),
        "constraints": _prose(body, "### Constraints", "### Success measures"),
        "success_measures": _prose(body, "### Success measures", _HEADINGS["issues"]),
        "issues": _parse_table(issues_section, _TABLES["issues"]),
        "priorities": _prose(body, _HEADINGS["priorities"], _HEADINGS["hypotheses"]),
        "hypotheses": _parse_table(_section(hypotheses_section, "### Hypotheses", "### Workplan"), _TABLES["hypotheses"]),
        "workplan": _parse_table(_section(hypotheses_section, "### Workplan"), _TABLES["workplan"]),
        "evidence": _parse_table(evidence_section, _TABLES["evidence"]),
        "synthesis": _prose(body, "### Current synthesis", "### Conflicting evidence"),
        "conflicting_evidence": _prose(body, "### Conflicting evidence", "### Material uncertainty"),
        "uncertainty": _prose(body, "### Material uncertainty", _HEADINGS["recommendation"]),
        "recommendation": _prose(
            body,
            "### Recommendation",
            "### Supporting evidence IDs" if has_recommendation_evidence else "### Alternatives",
        ),
        "recommendation_evidence": (
            _prose(body, "### Supporting evidence IDs", "### Alternatives")
            if has_recommendation_evidence else ""
        ),
        "alternatives": _prose(body, "### Alternatives", "### Risks"),
        "risks": _prose(body, "### Risks", "### Reversal conditions"),
        "reversal_conditions": _prose(body, "### Reversal conditions", _HEADINGS["revisions"]),
        "revisions": _parse_table(revisions_section, _TABLES["revisions"]),
        "linked_specs": _parse_table(specs_section, _TABLES["linked_specs"]),
    }


def _problem_dir(artifact_root: str | Path) -> Path:
    """Return the contained Problem Record directory."""
    root = Path(artifact_root).resolve()
    directory = (root / "problems").resolve()
    if directory.parent != root:
        raise ValueError("problem directory escapes artifact root")
    return directory


def _problem_path(artifact_root: str | Path, problem_id: str) -> Path:
    """Resolve a safe canonical Problem Record path."""
    normalized = str(problem_id).strip().upper()
    if not re.fullmatch(r"PRB-\d{4}-\d{4}", normalized):
        raise ValueError("problem ID must match PRB-YYYY-NNNN")
    return _problem_dir(artifact_root) / f"{normalized}.md"


def _next_problem_id(directory: Path) -> str:
    """Allocate the next stable year-scoped Problem Record ID."""
    year = datetime.now(timezone.utc).year
    numbers = []
    for path in directory.glob(f"PRB-{year}-*.md"):
        match = re.fullmatch(rf"PRB-{year}-(\d{{4}})\.md", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return f"PRB-{year}-{max(numbers, default=0) + 1:04d}"


def _write(path: Path, record: dict[str, Any]) -> None:
    """Atomically persist a Problem Record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(_render(record), encoding="utf-8")
    temporary.replace(path)


def _record_revision(record: dict[str, Any], change: str) -> None:
    """Increment metadata and append one durable revision-log entry."""
    revision = int(record["meta"].get("revision", 1)) + 1
    updated_at = _now()
    record["meta"].update({"revision": revision, "updated_at": updated_at})
    record["revisions"].append({
        "Revision": revision,
        "Time": updated_at,
        "Change": change,
    })


def _next_entity_id(rows: list[dict[str, Any]], prefix: str) -> str:
    """Allocate the next top-level issue, hypothesis, or evidence ID."""
    numbers = []
    for row in rows:
        match = re.fullmatch(rf"{prefix}(\d+)", str(row.get("ID", "")))
        if match:
            numbers.append(int(match.group(1)))
    return f"{prefix}{max(numbers, default=0) + 1}"


def _upsert(rows: list[dict[str, Any]], columns: list[str], value: dict[str, Any], prefix: str) -> str:
    """Insert or update a stable-ID row and return its ID."""
    normalized = {column: value.get(column, value.get(column.lower().replace(" ", "_"), "")) for column in columns}
    if "Issue" in columns:
        normalized["Issue"] = value.get("issue_id", value.get("title", normalized["Issue"]))
    row_id = str(value.get("id") or value.get("ID") or _next_entity_id(rows, prefix)).upper()
    normalized["ID"] = row_id
    for index, existing in enumerate(rows):
        if existing.get("ID") == row_id:
            rows[index] = {**existing, **{key: val for key, val in normalized.items() if val != ""}}
            return row_id
    rows.append(normalized)
    return row_id


def _items(value: Any) -> list[dict[str, Any]]:
    """Normalize one structured row or a list of rows."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise ValueError("structured Problem Record updates must be a dict or list of dicts")


def _validate_recommendation_traceability(record: dict[str, Any]) -> list[str]:
    """Return the evidence IDs supporting a recommendation or raise."""
    if not record["recommendation"]:
        raise ValueError("a captured recommendation is required")
    if not record["recommendation_evidence"]:
        raise ValueError("recommendation requires supporting evidence IDs")
    if not record["uncertainty"]:
        raise ValueError("recommendation requires material uncertainty")
    evidence_ids = {item["ID"] for item in record["evidence"]}
    linked_ids = [
        item.strip()
        for item in re.split(r"[,;\s]+", record["recommendation_evidence"])
        if item.strip()
    ]
    if (
        not linked_ids
        or any(re.fullmatch(r"E\d+", item) is None for item in linked_ids)
        or not set(linked_ids).issubset(evidence_ids)
    ):
        raise ValueError("recommendation supporting evidence IDs must exist in the record")
    evidence_by_id = {item["ID"]: item for item in record["evidence"]}
    if any(
        evidence_by_id[item].get(field) is None
        or not str(evidence_by_id[item].get(field, "")).strip()
        for item in linked_ids
        for field in ("Source", "Captured")
    ):
        raise ValueError("recommendation evidence requires source and capture-time provenance")
    return list(dict.fromkeys(linked_ids))


def _apply_changes(record: dict[str, Any], changes: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply updates and return the exact evidence rows accepted this invocation."""
    stage = int(record["meta"].get("stage", 1))
    accepted_evidence: list[dict[str, Any]] = []
    if "rigor_level" in changes:
        record["meta"]["rigor_level"] = normalize_problem_rigor(changes["rigor_level"])
    for field in (
        "definition", "decision_needed", "scope", "constraints", "success_measures",
        "priorities", "synthesis", "conflicting_evidence", "uncertainty",
        "recommendation", "recommendation_evidence", "alternatives", "risks", "reversal_conditions",
    ):
        if field in changes:
            record[field] = "" if changes[field] is None else str(changes[field]).strip()
    if "recommendation" in changes and not record["recommendation"]:
        raise ValueError("recommendation must be non-blank when provided")

    for issue in _items(changes.get("issues")) + _items(changes.get("issue")):
        issue = {"status": "active", **issue}
        issue_id = _upsert(record["issues"], _TABLES["issues"], issue, "I")
        if not re.fullmatch(r"I\d+(?:\.\d+)*", issue_id):
            raise ValueError("issue ID must match I<n> or I<n>.<n>")
        stage = max(stage, 2)
    for hypothesis in _items(changes.get("hypotheses")) + _items(changes.get("hypothesis")):
        hypothesis = {"status": "active", "critical": False, **hypothesis}
        hypothesis_id = _upsert(record["hypotheses"], _TABLES["hypotheses"], hypothesis, "H")
        if not re.fullmatch(r"H\d+", hypothesis_id):
            raise ValueError("hypothesis ID must match H<n>")
        stage = max(stage, 4)
    for item in _items(changes.get("workplan")):
        record["workplan"].append({column: item.get(column, item.get(column.lower(), "")) for column in _TABLES["workplan"]})
        stage = max(stage, 4)
    for evidence in _items(changes.get("evidence")):
        source_value = evidence.get("source", evidence.get("Source", ""))
        source = "" if source_value is None else str(source_value).strip()
        if not source:
            raise ValueError("evidence requires source provenance")
        evidence = {"confidence": "medium", **evidence}
        evidence["Source"] = source
        captured_value = evidence.get("captured", evidence.get("Captured", ""))
        captured = "" if captured_value is None else str(captured_value).strip()
        evidence["Captured"] = captured or _now()
        evidence_id = _upsert(record["evidence"], _TABLES["evidence"], evidence, "E")
        if not re.fullmatch(r"E\d+", evidence_id):
            raise ValueError("evidence ID must match E<n>")
        linked_hypotheses = _cell(evidence.get("hypotheses", evidence.get("Hypotheses", "")))
        for hypothesis_id in (item.strip() for item in html.unescape(linked_hypotheses).split(",")):
            for hypothesis in record["hypotheses"]:
                if hypothesis.get("ID") == hypothesis_id:
                    existing = [item.strip() for item in str(hypothesis.get("Evidence", "")).split(",") if item.strip()]
                    if evidence_id not in existing:
                        existing.append(evidence_id)
                    hypothesis["Evidence"] = ", ".join(existing)
        stage = max(stage, 5)
        accepted_evidence.append(next(row.copy() for row in record["evidence"] if row["ID"] == evidence_id))

    if changes.get("priorities"):
        stage = max(stage, 3)
    if changes.get("synthesis"):
        stage = max(stage, 6)
    if record["recommendation"] and any(
        field in changes for field in ("recommendation", "recommendation_evidence", "uncertainty")
    ):
        _validate_recommendation_traceability(record)
    if changes.get("recommendation"):
        stage = max(stage, 7)
    if "stage" in changes:
        stage = int(changes["stage"])
        if not 1 <= stage <= 7:
            raise ValueError("stage must be between 1 and 7")
    record["meta"]["stage"] = stage
    for field in ("status", "next_action", "title"):
        if field in changes:
            record["meta"][field] = changes[field]

    for hypothesis in record["hypotheses"]:
        if str(hypothesis.get("Status", "")).lower() in {"supported", "rejected"}:
            linked = set(re.findall(r"E\d+", str(hypothesis.get("Evidence", ""))))
            evidence_ids = {item["ID"] for item in record["evidence"]}
            if not linked or not linked.issubset(evidence_ids):
                raise ValueError("supported or rejected hypotheses require linked evidence")
    return accepted_evidence


def _context(record: dict[str, Any], path: Path, *, kind: str = "problem_record_context") -> dict[str, Any]:
    """Build the narrow action return contract."""
    meta = record["meta"]
    unresolved = sum(
        1 for item in record["hypotheses"]
        if str(item.get("Critical", "")).lower() in {"yes", "true"}
        and str(item.get("Status", "active")).lower() not in {"supported", "rejected", "deferred", "inconclusive"}
    )
    return {
        "kind": kind,
        "problem_id": meta["problem_id"],
        "project_id": meta["project_id"],
        "title": meta["title"],
        "status": meta["status"],
        "stage": meta["stage"],
        "artifact_path": str(path),
        "issue_count": len(record["issues"]),
        "hypothesis_count": len(record["hypotheses"]),
        "evidence_count": len(record["evidence"]),
        "unresolved_critical_count": unresolved,
        "linked_specs": [item.get("Spec", "") for item in record["linked_specs"]],
        "suggested_next_actions": [meta.get("next_action") or "Review and update the Problem Record."],
    }


def _resume(record: dict[str, Any], path: Path) -> dict[str, Any]:
    """Build a bounded continuation projection from a full Problem Record."""
    context = _context(record, path, kind="problem_resume_context")
    context.update({
        "problem_summary": (record["definition"] or record["meta"]["title"])[:500],
        "active_issues": [row for row in record["issues"] if row.get("Status", "active") == "active"][:5],
        "priority_hypotheses": [row for row in record["hypotheses"] if row.get("Status", "active") == "active"][:5],
        "material_evidence": record["evidence"][-5:],
        "conflicting_evidence": record["conflicting_evidence"][:500],
        "current_synthesis": record["synthesis"][:1000],
        "next_action": record["meta"].get("next_action", ""),
    })
    return context


def _evidence_entries(record: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert accepted durable rows to typed session-ledger payloads."""
    return [{
        "id": row["ID"], "kind": "problem_record", "status": "unknown",
        "source": str(row.get("Source") or "anchor:problem"),
        "observed_at": str(row.get("Captured") or _now()),
        "provenance": {"problem_id": record["meta"]["problem_id"],
                       "observation": row.get("Observation", "")},
    } for row in rows]


def _render_context(context: dict[str, Any]) -> str:
    """Render a compact human-facing action result."""
    lines = [f"# {context['problem_id']}: {context.get('title', '')}", ""]
    lines.extend([
        f"**Status:** {context['status']}",
        f"**Stage:** {context['stage']}/7",
        f"**Artifact:** `{context['artifact_path']}`",
        "",
    ])
    if context["kind"] == "problem_resume_context":
        lines.extend([
            "## Current synthesis", context.get("current_synthesis") or "[Not yet captured]", "",
            "## Next action", context.get("next_action") or "Review the record.",
        ])
    else:
        lines.append(
            f"Issues: {context['issue_count']} | Hypotheses: {context['hypothesis_count']} | "
            f"Evidence: {context['evidence_count']}"
        )
    return "\n".join(lines)


def render_problem_result(selector: str, result: dict[str, Any]) -> str:
    """Transport-neutral renderer for a dict-first problem dispatch result."""
    if selector in {"list", "status", ""}:
        lines = ["# Problem Records", ""]
        lines.extend(
            f"- **{item['problem_id']}** — {item['status']} — stage {item['stage']}/7 — {item['title']}"
            for item in result.get("problems", [])
        )
        if not result.get("problems"):
            lines.append("No Problem Records.")
        return "\n".join(lines)
    if result.get("error"):
        return str(result["error"])
    return _render_context(result)


def problem_action(
    artifact_root: str | Path,
    *args: Any,
    problem_id: str | None = None,
    project_id: str | None = None,
    output_format: str = "markdown",
    **changes: Any,
) -> dict[str, Any] | str:
    """Create, list, show, update, resume, link, or close Problem Records."""
    if output_format not in {"dict", "markdown"}:
        raise ValueError("output_format must be 'dict' or 'markdown'")
    directory = _problem_dir(artifact_root)
    command = str(args[0]).lower().strip() if args else "list"
    command_id = str(args[1]) if len(args) > 1 else problem_id

    if command in {"list", "status", ""}:
        records = []
        if directory.is_dir():
            for path in sorted(directory.glob("PRB-????-????.md")):
                record = _parse(path)
                records.append(_context(record, path))
        result: dict[str, Any] = {
            "kind": "problem_list_context",
            "project_id": project_id,
            "problems": records,
            "count": len(records),
            "suggested_next_actions": ["Create a record with anchor('problem', 'create', title='...')."] if not records else [],
        }
        if output_format == "dict":
            return result
        return render_problem_result(command, result)

    if command == "create":
        title = str(changes.pop("title", "")).strip()
        if not title:
            raise ValueError("create requires title=...")
        directory.mkdir(parents=True, exist_ok=True)
        new_id = _next_problem_id(directory)
        path = _problem_path(artifact_root, new_id)
        record = _empty_record(new_id, project_id or Path(artifact_root).resolve().name, title, changes.pop("rigor_level", "full"))
        accepted = _apply_changes(record, changes)
        _write(path, record)
        context = _context(record, path)
        context["created"] = True
        context["write_performed"] = True
        context["artifact_path"] = str(path)
        if accepted:
            context["accepted_evidence"] = _evidence_entries(record, accepted)
    else:
        if not command_id:
            raise ValueError(f"{command} requires a problem ID")
        path = _problem_path(artifact_root, command_id)
        if not path.is_file():
            raise FileNotFoundError(f"Problem Record does not exist: {command_id}")
        record = _parse(path)
        if command == "show":
            context = _context(record, path)
            context["record"] = record
        elif command == "resume":
            context = _resume(record, path)
        elif command == "update":
            accepted = _apply_changes(record, changes)
            _record_revision(record, str(changes.get("change_note") or "Problem Record updated"))
            _write(path, record)
            context = _context(record, path)
            context["updated"] = True
            context["write_performed"] = True
            context["artifact_path"] = str(path)
            if accepted:
                context["accepted_evidence"] = _evidence_entries(record, accepted)
        elif command == "link_spec":
            spec = str(changes.get("spec", "")).strip()
            if not spec:
                raise ValueError("link_spec requires spec=...")
            _validate_recommendation_traceability(record)
            wrote = not any(item.get("Spec") == spec for item in record["linked_specs"])
            if wrote:
                record["linked_specs"].append({
                    "Spec": spec,
                    "Recommendation revision": changes.get("recommendation_revision", record["meta"].get("revision", 1)),
                })
                _record_revision(record, f"Linked implementation spec {spec}")
                _write(path, record)
            context = _context(record, path)
            context["linked"] = spec
            context["write_performed"] = wrote
            if wrote:
                context["artifact_path"] = str(path)
        elif command == "close":
            outcome = str(changes.get("outcome", "closed")).lower()
            if outcome not in {"closed", "inconclusive"}:
                raise ValueError("close outcome must be 'closed' or 'inconclusive'")
            unresolved_critical = [
                item["ID"] for item in record["hypotheses"]
                if str(item.get("Critical", "")).lower() in {"yes", "true"}
                and str(item.get("Status", "active")).lower()
                not in {"supported", "rejected", "deferred", "inconclusive"}
            ]
            if unresolved_critical:
                raise ValueError(
                    "critical hypotheses must be resolved, deferred, or inconclusive before close: "
                    + ", ".join(unresolved_critical)
                )
            record["meta"].update({
                "status": outcome,
                "next_action": changes.get("next_action", "No further action."),
            })
            _record_revision(record, f"Problem closed as {outcome}")
            _write(path, record)
            context = _context(record, path)
            context["closed"] = True
            context["write_performed"] = True
            context["artifact_path"] = str(path)
        else:
            raise ValueError("Unknown problem sub-command. Valid: list, create, show, update, resume, link_spec, close")

    context.setdefault("write_performed", False)
    if output_format == "dict":
        return context
    return render_problem_result(command, context)
