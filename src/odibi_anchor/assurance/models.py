"""Frozen, strictly serialized contracts for the assurance kernel."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, cast

AssuranceTier = Literal["T0", "T1", "T2", "T3"]
QualityAttribute = Literal[
    "functional-suitability",
    "reliability",
    "performance-efficiency",
    "compatibility",
    "interaction-quality",
    "security",
    "maintainability",
    "adaptability",
    "safety",
]
Applicability = Literal["applicable", "not_applicable"]
EvidenceState = Literal["satisfied", "failed", "missing", "unavailable", "stale"]
ControlDisposition = Literal["advisory", "warning", "blocking"]
ExceptionStatus = Literal["active", "expired", "revoked"]

ASSURANCE_SCHEMA_VERSION = "1.0"
CORE_CATALOG_VERSION = "anchor-assurance-core/1.1"
LEGACY_CONTROL_IDS = ("AK-001", "AK-002", "AK-003", "AK-004", "AK-005", "AK-006")
T1_CONTROL_IDS = (
    "Anchor-T1-TESTS",
    "Anchor-T1-STATIC-RATCHET",
    "Anchor-T1-OUTPUT-CONTRACT",
    "Anchor-T1-DISTRIBUTION",
    "Anchor-T1-SCOPE-INTEGRITY",
)
CONTROL_IDS = LEGACY_CONTROL_IDS + T1_CONTROL_IDS
QUALITY_ATTRIBUTE_ORDER: tuple[QualityAttribute, ...] = (
    "functional-suitability",
    "reliability",
    "safety",
    "maintainability",
    "adaptability",
    "performance-efficiency",
    "compatibility",
    "security",
    "interaction-quality",
)
_TIERS = frozenset({"T0", "T1", "T2", "T3"})
_APPLICABILITY = frozenset({"applicable", "not_applicable"})
_EVIDENCE_STATES = frozenset({"satisfied", "failed", "missing", "unavailable", "stale"})
_DISPOSITIONS = frozenset({"advisory", "warning", "blocking"})
_EXCEPTION_STATUSES = frozenset({"active", "expired", "revoked"})


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} payload must be a mapping")
    missing = expected.difference(value)
    if missing:
        raise ValueError(f"Missing {label} fields: {sorted(missing)}")
    extra = set(value).difference(expected)
    if extra:
        raise ValueError(f"Unexpected {label} fields: {sorted(extra)}")


def _json_array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array")
    return value


def utc_datetime(value: str, field: str = "timestamp") -> datetime:
    """Parse an explicitly UTC ISO-8601 timestamp."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty UTC timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field} must be UTC")
    return parsed


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


_DIGEST_PREFIX = "sha256:"
_MAX_RESULT_BYTES = 65_536
_MAX_CONTAINER_ITEMS = 256
_MAX_VALUE_DEPTH = 8
_MAX_STRING_LENGTH = 4_096


