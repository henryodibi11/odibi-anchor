"""Canonical, immutable task classification and legacy normalization."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping, Sequence, cast

WorkType = Literal["investigate", "decide", "design", "change", "verify", "operate", "communicate"]
ExecutionMode = Literal["read_only", "artifact_only", "source_change", "data_change"]
RiskLevel = Literal["low", "medium", "high"]
RigorLevel = Literal["direct", "compact", "full"]
RequiredBefore = Literal["work", "artifact", "effect", "gate", "final"]

_WORK_TYPES = frozenset({"investigate", "decide", "design", "change", "verify", "operate", "communicate"})
_EXECUTION_MODES = frozenset({"read_only", "artifact_only", "source_change", "data_change"})
_RISKS = frozenset({"low", "medium", "high"})
_RIGORS = frozenset({"direct", "compact", "full"})
_REQUIRED_BEFORE = frozenset({"work", "artifact", "effect", "gate", "final"})
_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_LEGACY_DEFAULTS: dict[str, tuple[str, str, str, str, tuple[str, ...], tuple[str, ...]]] = {
    "planning": ("design", "artifact_only", "medium", "compact", ("general",), ("planning",)),
    "documentation": ("communicate", "artifact_only", "low", "direct", ("general",), ("documentation",)),
    "implementation": ("change", "source_change", "medium", "compact", ("code",), ("implementation",)),
    "testing": ("verify", "read_only", "medium", "compact", ("code",), ("testing",)),
    "debugging": ("investigate", "read_only", "medium", "compact", ("code",), ("debugging",)),
    "review": ("verify", "read_only", "medium", "direct", ("code",), ("review",)),
    "migration": ("change", "source_change", "high", "compact", ("code",), ("migration", "rollback")),
    "handoff": ("communicate", "artifact_only", "low", "direct", ("general",), ("handoff",)),
    "decision": ("decide", "artifact_only", "medium", "compact", ("general",), ("alternatives",)),
    "analysis": ("investigate", "read_only", "medium", "compact", ("general",), ("analysis",)),
    "retrospective": ("communicate", "artifact_only", "low", "direct", ("general",), ("retrospective",)),
    "greenfield": ("design", "artifact_only", "high", "compact", ("code",), ("greenfield", "public-surface")),
    "spec_creation": ("design", "artifact_only", "medium", "compact", ("code",), ("specification",)),
    "data": ("change", "data_change", "high", "compact", ("data",), ("data-write",)),
    "etl": ("operate", "data_change", "medium", "compact", ("data", "pipeline"), ("etl",)),
    "reconciliation": ("investigate", "read_only", "medium", "compact", ("data",), ("reconciliation",)),
    "refresh": ("operate", "data_change", "medium", "compact", ("data", "pipeline"), ("refresh",)),
}


@dataclass(frozen=True)
class EvidenceRequest:
    """Evidence explicitly requested by the caller."""

    id: str
    kind: str
    description: str
    required_before: RequiredBefore

    def __post_init__(self) -> None:
        """Reject incomplete or invalid declarations."""
        if not all(isinstance(value, str) and value.strip() for value in (self.id, self.kind, self.description)):
            raise ValueError("Evidence request id, kind, and description must be non-empty strings")
        if self.required_before not in _REQUIRED_BEFORE:
            raise ValueError(f"Unsupported required_before: {self.required_before!r}")


@dataclass(frozen=True)
class TaskProfile:
    """Orthogonal canonical classification for one accepted task."""

    schema_version: str
    work_type: WorkType
    execution_mode: ExecutionMode
    risk: RiskLevel
    rigor: RigorLevel
    domains: tuple[str, ...]
    traits: frozenset[str]
    caller_required_evidence: tuple[EvidenceRequest, ...]
    legacy_mode: str | None
    normalization_notes: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate direct construction and deserialization, not only normalization."""
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("schema_version must be a non-empty string")
        for name, value, allowed in (
            ("work_type", self.work_type, _WORK_TYPES),
            ("execution_mode", self.execution_mode, _EXECUTION_MODES),
            ("risk", self.risk, _RISKS),
            ("rigor", self.rigor, _RIGORS),
        ):
            if value not in allowed:
                raise ValueError(f"Unsupported {name}: {value!r}")
        if not isinstance(self.domains, tuple) or not self.domains:
            raise ValueError("domains must be a non-empty tuple")
        if self.domains != _canonical_labels(self.domains, "domain"):
            raise ValueError("domains must be a canonical deduplicated tuple")
        if not isinstance(self.traits, frozenset):
            raise ValueError("traits must be a frozenset")
        if tuple(sorted(self.traits)) != _canonical_labels(tuple(self.traits), "trait"):
            raise ValueError("traits must contain canonical labels")
        if not isinstance(self.caller_required_evidence, tuple) or not all(
            isinstance(item, EvidenceRequest) for item in self.caller_required_evidence
        ):
            raise ValueError("caller_required_evidence must contain EvidenceRequest values")
        if self.legacy_mode is not None and self.legacy_mode not in _LEGACY_DEFAULTS:
            raise ValueError(f"Unsupported legacy mode: {self.legacy_mode!r}")
        if not isinstance(self.normalization_notes, tuple) or not all(
            isinstance(note, str) for note in self.normalization_notes
        ):
            raise ValueError("normalization_notes must be a tuple of strings")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-compatible representation."""
        return {
            "schema_version": self.schema_version,
            "work_type": self.work_type,
            "execution_mode": self.execution_mode,
            "risk": self.risk,
            "rigor": self.rigor,
            "domains": list(self.domains),
            "traits": sorted(self.traits),
            "caller_required_evidence": [asdict(item) for item in self.caller_required_evidence],
            "legacy_mode": self.legacy_mode,
            "normalization_notes": list(self.normalization_notes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskProfile":
        """Rehydrate a profile from its serialized representation."""
        required = {
            "schema_version", "work_type", "execution_mode", "risk", "rigor",
            "domains", "traits", "caller_required_evidence", "legacy_mode",
            "normalization_notes",
        }
        missing = required.difference(value)
        if missing:
            raise ValueError(f"Missing TaskProfile fields: {sorted(missing)}")
        extra = set(value).difference(required)
        if extra:
            raise ValueError(f"Unexpected TaskProfile fields: {sorted(extra)}")
        for field in ("domains", "traits", "caller_required_evidence", "normalization_notes"):
            if not isinstance(value[field], list):
                raise ValueError(f"{field} must be a JSON array")
        for field, label in (("domains", "domain"), ("traits", "trait")):
            if tuple(value[field]) != _canonical_labels(tuple(value[field]), label):
                raise ValueError(f"{field} must be sorted canonical labels without duplicates")
        raw_evidence = value["caller_required_evidence"]
        if not all(isinstance(item, Mapping) for item in raw_evidence):
            raise ValueError("caller_required_evidence must be a list of mappings")
        evidence_fields = {"id", "kind", "description", "required_before"}
        if any(set(item) != evidence_fields for item in raw_evidence):
            raise ValueError("evidence entries must have the full stable serialized shape")
        evidence = tuple(EvidenceRequest(**item) for item in raw_evidence)
        return cls(
            schema_version=value["schema_version"],
            work_type=cast(WorkType, value["work_type"]),
            execution_mode=cast(ExecutionMode, value["execution_mode"]),
            risk=cast(RiskLevel, value["risk"]),
            rigor=cast(RigorLevel, value["rigor"]),
            domains=tuple(value["domains"]),
            traits=frozenset(value["traits"]),
            caller_required_evidence=evidence,
            legacy_mode=value["legacy_mode"],
            normalization_notes=tuple(value["normalization_notes"]),
        )


def normalize_task_profile(
    *,
    legacy_mode: str | None = None,
    work_type: str | None = None,
    execution_mode: str | None = None,
    risk: str | None = None,
    rigor: str | None = None,
    domains: Sequence[str] | None = None,
    traits: Sequence[str] | None = None,
    caller_required_evidence: Sequence[EvidenceRequest | Mapping[str, Any]] | None = None,
    task_text: str = "",
    priority: str | None = None,
) -> TaskProfile:
    """Normalize legacy and explicit task inputs without inferring mutation permission."""
    if legacy_mode is not None and legacy_mode not in _LEGACY_DEFAULTS:
        raise ValueError(f"Unsupported legacy mode: {legacy_mode!r}")
    defaults = _LEGACY_DEFAULTS.get(
        legacy_mode or "", ("investigate", "read_only", "medium", "compact", ("general",), ()),
    )
    values = [work_type, execution_mode, risk, rigor]
    allowed = [_WORK_TYPES, _EXECUTION_MODES, _RISKS, _RIGORS]
    names = ["work_type", "execution_mode", "risk", "rigor"]
    for name, value, valid in zip(names, values, allowed):
        if value is not None and value not in valid:
            raise ValueError(f"Unsupported {name}: {value!r}. Expected one of: {sorted(valid)}")

    notes = [f"normalized legacy mode {legacy_mode!r}"] if legacy_mode else ["applied safe defaults"]
    resolved = list(defaults[:4])
    for index, (name, value) in enumerate(zip(names, values)):
        if value is not None:
            if value != resolved[index]:
                notes.append(f"explicit {name} overrides {resolved[index]!r}")
            resolved[index] = value

    normalized_domains = _normalize_labels(domains if domains is not None else defaults[4], "domain")
    normalized_traits = set(_normalize_labels(traits if traits is not None else defaults[5], "trait"))
    if domains is not None and tuple(normalized_domains) != defaults[4]:
        notes.append("explicit domains override legacy defaults")
    if traits is not None and frozenset(normalized_traits) != frozenset(defaults[5]):
        notes.append("explicit traits override legacy defaults")

    text = task_text.lower()
    inferred = {
        "cross-system": ("cross-system", "cross system"),
        "schema-change": ("schema change", "change schema"),
        "public-contract-change": ("public contract", "breaking api"),
        "destructive": ("destructive", "overwrite", "drop table"),
        "irreversible": ("irreversible",),
        "material-migration": ("material migration",),
        "rollback-design": ("rollback design", "rollback plan required"),
    }
    for trait, markers in inferred.items():
        if any(marker in text for marker in markers) and trait not in normalized_traits:
            normalized_traits.add(trait)
            notes.append(f"inferred trait {trait!r} from task text")
    team_intent = re.search(
        r"\bteam[- ]visible\b|\b(?:create|draft|publish|update|comment on|close|assign)\b"
        r"[^.\n]{0,40}\b(?:asana|work[- ]item|ticket)\b",
        text,
    )
    negated_team_intent = re.search(
        r"\b(?:not|never)\s+team[- ]visible\b|"
        r"\b(?:do not|don't|never|without)\s+(?:\w+\s+){0,2}"
        r"(?:create|draft|publish|update|comment|close|assign)\b[^.\n]{0,40}"
        r"\b(?:asana|work[- ]item|ticket)\b",
        text,
    )
    if traits is None and team_intent and not negated_team_intent and "team-visible" not in normalized_traits:
        normalized_traits.add("team-visible")
        notes.append("inferred trait 'team-visible' from explicit team work-item action")
    material_traits = {
        "cross-system", "schema-change", "public-contract-change", "destructive",
        "irreversible", "material-migration", "rollback-design",
    }
    if rigor is None and normalized_traits & material_traits:
        resolved[3] = "full"
        notes.append("material uncertainty or high-risk trait selects full rigor")
    if priority == "critical":
        if risk is None:
            resolved[2] = "high"
        normalized_traits.add("critical-impact")
        notes.append("legacy critical priority adds critical-impact and defaults risk to high")

    evidence = tuple(
        item if isinstance(item, EvidenceRequest) else EvidenceRequest(**item)
        for item in (caller_required_evidence or ())
    )
    return TaskProfile(
        schema_version="1.0",
        work_type=cast(WorkType, resolved[0]),
        execution_mode=cast(ExecutionMode, resolved[1]),
        risk=cast(RiskLevel, resolved[2]),
        rigor=cast(RigorLevel, resolved[3]),
        domains=tuple(normalized_domains) or ("general",),
        traits=frozenset(normalized_traits),
        caller_required_evidence=evidence,
        legacy_mode=legacy_mode,
        normalization_notes=tuple(notes),
    )


def _normalize_labels(values: Sequence[str], label: str) -> tuple[str, ...]:
    """Validate, lowercase, and deduplicate kebab-case labels."""
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{label} values must be strings")
        normalized = value.strip().lower().replace("_", "-")
        if not _KEBAB.fullmatch(normalized):
            raise ValueError(f"Invalid {label}: {value!r}; expected lowercase kebab-case")
        if normalized not in result:
            result.append(normalized)
    return tuple(sorted(result))


def _canonical_labels(values: Sequence[str], label: str) -> tuple[str, ...]:
    """Validate already-canonical labels without repairing serialized/direct input."""
    if any(not isinstance(value, str) or not _KEBAB.fullmatch(value) for value in values):
        raise ValueError(f"{label} values must be lowercase kebab-case strings")
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label} values are not allowed")
    return tuple(sorted(values))
