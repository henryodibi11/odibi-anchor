"""Inert readiness verification; this module never activates the kernel."""

from __future__ import annotations

import hashlib
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._canonical import domain_digest, domain_preimage, normalize, safe_integer
from ._crypto import (
    ProcessIdentity,
    decode_canonical_base64,
    handshake_digest,
    sign_deployment_binding,
    verify_ed25519,
    verify_public_key,
)
from ._measurement import LocalMeasurementInputs, measure_local
from ._readiness_protocol import validate_bootstrap_payload


class ReadinessError(ValueError):
    pass


@dataclass(frozen=True)
class ReadinessConformanceCandidate:
    """Cryptographic conformance output that deliberately lacks a live verified fence."""

    deployment_attestation: Mapping[str, Any]
    deployment_attestation_digest: str
    deployment_binding: Mapping[str, Any]
    deployment_binding_request_digest: str
    attestor_id: str
    _attestor_public_key: bytes
    live_fence_status: str = "unverified"


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise ReadinessError(message)


def build_readiness_conformance_candidate(
    *,
    identity: ProcessIdentity,
    payload: object,
    trusted_release_pin: Mapping[str, Any],
    trusted_attestor_pin: Mapping[str, Any],
    now_ms: int,
    local_inputs: LocalMeasurementInputs,
) -> ReadinessConformanceCandidate:
    """Build inert protocol evidence; a real fence validator must activate it later."""
    normalized = validate_bootstrap_payload(payload)
    _equal(normalized["release_pin"], normalize(trusted_release_pin), "untrusted release pin")
    _equal(normalized["attestor_pin"], normalize(trusted_attestor_pin), "untrusted attestor pin")
    receipt = normalized["deployment_claim"]["readiness_receipt"]
    attestor = normalized["attestor_pin"]
    _equal(receipt["attestor_id"], attestor["attestor_id"], "receipt issuer mismatch")
    public = verify_public_key(attestor["public_key_base64"], attestor["public_key_digest"])
    unsigned = {key: value for key, value in receipt.items() if key != "signature_base64"}
    verify_ed25519(public, receipt["signature_base64"], domain_preimage("anchor-amp/readiness-signature/v1", unsigned))
    receipt_digest = domain_digest("anchor-amp/readiness/v1", receipt)
    _equal(receipt_digest, normalized["deployment_claim"]["readiness_receipt_digest"], "receipt digest mismatch")

    handshake = identity.handshake
    expected_handshake_digest = handshake_digest(handshake)
    for key in ("sidecar_instance_id", "challenge", "closure_public_key_base64", "closure_public_key_digest"):
        _equal(receipt[key], handshake[key], f"handshake {key} mismatch")
    _equal(receipt["gateway_handshake_digest"], expected_handshake_digest, "handshake digest mismatch")
    issued_at = int(handshake["issued_at_ms"])
    if (
        receipt["observed_at_ms"] < issued_at - 5_000
        or receipt["observed_at_ms"] > now_ms + 5_000
        or issued_at > now_ms + 5_000
    ):
        raise ReadinessError("future timestamp")
    if (
        receipt["expires_at_ms"] < receipt["observed_at_ms"]
        or receipt["expires_at_ms"] > issued_at + 60_000
        or receipt["expires_at_ms"] < now_ms
    ):
        raise ReadinessError("expired or excessive receipt lifetime")
    # Signature and issuer were authenticated first. Every later failure still burns this proof.
    try:
        identity._consume_authentic_challenge(receipt["challenge"])
    except ValueError as exc:
        raise ReadinessError(str(exc)) from exc

    local = measure_local(LocalMeasurementInputs(local_inputs.project_root, local_inputs.plugin_path, normalized["routing"]))
    _equal(local.canonical_root, normalized["project_root"], "project root mismatch")
    _equal(local.canonical_root, receipt["canonical_root"], "receipt root mismatch")
    _equal(str(local_inputs.plugin_path), normalized["deployment_claim"]["plugin_path"], "plugin path mismatch")
    release = normalized["release_pin"]
    _equal(local.plugin_digest, release["plugin_digest"], "measured plugin mismatch")
    _equal(receipt["setup_plugin_digest"], release["plugin_digest"], "setup plugin mismatch")
    _equal(receipt["setup_wheel_digest"], release["wheel_digest"], "setup wheel claim mismatch")
    _equal(local.anchor_version, release["anchor_version"], "installed version mismatch")
    compatibility = release["compatibility"]
    current_python = sys.version_info[:2]
    minimum_python = tuple(map(int, compatibility["python_min"].split(".")))
    maximum_python = tuple(map(int, compatibility["python_max"].split(".")))
    if not minimum_python <= current_python <= maximum_python:
        raise ReadinessError("incompatible Python version")
    _equal(receipt["amp_version"], compatibility["amp_version"], "Amp version mismatch")
    for tool in receipt["enabled_tools"]:
        adapter = tool["qualified_adapter_receipt"]
        _equal(
            domain_digest("anchor-amp/qualified-adapter/v1", adapter), tool["qualified_adapter_digest"],
            "qualified adapter digest mismatch",
        )
        _equal(adapter["amp_version"], receipt["amp_version"], "adapter Amp mismatch")
        if not tool["pre_call_observed"] or not tool["terminal_result_observed"]:
            raise ReadinessError("adapter observations incomplete")
    _equal(
        domain_digest("anchor-amp/enabled-tools/v1", receipt["enabled_tools"]),
        receipt["enabled_tools_digest"], "enabled tools digest mismatch",
    )
    evidence = receipt["competing_modifiers"]["evidence"]
    manifest = evidence["declaration_source_manifest"]
    _equal(manifest["amp_version"], compatibility["amp_version"], "manifest Amp mismatch")
    _equal(manifest["plugin_api_digest"], compatibility["plugin_api_digest"], "Plugin API mismatch")
    manifest_digest = domain_digest("anchor-amp/declaration-source-manifest/v1", manifest)
    _equal(evidence["declaration_source_manifest_digest"], manifest_digest, "manifest digest mismatch")
    _equal(manifest_digest, attestor["declaration_source_manifest_digest"], "manifest pin mismatch")
    _equal(
        domain_digest("anchor-amp/competing-modifiers-evidence/v1", evidence),
        receipt["competing_modifiers"]["evidence_digest"], "modifier evidence digest mismatch",
    )
    _equal(
        domain_digest("anchor-amp/sidecar-fence/v1", receipt["sidecar_fence"]),
        receipt["sidecar_fence_digest"], "fence digest mismatch",
    )
    _equal(receipt["sidecar_fence"]["qualification_suite_digest"], attestor["sidecar_fence_qualification_digest"], "fence pin mismatch")
    confirmation = receipt["confirmation"]
    confirmation_digest = attestor["confirmation_surface_receipt_digest"]
    if confirmation["status"] == "available":
        surface = confirmation["surface_receipt"]
        surface_digest = domain_digest("anchor-amp/confirmation-surface/v1", surface)
        _equal(confirmation["surface_receipt_digest"], surface_digest, "confirmation digest mismatch")
        _equal(surface_digest, confirmation_digest, "confirmation pin mismatch")
        result_key = decode_canonical_base64(surface["result_public_key_base64"], length=32)
        _equal(hashlib.sha256(result_key).hexdigest(), surface["result_public_key_digest"], "result key mismatch")
        _equal(confirmation["surface_receipt"]["amp_version"], receipt["amp_version"], "confirmation Amp mismatch")
        _equal(confirmation["surface_receipt"]["plugin_api_digest"], compatibility["plugin_api_digest"], "confirmation API mismatch")
    elif confirmation_digest is not None:
        raise ReadinessError("confirmation capability mismatch")
    closure_key = decode_canonical_base64(receipt["closure_public_key_base64"], length=32)
    _equal(hashlib.sha256(closure_key).hexdigest(), receipt["closure_public_key_digest"], "closure key mismatch")
    if normalized["routing"]["kind"] == "git":
        _equal(receipt["repository_id"], normalized["routing"]["repository_id"], "repository mismatch")
    elif receipt["repository_id"] is not None:
        raise ReadinessError("non-Git receipt has repository identity")

    issued = now_ms
    attestation = {
        "attestation_version": 1,
        "gateway_generation": uuid.uuid4().hex,
        "checkout_instance_id": receipt["checkout_instance_id"],
        "ledger_generation_id": receipt["ledger_generation_id"],
        "sidecar_instance_id": receipt["sidecar_instance_id"],
        "closure_public_key_digest": receipt["closure_public_key_digest"],
        "amp_workspace_id": receipt["amp_workspace_id"],
        "amp_project_id": receipt["amp_project_id"],
        "canonical_root": local.canonical_root,
        "repository_evidence": local.repository_evidence,
        "release_pin": release,
        "checkout_reuse": receipt["checkout_reuse"],
        "measured_plugin_digest": local.plugin_digest,
        "measured_cw_version": local.anchor_version,
        "amp_version": receipt["amp_version"],
        "enabled_tools": receipt["enabled_tools"],
        "enabled_tools_digest": receipt["enabled_tools_digest"],
        "confirmation": confirmation,
        "sidecar_fence": receipt["sidecar_fence"],
        "sidecar_fence_digest": receipt["sidecar_fence_digest"],
        "competing_modifiers": receipt["competing_modifiers"],
        "readiness_receipt_digest": receipt_digest,
        "observed_at_ms": receipt["observed_at_ms"],
        "issued_at_ms": issued,
        "provenance": receipt["provenance"],
        "limitations": receipt["limitations"],
    }
    attestation = normalize(attestation)
    deployment_digest = domain_digest("anchor-amp/deployment/v1", attestation)
    binding_without_signature = {
        "binding_version": 1,
        "checkout_instance_id": receipt["checkout_instance_id"],
        "ledger_generation_id": receipt["ledger_generation_id"],
        "sidecar_instance_id": receipt["sidecar_instance_id"],
        "gateway_handshake_digest": expected_handshake_digest,
        "readiness_receipt_digest": receipt_digest,
        "deployment_attestation": attestation,
        "deployment_attestation_digest": deployment_digest,
        "issued_at_ms": issued,
    }
    binding_digest = domain_digest("anchor-amp/deployment-binding-request/v1", binding_without_signature)
    binding = sign_deployment_binding(identity, binding_without_signature)
    return ReadinessConformanceCandidate(
        _freeze(attestation), deployment_digest, _freeze(binding), binding_digest,
        attestor["attestor_id"], bytes(public),
    )


