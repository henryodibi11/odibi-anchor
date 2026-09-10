"""Pure, inert DEPLOYMENT_BINDING conformance validation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._canonical import domain_digest, domain_preimage, normalize, safe_integer
from ._documents import (
    _absolute,
    _array,
    _bounded_text,
    _confirmation,
    _digest,
    _fence,
    _id,
    _modifiers,
    _obj,
    _provenance,
    _sorted_unique,
    _text,
    _tool,
    validate_readiness_receipt,
    validate_release_pin,
    validate_repository_evidence,
    validate_sidecar_handshake,
)
from ._verification import verify_ed25519, verify_public_key


class AuthorityProtocolError(ValueError):
    """A binding is malformed or differs from supplied comparison facts."""


@dataclass(frozen=True)
class BindingExpectations:
    sidecar_handshake: Mapping[str, Any]
    readiness_receipt: Mapping[str, Any]
    release_pin: Mapping[str, Any]
    repository_evidence: Mapping[str, Any]


@dataclass(frozen=True)
class DeploymentBindingConformance:
    deployment_binding: Mapping[str, Any]
    deployment_attestation: Mapping[str, Any]
    deployment_attestation_digest: str
    deployment_binding_request_digest: str
    authorizes_readiness: bool = False
    is_ack: bool = False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _attestation(value: object) -> dict[str, Any]:
    keys = {"attestation_version", "gateway_generation", "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id", "closure_public_key_digest", "amp_workspace_id", "amp_project_id", "canonical_root", "repository_evidence", "release_pin", "checkout_reuse", "measured_plugin_digest", "measured_cw_version", "amp_version", "enabled_tools", "enabled_tools_digest", "confirmation", "sidecar_fence", "sidecar_fence_digest", "competing_modifiers", "readiness_receipt_digest", "observed_at_ms", "issued_at_ms", "provenance", "limitations"}
    out = _obj(value, keys)
    if safe_integer(out["attestation_version"]) != 1:
        raise AuthorityProtocolError("invalid attestation version")
    for key in ("gateway_generation", "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
        out[key] = _id(out[key])
    for key in ("closure_public_key_digest", "measured_plugin_digest", "enabled_tools_digest", "sidecar_fence_digest", "readiness_receipt_digest"):
        out[key] = _digest(out[key])
    out["amp_workspace_id"] = _bounded_text(out["amp_workspace_id"])
    out["amp_project_id"] = _bounded_text(out["amp_project_id"])
    out["canonical_root"] = _absolute(out["canonical_root"])
    out["repository_evidence"] = validate_repository_evidence(out["repository_evidence"])
    out["release_pin"] = validate_release_pin(out["release_pin"])
    reuse = _obj(out["checkout_reuse"], {"status", "prior_closure_digest"})
    if reuse["status"] not in {"fresh", "cleanly_closed"} or (reuse["status"] == "fresh") != (reuse["prior_closure_digest"] is None):
        raise AuthorityProtocolError("invalid checkout reuse")
    if reuse["prior_closure_digest"] is not None:
        reuse["prior_closure_digest"] = _digest(reuse["prior_closure_digest"])
    out["checkout_reuse"] = reuse
    out["measured_cw_version"] = _text(out["measured_cw_version"])
    out["amp_version"] = _text(out["amp_version"])
    out["enabled_tools"] = _sorted_unique(_array(out["enabled_tools"], _tool), lambda item: item["effective_name"])
    out["confirmation"] = _confirmation(out["confirmation"])
    out["sidecar_fence"] = _fence(out["sidecar_fence"])
    out["competing_modifiers"] = _modifiers(out["competing_modifiers"])
    out["observed_at_ms"] = safe_integer(out["observed_at_ms"])
    out["issued_at_ms"] = safe_integer(out["issued_at_ms"])
    out["provenance"] = _array(out["provenance"], _provenance)
    out["limitations"] = _array(out["limitations"], _text)
    return out


def validate_deployment_binding(value: object, *, expectations: BindingExpectations, received_at_ms: int) -> DeploymentBindingConformance:
    """Validate bytes and supplied comparison facts without granting authority."""
    try:
        if type(expectations) is not BindingExpectations:
            raise AuthorityProtocolError("invalid binding expectations")
        received = safe_integer(received_at_ms)
        handshake = validate_sidecar_handshake(normalize(expectations.sidecar_handshake))
        receipt = validate_readiness_receipt(normalize(expectations.readiness_receipt))
        release = validate_release_pin(normalize(expectations.release_pin))
        repository = validate_repository_evidence(normalize(expectations.repository_evidence))
        keys = {"binding_version", "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id", "gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation", "deployment_attestation_digest", "issued_at_ms", "sidecar_signature_base64"}
        binding = _obj(value, keys)
        if safe_integer(binding["binding_version"]) != 1:
            raise AuthorityProtocolError("invalid binding version")
        for key in ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
            binding[key] = _id(binding[key])
        binding["gateway_handshake_digest"] = _digest(binding["gateway_handshake_digest"])
        binding["readiness_receipt_digest"] = _digest(binding["readiness_receipt_digest"])
        binding["deployment_attestation_digest"] = _digest(binding["deployment_attestation_digest"])
        binding["issued_at_ms"] = safe_integer(binding["issued_at_ms"])
        attestation = _attestation(binding["deployment_attestation"])
        binding["deployment_attestation"] = attestation
        unsigned = {key: item for key, item in binding.items() if key != "sidecar_signature_base64"}
        deployment_digest = domain_digest("anchor-amp/deployment/v1", attestation)
        request_digest = domain_digest("anchor-amp/deployment-binding-request/v1", unsigned)
        expected_receipt_digest = domain_digest("anchor-amp/readiness/v1", receipt)
        expected_handshake_digest = domain_digest("anchor-amp/sidecar-handshake/v1", handshake)
        comparisons = (
            (binding["deployment_attestation_digest"], deployment_digest),
            (binding["readiness_receipt_digest"], expected_receipt_digest),
            (binding["gateway_handshake_digest"], expected_handshake_digest),
        )
        if any(actual != expected for actual, expected in comparisons):
            raise AuthorityProtocolError("digest mismatch")
        inherited = ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id")
        if any(binding[key] != attestation[key] or binding[key] != receipt[key] for key in inherited):
            raise AuthorityProtocolError("identity mismatch")
        handshake_fields = (
            "sidecar_instance_id", "challenge", "closure_public_key_base64",
            "closure_public_key_digest",
        )
        if (
            any(handshake[key] != receipt[key] for key in handshake_fields)
            or receipt["gateway_handshake_digest"] != expected_handshake_digest
        ):
            raise AuthorityProtocolError("handshake mismatch")
        if hashlib.sha256(verify_public_key(handshake["closure_public_key_base64"], handshake["closure_public_key_digest"])).hexdigest() != receipt["closure_public_key_digest"]:
            raise AuthorityProtocolError("closure key mismatch")
        if (
            receipt["setup_plugin_digest"] != release["plugin_digest"]
            or receipt["setup_wheel_digest"] != release["wheel_digest"]
            or receipt["amp_version"] != release["compatibility"]["amp_version"]
        ):
            raise AuthorityProtocolError("release mismatch")
        if repository["outcome"] == "available":
            fingerprint = repository["fingerprint"]
            if (
                receipt["repository_id"] != fingerprint["repository_id"]
                or receipt["canonical_root"] != fingerprint["canonical_root"]
            ):
                raise AuthorityProtocolError("repository mismatch")
        elif receipt["repository_id"] is not None:
            raise AuthorityProtocolError("repository mismatch")
        expected_fields = {
            "closure_public_key_digest": receipt["closure_public_key_digest"], "amp_workspace_id": receipt["amp_workspace_id"], "amp_project_id": receipt["amp_project_id"],
            "canonical_root": receipt["canonical_root"], "release_pin": release, "repository_evidence": repository, "checkout_reuse": receipt["checkout_reuse"],
            "measured_plugin_digest": release["plugin_digest"], "measured_cw_version": release["anchor_version"], "amp_version": receipt["amp_version"],
            "enabled_tools": receipt["enabled_tools"], "enabled_tools_digest": receipt["enabled_tools_digest"], "confirmation": receipt["confirmation"],
            "sidecar_fence": receipt["sidecar_fence"], "sidecar_fence_digest": receipt["sidecar_fence_digest"], "competing_modifiers": receipt["competing_modifiers"],
            "readiness_receipt_digest": expected_receipt_digest, "observed_at_ms": receipt["observed_at_ms"], "provenance": receipt["provenance"], "limitations": receipt["limitations"],
        }
        if any(attestation[key] != expected for key, expected in expected_fields.items()):
            raise AuthorityProtocolError("attestation differs from expectations")
        if binding["issued_at_ms"] != attestation["issued_at_ms"]:
            raise AuthorityProtocolError("issued time mismatch")
        if abs(received - binding["issued_at_ms"]) > 60_000 or received > receipt["expires_at_ms"]:
            raise AuthorityProtocolError("binding is stale")
        public = verify_public_key(receipt["closure_public_key_base64"], receipt["closure_public_key_digest"])
        verify_ed25519(public, binding["sidecar_signature_base64"], domain_preimage("anchor-amp/deployment-binding-signature/v1", unsigned))
        return DeploymentBindingConformance(_freeze(binding), _freeze(attestation), deployment_digest, request_digest)
    except AuthorityProtocolError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthorityProtocolError("invalid deployment binding") from exc
