"""Single closed assurance control catalog and canonical digesting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from odibi_anchor.assurance.models import (
    CORE_CATALOG_VERSION,
    LEGACY_CONTROL_IDS,
    QUALITY_ATTRIBUTE_ORDER,
    T1_CONTROL_IDS,
    ControlDisposition,
    QualityAttribute,
)
from odibi_anchor.assurance.overlays import (
    OVERLAY_CATALOG_VERSION,
    OVERLAY_CONTROLS,
    overlay_catalog_digest,
)

CATALOG_VERSION = CORE_CATALOG_VERSION


@dataclass(frozen=True)
class ControlDefinition:
    """Immutable definition of one project-owned assurance control."""

    control_id: str
    quality_attribute: QualityAttribute
    applicability_rule: str
    accepted_evidence: tuple[str, ...]
    disposition: ControlDisposition = "advisory"

    def __post_init__(self) -> None:
        if self.control_id not in LEGACY_CONTROL_IDS + T1_CONTROL_IDS:
            raise ValueError(f"Unknown assurance control: {self.control_id!r}")
        if self.quality_attribute not in QUALITY_ATTRIBUTE_ORDER:
            raise ValueError(f"Unknown quality attribute: {self.quality_attribute!r}")
        if not isinstance(self.applicability_rule, str) or not self.applicability_rule:
            raise ValueError("control applicability must be a non-empty string")
        if (
            not isinstance(self.accepted_evidence, tuple)
            or not self.accepted_evidence
            or any(not isinstance(item, str) or not item for item in self.accepted_evidence)
        ):
            raise ValueError("control applicability and accepted evidence must be declared")
        if self.disposition not in {"advisory", "warning", "blocking"}:
            raise ValueError("Unsupported control disposition")

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "quality_attribute": self.quality_attribute,
            "applicability_rule": self.applicability_rule,
            "accepted_evidence": list(self.accepted_evidence),
            "disposition": self.disposition,
        }


CORE_CONTROLS: tuple[ControlDefinition, ...] = (
    ControlDefinition(
        "AK-001",
        "functional-suitability",
        "all-plans",
        (
            "accepted task context",
            "current successful result-bearing verification or mode-appropriate inspection",
        ),
    ),
    ControlDefinition(
        "AK-002",
        "compatibility",
        "quality-attribute:compatibility",
        ("current matching caller-required evidence with resolvable referenced IDs",),
    ),
    ControlDefinition(
        "AK-003",
        "maintainability",
        "quality-attribute:maintainability",
        ("latest current unfiltered successful test with target and exit code",),
    ),
    ControlDefinition(
        "AK-004",
        "safety",
        "quality-attribute:safety",
        ("current successful review and preflight evidence",),
    ),
    ControlDefinition(
        "AK-005",
        "reliability",
        "quality-attribute:reliability",
        ("current successful result or outcome evidence with source and observation time",),
    ),
    ControlDefinition(
        "AK-006",
        "safety",
        "tier:T3-and-mutating-mode",
        ("current successful recovery or rollback evidence", "valid active exception"),
    ),
)

if tuple(control.control_id for control in CORE_CONTROLS) != LEGACY_CONTROL_IDS:
    raise RuntimeError("The v1 assurance catalog must contain exactly AK-001 through AK-006")


T1_CONTROLS: tuple[ControlDefinition, ...] = (
    ControlDefinition(
        "Anchor-T1-TESTS",
        "functional-suitability",
        "changed-executable-python-or-tests",
        ("fresh subject/scope-bound canonical scripts/run_tests.py result",),
    ),
    ControlDefinition(
        "Anchor-T1-STATIC-RATCHET",
        "maintainability",
        "changed-python-in-configured-static-scope",
        ("fresh Ruff ratchet result", "fresh Pyright ratchet result"),
    ),
    ControlDefinition(
        "Anchor-T1-OUTPUT-CONTRACT",
        "compatibility",
        "installed-package-or-tool-output-contract-affected",
        ("fresh scripts/verify_outputs.py result with explicit passing probes",),
    ),
    ControlDefinition(
        "Anchor-T1-DISTRIBUTION",
        "adaptability",
        "packaging-resource-cli-build-or-release-path-affected",
        ("fresh distribution result with every required local subcheck passing",),
    ),
    ControlDefinition(
        "Anchor-T1-SCOPE-INTEGRITY",
        "safety",
        "any-implementation-delivery",
        ("fresh repository snapshot and intended/touched scope reconciliation",),
    ),
)

if tuple(control.control_id for control in T1_CONTROLS) != T1_CONTROL_IDS:
    raise RuntimeError("The T1 catalog must contain exactly the five qualified core controls")

CORE_CONTROL_CATALOG = CORE_CONTROLS + T1_CONTROLS
CONTROL_CATALOG = CORE_CONTROL_CATALOG + OVERLAY_CONTROLS


def core_catalog_digest() -> str:
    """Return the SHA-256 digest of the versioned eleven-control core catalog."""
    payload = {
        "catalog_version": CATALOG_VERSION,
        "controls": [control.to_dict() for control in CORE_CONTROL_CATALOG],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def catalog_digest() -> str:
    """Return the SHA-256 digest of canonical core and overlay catalog content."""
    payload = {
        "core_catalog_version": CATALOG_VERSION,
        "overlay_catalog_version": OVERLAY_CATALOG_VERSION,
        "core_catalog_digest": core_catalog_digest(),
        "overlay_catalog_digest": overlay_catalog_digest(),
        "controls": [control.to_dict() for control in CONTROL_CATALOG],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