def verify_bound_ack_conformance(
    candidate: ReadinessConformanceCandidate, ack: object, *, now_ms: int,
) -> None:
    """Verify BOUND bytes without claiming that the referenced live fence was established."""
    if not isinstance(ack, dict):
        raise ReadinessError("invalid BOUND acknowledgement")
    keys = {
        "ack_version", "status", "checkout_instance_id", "ledger_generation_id", "request_kind",
        "request_digest", "deployment_attestation_digest", "closure_digest", "committed_at_ms",
        "acknowledgement_digest", "attestor_signature_base64",
    }
    if set(ack) != keys:
        raise ReadinessError("invalid BOUND acknowledgement")
    try:
        value = normalize(ack)
        if safe_integer(value["ack_version"]) != 1 or type(value["committed_at_ms"]) is not int:
            raise ValueError
        for key in ("request_digest", "deployment_attestation_digest", "acknowledgement_digest"):
            if not isinstance(value[key], str) or len(value[key]) != 64 or any(char not in "0123456789abcdef" for char in value[key]):
                raise ValueError
        decode_canonical_base64(value["attestor_signature_base64"], length=64)
    except (ValueError, TypeError, KeyError) as exc:
        raise ReadinessError("invalid BOUND acknowledgement") from exc
    binding = candidate.deployment_binding
    expected = {
        "ack_version": 1, "status": "BOUND", "checkout_instance_id": binding["checkout_instance_id"],
        "ledger_generation_id": binding["ledger_generation_id"], "request_kind": "deployment_binding",
        "request_digest": candidate.deployment_binding_request_digest,
        "deployment_attestation_digest": candidate.deployment_attestation_digest, "closure_digest": None,
    }
    for key, expected_value in expected.items():
        _equal(value[key], expected_value, f"BOUND {key} mismatch")
    if value["committed_at_ms"] > now_ms + 5_000 or now_ms - value["committed_at_ms"] > 60_000:
        raise ReadinessError("stale BOUND acknowledgement")
    digest_object = {key: item for key, item in value.items() if key not in {"acknowledgement_digest", "attestor_signature_base64"}}
    _equal(domain_digest("anchor-amp/ledger-ack/v1", digest_object), value["acknowledgement_digest"], "ACK digest mismatch")
    signed = {key: item for key, item in value.items() if key != "attestor_signature_base64"}
    verify_ed25519(
        candidate._attestor_public_key, value["attestor_signature_base64"],
        domain_preimage("anchor-amp/ledger-ack-signature/v1", signed),
    )
