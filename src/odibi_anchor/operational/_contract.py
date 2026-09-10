"""Strict, bounded contracts shared by operational evidence collectors."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal

SCHEMA_VERSION = 1
EVIDENCE_INTAKE_SCHEMA_VERSION = 1
MAX_DEPTH = 24
MAX_STRING_CHARS = 16_384
MAX_ITEMS = 1_000
MAX_PAYLOAD_BYTES = 1_048_576
CollectorStatus = Literal["collected", "unavailable", "denied", "failed"]
AcquisitionOutcome = Literal["succeeded", "unavailable", "denied", "error"]
Completeness = Literal["complete", "partial", "unknown", "not_applicable"]
Assessment = Literal["pass", "fail", "unknown"]
ExecutionClass = Literal["managed", "delegated"]
ProviderBinding = Literal["caller_pinned", "fixture_supplied", "not_applicable"]
ProvenanceKind = Literal["mechanical", "attested"]
EpistemicClass = Literal["direct_observation", "inference", "attestation"]
ClaimPolarity = Literal["presence", "absence"]

_ALLOWED_EFFECTS = frozenset({"read", "artifact_write", "source_write", "data_write", "external_mutation"})


class ContractError(ValueError):
    """A value cannot safely be represented by the evidence contract."""


def utc_now() -> str:
    """Return an aware UTC timestamp in the canonical JSON spelling."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_json(value: Any, *, _depth: int = 0) -> Any:
    """Normalize explicit JSON containers; never stringify arbitrary objects."""
    if _depth > MAX_DEPTH:
        raise ContractError(f"JSON nesting exceeds {MAX_DEPTH}")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ContractError("JSON numbers must be finite")
        return value
    if type(value) is str:
        if len(value) > MAX_STRING_CHARS:
            raise ContractError(f"string exceeds {MAX_STRING_CHARS} characters")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_ITEMS:
            raise ContractError(f"object exceeds {MAX_ITEMS} members")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ContractError("JSON object keys must be strings")
            result[key] = normalize_json(item, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise ContractError(f"array exceeds {MAX_ITEMS} items")
        return [normalize_json(item, _depth=_depth + 1) for item in value]
    raise ContractError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Encode deterministic strict UTF-8 JSON and enforce the payload bound."""
    data = json.dumps(normalize_json(value), ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ContractError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return data


def _freeze_json(value: Any) -> Any:
    """Detach strict JSON data and recursively expose immutable containers."""
    normalized = normalize_json(value)
    if isinstance(normalized, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in normalized.items()})
    if isinstance(normalized, list):
        return tuple(_freeze_json(item) for item in normalized)
    return normalized


def _thaw_json(value: Any) -> Any:
    """Return detached JSON containers from recursively frozen contract data."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_mapping(name: str, value: Mapping[str, Any], *, required: bool = False) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be a mapping")
    frozen = _freeze_json(value)
    if required and not frozen:
        raise ContractError(f"{name} must be non-empty")
    return frozen


