"""Pure, peer-neutral host-capability qualification conformance."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._canonical import dumps, safe_integer
from ._documents import _array, _bounded_text, _digest, _id, _obj

CAPABILITY_IDS = (
    "authenticated_authority_channel",
    "checkout_creation_evidence",
    "control_workload_fencing_support",
    "effective_inventory",
    "gateway_admission_revocation",
    "terminal_lifecycle",
)
EVIDENCE_CLASSES = (
    "adversarial_qualification",
    "claim_requirement_mapping",
    "supported_interface_commitment",
    "threat_review",
)
MAX_VALIDITY_MS = 2_592_000_000
_TARGET_DOMAIN = b"anchor-governance/host-capability-target/v1"


class HostCapabilityQualificationProtocolError(ValueError):
    """A qualification record or its supplied comparison context is malformed."""


@dataclass(frozen=True)
class HostCapabilityQualificationExpectations:
    expected_target: Mapping[str, Any]
    evaluated_at_ms: int
    provider_issuer_id: str
    provider_issuer_key_or_artifact_digest: str
    qualification_authority_id: str
    qualification_authority_key_or_artifact_digest: str
    invalidated_target_digests: tuple[str, ...]
    invalidated_evidence_record_digests: tuple[str, ...]
    invalidation_view_complete: bool
    invalidation_view_observed_at_ms: int


@dataclass(frozen=True)
class HostCapabilityQualificationConformance:
    qualification: Mapping[str, Any]
    qualification_target_digest: str
    capability_statuses: Mapping[str, str]
    effective_status: str
    authorizes_readiness: bool = False
    production_ready: bool = False
    establishes_authority: bool = False
    activates_adapter: bool = False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _unique_digests(value: object) -> list[str]:
    digests = _array(value, _digest)
    if len(digests) != len(set(digests)):
        raise HostCapabilityQualificationProtocolError("duplicate digest")
    return sorted(digests)


def _strict_obj(value: object, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise HostCapabilityQualificationProtocolError("invalid object type")
    return _obj(value, keys)


def _target(value: object) -> dict[str, Any]:
    if type(value) is not dict or type(value.get("host_product_identity")) is not dict:
        raise HostCapabilityQualificationProtocolError("invalid qualification target")
    out = _strict_obj(
        value,
        {
            "capability_contract_digest",
            "host_product_identity",
            "host_adapter_build_digest",
            "host_environment_build_id",
            "resolved_host_configuration_digest",
            "deployment_topology_digest",
            "qualification_suite_digest",
        },
    )
    out["capability_contract_digest"] = _digest(out["capability_contract_digest"])
    product = _strict_obj(out["host_product_identity"], {"product_id", "executable_build_id"})
    product["product_id"] = _bounded_text(product["product_id"])
    product["executable_build_id"] = _bounded_text(product["executable_build_id"])
    out["host_product_identity"] = product
    out["host_adapter_build_digest"] = _digest(out["host_adapter_build_digest"])
    out["host_environment_build_id"] = _bounded_text(out["host_environment_build_id"])
    for key in (
        "resolved_host_configuration_digest",
        "deployment_topology_digest",
        "qualification_suite_digest",
    ):
        out[key] = _digest(out[key])
    return out


def _target_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_TARGET_DOMAIN + b"\0" + dumps(value)).hexdigest()


def _expectations(
    value: HostCapabilityQualificationExpectations,
) -> tuple[str, int, tuple[str, str], tuple[str, str], set[str], set[str], bool]:
    if type(value) is not HostCapabilityQualificationExpectations:
        raise HostCapabilityQualificationProtocolError("invalid qualification expectations")
    expected_target = _target(value.expected_target)
    expected_target_digest = _target_digest(expected_target)
    evaluated_at_ms = safe_integer(value.evaluated_at_ms)
    provider_pin = (
        _id(value.provider_issuer_id),
        _digest(value.provider_issuer_key_or_artifact_digest),
    )
    authority_pin = (
        _id(value.qualification_authority_id),
        _digest(value.qualification_authority_key_or_artifact_digest),
    )
    if (
        provider_pin[0] == authority_pin[0]
        or provider_pin[1] == authority_pin[1]
        or authority_pin[1] == expected_target["host_adapter_build_digest"]
    ):
        raise HostCapabilityQualificationProtocolError("qualification authority is not independent")
    if type(value.invalidated_target_digests) is not tuple:
        raise HostCapabilityQualificationProtocolError("invalid target invalidations")
    if type(value.invalidated_evidence_record_digests) is not tuple:
        raise HostCapabilityQualificationProtocolError("invalid evidence invalidations")
    invalidated_targets = {_digest(item) for item in value.invalidated_target_digests}
    invalidated_records = {_digest(item) for item in value.invalidated_evidence_record_digests}
    if len(invalidated_targets) != len(value.invalidated_target_digests):
        raise HostCapabilityQualificationProtocolError("duplicate target invalidation")
    if len(invalidated_records) != len(value.invalidated_evidence_record_digests):
        raise HostCapabilityQualificationProtocolError("duplicate evidence invalidation")
    if type(value.invalidation_view_complete) is not bool:
        raise HostCapabilityQualificationProtocolError("invalid invalidation completeness")
    invalidation_observed_at_ms = safe_integer(value.invalidation_view_observed_at_ms)
    invalidation_current = (
        value.invalidation_view_complete and invalidation_observed_at_ms == evaluated_at_ms
    )
    return (
        expected_target_digest,
        evaluated_at_ms,
        provider_pin,
        authority_pin,
        invalidated_targets,
        invalidated_records,
        invalidation_current,
    )


def _evidence(value: object) -> dict[str, Any]:
    out = _strict_obj(
        value,
        {
            "evidence_class",
            "record_digest",
            "qualification_target_digest",
            "issuer_id",
            "issuer_key_or_artifact_digest",
            "conclusion",
            "observed_at_ms",
            "valid_until_ms",
        },
    )
    if out["evidence_class"] not in EVIDENCE_CLASSES:
        raise HostCapabilityQualificationProtocolError("invalid evidence class")
    out["record_digest"] = _digest(out["record_digest"])
    out["qualification_target_digest"] = _digest(out["qualification_target_digest"])
    out["issuer_id"] = _id(out["issuer_id"])
    out["issuer_key_or_artifact_digest"] = _digest(out["issuer_key_or_artifact_digest"])
    if out["conclusion"] not in {"PASS", "FAIL", "UNKNOWN"}:
        raise HostCapabilityQualificationProtocolError("invalid evidence conclusion")
    out["observed_at_ms"] = safe_integer(out["observed_at_ms"])
    out["valid_until_ms"] = safe_integer(out["valid_until_ms"])
    if (
        out["observed_at_ms"] > out["valid_until_ms"]
        or out["valid_until_ms"] - out["observed_at_ms"] > MAX_VALIDITY_MS
    ):
        raise HostCapabilityQualificationProtocolError("invalid evidence validity")
    return out


def _capability(value: object) -> dict[str, Any]:
    if type(value) is not dict or any(
        type(value.get(key)) is not list
        for key in ("evidence", "material_limitation_digests", "conflict_digests")
    ):
        raise HostCapabilityQualificationProtocolError("invalid capability arrays")
    evidence = [_evidence(item) for item in value["evidence"]]
    out = _strict_obj(
        value,
        {
            "capability_id",
            "provider_response",
            "evidence",
            "material_limitation_digests",
            "conflict_digests",
        },
    )
    if out["capability_id"] not in CAPABILITY_IDS:
        raise HostCapabilityQualificationProtocolError("invalid capability")
    if out["provider_response"] not in {"SUPPORTED", "ROADMAP", "UNSUPPORTED", "UNKNOWN"}:
        raise HostCapabilityQualificationProtocolError("invalid provider response")
    classes = [item["evidence_class"] for item in evidence]
    if len(classes) != len(set(classes)):
        raise HostCapabilityQualificationProtocolError("duplicate evidence class")
    out["evidence"] = sorted(evidence, key=lambda item: item["evidence_class"])
    out["material_limitation_digests"] = _unique_digests(out["material_limitation_digests"])
    out["conflict_digests"] = _unique_digests(out["conflict_digests"])
    return out


def _status(
    capability: Mapping[str, Any],
    *,
    candidate_target_digest: str,
    expected_target_digest: str,
    evaluated_at_ms: int,
    provider_pin: tuple[str, str],
    authority_pin: tuple[str, str],
    invalidated_targets: set[str],
    invalidated_records: set[str],
    invalidation_current: bool,
) -> str:
    response = capability["provider_response"]
    evidence = capability["evidence"]
    if response in {"ROADMAP", "UNSUPPORTED"}:
        return "UNSUPPORTED"

    current_fail = False
    provenance_mismatch = False
    expired = candidate_target_digest != expected_target_digest
    pass_count = 0
    for item in evidence:
        expected_pin = provider_pin if item["evidence_class"] == "supported_interface_commitment" else authority_pin
        issuer_matches = (
            item["issuer_id"], item["issuer_key_or_artifact_digest"]
        ) == expected_pin
        provenance_mismatch |= not issuer_matches
        is_temporally_current = item["observed_at_ms"] <= evaluated_at_ms <= item["valid_until_ms"]
        is_eligible_pass = (
            item["qualification_target_digest"] == expected_target_digest
            and is_temporally_current
            and item["record_digest"] not in invalidated_records
        )
        if issuer_matches and is_temporally_current and item["conclusion"] == "FAIL":
            current_fail = True
        if issuer_matches and is_eligible_pass and item["conclusion"] == "PASS":
            pass_count += 1
        expired |= (
            item["qualification_target_digest"] != expected_target_digest
            or not is_temporally_current
            or item["record_digest"] in invalidated_records
        )

    if current_fail:
        return "UNSUPPORTED"
    if provenance_mismatch or capability["conflict_digests"] or not invalidation_current:
        return "UNKNOWN"
    if candidate_target_digest in invalidated_targets:
        expired = True
    if expired:
        return "EXPIRED"
    if response == "UNKNOWN" or pass_count == 0:
        return "UNKNOWN"
    if pass_count < len(EVIDENCE_CLASSES) or capability["material_limitation_digests"]:
        return "PARTIAL"
    return "SUPPORTED"


def validate_host_capability_qualification(
    value: object, *, expectations: HostCapabilityQualificationExpectations
) -> HostCapabilityQualificationConformance:
    """Reduce supplied comparison facts without establishing trusted sourcing or authority."""
    try:
        (
            expected_target_digest,
            evaluated_at_ms,
            provider_pin,
            authority_pin,
            invalidated_targets,
            invalidated_records,
            invalidation_current,
        ) = _expectations(expectations)
        if type(value) is not dict or type(value.get("capabilities")) is not list:
            raise HostCapabilityQualificationProtocolError("invalid qualification capabilities")
        capabilities = [_capability(item) for item in value["capabilities"]]
        candidate_target = _target(value.get("target"))
        qualification = _strict_obj(value, {"qualification_version", "target", "capabilities"})
        if safe_integer(qualification["qualification_version"]) != 1:
            raise HostCapabilityQualificationProtocolError("invalid qualification version")
        candidate_target_digest = _target_digest(candidate_target)
        qualification["target"] = candidate_target
        capability_ids = [item["capability_id"] for item in capabilities]
        if set(capability_ids) != set(CAPABILITY_IDS) or len(capability_ids) != len(CAPABILITY_IDS):
            raise HostCapabilityQualificationProtocolError("invalid capability set")
        qualification["capabilities"] = sorted(capabilities, key=lambda item: item["capability_id"])
        for capability in capabilities:
            if any(
                item["qualification_target_digest"] != candidate_target_digest
                for item in capability["evidence"]
            ):
                raise HostCapabilityQualificationProtocolError("mixed qualification targets")

        statuses = {
            capability["capability_id"]: _status(
                capability,
                candidate_target_digest=candidate_target_digest,
                expected_target_digest=expected_target_digest,
                evaluated_at_ms=evaluated_at_ms,
                provider_pin=provider_pin,
                authority_pin=authority_pin,
                invalidated_targets=invalidated_targets,
                invalidated_records=invalidated_records,
                invalidation_current=invalidation_current,
            )
            for capability in qualification["capabilities"]
        }
        effective = "SUPPORTED" if all(status == "SUPPORTED" for status in statuses.values()) else "UNSUPPORTED"
        return HostCapabilityQualificationConformance(
            _freeze(qualification),
            candidate_target_digest,
            _freeze(statuses),
            effective,
        )
    except HostCapabilityQualificationProtocolError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise HostCapabilityQualificationProtocolError("invalid host capability qualification") from exc
