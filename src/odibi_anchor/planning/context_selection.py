"""Deterministic, provider-neutral selection of task-relevant context questions."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from odibi_anchor.planning._task_profile import TaskProfile

EyePriority = Literal["required", "recommended"]
CONTEXT_PLAN_SCHEMA_VERSION = "1.0"
DEFAULT_MAX_QUESTIONS = 6
MAX_ACTIONS_PER_QUESTION = 4


@dataclass(frozen=True)
class EyeDefinition:
    """One stable situational-awareness question and its evidence sources."""

    id: str
    family: str
    question: str
    expected_evidence: tuple[str, ...]
    candidate_actions: tuple[str, ...]


EYE_DEFINITIONS: tuple[EyeDefinition, ...] = (
    EyeDefinition(
        "environment.capability",
        "environment-capability",
        "What runtime, identity, permissions, dependencies, and evidence channels are available?",
        ("runtime and platform identity", "available capabilities and constraints",
         "unsupported or inaccessible evidence channels"),
        ("orient", "manifest", "status", "environment_diff"),
    ),
    EyeDefinition(
        "work.intent",
        "work-intent",
        "What outcome, scope, prior decisions, approvals, and definition of done govern this work?",
        ("task outcome and acceptance criteria", "scope and approval boundaries",
         "relevant specifications, work items, and prior decisions"),
        ("memory", "status", "session_delta"),
    ),
    EyeDefinition(
        "code.change",
        "code-change",
        "What code owns the behavior, what changed, and which contracts or consumers are affected?",
        ("owning files and symbols", "repository changes and dependency impact",
         "applicable conventions and tests"),
        ("map", "impact", "lookup", "session_diff"),
    ),
    EyeDefinition(
        "data.state-quality",
        "data-state-quality",
        "What is the current data shape, freshness, quality, grain, and observed drift?",
        ("schema, grain, row-count, and freshness facts", "null, duplicate, and quality evidence",
         "comparable before/after observations"),
        ("profile_table", "quality", "contract", "diff"),
    ),
    EyeDefinition(
        "execution.incident",
        "execution-incident",
        "What ran, what failed or diverged, and which execution evidence supports the diagnosis?",
        ("run identity and timing", "failure and log evidence", "healthy-versus-failed differences"),
        ("run_diff", "trace", "known_error", "spark_diagnose"),
    ),
    EyeDefinition(
        "dependency.lineage-impact",
        "dependency-lineage-impact",
        "What feeds and consumes this component, and what could be affected by a change?",
        ("declared or observed dependencies", "upstream and downstream lineage",
         "impact confidence and known gaps"),
        ("impact", "uc_context", "trace_row", "pre_join"),
    ),
    EyeDefinition(
        "performance.cost",
        "performance-cost",
        "Where are time and resources being spent, and is the behavior anomalous or worth optimizing?",
        ("duration and resource metrics", "execution-plan evidence", "comparable workload baseline"),
        ("spark_diagnose", "run_diff", "table_trend"),
    ),
    EyeDefinition(
        "verification.delivery",
        "verification-delivery",
        "Was the intended outcome achieved, and is the result reproducible, reviewable, and ready to hand off?",
        ("acceptance-criteria results", "tests and validation with provenance",
         "remaining unknowns and delivery evidence"),
        ("preflight", "test", "review", "gate"),
    ),
)

_DEFINITION_BY_ID = {definition.id: definition for definition in EYE_DEFINITIONS}
_DEFINITION_ORDER = {definition.id: index for index, definition in enumerate(EYE_DEFINITIONS)}

_TEXT_SIGNALS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("environment", "permission", "credential", "runtime", "dependency"),
     "environment.capability", "Task text indicates environment or capability uncertainty."),
    (("code", "function", "module", "repository", "refactor", "bug", "implementation"),
     "code.change", "Task text indicates code ownership or change context is relevant."),
    (("data quality", "table", "schema", "row", "column", "freshness", "duplicate", "null"),
     "data.state-quality", "Task text indicates data-state evidence is relevant."),
    (("incident", "failed", "failure", "error", "job", "run", "pipeline", "debug"),
     "execution.incident", "Task text indicates execution or incident evidence is relevant."),
    (("lineage", "upstream", "downstream", "impact", "consumer", "join", "blast radius"),
     "dependency.lineage-impact", "Task text indicates dependency or impact evidence is relevant."),
    (("performance", "slow", "latency", "cost", "expensive", "optimize", "spill", "skew"),
     "performance.cost", "Task text indicates performance or cost evidence is relevant."),
    (("verify", "test", "review", "deliver", "pull request", "pr-ready", "acceptance"),
     "verification.delivery", "Task text indicates verification or delivery evidence is relevant."),
)

_ACTION_SIGNALS: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("join", "joining"), "dependency.lineage-impact", ("pre_join",)),
    (("merge", "upsert", "scd"), "data.state-quality", ("pre_merge", "quality", "schema_diff")),
    (("empty", "zero rows", "no rows", "lost rows"), "execution.incident", ("diagnose_empty", "pre_join")),
    (("coerce", "whitespace", "case mismatch", "format mismatch"),
     "data.state-quality", ("coerce_check", "coerce_fix")),
    (("lineage", "trace value", "wrong value"),
     "dependency.lineage-impact", ("trace_row", "explain_row")),
    (("blast radius", "affected consumer"), "dependency.lineage-impact", ("impact",)),
    (("data validation", "dataset validation", "validate data", "validate dataset", "validate table", "validate rows"),
     "data.state-quality", ("validate", "quality")),
)

_QUALIFIED_ACTION_SIGNALS: tuple[
    tuple[tuple[str, ...], tuple[str, ...], str, tuple[str, ...]], ...
] = (
    (("validation", "validate", "quality"), ("data", "dataset", "table", "row"),
     "data.state-quality", ("validate", "quality")),
    (("dependency", "dependencies"), ("data", "dataset", "table", "upstream", "downstream", "consumer"),
     "dependency.lineage-impact", ("impact",)),
)

_NEGATION_SIGNAL = re.compile(
    r"\b(?:do\s+not|don't|does\s+not|doesn't|must\s+not|mustn't|should\s+not|"
    r"shouldn't|without|exclude(?:d)?|avoid|out\s+of\s+scope|no\s+need\s+to)\b"
)


def _contains_signal(text: str, keyword: str) -> bool:
    """Match complete words or phrases without collisions such as data/Databricks."""
    forms = {keyword}
    if keyword.endswith("y"):
        forms.add(f"{keyword[:-1]}ies")
    elif not keyword.endswith("s"):
        forms.update({f"{keyword}s", f"{keyword}es"})
    for form in forms:
        phrase_pattern = re.escape(form).replace(r"\ ", r"\s+")
        if re.search(rf"(?<!\w){phrase_pattern}(?!\w)", text):
            return True
    return False


def _positive_signal_text(text: str) -> str:
    """Remove explicitly negated clauses before fuzzy task-text inference."""
    clauses = re.split(r"(?<=[,;.!?\n])|\b(?:but|however)\b", text.lower())
    positive: list[str] = []
    for clause in clauses:
        match = _NEGATION_SIGNAL.search(clause)
        if match is None:
            positive.append(clause)
        elif clause[:match.start()].strip():
            positive.append(clause[:match.start()])
    return " ".join(positive)


def _text_signal_eye_ids(text: str) -> set[str]:
    """Return eye IDs named by text without changing signal priority."""
    text_ids = {
        eye_id
        for keywords, eye_id, _reason in _TEXT_SIGNALS
        if any(_contains_signal(text, keyword) for keyword in keywords)
    }
    action_ids = {
        eye_id
        for keywords, eye_id, _actions in _ACTION_SIGNALS
        if any(_contains_signal(text, keyword) for keyword in keywords)
    }
    qualified_ids = {
        eye_id
        for qualifiers, objects, eye_id, _actions in _QUALIFIED_ACTION_SIGNALS
        if (any(_contains_signal(text, keyword) for keyword in qualifiers)
            and any(_contains_signal(text, keyword) for keyword in objects))
    }
    return text_ids | action_ids | qualified_ids


def _add_signal(
    signals: dict[str, dict[str, Any]],
    eye_id: str,
    *,
    priority: EyePriority,
    score: int,
    reason: str,
) -> None:
    """Merge a signal without allowing weaker text inference to lower priority."""
    current = signals.setdefault(eye_id, {"priority": priority, "score": score, "reasons": []})
    if priority == "required":
        current["priority"] = "required"
    current["score"] = max(current["score"], score)
    if reason not in current["reasons"]:
        current["reasons"].append(reason)


def _family_for_evidence(kind: str, description: str) -> str:
    """Route caller-declared evidence to the closest stable eye."""
    text = f"{kind} {description}".lower()
    for keywords, eye_id, _reason in _TEXT_SIGNALS:
        if any(_contains_signal(text, keyword) for keyword in keywords):
            return eye_id
    return "work.intent"


def _structured_signals(profile: TaskProfile) -> dict[str, dict[str, Any]]:
    """Select required questions only from canonical structured task facts."""
    signals: dict[str, dict[str, Any]] = {}
    _add_signal(signals, "work.intent", priority="required", score=100,
                reason="Every task needs an explicit outcome, scope, and definition of done.")

    domains = set(profile.domains)
    traits = set(profile.traits)
    if "code" in domains or profile.execution_mode == "source_change":
        _add_signal(signals, "code.change", priority="required", score=95,
                    reason="The canonical task profile includes code or source changes.")
    if "data" in domains or profile.execution_mode == "data_change":
        _add_signal(signals, "data.state-quality", priority="required", score=95,
                    reason="The canonical task profile includes data work or data changes.")
    if "pipeline" in domains or profile.work_type == "operate":
        _add_signal(signals, "execution.incident", priority="required", score=90,
                    reason="Operating pipelines requires execution-state evidence.")
    if profile.execution_mode in {"source_change", "data_change"} or profile.work_type == "verify":
        _add_signal(signals, "verification.delivery", priority="required", score=95,
                    reason="Changed or verification work requires outcome and delivery evidence.")
    if profile.work_type == "investigate" or "debugging" in traits:
        _add_signal(signals, "environment.capability", priority="recommended", score=75,
                    reason="Investigation quality depends on known environment capabilities.")
    if "debugging" in traits:
        _add_signal(signals, "execution.incident", priority="required", score=95,
                    reason="Debugging requires observed failure and execution evidence.")
    if profile.execution_mode == "data_change" or "migration" in traits:
        _add_signal(signals, "dependency.lineage-impact", priority="recommended", score=80,
                    reason="Changes should account for upstream, downstream, and consumer impact.")
    if profile.risk == "high":
        _add_signal(signals, "environment.capability", priority="required", score=90,
                    reason="High-risk work must establish capabilities and constraints before acting.")
        _add_signal(signals, "verification.delivery", priority="required", score=100,
                    reason="High-risk work requires explicit verification and delivery evidence.")
        _add_signal(signals, "dependency.lineage-impact", priority="recommended", score=85,
                    reason="High-risk work benefits from explicit blast-radius evidence.")

    for request in profile.caller_required_evidence:
        eye_id = _family_for_evidence(request.kind, request.description)
        _add_signal(signals, eye_id, priority="required", score=100,
                    reason=f"Caller explicitly requires evidence `{request.id}` before {request.required_before}.")
    return signals


def select_context(
    profile: TaskProfile,
    *,
    task_text: str = "",
    out_of_scope: Iterable[str] | None = None,
    available_actions: Iterable[str] | None = None,
    already_called: Iterable[str] | None = None,
    max_questions: int = DEFAULT_MAX_QUESTIONS,
) -> dict[str, Any]:
    """Build a bounded context plan without collecting evidence or probing providers."""
    if not isinstance(profile, TaskProfile):
        raise TypeError("profile must be a TaskProfile")
    if type(max_questions) is not int or max_questions < 1:
        raise ValueError("max_questions must be a positive integer")

    signals = _structured_signals(profile)
    lowered = _positive_signal_text(task_text)
    excluded_text = " ".join(str(item).lower() for item in (out_of_scope or ()))
    excluded_eye_ids = _text_signal_eye_ids(excluded_text)
    for keywords, eye_id, reason in _TEXT_SIGNALS:
        if (eye_id not in excluded_eye_ids and
                any(_contains_signal(lowered, keyword) for keyword in keywords)):
            _add_signal(signals, eye_id, priority="recommended", score=60, reason=reason)
    action_hints: dict[str, list[str]] = {}
    for keywords, eye_id, actions in _ACTION_SIGNALS:
        if (eye_id not in excluded_eye_ids and
                any(_contains_signal(lowered, keyword) for keyword in keywords)):
            _add_signal(
                signals,
                eye_id,
                priority="recommended",
                score=65,
                reason="Task text identifies a focused existing evidence action.",
            )
            action_hints.setdefault(eye_id, []).extend(actions)
    for qualifiers, objects, eye_id, actions in _QUALIFIED_ACTION_SIGNALS:
        if (eye_id not in excluded_eye_ids
                and any(_contains_signal(lowered, keyword) for keyword in qualifiers)
                and any(_contains_signal(lowered, keyword) for keyword in objects)):
            _add_signal(
                signals,
                eye_id,
                priority="recommended",
                score=65,
                reason="Task text identifies a focused existing evidence action.",
            )
            action_hints.setdefault(eye_id, []).extend(actions)

    ranked = sorted(
        signals.items(),
        key=lambda item: (
            0 if item[1]["priority"] == "required" else 1,
            -item[1]["score"],
            _DEFINITION_ORDER[item[0]],
        ),
    )
    required = [item for item in ranked if item[1]["priority"] == "required"]
    recommended = [item for item in ranked if item[1]["priority"] == "recommended"]
    selected = required + recommended[:max(0, max_questions - len(required))]
    omitted = ranked[len(required) + max(0, max_questions - len(required)):]

    known_actions = set(available_actions) if available_actions is not None else None
    called = set(already_called or ())
    questions: list[dict[str, Any]] = []
    for eye_id, signal in selected:
        definition = _DEFINITION_BY_ID[eye_id]
        ordered_candidates = tuple(dict.fromkeys([
            *action_hints.get(eye_id, ()),
            *definition.candidate_actions,
        ]))
        if known_actions is None:
            action_names = ordered_candidates[:MAX_ACTIONS_PER_QUESTION]
            availability = "unknown"
        else:
            action_names = tuple(
                action for action in ordered_candidates if action in known_actions
            )[:MAX_ACTIONS_PER_QUESTION]
            availability = "registered"
        questions.append({
            "id": definition.id,
            "family": definition.family,
            "priority": signal["priority"],
            "question": definition.question,
            "reasons": list(signal["reasons"]),
            "expected_evidence": list(definition.expected_evidence),
            "action_support": (
                "unknown" if known_actions is None else
                "registered" if action_names else "none_available"
            ),
            "candidate_actions": [
                {
                    "name": action,
                    "availability": availability,
                    "called_this_session": action in called,
                    "focused": action in action_hints.get(eye_id, ()),
                }
                for action in action_names
            ],
        })

    return {
        "schema_version": CONTEXT_PLAN_SCHEMA_VERSION,
        "questions": questions,
        "not_selected": [
            definition.id for definition in EYE_DEFINITIONS
            if definition.id not in {question["id"] for question in questions}
        ],
        "truncated": bool(omitted),
        "omitted_question_count": len(omitted),
        "omitted_required_count": sum(
            1 for _eye_id, signal in omitted if signal["priority"] == "required"
        ),
        "selection_basis": {
            "work_type": profile.work_type,
            "execution_mode": profile.execution_mode,
            "risk": profile.risk,
            "domains": list(profile.domains),
            "traits": sorted(profile.traits),
            "text_inference_can_require": False,
        },
    }


def project_context_generators(context_plan: Mapping[str, Any]) -> list[dict[str, str]]:
    """Project selected candidate actions into the legacy discovery record shape."""
    projected: list[dict[str, str]] = []
    seen: set[str] = set()
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for question in context_plan.get("questions", ()):
        if not isinstance(question, Mapping):
            continue
        for action in question.get("candidate_actions", ()):
            if isinstance(action, Mapping):
                candidates.append((question, action))
    candidates.sort(key=lambda item: 0 if item[1].get("focused") else 1)
    for question, action in candidates:
        name = action.get("name")
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        status = "already_called" if action.get("called_this_session") else str(action.get("availability", "unknown"))
        projected.append({
            "name": name,
            "status": status,
            "reason": f"Supports the selected `{question.get('family', 'context')}` evidence question.",
        })
    return projected


def eye_catalog() -> list[dict[str, Any]]:
    """Return the stable public eye inventory without exposing mutable definitions."""
    return [
        {
            "id": definition.id,
            "family": definition.family,
            "question": definition.question,
            "expected_evidence": list(definition.expected_evidence),
            "candidate_actions": list(definition.candidate_actions),
        }
        for definition in EYE_DEFINITIONS
    ]


__all__ = [
    "CONTEXT_PLAN_SCHEMA_VERSION",
    "EYE_DEFINITIONS",
    "EyeDefinition",
    "eye_catalog",
    "project_context_generators",
    "select_context",
]
