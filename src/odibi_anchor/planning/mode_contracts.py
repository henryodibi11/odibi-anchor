"""Mode contracts: what a task mode asks of an agent.

A contract is derived from the task profile (execution mode, risk, rigor) plus a
small per-mode table. It names the fields to fill, the records to write, the
evidence that counts, the review its rigor requires, and where the owner is
needed. Pure data and one function; no I/O.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from odibi_anchor.planning._task_profile import _LEGACY_DEFAULTS, TaskProfile, normalize_task_profile

# The rigor table from docs/guides/agent-first-workflows.md ("Modes are contracts").
_REVIEW_BY_RIGOR: dict[str, dict[str, Any]] = {
    "direct": {
        "plan_reviews_before_owner_approval": 1,
        "distinct_angles": False,
        "required_angles": [],
        "delivery_review": "self review allowed (unless high risk)",
    },
    "compact": {
        "plan_reviews_before_owner_approval": 2,
        "distinct_angles": True,
        "required_angles": [],
        "delivery_review": "self review allowed (unless high risk)",
    },
    "full": {
        "plan_reviews_before_owner_approval": 4,
        "distinct_angles": True,
        "required_angles": ["adversarial"],
        "delivery_review": "independent review required",
    },
}

_PLANNING_RECORDS = [
    {"record": "problem", "how": 'anchor("problem", "create"|"update", ...) for the investigation'},
    {"record": "spec", "how": 'anchor("spec", "create", content=...) for the shared design'},
    {"record": "work_items", "how": 'anchor("work_item", "create", ..., depends_on=[...]): one pull request each'},
    {"record": "work_item_reviews",
     "how": 'anchor("work_item", "review", ...) with angle, verdict, findings and reviewer'},
    {"record": "plan_approval",
     "how": 'anchor("work_item", "request_plan_approval", ...) once the reviews satisfy the rigor'},
]
_PROBLEM_RECORD = {"record": "problem", "how": 'anchor("problem", "create"|"update", ...) for the investigation'}
_WORKFLOW_PLAN = {
    "source_change": {
        "record": "workflow_plan", "fields": ["source_paths", "criteria"],
        "how": 'anchor("workflow", "check_plan", plan=...), then "create"; bind workflow_id on the task '
               'and accept_plan before edits',
    },
    "artifact_only": {
        "record": "workflow_plan", "fields": ["artifact_paths", "criteria"],
        "how": 'anchor("workflow", "check_plan", plan=...), then "create"; bind workflow_id on the task '
               'and accept_plan before writing',
    },
}

# Per-mode additions: extra records and notes. Everything else derives from the profile.
_MODE_TABLE: dict[str, dict[str, list[Any]]] = {
    "planning": {"records": _PLANNING_RECORDS},
    "spec_creation": {"records": _PLANNING_RECORDS},
    "greenfield": {"records": _PLANNING_RECORDS},
    "debugging": {"records": [_PROBLEM_RECORD]},
    "documentation": {"notes": [
        "Documentation in a repository: use execution_mode=\"source_change\" with command or review "
        "criteria. The artifact_only default covers managed artifacts only.",
    ]},
    "pipeline": {"notes": [
        "Use command criteria that validate and run the pipeline configuration, e.g. "
        '{"id": "run", "method": "command", "expected": "pipeline runs", "argv": [...]}.',
    ]},
    "data": {"notes": ["Data-only changes stay unphased (no workflow); verify the data effect before gate."]},
    "etl": {"notes": ["Data-only changes stay unphased (no workflow); verify the data effect before gate."]},
    "refresh": {"notes": ["Data-only changes stay unphased (no workflow); verify the data effect before gate."]},
}


def _resolve_profile(mode: str | None, profile: TaskProfile | Mapping[str, Any] | None) -> TaskProfile:
    if profile is None:
        if mode is None:
            raise ValueError(f"mode or profile is required; modes: {', '.join(sorted(_LEGACY_DEFAULTS))}")
        return normalize_task_profile(legacy_mode=mode)
    if isinstance(profile, Mapping):
        if set(profile) >= {"schema_version", "legacy_mode", "normalization_notes"}:
            return TaskProfile.from_dict(profile)
        fields = ("work_type", "execution_mode", "risk", "rigor", "domains", "traits")
        return normalize_task_profile(legacy_mode=mode, **{k: profile[k] for k in fields if k in profile})
    return profile


def contract_for(mode: str | None, *, profile: TaskProfile | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the contract for ``mode`` (or the accepted ``profile``)."""
    from odibi_anchor._dispatcher._effects import task_profile_effect_compatible
    from odibi_anchor._dispatcher._workflow_admission import lightweight_artifact_task, planning_record_task

    resolved = _resolve_profile(mode, profile)
    mode = mode or resolved.legacy_mode
    execution = resolved.execution_mode
    # Same rule as require_task_workflow: source-capable or substantive artifact tasks.
    workflow_required = task_profile_effect_compatible("source_write", resolved) or (
        execution == "artifact_only" and not lightweight_artifact_task(resolved)
        and not planning_record_task(resolved)
    )
    table = _MODE_TABLE.get(mode or "", {})
    records = list(table.get("records", []))
    if workflow_required:
        records.append(_WORKFLOW_PLAN["artifact_only" if execution == "artifact_only" else "source_change"])
    if resolved.rigor == "full" and _PROBLEM_RECORD not in records:
        records.insert(0, _PROBLEM_RECORD)
    required = ["task", "goal", "mode", "acceptance_criteria"]
    if workflow_required:
        required += ["workflow_id", "trust_domain"]
    recommended = ["risk", "rigor", "in_scope", "constraints"]
    if execution == "source_change":
        recommended.append("repository_scope")
    notes = list(table.get("notes", []))
    if execution == "source_change":
        notes.append("A source_change task requires a clean initial Git worktree.")
    review = dict(_REVIEW_BY_RIGOR[resolved.rigor])
    review["required_angles"] = list(review["required_angles"])
    review["self_review_allowed"] = resolved.rigor != "full" and resolved.risk != "high"
    return {
        "kind": "mode_contract",
        "mode": mode,
        "work_type": resolved.work_type,
        "execution_mode": execution,
        "risk": resolved.risk,
        "rigor": resolved.rigor,
        "workflow_required": workflow_required,
        "required_task_fields": required,
        "recommended_task_fields": recommended,
        "records": copy.deepcopy(records),
        "accepted_criteria": ["pytest", "command", "review"]
        + (["artifact_sha256"] if execution == "artifact_only" else []),
        "review_requirement": review,
        "owner_touchpoints": {
            "plan_approval": resolved.work_type == "design",
            "delivery_approval": workflow_required,
            "lessons": True,
            "blockers": True,
        },
        "notes": notes,
    }
