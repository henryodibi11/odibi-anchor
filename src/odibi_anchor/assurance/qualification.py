"""Immutable outcome-qualification records and deterministic score construction."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from types import MappingProxyType
from typing import Any, Literal

from odibi_anchor.assurance.models import utc_datetime

JudgementOutcome = Literal["pass", "fail", "unavailable", "not_applicable"]
TerminalStatus = Literal["completed", "failed", "incomplete", "unavailable"]

_TIERS = frozenset({"T0", "T1", "T2", "T3"})
_SCOPES = frozenset({"localized", "systemic"})
_OUTCOMES = frozenset({"pass", "fail", "unavailable", "not_applicable"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "incomplete", "unavailable"})
_SEVERITIES = frozenset({"none", "low", "medium", "high", "critical"})
_FORBIDDEN_EVALUATOR_FIELDS = frozenset(
    {
        "answer",
        "expected_answer",
        "oracle",
        "oracle_payload",
        "rubric",
        "seeded_defect",
        "seeded_defect_location",
        "evaluator_payload",
    }
)


def canonical_json_bytes(value: object) -> bytes:
    """Return the repository's stable, human-readable canonical JSON encoding."""
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _sha256(value: Any, field: str) -> str:
    text = _nonempty(value, field)
    digest = text.removeprefix("sha256:")
    if (
        not text.startswith("sha256:")
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _strings(value: Any, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    result = tuple(_nonempty(item, field) for item in value)
    if not allow_empty and not result:
        raise ValueError(f"{field} cannot be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} cannot contain duplicates")
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a JSON object")
    return MappingProxyType(dict(value))


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = expected.difference(value)
    extra = set(value).difference(expected)
    if missing:
        raise ValueError(f"Missing {label} fields: {sorted(missing)}")
    if extra:
        raise ValueError(f"Unexpected {label} fields: {sorted(extra)}")


def _reject_evaluator_payload(value: object, path: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _FORBIDDEN_EVALUATOR_FIELDS:
                raise ValueError(f"Evaluator payload is forbidden in agent-visible {path}: {key}")
            _reject_evaluator_payload(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_evaluator_payload(item, f"{path}[{index}]")


def _loads_canonical(raw: bytes, label: str) -> Any:
    def ordered(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            raise ValueError(f"{label} object keys must be unique and canonically ordered")
        return dict(pairs)

    if not raw.endswith(b"\n") or raw.rstrip(b"\n").endswith((b" ", b"\t", b"\r")):
        raise ValueError(f"{label} must end in one canonical newline")
    try:
        return json.loads(raw, object_pairs_hook=ordered)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is malformed JSON") from exc


@dataclass(frozen=True)
class ScenarioDescriptor:
    scenario_id: str
    version: str
    tier: str
    scope: str
    work_type: str
    traits: tuple[str, ...]
    applicable_controls: tuple[str, ...]
    fixture_digest: str
    evaluator_digest: str
    expected_evidence_classes: tuple[str, ...]
    oracle_owner: str
    scenario_family: str
    corpus_split: str
    execution_budget: Mapping[str, Any]
    evaluator_reference: str

    def __post_init__(self) -> None:
        for field in ("scenario_id", "version", "work_type", "oracle_owner", "scenario_family", "evaluator_reference"):
            _nonempty(getattr(self, field), field)
        if self.tier not in _TIERS:
            raise ValueError(f"Unsupported tier: {self.tier!r}")
        if self.scope not in _SCOPES:
            raise ValueError(f"Unsupported scope: {self.scope!r}")
        if self.corpus_split not in {"development", "qualification", "holdout"}:
            raise ValueError(f"Unsupported corpus_split: {self.corpus_split!r}")
        object.__setattr__(self, "traits", _strings(self.traits, "traits"))
        object.__setattr__(
            self,
            "applicable_controls",
            _strings(
                self.applicable_controls,
                "applicable_controls",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "expected_evidence_classes",
            _strings(
                self.expected_evidence_classes,
                "expected_evidence_classes",
                allow_empty=False,
            ),
        )
        _sha256(self.fixture_digest, "fixture_digest")
        _sha256(self.evaluator_digest, "evaluator_digest")
        budget = _mapping(self.execution_budget, "execution_budget")
        if set(budget) != {"max_assurance_calls", "max_assurance_seconds", "max_evidence_items"}:
            raise ValueError("execution_budget has unexpected fields")
        if any(not isinstance(item, int) or item < 0 for item in budget.values()):
            raise ValueError("execution_budget values must be non-negative integers")
        object.__setattr__(self, "execution_budget", budget)
        _reject_evaluator_payload(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "applicable_controls": list(self.applicable_controls),
            "corpus_split": self.corpus_split,
            "evaluator_digest": self.evaluator_digest,
            "evaluator_reference": self.evaluator_reference,
            "execution_budget": dict(self.execution_budget),
            "expected_evidence_classes": list(self.expected_evidence_classes),
            "fixture_digest": self.fixture_digest,
            "oracle_owner": self.oracle_owner,
            "scenario_family": self.scenario_family,
            "scenario_id": self.scenario_id,
            "scope": self.scope,
            "tier": self.tier,
            "traits": list(self.traits),
            "version": self.version,
            "work_type": self.work_type,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ScenarioDescriptor:
        expected = {
            "scenario_id",
            "version",
            "tier",
            "scope",
            "work_type",
            "traits",
            "applicable_controls",
            "fixture_digest",
            "evaluator_digest",
            "expected_evidence_classes",
            "oracle_owner",
            "scenario_family",
            "corpus_split",
            "execution_budget",
            "evaluator_reference",
        }
        _exact(value, expected, "ScenarioDescriptor")
        return cls(**value)


@dataclass(frozen=True)
class QualificationRun:
    run_id: str
    scenario_id: str
    scenario_version: str
    source_commit: str
    distribution_version: str
    distribution_digest: str
    agent_family: str
    agent_version: str
    producer_id: str
    host: str
    transport: str
    tier: str
    scope: str
    selected_profile: str
    control_version: str
    evaluator_digest: str
    started_at: str
    ended_at: str
    terminal_status: TerminalStatus
    artifact_digests: tuple[str, ...]
    protocol_violations: tuple[str, ...]
    attempt: int = 1

    def __post_init__(self) -> None:
        for field in (
            "run_id",
            "scenario_id",
            "scenario_version",
            "source_commit",
            "distribution_version",
            "agent_family",
            "agent_version",
            "producer_id",
            "host",
            "transport",
            "selected_profile",
            "control_version",
        ):
            _nonempty(getattr(self, field), field)
        if self.tier not in _TIERS or self.scope not in _SCOPES:
            raise ValueError("run tier and scope must be explicit independent dimensions")
        if self.terminal_status not in _TERMINAL_STATUSES:
            raise ValueError(f"Unsupported terminal_status: {self.terminal_status!r}")
        if not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if utc_datetime(self.ended_at, "ended_at") < utc_datetime(self.started_at, "started_at"):
            raise ValueError("ended_at cannot be before started_at")
        _sha256(self.distribution_digest, "distribution_digest")
        _sha256(self.evaluator_digest, "evaluator_digest")
        digests = _strings(self.artifact_digests, "artifact_digests")
        for index, digest in enumerate(digests):
            _sha256(digest, f"artifact_digests[{index}]")
        object.__setattr__(self, "artifact_digests", digests)
        object.__setattr__(
            self,
            "protocol_violations",
            _strings(
                self.protocol_violations,
                "protocol_violations",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "artifact_digests": list(self.artifact_digests),
            "protocol_violations": list(self.protocol_violations),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> QualificationRun:
        expected = set(cls.__dataclass_fields__)
        _exact(value, expected, "QualificationRun")
        return cls(**value)


@dataclass(frozen=True)
class ScenarioJudgement:
    judgement_id: str
    run_id: str
    scenario_id: str
    behavior_id: str
    outcome: JudgementOutcome
    defect_severity: str
    escaped_defect: bool
    false_positive: bool
    unsupported_claim: bool
    reviewer_corrections: int
    evidence_references: tuple[str, ...]
    reviewer_id: str
    reviewer_blinded: bool
    first_attempt: bool
    independently_reviewed: bool
    material_claims: int
    detectable_defects: int
    emitted_findings: int
    applicable_evidence_requirements: int
    ceremony_wall_seconds: int
    ceremony_calls: int
    ceremony_prompts: int
    ceremony_evidence_items: int
    ceremony_reviewer_actions: int

    def __post_init__(self) -> None:
        for field in ("judgement_id", "run_id", "scenario_id", "behavior_id", "reviewer_id"):
            _nonempty(getattr(self, field), field)
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"Unsupported judgement outcome: {self.outcome!r}")
        if self.defect_severity not in _SEVERITIES:
            raise ValueError(f"Unsupported defect_severity: {self.defect_severity!r}")
        numeric = (
            "reviewer_corrections",
            "material_claims",
            "detectable_defects",
            "emitted_findings",
            "applicable_evidence_requirements",
            "ceremony_wall_seconds",
            "ceremony_calls",
            "ceremony_prompts",
            "ceremony_evidence_items",
            "ceremony_reviewer_actions",
        )
        if any(not isinstance(getattr(self, field), int) or getattr(self, field) < 0 for field in numeric):
            raise ValueError("judgement counts must be non-negative integers")
        object.__setattr__(
            self,
            "evidence_references",
            _strings(
                self.evidence_references,
                "evidence_references",
            ),
        )
        if self.outcome == "not_applicable" and self.applicable_evidence_requirements:
            raise ValueError("not_applicable judgements cannot claim applicable evidence requirements")

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "evidence_references": list(self.evidence_references)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ScenarioJudgement:
        _exact(value, set(cls.__dataclass_fields__), "ScenarioJudgement")
        return cls(**value)


@dataclass(frozen=True)
class OutcomeScorecard:
    cohort_keys: Mapping[str, str]
    counts: Mapping[str, Mapping[str, int]]
    confidence_intervals: Mapping[str, tuple[float, float] | None]
    exclusions: tuple[Mapping[str, str], ...]
    ceremony_samples: Mapping[str, tuple[int, ...]]
    judgement_ids: tuple[str, ...]
    critical_failures: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cohort_keys", _mapping(self.cohort_keys, "cohort_keys"))
        object.__setattr__(
            self, "counts", MappingProxyType({key: MappingProxyType(dict(value)) for key, value in self.counts.items()})
        )
        object.__setattr__(
            self,
            "confidence_intervals",
            MappingProxyType(
                dict(
                    self.confidence_intervals,
                )
            ),
        )
        object.__setattr__(self, "exclusions", tuple(MappingProxyType(dict(item)) for item in self.exclusions))
        object.__setattr__(
            self,
            "ceremony_samples",
            MappingProxyType({key: tuple(value) for key, value in self.ceremony_samples.items()}),
        )
        object.__setattr__(self, "judgement_ids", _strings(self.judgement_ids, "judgement_ids"))
        object.__setattr__(
            self,
            "critical_failures",
            _strings(
                self.critical_failures,
                "critical_failures",
            ),
        )

    def rate(self, metric: str) -> float | None:
        value = self.counts[metric]
        denominator = value["denominator"]
        return value["numerator"] / denominator if denominator else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ceremony_samples": {key: list(value) for key, value in self.ceremony_samples.items()},
            "cohort_keys": dict(self.cohort_keys),
            "confidence_intervals": {
                key: list(value) if value is not None else None for key, value in self.confidence_intervals.items()
            },
            "counts": {key: dict(value) for key, value in self.counts.items()},
            "critical_failures": list(self.critical_failures),
            "exclusions": [dict(item) for item in self.exclusions],
            "judgement_ids": list(self.judgement_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OutcomeScorecard:
        _exact(value, set(cls.__dataclass_fields__), "OutcomeScorecard")
        intervals = {
            key: tuple(item) if item is not None else None for key, item in value["confidence_intervals"].items()
        }
        return cls(**{**value, "confidence_intervals": intervals})


@dataclass(frozen=True)
class OutcomeComparison:
    candidate_cohort: Mapping[str, str]
    baseline_cohort: Mapping[str, str]
    metric_deltas: Mapping[str, float | None]
    regressions: tuple[str, ...]
    critical_failures: tuple[str, ...]
    comparable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_cohort", _mapping(self.candidate_cohort, "candidate_cohort"))
        object.__setattr__(self, "baseline_cohort", _mapping(self.baseline_cohort, "baseline_cohort"))
        object.__setattr__(
            self,
            "metric_deltas",
            MappingProxyType(dict(self.metric_deltas)),
        )
        object.__setattr__(self, "regressions", _strings(self.regressions, "regressions"))
        object.__setattr__(
            self,
            "critical_failures",
            _strings(self.critical_failures, "critical_failures"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_cohort": dict(self.baseline_cohort),
            "candidate_cohort": dict(self.candidate_cohort),
            "comparable": self.comparable,
            "critical_failures": list(self.critical_failures),
            "metric_deltas": dict(self.metric_deltas),
            "regressions": list(self.regressions),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OutcomeComparison:
        _exact(value, set(cls.__dataclass_fields__), "OutcomeComparison")
        return cls(**value)


def load_scenario_manifest(path: Path) -> tuple[ScenarioDescriptor, ...]:
    """Load a canonical public manifest and reject leaked or inconsistent content."""
    raw = path.read_bytes()
    payload = _loads_canonical(raw, "scenario manifest")
    if not isinstance(payload, Mapping):
        raise ValueError("scenario manifest must be a JSON object")
    _exact(payload, {"manifest_version", "manifest_digest", "scenarios"}, "scenario manifest")
    _nonempty(payload["manifest_version"], "manifest_version")
    expected_digest = _digest(
        canonical_json_bytes(
            {
                "manifest_version": payload["manifest_version"],
                "scenarios": payload["scenarios"],
            }
        )
    )
    if payload["manifest_digest"] != expected_digest:
        raise ValueError("scenario manifest digest mismatch")
    _reject_evaluator_payload(payload)
    if not isinstance(payload["scenarios"], list):
        raise ValueError("scenarios must be an array")
    scenarios = tuple(ScenarioDescriptor.from_dict(item) for item in payload["scenarios"])
    identities = [(item.scenario_id, item.version) for item in scenarios]
    if len(set(identities)) != len(identities):
        raise ValueError("scenario IDs and versions must be unique")
    return scenarios


def validate_run(run: QualificationRun, manifest: Sequence[ScenarioDescriptor]) -> None:
    """Validate one run against its exact immutable public scenario descriptor."""
    matches = [
        item for item in manifest if (item.scenario_id == run.scenario_id and item.version == run.scenario_version)
    ]
    if len(matches) != 1:
        raise ValueError("run does not identify exactly one manifest scenario")
    scenario = matches[0]
    if run.tier != scenario.tier or run.scope != scenario.scope:
        raise ValueError("run tier/scope bindings do not match the manifest")
    if run.evaluator_digest != scenario.evaluator_digest:
        raise ValueError("run evaluator digest mismatch")
    if run.protocol_violations:
        raise ValueError("run contains protocol violations")


def _wilson(numerator: int, denominator: int) -> tuple[float, float] | None:
    if denominator == 0:
        return None
    z = 1.959963984540054
    proportion = numerator / denominator
    scale = 1 + z * z / denominator
    center = (proportion + z * z / (2 * denominator)) / scale
    margin = z * math.sqrt(proportion * (1 - proportion) / denominator + z * z / (4 * denominator**2)) / scale
    return (max(0.0, center - margin), min(1.0, center + margin))


def build_scorecard(
    runs: Sequence[QualificationRun],
    judgements: Sequence[ScenarioJudgement],
) -> OutcomeScorecard:
    """Build raw-count-first metrics; incomplete and unavailable evidence fail closed."""
    by_id = {run.run_id: run for run in runs}
    if len(by_id) != len(runs):
        raise ValueError("run IDs must be unique")
    for judgement in judgements:
        run = by_id.get(judgement.run_id)
        if run is None or judgement.scenario_id != run.scenario_id:
            raise ValueError("judgement does not bind to its run and scenario")
        if judgement.reviewer_id == run.producer_id:
            raise ValueError("the producing agent cannot judge its own run")
    first_runs: dict[str, QualificationRun] = {}
    for run in sorted(runs, key=lambda item: (item.scenario_id, item.attempt, item.run_id)):
        first_runs.setdefault(run.scenario_id, run)
    completed = {run.run_id for run in first_runs.values() if run.terminal_status == "completed"}
    included = [item for item in judgements if item.run_id in completed]
    by_run: dict[str, list[ScenarioJudgement]] = {run_id: [] for run_id in completed}
    for judgement in included:
        by_run[judgement.run_id].append(judgement)
    reviewed_runs = {
        run_id: items
        for run_id, items in by_run.items()
        if items and all(item.independently_reviewed for item in items)
    }
    counts = {
        "first_pass_success": {
            "numerator": sum(
                bool(items)
                and all(
                    item.outcome == "pass"
                    and item.first_attempt
                    and item.reviewer_corrections == 0
                    for item in items
                )
                for items in by_run.values()
            ),
            "denominator": len(completed),
        },
        "review_rework": {
            "numerator": sum(
                item.reviewer_corrections
                for items in reviewed_runs.values()
                for item in items
            ),
            "denominator": len(reviewed_runs),
        },
        "review_rework_scenarios": {
            "numerator": sum(
                any(item.reviewer_corrections > 0 for item in items)
                for items in reviewed_runs.values()
            ),
            "denominator": len(reviewed_runs),
        },
        "unsupported_claims": {
            "numerator": sum(item.unsupported_claim for item in included),
            "denominator": sum(item.material_claims for item in included),
        },
        "escaped_defects": {
            "numerator": sum(item.escaped_defect for item in included),
            "denominator": sum(item.detectable_defects for item in included),
        },
        "false_positives": {
            "numerator": sum(item.false_positive for item in included),
            "denominator": sum(item.emitted_findings for item in included),
        },
        "unavailable_evidence": {
            "numerator": sum(
                item.applicable_evidence_requirements for item in included if item.outcome == "unavailable"
            ),
            "denominator": sum(item.applicable_evidence_requirements for item in included),
        },
    }
    exclusions = tuple(
        {"reason": f"terminal_status:{run.terminal_status}", "run_id": run.run_id}
        for run in first_runs.values()
        if run.terminal_status != "completed"
    )
    cohort_fields = (
        "control_version",
        "tier",
        "scope",
        "agent_family",
        "host",
        "transport",
    )
    cohort_keys = {
        field: next(iter(values)) if len(values := {getattr(run, field) for run in runs}) == 1 else "mixed"
        for field in cohort_fields
    }
    intervals = {key: _wilson(value["numerator"], value["denominator"]) for key, value in counts.items()}
    ceremony = {
        metric: tuple(
            sum(getattr(item, field) for item in items)
            for _, items in sorted(by_run.items())
        )
        for metric, field in (
            ("wall_seconds", "ceremony_wall_seconds"),
            ("calls", "ceremony_calls"),
            ("prompts", "ceremony_prompts"),
            ("evidence_items", "ceremony_evidence_items"),
            ("reviewer_actions", "ceremony_reviewer_actions"),
        )
    }
    critical = tuple(
        sorted(
            item.judgement_id
            for item in included
            if item.defect_severity == "critical"
            and (item.outcome != "pass" or item.escaped_defect or item.false_positive)
        )
    )
    return OutcomeScorecard(
        cohort_keys=cohort_keys,
        counts=counts,
        confidence_intervals=intervals,
        exclusions=exclusions,
        ceremony_samples=ceremony,
        judgement_ids=tuple(sorted(item.judgement_id for item in included)),
        critical_failures=critical,
    )


def compare_cohorts(
    candidate: OutcomeScorecard,
    baseline: OutcomeScorecard,
) -> OutcomeComparison:
    """Compare like scorecards without a composite score or critical-failure averaging."""
    deltas: dict[str, float | None] = {}
    regressions: list[str] = []
    cohorts_match = candidate.cohort_keys == baseline.cohort_keys
    if not cohorts_match:
        regressions.append("cohort:mismatch")
    higher_is_better = {"first_pass_success"}
    for metric in sorted(set(candidate.counts).intersection(baseline.counts)):
        candidate_rate = candidate.rate(metric)
        baseline_rate = baseline.rate(metric)
        if candidate_rate is None or baseline_rate is None:
            deltas[metric] = None
            regressions.append(f"{metric}:missing_denominator")
            continue
        delta = candidate_rate - baseline_rate
        deltas[metric] = delta
        if (metric in higher_is_better and delta < 0) or (metric not in higher_is_better and delta > 0):
            regressions.append(metric)
    critical = tuple(sorted(set(candidate.critical_failures)))
    comparable = (
        cohorts_match
        and not critical
        and not any(value is None for value in deltas.values())
    )
    return OutcomeComparison(
        candidate_cohort=candidate.cohort_keys,
        baseline_cohort=baseline.cohort_keys,
        metric_deltas=MappingProxyType(deltas),
        regressions=tuple(regressions),
        critical_failures=critical,
        comparable=comparable,
    )


def render_scorecard_markdown(scorecard: OutcomeScorecard) -> str:
    """Render critical outcomes before rates and ceremony navigation summaries."""
    lines = ["# Outcome scorecard", "", "## Critical failures"]
    lines.extend(f"- `{item}`" for item in scorecard.critical_failures)
    if not scorecard.critical_failures:
        lines.append("- None observed in this scorecard.")
    lines.extend(["", "## Metrics", "", "| Metric | Numerator | Denominator | Rate |", "| --- | ---: | ---: | ---: |"])
    for metric, counts in scorecard.counts.items():
        rate = scorecard.rate(metric)
        lines.append(
            f"| {metric} | {counts['numerator']} | {counts['denominator']} | "
            f"{'unavailable' if rate is None else f'{rate:.3f}'} |"
        )
    lines.extend(["", "## Ceremony"])
    for metric, samples in scorecard.ceremony_samples.items():
        if samples:
            ordered = sorted(samples)
            p90 = ordered[max(0, math.ceil(len(ordered) * 0.9) - 1)]
            lines.append(f"- {metric}: median {median(samples):g}; p90 {p90}; n={len(samples)}")
        else:
            lines.append(f"- {metric}: unavailable; n=0")
    return "\n".join(lines) + "\n"