def _sha256_digest(value: Any, field: str) -> str:
    value = _nonempty(value, field)
    digest = value.removeprefix(_DIGEST_PREFIX)
    if not value.startswith(_DIGEST_PREFIX) or len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _freeze_result_value(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > _MAX_VALUE_DEPTH:
        raise ValueError(f"{field} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            raise ValueError(f"{field} contains an oversized string")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError(f"{field} contains too many mapping entries")
        if any(not isinstance(key, str) or not key for key in value):
            raise ValueError(f"{field} mapping keys must be non-empty strings")
        return MappingProxyType({
            key: _freeze_result_value(item, field=field, depth=depth + 1)
            for key, item in sorted(value.items())
        })
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError(f"{field} contains too many list entries")
        return tuple(
            _freeze_result_value(item, field=field, depth=depth + 1) for item in value
        )
    raise ValueError(f"{field} contains a non-JSON value")


def _thaw_result_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_result_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_result_value(item) for item in value]
    return value


@dataclass(frozen=True)
class CommandResultEvidence:
    """One immutable, subject-bound normalized command result."""

    evidence_id: str
    check_id: str
    state: EvidenceState
    observed_at: str
    completed_at: str
    subject_digest: str
    scope_digest: str
    producer: str
    producer_version: str
    result: Mapping[str, Any]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        for field in ("evidence_id", "check_id", "producer", "producer_version"):
            _nonempty(getattr(self, field), field)
        if self.state not in _EVIDENCE_STATES:
            raise ValueError(f"Unsupported evidence state: {self.state!r}")
        observed = utc_datetime(self.observed_at, "observed_at")
        completed = utc_datetime(self.completed_at, "completed_at")
        if completed < observed:
            raise ValueError("completed_at cannot be before observed_at")
        _sha256_digest(self.subject_digest, "subject_digest")
        _sha256_digest(self.scope_digest, "scope_digest")
        frozen_result = _freeze_result_value(self.result, field="result")
        frozen_provenance = _freeze_result_value(self.provenance, field="provenance")
        if not isinstance(frozen_result, Mapping) or not isinstance(frozen_provenance, Mapping):
            raise ValueError("result and provenance must be JSON objects")
        serialized = json.dumps(
            {"result": _thaw_result_value(frozen_result),
             "provenance": _thaw_result_value(frozen_provenance)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(serialized) > _MAX_RESULT_BYTES:
            raise ValueError("result and provenance exceed the bounded evidence size")
        object.__setattr__(self, "result", frozen_result)
        object.__setattr__(self, "provenance", frozen_provenance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "check_id": self.check_id,
            "state": self.state,
            "observed_at": self.observed_at,
            "completed_at": self.completed_at,
            "subject_digest": self.subject_digest,
            "scope_digest": self.scope_digest,
            "producer": self.producer,
            "producer_version": self.producer_version,
            "result": _thaw_result_value(self.result),
            "provenance": _thaw_result_value(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CommandResultEvidence:
        expected = {
            "evidence_id", "check_id", "state", "observed_at", "completed_at",
            "subject_digest", "scope_digest", "producer", "producer_version",
            "result", "provenance",
        }
        _exact_fields(value, expected, "CommandResultEvidence")
        return cls(**value)

    def is_fresh(self, now: datetime, max_age: timedelta) -> bool:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("now must be UTC")
        if max_age < timedelta(0):
            raise ValueError("max_age cannot be negative")
        return utc_datetime(self.completed_at, "completed_at") >= now - max_age


@dataclass(frozen=True)
class AssurancePlan:
    """Deterministic task assurance intent selected from a TaskProfile."""

    schema_version: str
    catalog_version: str
    tier: AssuranceTier
    quality_attributes: tuple[QualityAttribute, ...]
    controls: tuple[str, ...]
    rationale: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != ASSURANCE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported AssurancePlan schema_version: {self.schema_version!r}")
        if self.catalog_version != CORE_CATALOG_VERSION:
            raise ValueError(f"Unsupported assurance catalog_version: {self.catalog_version!r}")
        if self.tier not in _TIERS:
            raise ValueError(f"Unsupported assurance tier: {self.tier!r}")
        if not isinstance(self.quality_attributes, tuple) or not self.quality_attributes:
            raise ValueError("quality_attributes must be a non-empty tuple")
        canonical_attributes = tuple(
            attribute for attribute in QUALITY_ATTRIBUTE_ORDER
            if attribute in self.quality_attributes
        )
        if canonical_attributes != self.quality_attributes or len(set(self.quality_attributes)) != len(
            self.quality_attributes
        ):
            raise ValueError("quality_attributes must be unique and canonically ordered")
        if not isinstance(self.controls, tuple) or self.controls != tuple(sorted(set(self.controls))):
            raise ValueError("controls must be a sorted tuple without duplicates")
        unknown = set(self.controls).difference(CONTROL_IDS)
        if unknown:
            raise ValueError(f"Unknown assurance control: {sorted(unknown)}")
        if not isinstance(self.rationale, tuple) or self.rationale != tuple(sorted(set(self.rationale))):
            raise ValueError("rationale must be a sorted tuple of unique rule IDs")
        if any(not isinstance(item, str) or not item for item in self.rationale):
            raise ValueError("rationale rule IDs must be non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "tier": self.tier,
            "quality_attributes": list(self.quality_attributes),
            "controls": list(self.controls),
            "rationale": list(self.rationale),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AssurancePlan:
        expected = {
            "schema_version", "catalog_version", "tier", "quality_attributes", "controls", "rationale",
        }
        _exact_fields(value, expected, "AssurancePlan")
        attributes = _json_array(value["quality_attributes"], "quality_attributes")
        controls = _json_array(value["controls"], "controls")
        rationale = _json_array(value["rationale"], "rationale")
        return cls(
            schema_version=value["schema_version"],
            catalog_version=value["catalog_version"],
            tier=cast(AssuranceTier, value["tier"]),
            quality_attributes=cast(tuple[QualityAttribute, ...], tuple(attributes)),
            controls=tuple(controls),
            rationale=tuple(rationale),
        )


@dataclass(frozen=True)
class AssuranceException:
    """Time-bounded advisory exception data with no authorization semantics."""

    exception_id: str
    control_id: str
    reason: str
    approved_by: str
    created_at: str
    expires_at: str
    status: ExceptionStatus

    def __post_init__(self) -> None:
        for field in ("exception_id", "reason", "approved_by"):
            _nonempty(getattr(self, field), field)
        if self.control_id not in CONTROL_IDS:
            raise ValueError(f"Unknown assurance control: {self.control_id!r}")
        if self.status not in _EXCEPTION_STATUSES:
            raise ValueError(f"Unsupported exception status: {self.status!r}")
        created = utc_datetime(self.created_at, "created_at")
        expires = utc_datetime(self.expires_at, "expires_at")
        if created >= expires:
            raise ValueError("created_at must be before expires_at")

    def to_dict(self) -> dict[str, str]:
        return {
            "exception_id": self.exception_id,
            "control_id": self.control_id,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AssuranceException:
        expected = {
            "exception_id", "control_id", "reason", "approved_by", "created_at", "expires_at", "status",
        }
        _exact_fields(value, expected, "AssuranceException")
        return cls(**value)


@dataclass(frozen=True)
class ControlResult:
    """One catalog control's advisory evidence result."""

    control_id: str
    applicability: Applicability
    evidence_state: EvidenceState
    disposition: ControlDisposition
    evidence_ids: tuple[str, ...]
    exception_id: str | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.control_id not in CONTROL_IDS:
            raise ValueError(f"Unknown assurance control: {self.control_id!r}")
        if self.applicability not in _APPLICABILITY:
            raise ValueError(f"Unsupported applicability: {self.applicability!r}")
        if self.evidence_state not in _EVIDENCE_STATES:
            raise ValueError(f"Unsupported evidence state: {self.evidence_state!r}")
        if self.disposition not in _DISPOSITIONS:
            raise ValueError(f"Unsupported control disposition: {self.disposition!r}")
        if not isinstance(self.evidence_ids, tuple) or self.evidence_ids != tuple(
            sorted(set(self.evidence_ids))
        ):
            raise ValueError("evidence_ids must be a sorted tuple without duplicates")
        if any(not isinstance(item, str) or not item for item in self.evidence_ids):
            raise ValueError("evidence_ids must contain non-empty strings")
        if self.exception_id is not None:
            _nonempty(self.exception_id, "exception_id")
        if not isinstance(self.reasons, tuple) or not self.reasons or any(
            not isinstance(item, str) or not item for item in self.reasons
        ):
            raise ValueError("reasons must be a non-empty tuple of strings")
        if self.applicability == "not_applicable" and (self.evidence_ids or self.exception_id):
            raise ValueError("not-applicable controls cannot carry evidence or exceptions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "applicability": self.applicability,
            "evidence_state": self.evidence_state,
            "disposition": self.disposition,
            "evidence_ids": list(self.evidence_ids),
            "exception_id": self.exception_id,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControlResult:
        expected = {
            "control_id", "applicability", "evidence_state", "disposition", "evidence_ids",
            "exception_id", "reasons",
        }
        _exact_fields(value, expected, "ControlResult")
        evidence_ids = _json_array(value["evidence_ids"], "evidence_ids")
        reasons = _json_array(value["reasons"], "reasons")
        return cls(
            control_id=value["control_id"],
            applicability=cast(Applicability, value["applicability"]),
            evidence_state=cast(EvidenceState, value["evidence_state"]),
            disposition=cast(ControlDisposition, value["disposition"]),
            evidence_ids=tuple(evidence_ids),
            exception_id=value["exception_id"],
            reasons=tuple(reasons),
        )


@dataclass(frozen=True)
class AssuranceAssessment:
    """Complete six-control advisory assessment for one assurance plan."""

    schema_version: str
    mode: Literal["shadow"]
    plan: AssurancePlan
    results: tuple[ControlResult, ...]

    def __post_init__(self) -> None:
        if self.schema_version != ASSURANCE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported AssuranceAssessment schema_version: {self.schema_version!r}")
        if self.mode != "shadow":
            raise ValueError("AssuranceAssessment mode must be 'shadow'")
        if not isinstance(self.plan, AssurancePlan):
            raise TypeError("plan must be an AssurancePlan")
        if not isinstance(self.results, tuple) or tuple(
            result.control_id for result in self.results
        ) != LEGACY_CONTROL_IDS:
            raise ValueError("results must contain every legacy control in control-ID order")
        for result in self.results:
            expected = "applicable" if result.control_id in self.plan.controls else "not_applicable"
            if result.applicability != expected:
                raise ValueError("result applicability must match the plan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "plan": self.plan.to_dict(),
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AssuranceAssessment:
        expected = {"schema_version", "mode", "plan", "results"}
        _exact_fields(value, expected, "AssuranceAssessment")
        if not isinstance(value["plan"], Mapping):
            raise ValueError("plan must be a JSON object")
        results = _json_array(value["results"], "results")
        if any(not isinstance(result, Mapping) for result in results):
            raise ValueError("results must contain JSON objects")
        return cls(
            schema_version=value["schema_version"],
            mode=value["mode"],
            plan=AssurancePlan.from_dict(value["plan"]),
            results=tuple(ControlResult.from_dict(result) for result in results),
        )
