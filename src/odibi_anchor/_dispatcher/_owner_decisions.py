"""Pending owner decisions: open observations and candidate memories.

Read-only and bounded. ``gate`` and ``learning assess`` results carry the summary
so the agent can show it in its report; the owner decides and the agent records
the decision through the listed operation. A read failure never fails the caller:
it returns a degraded marker instead.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from odibi_anchor._recovery import dispatcher_operation

_LIMIT = 10
_OWNER_FIELDS = ["actor_ref", "decision_source", "rationale"]
_OWNER_PLACEHOLDERS = {
    "actor_ref": "<owner_ref>", "decision_source": "<decision_source>", "rationale": "<owner rationale>",
}
_INSTRUCTION = (
    "Show these in your report. The owner decides; record the decision with the matching "
    "operation, replacing the <...> owner fields with the owner's answer. Learning triage writes "
    "a record, so run it in a task that may write artifacts (for example mode=\"retrospective\")."
)


def pending_owner_decisions(memory_db: str | Path, *, project: str | None, limit: int = _LIMIT) -> dict[str, Any]:
    """Return pending owner decisions for ``project``, or a degraded marker on any read error."""
    try:
        return _read_pending(Path(memory_db), project, limit)
    except Exception as exc:  # must never fail gate or assess
        return {
            "kind": "pending_owner_decisions", "status": "degraded", "project": project,
            "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def _read_pending(path: Path, project: str | None, limit: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "pending_owner_decisions", "status": "ready", "project": project,
        "observations": {"total": 0, "items": []},
        "candidate_memories": {"total": 0, "items": []},
        "instruction": _INSTRUCTION,
    }
    if not path.is_file():
        return result
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"learning_items", "learning_evidence_refs"} <= tables:
            result["observations"] = _observations(connection, project, limit)
        if "memories" in tables:
            result["candidate_memories"] = _candidate_memories(connection, project, limit)
    finally:
        connection.close()
    return result


def _observations(connection: sqlite3.Connection, project: str | None, limit: int) -> dict[str, Any]:
    scope = (
        "i.kind='observation' AND i.status='open' AND (i.applicability_scope='workbench' "
        "OR EXISTS (SELECT 1 FROM json_each(i.project_refs) WHERE value=?))"
    )
    total = connection.execute(f"SELECT count(*) FROM learning_items i WHERE {scope}", (project,)).fetchone()[0]
    rows = connection.execute(
        "SELECT i.*, (SELECT count(*) FROM learning_items r WHERE r.kind='observation' "
        "AND r.status NOT IN ('rejected','retired') AND r.recurrence_key=i.recurrence_key) AS recurrence_count "
        f"FROM learning_items i WHERE {scope} "
        "ORDER BY (recurrence_count>=3 OR i.impact IN ('high','critical')) DESC, i.created_at DESC, i.item_id DESC "
        "LIMIT ?",
        (project, limit),
    ).fetchall()
    evidence: dict[str, list[dict[str, Any]]] = {row["item_id"]: [] for row in rows}
    if rows:
        placeholders = ",".join("?" for _ in rows)
        for entry in connection.execute(
            "SELECT item_id,reference_type,reference,summary,observed_at FROM learning_evidence_refs "
            f"WHERE item_id IN ({placeholders}) ORDER BY item_id,evidence_id",
            list(evidence),
        ):
            item = {key: entry[key] for key in ("reference_type", "reference", "summary", "observed_at")}
            evidence[entry["item_id"]].append({key: value for key, value in item.items() if value is not None})
    return {"total": total, "items": [_observation_entry(row, evidence[row["item_id"]]) for row in rows]}


def _observation_entry(row: sqlite3.Row, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    item_id, version = row["item_id"], row["version"]
    status_change = {"item_id": item_id, "expected_version": version, "actor_kind": "human", **_OWNER_PLACEHOLDERS}
    derive = {
        "decision": "derive_lesson", "actor_kind": "human", **_OWNER_PLACEHOLDERS,
        "source_item_ids": [item_id], "expected_source_versions": {item_id: version},
        "summary": row["summary"], "impact": row["impact"],
        "applicability_scope": row["applicability_scope"],
        "project_refs": json.loads(row["project_refs"]),
        "work_package_refs": json.loads(row["work_package_refs"]),
        "environment_refs": json.loads(row["environment_refs"]),
        "evidence": evidence,
        "provenance": {"source_action": "learning.triage", "source_version": "v1"},
    }
    options = [
        ("derive_lesson", "make this observation a lesson", derive),
        ("defer", "decide later", {"decision": "defer", **status_change}),
        ("reject", "reject this observation", {"decision": "reject", **status_change}),
    ]
    return {
        "id": item_id, "kind": "observation", "summary": row["summary"],
        "observation_type": row["observation_type"], "impact": row["impact"],
        "recurrence_count": row["recurrence_count"],
        "attention_due": row["recurrence_count"] >= 3 or row["impact"] in ("high", "critical"),
        "created_at": row["created_at"],
        "options": [
            {"decision": decision, "fill_before_running": _OWNER_FIELDS, **dispatcher_operation(
                "learning", "triage", kwargs=kwargs, reason=reason,
                requires_owner=True, retry_safety="state_checked",
            )}
            for decision, reason, kwargs in options
        ],
    }


def _candidate_memories(connection: sqlite3.Connection, project: str | None, limit: int) -> dict[str, Any]:
    from odibi_anchor._dispatcher._memory_actions import _memory_entry_operations

    scope = "status='candidate' AND project IN (?, 'all')"
    total = connection.execute(f"SELECT count(*) FROM memories WHERE {scope}", (project,)).fetchone()[0]
    rows = connection.execute(
        f"SELECT id,project,type,content,confidence,created FROM memories WHERE {scope} "
        "ORDER BY created DESC, id DESC LIMIT ?",
        (project, limit),
    ).fetchall()
    items = []
    for row in rows:
        options = []
        for operation in _memory_entry_operations({"id": row["id"], "status": "candidate"}):
            command = operation["kwargs"].get("command")
            options.append({"decision": "activate" if command else "reject", **operation})
        content = row["content"]
        items.append({
            "id": row["id"], "kind": "candidate_memory", "project": row["project"], "type": row["type"],
            "summary": content if len(content) <= 200 else content[:199] + "\u2026",
            "confidence": row["confidence"], "created": row["created"], "options": options,
        })
    return {"total": total, "items": items}