def _string_tuple(name: str, value: tuple[str, ...], *, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"{name} must contain non-empty strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ContractError(f"{name} must not contain duplicates")
    if allowed is not None and any(item not in allowed for item in result):
        raise ContractError(f"{name} contains an unsupported value")
    return result


def _non_empty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")


def _utc_timestamp(name: str, value: str) -> datetime:
    _non_empty(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"{name} must use UTC")
    return parsed


@dataclass(frozen=True)
class CollectorResult:
    """Versioned, provider-neutral result from one evidence channel."""

    collector: str
    status: CollectorStatus
    source: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, Any] = field(default_factory=dict)
    facts: Mapping[str, Any] = field(default_factory=dict)
    findings: tuple[Mapping[str, Any], ...] = ()
    limitations: tuple[str, ...] = ()
    error: Mapping[str, str] | None = None
    redaction: Mapping[str, Any] = field(default_factory=lambda: {"categories": [], "count": 0})
    observed_at: str = field(default_factory=utc_now)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError(f"unsupported schema_version: {self.schema_version}")
        if not self.collector.strip():
            raise ContractError("collector must be non-empty")
        if self.status not in ("collected", "unavailable", "denied", "failed"):
            raise ContractError(f"invalid collector status: {self.status}")
        if self.status != "collected" and self.findings:
            raise ContractError("only collected results may contain affirmative findings")
        try:
            parsed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("observed_at must be ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ContractError("observed_at must use UTC")
        normalize_json(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached strict-JSON representation."""
        return normalize_json(asdict(self))


@dataclass(frozen=True)
class CapabilityRequest:
    """A versioned, decision-relevant request for one evidence capability."""

    request_id: str
    capability_id: str
    capability_contract_version: str
    question: str
    target: Mapping[str, Any]
    scope: Mapping[str, Any]
    required_coverage: Mapping[str, Any]
    freshness_requirement: Mapping[str, Any]
    exclusions: tuple[str, ...] = ()
    resource_constraints: Mapping[str, Any] = field(default_factory=dict)
    allowed_effects: tuple[str, ...] = ("read",)
    partial_evidence_useful: bool = False
    pinned_provider_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "capability_id", "capability_contract_version", "question"):
            _non_empty(name, getattr(self, name))
        if self.pinned_provider_id is not None:
            _non_empty("pinned_provider_id", self.pinned_provider_id)
        if type(self.partial_evidence_useful) is not bool:
            raise ContractError("partial_evidence_useful must be boolean")
        for name in ("target", "scope", "required_coverage", "freshness_requirement"):
            object.__setattr__(self, name, _freeze_mapping(name, getattr(self, name), required=True))
        object.__setattr__(self, "resource_constraints", _freeze_mapping(
            "resource_constraints", self.resource_constraints,
        ))
        object.__setattr__(self, "exclusions", _string_tuple("exclusions", self.exclusions))
        object.__setattr__(self, "allowed_effects", _string_tuple(
            "allowed_effects", self.allowed_effects, allowed=_ALLOWED_EFFECTS,
        ))

    def to_dict(self) -> dict[str, Any]:
        return normalize_json({
            "request_id": self.request_id,
            "capability_id": self.capability_id,
            "capability_contract_version": self.capability_contract_version,
            "question": self.question,
            "target": _thaw_json(self.target),
            "scope": _thaw_json(self.scope),
            "required_coverage": _thaw_json(self.required_coverage),
            "freshness_requirement": _thaw_json(self.freshness_requirement),
            "exclusions": list(self.exclusions),
            "resource_constraints": _thaw_json(self.resource_constraints),
            "allowed_effects": list(self.allowed_effects),
            "partial_evidence_useful": self.partial_evidence_useful,
            "pinned_provider_id": self.pinned_provider_id,
        })


@dataclass(frozen=True)
class ProviderDeclaration:
    """Capability-scoped declaration supplied without provider discovery."""

    provider_id: str
    implementation_version: str
    capability_id: str
    supported_contract_versions: tuple[str, ...]
    execution_class: ExecutionClass
    environment_requirements: Mapping[str, Any] = field(default_factory=dict)
    network_requirement: Literal["none", "optional", "required"] = "none"
    credential_requirements: Mapping[str, Any] = field(default_factory=dict)
    required_effects: tuple[str, ...] = ("read",)
    coverage_semantics: Literal["exhaustive", "bounded", "best_effort", "unknown"] = "unknown"
    limitations: tuple[str, ...] = ()
    maturity: str = "experimental"

    def __post_init__(self) -> None:
        for name in ("provider_id", "implementation_version", "capability_id", "maturity"):
            _non_empty(name, getattr(self, name))
        object.__setattr__(self, "supported_contract_versions", _string_tuple(
            "supported_contract_versions", self.supported_contract_versions,
        ))
        if not self.supported_contract_versions:
            raise ContractError("supported_contract_versions must be non-empty")
        if self.execution_class not in {"managed", "delegated"}:
            raise ContractError("invalid execution_class")
        if self.network_requirement not in {"none", "optional", "required"}:
            raise ContractError("invalid network_requirement")
        if self.coverage_semantics not in {"exhaustive", "bounded", "best_effort", "unknown"}:
            raise ContractError("invalid coverage_semantics")
        object.__setattr__(self, "environment_requirements", _freeze_mapping(
            "environment_requirements", self.environment_requirements,
        ))
        object.__setattr__(self, "credential_requirements", _freeze_mapping(
            "credential_requirements", self.credential_requirements,
        ))
        object.__setattr__(self, "required_effects", _string_tuple(
            "required_effects", self.required_effects, allowed=_ALLOWED_EFFECTS,
        ))
        object.__setattr__(self, "limitations", _string_tuple("limitations", self.limitations))

    def to_dict(self) -> dict[str, Any]:
        return normalize_json({
            "provider_id": self.provider_id,
            "implementation_version": self.implementation_version,
            "capability_id": self.capability_id,
            "supported_contract_versions": list(self.supported_contract_versions),
            "execution_class": self.execution_class,
            "environment_requirements": _thaw_json(self.environment_requirements),
            "network_requirement": self.network_requirement,
            "credential_requirements": _thaw_json(self.credential_requirements),
            "required_effects": list(self.required_effects),
            "coverage_semantics": self.coverage_semantics,
            "limitations": list(self.limitations),
            "maturity": self.maturity,
        })


@dataclass(frozen=True)
class CollectionAttempt:
    """One managed, delegated, denied, or unavailable acquisition attempt."""

    attempt_id: str
    request_id: str
    provider_id: str | None
    provider_binding: ProviderBinding
    provenance_kind: ProvenanceKind
    acquisition_outcome: AcquisitionOutcome
    completeness: Completeness
    started_at: str
    completed_at: str
    executor_identity: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)
    additional_exclusions: tuple[str, ...] = ()
    truncated: bool = False
    source_identity: Mapping[str, Any] = field(default_factory=dict)
    environment_identity: Mapping[str, Any] = field(default_factory=dict)
    raw_artifact_ref: Mapping[str, Any] | None = None
    limitations: tuple[str, ...] = ()
    error: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        for name in ("attempt_id", "request_id"):
            _non_empty(name, getattr(self, name))
        if self.provider_id is not None:
            _non_empty("provider_id", self.provider_id)
        if self.provider_binding not in {"caller_pinned", "fixture_supplied", "not_applicable"}:
            raise ContractError("invalid provider_binding")
        if self.provenance_kind not in {"mechanical", "attested"}:
            raise ContractError("invalid provenance_kind")
        if self.acquisition_outcome not in {"succeeded", "unavailable", "denied", "error"}:
            raise ContractError("invalid acquisition_outcome")
        if self.completeness not in {"complete", "partial", "unknown", "not_applicable"}:
            raise ContractError("invalid completeness")
        if type(self.truncated) is not bool:
            raise ContractError("truncated must be boolean")
        started = _utc_timestamp("started_at", self.started_at)
        completed = _utc_timestamp("completed_at", self.completed_at)
        if completed < started:
            raise ContractError("completed_at must not precede started_at")
        if self.provider_binding == "not_applicable" and self.provider_id is not None:
            raise ContractError("not_applicable provider binding cannot identify a provider")
        if self.provider_binding != "not_applicable" and self.provider_id is None:
            raise ContractError("provider binding requires provider_id")
        if self.acquisition_outcome in {"succeeded", "error"} and self.completeness == "not_applicable":
            raise ContractError("executed attempts require an applicable completeness")
        if self.acquisition_outcome in {"unavailable", "denied"} and self.completeness != "not_applicable":
            raise ContractError("unavailable and denied attempts require not_applicable completeness")
        if self.completeness == "complete" and (self.truncated or self.additional_exclusions):
            raise ContractError("complete attempts cannot be truncated or add exclusions")
        object.__setattr__(self, "additional_exclusions", _string_tuple(
            "additional_exclusions", self.additional_exclusions,
        ))
        object.__setattr__(self, "limitations", _string_tuple("limitations", self.limitations))
        if self.truncated and not self.limitations:
            raise ContractError("truncated attempts require a limitation")
        for name in ("executor_identity", "coverage", "limits", "source_identity", "environment_identity"):
            object.__setattr__(self, name, _freeze_mapping(name, getattr(self, name)))
        if self.raw_artifact_ref is not None:
            object.__setattr__(self, "raw_artifact_ref", _freeze_mapping(
                "raw_artifact_ref", self.raw_artifact_ref, required=True,
            ))
        if self.error is not None:
            if not isinstance(self.error, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in self.error.items()
            ):
                raise ContractError("error must map strings to strings")
            object.__setattr__(self, "error", _freeze_mapping("error", self.error, required=True))

    def to_dict(self) -> dict[str, Any]:
        return normalize_json({
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "provider_binding": self.provider_binding,
            "provenance_kind": self.provenance_kind,
            "acquisition_outcome": self.acquisition_outcome,
            "completeness": self.completeness,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "executor_identity": _thaw_json(self.executor_identity),
            "coverage": _thaw_json(self.coverage),
            "limits": _thaw_json(self.limits),
            "additional_exclusions": list(self.additional_exclusions),
            "truncated": self.truncated,
            "source_identity": _thaw_json(self.source_identity),
            "environment_identity": _thaw_json(self.environment_identity),
            "raw_artifact_ref": _thaw_json(self.raw_artifact_ref),
            "limitations": list(self.limitations),
            "error": _thaw_json(self.error),
        })


@dataclass(frozen=True)
class EvidenceClaim:
    """One auditable direct observation, inference, or attestation."""

    claim_id: str
    attempt_id: str
    statement: str
    polarity: ClaimPolarity
    epistemic_class: EpistemicClass
    source_locator: Mapping[str, Any]
    coverage_basis: Mapping[str, Any]
    freshness_anchor: Mapping[str, Any]
    derivation: str | None = None
    contradictions: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("claim_id", "attempt_id", "statement"):
            _non_empty(name, getattr(self, name))
        if self.polarity not in {"presence", "absence"}:
            raise ContractError("invalid claim polarity")
        if self.epistemic_class not in {"direct_observation", "inference", "attestation"}:
            raise ContractError("invalid epistemic_class")
        if self.derivation is not None:
            _non_empty("derivation", self.derivation)
        if self.epistemic_class == "inference" and self.derivation is None:
            raise ContractError("inference claims require derivation")
        for name in ("source_locator", "coverage_basis", "freshness_anchor"):
            object.__setattr__(self, name, _freeze_mapping(name, getattr(self, name), required=True))
        object.__setattr__(self, "provider_metadata", _freeze_mapping(
            "provider_metadata", self.provider_metadata,
        ))
        object.__setattr__(self, "contradictions", _string_tuple("contradictions", self.contradictions))
        object.__setattr__(self, "uncertainty", _string_tuple("uncertainty", self.uncertainty))

    def to_dict(self) -> dict[str, Any]:
        return normalize_json({
            "claim_id": self.claim_id,
            "attempt_id": self.attempt_id,
            "statement": self.statement,
            "polarity": self.polarity,
            "epistemic_class": self.epistemic_class,
            "source_locator": _thaw_json(self.source_locator),
            "coverage_basis": _thaw_json(self.coverage_basis),
            "freshness_anchor": _thaw_json(self.freshness_anchor),
            "derivation": self.derivation,
            "contradictions": list(self.contradictions),
            "uncertainty": list(self.uncertainty),
            "provider_metadata": _thaw_json(self.provider_metadata),
        })


@dataclass(frozen=True)
class EvidenceIntake:
    """Immutable envelope for one request, one attempt, and its claims."""

    request: CapabilityRequest
    attempt: CollectionAttempt
    assessment: Assessment
    provider: ProviderDeclaration | None = None
    claims: tuple[EvidenceClaim, ...] = ()
    assessment_basis: tuple[str, ...] = ()
    schema_version: int = EVIDENCE_INTAKE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != EVIDENCE_INTAKE_SCHEMA_VERSION:
            raise ContractError(f"unsupported evidence intake schema_version: {self.schema_version}")
        if not isinstance(self.request, CapabilityRequest) or not isinstance(self.attempt, CollectionAttempt):
            raise ContractError("request and attempt must use evidence contract types")
        if self.provider is not None and not isinstance(self.provider, ProviderDeclaration):
            raise ContractError("provider must use ProviderDeclaration")
        if not isinstance(self.claims, (tuple, list)) or any(
            not isinstance(claim, EvidenceClaim) for claim in self.claims
        ):
            raise ContractError("claims must contain EvidenceClaim values")
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "assessment_basis", _string_tuple(
            "assessment_basis", self.assessment_basis,
        ))
        if self.assessment not in {"pass", "fail", "unknown"}:
            raise ContractError("invalid assessment")
        if self.attempt.request_id != self.request.request_id:
            raise ContractError("attempt request_id does not match request")
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ContractError("claim IDs must be unique")
        if any(claim.attempt_id != self.attempt.attempt_id for claim in self.claims):
            raise ContractError("claim attempt_id does not match attempt")
        if any(claim_id not in claim_ids for claim_id in self.assessment_basis):
            raise ContractError("assessment_basis must reference intake claims")
        if self.assessment in {"pass", "fail"} and not self.assessment_basis:
            raise ContractError("pass and fail assessments require claim evidence")
        if self.attempt.acquisition_outcome in {"unavailable", "denied"} and self.claims:
            raise ContractError("unavailable and denied attempts cannot contain claims")
        if self.request.pinned_provider_id is not None and (
            self.attempt.provider_id != self.request.pinned_provider_id
            or self.attempt.provider_binding != "caller_pinned"
        ):
            raise ContractError("attempt does not honor the pinned provider")
        if self.request.pinned_provider_id is None and self.attempt.provider_binding == "caller_pinned":
            raise ContractError("caller_pinned binding requires a pinned provider request")
        if self.attempt.provider_binding == "not_applicable" and self.provider is not None:
            raise ContractError("not_applicable binding cannot include a provider declaration")
        if self.provider is not None:
            if self.provider.provider_id != self.attempt.provider_id:
                raise ContractError("provider declaration does not match attempt")
            if self.provider.capability_id != self.request.capability_id:
                raise ContractError("provider capability does not match request")
            if self.attempt.acquisition_outcome in {"succeeded", "error"}:
                if self.request.capability_contract_version not in self.provider.supported_contract_versions:
                    raise ContractError("provider does not support the capability contract version")
                if not set(self.provider.required_effects).issubset(self.request.allowed_effects):
                    raise ContractError("provider requires effects not allowed by the request")
                expected_provenance = "mechanical" if self.provider.execution_class == "managed" else "attested"
                if self.attempt.provenance_kind != expected_provenance:
                    raise ContractError("attempt provenance does not match provider execution class")
                if self.provider.execution_class == "delegated" and not self.attempt.executor_identity:
                    raise ContractError("delegated attempts require executor_identity")
        elif self.attempt.acquisition_outcome in {"succeeded", "error"}:
            raise ContractError("executed attempts require a provider declaration")
        for claim in self.claims:
            if claim.polarity == "absence" and (
                self.provider is None
                or self.provider.coverage_semantics != "exhaustive"
                or self.attempt.completeness != "complete"
                or self.attempt.truncated
                or self.attempt.additional_exclusions
            ):
                raise ContractError("absence claims require exhaustive, complete coverage")
        canonical_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return normalize_json({
            "schema_version": self.schema_version,
            "request": self.request.to_dict(),
            "provider": self.provider.to_dict() if self.provider is not None else None,
            "attempt": self.attempt.to_dict(),
            "claims": [claim.to_dict() for claim in self.claims],
            "assessment": self.assessment,
            "assessment_basis": list(self.assessment_basis),
        })
