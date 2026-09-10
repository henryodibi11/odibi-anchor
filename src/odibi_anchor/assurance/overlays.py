"""Immutable standards-overlay metadata and pure structured selection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

OVERLAY_CATALOG_VERSION = "1.0.0"
OVERLAY_REFERENCE_ID = "assurance.standards-overlays"
OVERLAY_REFERENCE_VERSION = "1.0.0"
_SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")

OverlayEvidenceState = Literal[
    "satisfied", "failed", "unavailable", "not_assessed",
    "not_applicable", "excepted",
]


@dataclass(frozen=True)
class SourceDefinition:
    """Bibliographic source metadata; no source body is distributed."""

    source_id: str
    baseline: str
    url: str
    license_boundary: str
    captured_on: str
    refresh_triggers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.source_id or not self.baseline or not self.license_boundary:
            raise ValueError("source metadata must be non-empty")
        if not self.url.startswith("https://"):
            raise ValueError("source URL must use HTTPS")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.captured_on):
            raise ValueError("captured_on must be an ISO date")
        if not isinstance(self.refresh_triggers, tuple) or not self.refresh_triggers:
            raise ValueError("source refresh triggers must be a non-empty tuple")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "baseline": self.baseline,
            "url": self.url,
            "license_boundary": self.license_boundary,
            "captured_on": self.captured_on,
            "refresh_triggers": list(self.refresh_triggers),
        }


@dataclass(frozen=True)
class ApplicabilityPredicate:
    """Closed structured predicate with no free-text inputs."""

    domains_any: tuple[str, ...] = ()
    traits_any: tuple[str, ...] = ()
    effects_any: tuple[str, ...] = ()
    path_classes_any: tuple[str, ...] = ()
    require_domain_match: bool = False
    require_tier: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.domains_any, self.traits_any, self.effects_any, self.path_classes_any,
        ):
            if not isinstance(value, tuple) or value != tuple(sorted(set(value))):
                raise ValueError("predicate values must be sorted tuples without duplicates")
        if self.require_tier not in {None, "T3"}:
            raise ValueError("unsupported overlay tier predicate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "domains_any": list(self.domains_any),
            "traits_any": list(self.traits_any),
            "effects_any": list(self.effects_any),
            "path_classes_any": list(self.path_classes_any),
            "require_domain_match": self.require_domain_match,
            "require_tier": self.require_tier,
        }


@dataclass(frozen=True)
class OverlayDefinition:
    overlay_id: str
    version: str
    title: str
    predicate: ApplicabilityPredicate
    control_ids: tuple[str, ...]
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.overlay_id or not self.title or not _SEMVER.fullmatch(self.version):
            raise ValueError("overlay identity and semver version are required")
        if not isinstance(self.predicate, ApplicabilityPredicate):
            raise TypeError("overlay predicate must be structured")
        for field in (self.control_ids, self.source_ids):
            if not isinstance(field, tuple) or not field or field != tuple(sorted(set(field))):
                raise ValueError("overlay IDs must be non-empty sorted tuples")

    def to_dict(self) -> dict[str, Any]:
        return {
            "overlay_id": self.overlay_id,
            "version": self.version,
            "title": self.title,
            "predicate": self.predicate.to_dict(),
            "control_ids": list(self.control_ids),
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class OverlayControl:
    control_id: str
    version: str
    overlay_id: str
    intent: str
    required_evidence: tuple[str, ...]
    source_ids: tuple[str, ...]
    unavailable_disposition: Literal["open"] = "open"
    disposition: Literal["advisory"] = "advisory"

    def __post_init__(self) -> None:
        if not self.control_id or not self.overlay_id or not self.intent:
            raise ValueError("overlay control identity and intent are required")
        if not _SEMVER.fullmatch(self.version):
            raise ValueError("overlay control version must be semver")
        for field in (self.required_evidence, self.source_ids):
            if not isinstance(field, tuple) or not field or field != tuple(sorted(set(field))):
                raise ValueError("overlay evidence and source IDs must be non-empty sorted tuples")
        if self.unavailable_disposition != "open" or self.disposition != "advisory":
            raise ValueError("overlay controls are open and advisory in shadow mode")

    @property
    def accepted_evidence(self) -> tuple[str, ...]:
        return self.required_evidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "version": self.version,
            "overlay_id": self.overlay_id,
            "intent": self.intent,
            "required_evidence": list(self.required_evidence),
            "unavailable_disposition": self.unavailable_disposition,
            "source_ids": list(self.source_ids),
            "disposition": self.disposition,
        }


_DOMAIN_VALUES = frozenset({
    "general", "code", "python", "application", "web", "api", "identity", "data",
    "analytics", "ai", "ml", "agent", "frontend", "user_interface", "documentation",
    "operations", "reliability",
})
_TRAIT_VALUES = frozenset({
    "implementation", "handles_secrets", "handles_personal_data", "authentication",
    "authorization", "untrusted_input", "network_exposed", "cryptography", "data_contract",
    "data_transform", "data_join", "data_write", "schema_change", "personal_data",
    "model_inference", "prompt_or_context_change", "tool_calling", "retrieval",
    "autonomous_action", "human_facing_ai_output", "sensitive_model_input",
    "user_facing_interaction", "visual_content", "audio_video", "document_export",
    "new_component", "component_boundary_change", "public_contract_change",
    "persistence_change", "deployment_topology_change", "cross_system_dependency",
    "production_runtime", "scheduled_work", "service_level", "operational_dependency",
    "on_call_change", "capacity_change", "dependency_change", "build_change",
    "release_change", "artifact_production", "third_party_action", "safety_critical",
    "regulated_process", "irreversible_change", "hazardous_process", "large_blast_radius",
    "control_logic_change",
})
_EFFECT_VALUES = frozenset({
    "external_publish", "permission_change", "secret_change", "data_mutation",
    "schema_mutation", "infrastructure_mutation", "deployment", "production_configuration",
    "release", "production_data_destructive", "schema_destructive",
})
_PATH_CLASS_VALUES = frozenset({
    "auth_boundary", "security_policy", "request_handler", "secret_configuration", "schema",
    "model", "migration", "pipeline", "quality_rule", "prompt", "model_config",
    "agent_policy", "tool_boundary", "retrieval_policy", "ui_component", "style", "template",
    "user_document", "architecture_description", "public_api", "infrastructure", "deployment",
    "observability", "runbook", "scheduler", "service_configuration", "dependency_manifest",
    "lockfile", "build_pipeline", "release_pipeline", "artifact_manifest",
})
_CAPABILITY_VALUES = frozenset({
    "automated_accessibility", "manual_accessibility", "security_scanner",
    "vulnerability_scanner", "license_scanner", "telemetry", "simulation",
})


@dataclass(frozen=True)
class OverlayInput:
    """Normalized facts accepted by overlay predicates."""

    work_type: str
    execution_mode: str
    assurance_tier: str
    domains: frozenset[str] = frozenset()
    traits: frozenset[str] = frozenset()
    declared_effects: frozenset[str] = frozenset()
    quality_attributes: frozenset[str] = frozenset()
    changed_path_classes: frozenset[str] = frozenset()
    repository_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.work_type not in {"change", "investigate", "operate", "document", "review"}:
            raise ValueError(f"unsupported overlay work_type: {self.work_type!r}")
        if self.execution_mode not in {"read_only", "artifact_only", "source_change", "data_change"}:
            raise ValueError(f"unsupported overlay execution_mode: {self.execution_mode!r}")
        if self.assurance_tier not in {"T0", "T1", "T2", "T3"}:
            raise ValueError(f"unsupported overlay assurance_tier: {self.assurance_tier!r}")
        fields = (
            ("domains", self.domains, _DOMAIN_VALUES),
            ("traits", self.traits, _TRAIT_VALUES),
            ("declared_effects", self.declared_effects, _EFFECT_VALUES),
            ("changed_path_classes", self.changed_path_classes, _PATH_CLASS_VALUES),
            ("repository_capabilities", self.repository_capabilities, _CAPABILITY_VALUES),
        )
        for name, value, allowed in fields:
            if not isinstance(value, frozenset):
                raise TypeError(f"{name} must be a frozenset")
            unknown = value.difference(allowed)
            if unknown:
                raise ValueError(f"unsupported {name}: {sorted(unknown)}")
        if not isinstance(self.quality_attributes, frozenset) or any(
            not isinstance(value, str) or not value for value in self.quality_attributes
        ):
            raise ValueError("quality_attributes must be a frozenset of non-empty strings")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OverlayInput:
        expected = {
            "work_type", "execution_mode", "assurance_tier", "domains", "traits",
            "declared_effects", "quality_attributes", "changed_path_classes",
            "repository_capabilities",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("OverlayInput requires the exact structured fields")
        set_fields = expected.difference({"work_type", "execution_mode", "assurance_tier"})
        if any(not isinstance(value[field], list) for field in set_fields):
            raise ValueError("OverlayInput set fields must be JSON arrays")
        return cls(**{
            **{field: value[field] for field in ("work_type", "execution_mode", "assurance_tier")},
            **{field: frozenset(value[field]) for field in set_fields},
        })


@dataclass(frozen=True)
class OverlaySelectionItem:
    overlay_id: str
    version: str
    reason_codes: tuple[str, ...]
    control_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = {
            "overlay_id": self.overlay_id,
            "version": self.version,
            "reason_codes": list(self.reason_codes),
        }
        if self.control_ids:
            value["control_ids"] = list(self.control_ids)
        return value


@dataclass(frozen=True)
class OverlaySelection:
    catalog_version: str
    catalog_digest: str
    selected: tuple[OverlaySelectionItem, ...]
    not_selected: tuple[OverlaySelectionItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
            "selected": [item.to_dict() for item in self.selected],
            "not_selected": [item.to_dict() for item in self.not_selected],
        }


@dataclass(frozen=True)
class OverlayException:
    control_id: str
    control_version: str
    rationale: str
    owner: str
    scope_digest: str
    created_at: str
    expires_at: str
    compensating_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        controls = {item.control_id: item for item in OVERLAY_CONTROLS}
        if self.control_id not in controls or controls[self.control_id].version != self.control_version:
            raise ValueError("overlay exception must name a current control and version")
        if not self.rationale or not self.owner:
            raise ValueError("overlay exception rationale and owner are required")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.scope_digest):
            raise ValueError("overlay exception scope_digest must be SHA-256")
        if not self.created_at or not self.expires_at or not self.compensating_evidence_ids:
            raise ValueError("overlay exception expiry and compensating evidence are required")
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("overlay exception timestamps must be ISO-8601 UTC") from exc
        if (
            created.tzinfo is None or created.utcoffset() != UTC.utcoffset(created)
            or expires.tzinfo is None or expires.utcoffset() != UTC.utcoffset(expires)
            or created >= expires
        ):
            raise ValueError("overlay exception timestamps must be ordered UTC values")
        if self.compensating_evidence_ids != tuple(sorted(set(self.compensating_evidence_ids))):
            raise ValueError("compensating evidence IDs must be a sorted tuple")


@dataclass(frozen=True)
class OverlayControlResult:
    control_id: str
    control_version: str
    overlay_id: str
    evidence_state: OverlayEvidenceState
    evidence_ids: tuple[str, ...]
    exception_owner: str | None
    reason_codes: tuple[str, ...]
    disposition: Literal["advisory"] = "advisory"

    def __post_init__(self) -> None:
        if self.evidence_state not in {
            "satisfied", "failed", "unavailable", "not_assessed",
            "not_applicable", "excepted",
        }:
            raise ValueError("unsupported overlay evidence state")
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("overlay evidence IDs must be a sorted tuple")
        if not self.reason_codes or self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("overlay reason codes must be a non-empty sorted tuple")
        if self.evidence_state == "not_applicable" and (self.evidence_ids or self.exception_owner):
            raise ValueError("not-applicable overlay controls cannot carry evidence")
        if self.evidence_state == "excepted" and not self.exception_owner:
            raise ValueError("excepted overlay controls require an owner")

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "control_version": self.control_version,
            "overlay_id": self.overlay_id,
            "evidence_state": self.evidence_state,
            "evidence_ids": list(self.evidence_ids),
            "exception_owner": self.exception_owner,
            "reason_codes": list(self.reason_codes),
            "disposition": self.disposition,
        }


def _source(source_id: str, baseline: str, url: str, license_boundary: str,
            refresh_trigger: str) -> SourceDefinition:
    return SourceDefinition(
        source_id, baseline, url, license_boundary, "2026-08-20", (refresh_trigger,),
    )


SOURCES: tuple[SourceDefinition, ...] = (
    _source("adr.nygard", "Michael Nygard, Documenting Architecture Decisions, 2011", "https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions", "Copyright source article; use the ADR concept and original Anchor wording only.", "Canonical source or adopted ADR contract changes."),
    _source("arc42.template", "arc42 template v8.2", "https://arc42.org/", "CC BY-SA 4.0; attribute and do not import the template wholesale.", "Adopted arc42 release or license changes."),
    _source("c4.model", "C4 model website baseline captured 2026-08-20", "https://c4model.com/", "Copyright Simon Brown; site content CC BY 4.0; attribute and use concepts only.", "Site version, license, or adopted notation guidance changes."),
    _source("google.sre", "Google SRE Books online baseline captured 2026-08-20", "https://sre.google/books/", "CC BY-NC-ND 4.0 for book text; link and use general concepts without adaptation.", "License, canonical guidance, or adopted reliability policy changes."),
    _source("iec.fmea", "IEC 60812:2018, FMEA/FMECA", "https://webstore.iec.ch/en/publication/26359", "Copyright IEC; metadata and general concepts only; no tables, scales, or clauses.", "IEC publishes an amendment, revision, or withdrawal."),
    _source("iec.hazop", "IEC 61882:2016, HAZOP studies", "https://webstore.iec.ch/en/publication/24321", "Copyright IEC; metadata and general concepts only; no guide-word tables or clauses.", "IEC publishes an amendment, revision, or withdrawal."),
    _source("iso.25012", "ISO/IEC 25012:2008, Data quality model", "https://www.iso.org/standard/35736.html", "Copyright ISO; bibliographic metadata and public concepts only; no clauses or tables.", "ISO publishes an amendment, revision, or withdrawal."),
    _source("nist.ai-rmf", "NIST AI RMF 1.0 and Generative AI Profile", "https://www.nist.gov/itl/ai-risk-management-framework", "US Government works; cite NIST.", "NIST revises the RMF/profile or publishes applicable agent guidance."),
    _source("nist.ssdf", "NIST SP 800-218, SSDF v1.1, February 2022", "https://csrc.nist.gov/pubs/sp/800/218/final", "US Government work; cite NIST and use concepts only.", "NIST publishes a revision, errata, or successor."),
    _source("osha.moc", "OSHA 29 CFR 1910.119(l), Management of change", "https://www.osha.gov/laws-regs/regulations/standardnumber/1910/1910.119", "US Government regulation; quote only when legally necessary and separately reviewed; not legal advice.", "Regulation, official interpretation, or governing jurisdiction changes."),
    _source("owasp.asvs", "OWASP ASVS 5.0.0, May 2025", "https://owasp.org/www-project-application-security-verification-standard/", "CC BY-SA 4.0 upstream; preserve attribution and keep Anchor wording original.", "Adopted ASVS version or license changes."),
    _source("owasp.genai", "OWASP Top 10 for LLM Applications 2025", "https://genai.owasp.org/llm-top-10/", "CC BY-SA 4.0 upstream; original Anchor synthesis with attribution.", "OWASP publishes a new adopted list/version or changes scope/license."),
    _source("slsa", "SLSA specification v1.1", "https://slsa.dev/spec/v1.1/", "Community Specification License 1.0; cite and use provenance concepts without claiming a level.", "Adopted SLSA release, track model, or license changes."),
    _source("uk.data-quality", "UK Government Data Quality Framework, December 2020", "https://www.gov.uk/government/publications/the-government-data-quality-framework", "Open Government Licence v3.0; attribute the source.", "GOV.UK marks updated or publishes a successor."),
    _source("w3c.wcag", "WCAG 2.2, W3C Recommendation, 5 October 2023", "https://www.w3.org/TR/WCAG22/", "W3C Document License; link and paraphrase; do not republish normative text.", "Recommendation, errata, techniques, or supported-platform policy changes."),
)


def _predicate(
    *,
    domains_any: Collection[str] = (),
    traits_any: Collection[str] = (),
    effects_any: Collection[str] = (),
    path_classes_any: Collection[str] = (),
    require_domain_match: bool = False,
    require_tier: str | None = None,
) -> ApplicabilityPredicate:
    return ApplicabilityPredicate(
        domains_any=tuple(sorted(domains_any)),
        traits_any=tuple(sorted(traits_any)),
        effects_any=tuple(sorted(effects_any)),
        path_classes_any=tuple(sorted(path_classes_any)),
        require_domain_match=require_domain_match,
        require_tier=require_tier,
    )


OVERLAYS: tuple[OverlayDefinition, ...] = (
    OverlayDefinition("accessibility.user-interface", "1.0.0", "Accessibility user interface", _predicate(domains_any={"documentation", "frontend", "user_interface"}, traits_any={"audio_video", "document_export", "user_facing_interaction", "visual_content"}, path_classes_any={"style", "template", "ui_component", "user_document"}, require_domain_match=True), ("A11Y-OPERATE-01", "A11Y-PERCEIVE-01", "A11Y-ROBUST-01"), ("w3c.wcag",)),
    OverlayDefinition("ai.agent-risk", "1.0.0", "AI and agent risk", _predicate(domains_any={"agent", "ai", "ml"}, traits_any={"autonomous_action", "human_facing_ai_output", "model_inference", "prompt_or_context_change", "retrieval", "sensitive_model_input", "tool_calling"}, path_classes_any={"agent_policy", "model_config", "prompt", "retrieval_policy", "tool_boundary"}), ("AIR-BOUNDARY-01", "AIR-EVAL-01", "AIR-HUMAN-01"), ("nist.ai-rmf", "owasp.genai")),
    OverlayDefinition("architecture.change", "1.0.0", "Architecture change", _predicate(traits_any={"component_boundary_change", "cross_system_dependency", "deployment_topology_change", "new_component", "persistence_change", "public_contract_change"}, effects_any={"infrastructure_mutation", "schema_mutation"}, path_classes_any={"architecture_description", "infrastructure", "migration", "public_api"}), ("ARC-CONSIST-01", "ARC-CONTEXT-01", "ARC-DECISION-01"), ("adr.nygard", "arc42.template", "c4.model")),
    OverlayDefinition("change-safety.high-risk", "1.0.0", "High-risk change safety", _predicate(traits_any={"control_logic_change", "hazardous_process", "irreversible_change", "large_blast_radius", "regulated_process", "safety_critical"}, effects_any={"infrastructure_mutation", "permission_change", "production_data_destructive", "schema_destructive"}, require_tier="T3"), ("HRC-CHANGE-01", "HRC-DEVIATION-01", "HRC-FAILURE-01"), ("iec.fmea", "iec.hazop", "osha.moc")),
    OverlayDefinition("data.quality", "1.0.0", "Data quality", _predicate(domains_any={"analytics", "data"}, traits_any={"data_contract", "data_join", "data_transform", "data_write", "personal_data", "schema_change"}, effects_any={"data_mutation", "schema_mutation"}, path_classes_any={"migration", "model", "pipeline", "quality_rule", "schema"}), ("DAT-GRAIN-01", "DAT-TRACE-01", "DAT-VALID-01"), ("iso.25012", "uk.data-quality")),
    OverlayDefinition("operations.reliability", "1.0.0", "Operations reliability", _predicate(domains_any={"operations", "reliability"}, traits_any={"capacity_change", "on_call_change", "operational_dependency", "production_runtime", "scheduled_work", "service_level"}, effects_any={"deployment", "infrastructure_mutation", "production_configuration"}, path_classes_any={"deployment", "observability", "runbook", "scheduler", "service_configuration"}), ("OPS-FAIL-01", "OPS-RUN-01", "OPS-SLI-01"), ("google.sre",)),
    OverlayDefinition("security.application", "1.0.0", "Application security", _predicate(domains_any={"api", "application", "identity", "web"}, traits_any={"authentication", "authorization", "cryptography", "handles_personal_data", "handles_secrets", "network_exposed", "untrusted_input"}, effects_any={"external_publish", "permission_change", "secret_change"}, path_classes_any={"auth_boundary", "request_handler", "secret_configuration", "security_policy"}), ("SEC-AUTH-01", "SEC-INPUT-01", "SEC-SECRET-01"), ("nist.ssdf", "owasp.asvs")),
    OverlayDefinition("supply-chain.software", "1.0.0", "Software supply chain", _predicate(traits_any={"artifact_production", "build_change", "dependency_change", "release_change", "third_party_action"}, effects_any={"external_publish", "release"}, path_classes_any={"artifact_manifest", "build_pipeline", "dependency_manifest", "lockfile", "release_pipeline"}), ("SUP-BUILD-01", "SUP-DEP-01", "SUP-PROV-01"), ("nist.ssdf", "slsa")),
)


_CONTROL_ROWS = (
    ("A11Y-OPERATE-01", "accessibility.user-interface", "Changed interactions are keyboard operable with visible focus and no keyboard trap.", ("accessibility-automated", "accessibility-keyboard")),
    ("A11Y-PERCEIVE-01", "accessibility.user-interface", "Changed non-text content, structure, status, and contrast have appropriate programmatic or textual equivalents.", ("accessibility-automated", "accessibility-semantic")),
    ("A11Y-ROBUST-01", "accessibility.user-interface", "Changed interfaces retain meaningful names, roles, values, labels, and error identification.", ("accessibility-automated", "accessibility-semantic")),
    ("AIR-BOUNDARY-01", "ai.agent-risk", "Model-controlled content is untrusted and cannot expand tool authority or bypass policy.", ("ai-authority-review", "ai-evaluation")),
    ("AIR-EVAL-01", "ai.agent-risk", "Material behavior changes retain representative evaluations including misuse and failure cases.", ("ai-evaluation",)),
    ("AIR-HUMAN-01", "ai.agent-risk", "Consequential outputs expose limitations and retain required human decision or safe-stop boundaries.", ("ai-authority-review", "ai-evaluation")),
    ("ARC-CONSIST-01", "architecture.change", "Architecture descriptions, decisions, interfaces, and implementation do not materially contradict one another.", ("architecture-artifact-review", "architecture-consistency-review")),
    ("ARC-CONTEXT-01", "architecture.change", "Affected people, external systems, trust boundaries, and responsibilities are represented at the smallest useful level.", ("architecture-artifact-review",)),
    ("ARC-DECISION-01", "architecture.change", "Consequential design choices record context, decision, alternatives, consequences, and status.", ("architecture-artifact-review", "architecture-consistency-review")),
    ("DAT-GRAIN-01", "data.quality", "Output grain and keys are explicit and checked for null or duplicate violations.", ("data-contract", "data-key-check")),
    ("DAT-TRACE-01", "data.quality", "Material transformations and source-to-output count differences are attributable and unexplained loss is surfaced.", ("data-contract", "data-reconciliation")),
    ("DAT-VALID-01", "data.quality", "Critical values are checked against owned validity, completeness, consistency, and timeliness rules.", ("data-contract", "data-quality-result")),
    ("HRC-CHANGE-01", "change-safety.high-risk", "The change identifies owner, reason, baseline, prerequisites, authorization, communication, verification, and restoration.", ("approved-change-record", "rollback-verification")),
    ("HRC-DEVIATION-01", "change-safety.high-risk", "Hazardous process or control-logic changes receive qualified deviation review before execution.", ("approved-change-record", "hazard-review")),
    ("HRC-FAILURE-01", "change-safety.high-risk", "Credible failure modes retain consequences, controls, detection, and owned treatment without false precision.", ("hazard-review", "rollback-verification")),
    ("OPS-FAIL-01", "operations.reliability", "Expected failure modes have bounded impact, actionable observation, and tested recovery or safe degradation.", ("operations-observed-result", "operations-recovery")),
    ("OPS-RUN-01", "operations.reliability", "Operator steps name prerequisites, expected result, escalation, verification, and rollback.", ("operations-observed-result", "runbook-review")),
    ("OPS-SLI-01", "operations.reliability", "A user-relevant success signal and failure condition exist for changed production behavior.", ("operations-observed-result",)),
    ("SEC-AUTH-01", "security.application", "Identity and authorization decisions deny unintended access in retained positive and negative tests.", ("security-negative-test", "security-trust-review")),
    ("SEC-INPUT-01", "security.application", "Untrusted input is constrained at its trust boundary and failure behavior is tested.", ("security-negative-test", "security-trust-review")),
    ("SEC-SECRET-01", "security.application", "Secrets are neither embedded nor exposed and changed handling has a revocation-safe path.", ("secret-leak-inspection", "security-trust-review")),
    ("SUP-BUILD-01", "supply-chain.software", "Builds do not silently consume undeclared mutable inputs and metadata identifies material dependencies.", ("build-integrity", "supply-metadata")),
    ("SUP-DEP-01", "supply-chain.software", "Changed dependencies have reviewed origin, version, license, integrity, compatibility, and vulnerability evidence.", ("dependency-review", "supply-metadata")),
    ("SUP-PROV-01", "supply-chain.software", "Released artifacts are traceable to a source revision and declared build process.", ("build-integrity", "supply-metadata")),
)

_OVERLAY_BY_ID = MappingProxyType({item.overlay_id: item for item in OVERLAYS})
_SOURCE_IDS = frozenset(item.source_id for item in SOURCES)
OVERLAY_CONTROLS: tuple[OverlayControl, ...] = tuple(
    OverlayControl(
        control_id, "1.0.0", overlay_id, intent, tuple(sorted(requirements)),
        _OVERLAY_BY_ID[overlay_id].source_ids,
    )
    for control_id, overlay_id, intent, requirements in sorted(_CONTROL_ROWS)
)


def validate_overlay_catalog(
    overlays: tuple[OverlayDefinition, ...] = OVERLAYS,
    controls: tuple[OverlayControl, ...] = OVERLAY_CONTROLS,
    sources: tuple[SourceDefinition, ...] = SOURCES,
) -> None:
    """Fail closed on structural catalog drift."""
    source_ids = [item.source_id for item in sources]
    overlay_ids = [item.overlay_id for item in overlays]
    control_ids = [item.control_id for item in controls]
    if len(source_ids) != len(set(source_ids)) or len(overlay_ids) != len(set(overlay_ids)):
        raise ValueError("duplicate source or overlay ID")
    if len(control_ids) != len(set(control_ids)):
        raise ValueError("duplicate overlay control ID")
    if set(source_ids) != _SOURCE_IDS:
        raise ValueError("overlay source registry does not match its closed source IDs")
    by_overlay: dict[str, set[str]] = {overlay_id: set() for overlay_id in overlay_ids}
    for control in controls:
        if control.overlay_id not in by_overlay:
            raise ValueError(f"dangling overlay control: {control.control_id}")
        if not set(control.source_ids).issubset(source_ids):
            raise ValueError(f"unknown source ID on control: {control.control_id}")
        by_overlay[control.overlay_id].add(control.control_id)
    for overlay in overlays:
        if not set(overlay.source_ids).issubset(source_ids):
            raise ValueError(f"unknown source ID on overlay: {overlay.overlay_id}")
        if set(overlay.control_ids) != by_overlay[overlay.overlay_id]:
            raise ValueError(f"overlay control ownership mismatch: {overlay.overlay_id}")


def overlay_catalog_digest() -> str:
    payload = {
        "catalog_version": OVERLAY_CATALOG_VERSION,
        "sources": [item.to_dict() for item in SOURCES],
        "overlays": [item.to_dict() for item in OVERLAYS],
        "controls": [item.to_dict() for item in OVERLAY_CONTROLS],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _predicate_reasons(predicate: ApplicabilityPredicate, value: OverlayInput) -> tuple[str, ...]:
    domains = sorted(value.domains.intersection(predicate.domains_any))
    traits = sorted(value.traits.intersection(predicate.traits_any))
    effects = sorted(value.declared_effects.intersection(predicate.effects_any))
    paths = sorted(value.changed_path_classes.intersection(predicate.path_classes_any))
    if predicate.require_domain_match and not domains:
        return ()
    consequence = bool(traits or effects or paths)
    if predicate.require_domain_match and not consequence:
        return ()
    if predicate.require_tier and (value.assurance_tier != predicate.require_tier or not (traits or effects)):
        return ()
    if not predicate.require_domain_match and not (domains or consequence):
        return ()
    reasons = [*(f"domain:{item}" for item in domains), *(f"trait:{item}" for item in traits),
               *(f"effect:{item}" for item in effects), *(f"path_class:{item}" for item in paths)]
    if predicate.require_tier:
        reasons.append(f"tier:{value.assurance_tier}")
    return tuple(sorted(reasons))


def select_overlays(
    value: OverlayInput,
    overlays: tuple[OverlayDefinition, ...] = OVERLAYS,
) -> OverlaySelection:
    """Select overlays deterministically from normalized structured facts only."""
    if not isinstance(value, OverlayInput):
        raise TypeError("value must be an OverlayInput")
    selected: list[OverlaySelectionItem] = []
    not_selected: list[OverlaySelectionItem] = []
    for overlay in sorted(overlays, key=lambda item: item.overlay_id):
        reasons = _predicate_reasons(overlay.predicate, value)
        if reasons:
            selected.append(OverlaySelectionItem(
                overlay.overlay_id, overlay.version, reasons, overlay.control_ids,
            ))
        else:
            not_selected.append(OverlaySelectionItem(
                overlay.overlay_id, overlay.version, ("predicate_not_met",),
            ))
    return OverlaySelection(
        OVERLAY_CATALOG_VERSION, overlay_catalog_digest(), tuple(selected), tuple(not_selected),
    )


validate_overlay_catalog()
