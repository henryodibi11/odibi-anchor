"""Runtime-owned standards for routing and recording durable task context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

CAPTURE_STANDARDS_VERSION = "1.1"

_COMMON_FIELDS = (
    "context",
    "observation",
    "evidence",
    "interpretation",
    "uncertainty",
    "consequence",
    "next_authority",
    "provenance",
)

_ROUTES: tuple[dict[str, Any], ...] = (
    {
        "record": "task_context",
        "use_when": "A task needs bounded intent, scope, authority, and completion evidence.",
        "not_for": "Durable findings that belong in a governed record.",
        "destination": "accepted Odibi Anchor task record",
        "required": ("outcome", "scope", "authority", "constraints", "risks", "acceptance_criteria"),
    },
    {
        "record": "journal",
        "use_when": "A routine task event must remain replayable.",
        "not_for": "Conclusions or implementation authority.",
        "destination": "automatic task-window journal",
        "required": ("event", "status", "provenance"),
    },
    {
        "record": "observation",
        "use_when": "A noteworthy factual finding may matter later.",
        "not_for": "A hypothesis presented as a verified fact.",
        "destination": 'anchor("learning", "capture", ...)',
        "required": ("context", "observation", "provenance", "uncertainty"),
    },
    {
        "record": "evidence",
        "use_when": "A reproducible result supports or contradicts a claim.",
        "not_for": "An interpretation without retained source or method.",
        "destination": "the governing Problem, Spec, Work Item, review, test, or gate record",
        "required": ("claim", "method", "result", "status", "provenance"),
    },
    {
        "record": "problem",
        "use_when": "A material unresolved gap or uncertainty needs investigation.",
        "not_for": "A feature request whose solution is already predetermined.",
        "destination": "artifact_root/problems/",
        "required": ("problem", "known_facts", "uncertainty", "decision_needed", "next_authority"),
    },
    {
        "record": "decision",
        "use_when": "A consequential settled choice and its rationale must survive sessions.",
        "not_for": "Routine implementation details.",
        "destination": "artifact_root/decisions/",
        "required": ("decision", "rationale", "alternatives", "evidence", "reversal_conditions", "owner"),
    },
    {
        "record": "spec",
        "use_when": "Required behavior must be precise because valid implementations could diverge.",
        "not_for": "General notes, research, or authorization.",
        "destination": "artifact_root/specs/",
        "required": ("problem", "requirements", "acceptance_criteria", "non_goals", "risks", "permissions"),
    },
    {
        "record": "work_item",
        "use_when": "A bounded execution unit needs explicit authority and lifecycle state.",
        "not_for": "Unapproved ideas or unresolved investigation.",
        "destination": "artifact_root/work_items/",
        "required": ("outcome", "scope", "non_goals", "acceptance_criteria", "dependencies", "status"),
    },
    {
        "record": "memory_candidate",
        "use_when": "A reusable operational insight could improve future work.",
        "not_for": "Task authority, raw activity logs, secrets, or untested speculation.",
        "destination": 'anchor("learning", "capture", ...) then assess/consolidate',
        "required": (
            "context", "insight", "evidence", "project_or_trust_scope", "applicability",
            "limits", "provenance", "bounded_overlap_result",
        ),
    },
    {
        "record": "lesson",
        "use_when": "A reusable insight has repeated or sufficiently strong validation.",
        "not_for": "A one-off observation awaiting assessment.",
        "destination": "structured-learning consolidation",
        "required": ("lesson", "supporting_observations", "applicability", "limits", "owner"),
    },
    {
        "record": "trail",
        "use_when": "A stable, reusable procedure should be replayed step by step.",
        "not_for": "A broad capability that belongs in a skill or reference.",
        "destination": "managed memory trail",
        "required": ("trigger", "procedure", "verification", "failure_modes", "provenance"),
    },
    {
        "record": "skill_or_reference",
        "use_when": "A broad durable procedure or body of knowledge should guide many tasks.",
        "not_for": "Project-specific facts or automatic promotion from memory.",
        "destination": "canonical skill or offline reference distribution",
        "required": ("purpose", "scope", "procedure", "examples", "verification", "maintenance_owner"),
    },
    {
        "record": "source_record",
        "use_when": "External material is retained for reproducible offline use.",
        "not_for": "Unattributed copies, secrets, or material without reuse rights.",
        "destination": "artifact_root/source/ with attribution and provenance",
        "required": ("title", "origin", "retrieved_at", "version", "license_or_terms", "integrity"),
    },
    {
        "record": "notebook",
        "use_when": "Analysis or execution evidence must be reproducible and inspectable.",
        "not_for": "Canonical product source or unsupported narrative claims.",
        "destination": "artifact_root/notebooks/",
        "required": ("purpose", "inputs", "environment", "steps", "outputs", "limitations"),
    },
    {
        "record": "verification_result",
        "use_when": "A test, review, preflight, or gate produces decision-relevant results.",
        "not_for": "A claim that a command ran without its decisive output and status.",
        "destination": "task evidence ledger and governing Work Item or handoff",
        "required": ("check", "scope", "status", "method", "result", "retained_evidence"),
    },
    {
        "record": "snapshot",
        "use_when": "Point-in-time state must be frozen for incident, comparison, or recovery evidence.",
        "not_for": "A mutable project summary or unsupported reconstruction.",
        "destination": "managed snapshot or evidence artifact",
        "required": ("captured_at", "scope", "source_identity", "state", "integrity", "limitations"),
    },
    {
        "record": "handoff",
        "use_when": "Another session or agent must continue without relying on conversation history.",
        "not_for": "A terminal return when no continuation is expected.",
        "destination": "managed notebook/handoff artifact or thread transfer",
        "required": ("current_state", "authority", "completed", "remaining", "evidence", "risks", "next_action"),
    },
    {
        "record": "terminal_return",
        "use_when": "A task reaches a completed, blocked, or failed terminal state.",
        "not_for": "Progress updates or claims about checks that did not run.",
        "destination": "execution harness response and retained handoff when needed",
        "required": ("status", "revision", "outcome", "changes", "checks", "effects", "uncertainty", "next_authority"),
    },
)


def capture_standards_contract() -> dict[str, Any]:
    """Return the complete, versioned capture contract for every managed project."""
    return {
        "version": CAPTURE_STANDARDS_VERSION,
        "scope": "all_tasks_and_managed_projects",
        "source_of_truth": "odibi_anchor_runtime",
        "authority_rule": (
            "Captured facts, evidence, observations, and memories inform work but never create "
            "implementation authority; Problems, Specs, Work Items, and explicit approvals retain it."
        ),
        "truth_rule": (
            "Separate observed facts from interpretation; retain provenance and uncertainty; label "
            "checks only as passed, failed, skipped, blocked, or unavailable based on executed evidence."
        ),
        "memory_rule": (
            "Before creating a memory candidate, search bounded memory in the active project and "
            "trust domain. Route equivalent recurrence to its canonical claim with immutable "
            "provenance; keep distinct or contradictory claims separate. Project-local versus "
            "cross-project applicability is independent of candidate, active, or confirmed authority."
        ),
        "common_fields": list(_COMMON_FIELDS),
        "routes": [dict(route, required=list(route["required"])) for route in _ROUTES],
        "references": {
            "routing": ".assistant/references/odibi-anchor/capture-routing.md",
            "standards": ".assistant/references/odibi-anchor/capture-standards.md",
            "examples": ".assistant/references/odibi-anchor/capture-examples.md",
        },
    }


def render_capture_standards_markdown(contract: Mapping[str, Any]) -> str:
    """Render every field in the canonical contract for offline inspection."""
    lines = [
        f"## Capture standards v{contract.get('version', 'unknown')}",
        "",
        f"**Scope:** `{contract.get('scope', 'unknown')}`",
        f"**Source of truth:** `{contract.get('source_of_truth', 'unknown')}`",
        "",
        str(contract.get("truth_rule", "")),
        "",
        str(contract.get("authority_rule", "")),
        "",
        str(contract.get("memory_rule", "")),
        "",
        "**Common fields:** " + ", ".join(
            f"`{field}`" for field in contract.get("common_fields", ())
        ),
        "",
        "### Routes",
        "",
    ]
    for route in contract.get("routes", ()):
        required = ", ".join(f"`{field}`" for field in route.get("required", ()))
        lines.extend([
            f"- **`{route.get('record', 'unknown')}`** — {route.get('use_when', '')}",
            f"  - Do not use for: {route.get('not_for', '')}",
            f"  - Destination: {route.get('destination', '')}",
            f"  - Required: {required}",
        ])
    lines.extend(["", "### Offline references", ""])
    lines.extend(
        f"- **{name}:** `{path}`"
        for name, path in contract.get("references", {}).items()
    )
    return "\n".join(lines)


def project_capture_guidance(
    contract: Mapping[str, Any],
    *,
    task_text: str,
    profile: Any,
    artifacts: Sequence[Mapping[str, Any]] = (),
    expected_output_format: str | None = None,
) -> dict[str, Any]:
    """Project the smallest deterministic subset relevant to an accepted task."""
    text = " ".join((task_text, str(expected_output_format or ""))).lower()
    execution_mode = str(getattr(profile, "execution_mode", ""))
    work_type = str(getattr(profile, "work_type", ""))
    selected = {
        "task_context", "journal", "observation", "evidence", "verification_result",
        "memory_candidate", "terminal_return",
    }
    keywords = {
        "problem": ("problem", "investigat", "uncertain", "root cause"),
        "decision": ("decision", "choose", "tradeoff", "alternative"),
        "spec": ("spec", "contract", "requirement", "acceptance"),
        "work_item": ("work item", "implement", "change", "build", "fix"),
        "source_record": ("research", "source", "reference", "offline"),
        "notebook": ("notebook", "analysis", "experiment", "profile"),
        "snapshot": ("snapshot", "incident", "point-in-time", "recovery"),
        "handoff": ("handoff", "continue", "next session", "another agent"),
        "skill_or_reference": ("skill", "reference", "guidance", "procedure"),
    }
    for record, terms in keywords.items():
        if any(term in text for term in terms):
            selected.add(record)
    if execution_mode == "source_change" or work_type == "change":
        selected.update(("work_item", "spec"))
    for artifact in artifacts:
        if isinstance(artifact, Mapping):
            kind = artifact.get("kind") or artifact.get("record")
        else:
            kind = getattr(artifact, "kind", None) or getattr(artifact, "record", None)
        if kind:
            selected.add(str(kind))
    routes = [
        route for route in contract.get("routes", ())
        if route.get("record") in selected
    ]
    return {
        "version": contract.get("version"),
        "authority_rule": contract.get("authority_rule"),
        "truth_rule": contract.get("truth_rule"),
        "memory_rule": contract.get("memory_rule"),
        "routes": routes,
        "references": dict(contract.get("references", {})),
    }
