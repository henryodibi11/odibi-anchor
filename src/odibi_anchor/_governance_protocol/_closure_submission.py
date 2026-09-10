"""Pure, inert CLOSURE_SUBMISSION conformance validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._canonical import domain_digest, domain_preimage, safe_integer
from ._documents import _bounded_text, _digest, _id, _obj
from ._verification import verify_ed25519, verify_public_key


class ClosureProtocolError(ValueError):
    """A closure submission is malformed or differs from supplied facts."""


@dataclass(frozen=True)
class ClosureExpectations:
    checkout_instance_id: str
    ledger_generation_id: str
    sidecar_instance_id: str
    gateway_handshake_digest: str
    readiness_receipt_digest: str
    deployment_attestation_digest: str
    thread_id: str
    gateway_generation: str
    task_generation: str
    closure_public_key_base64: str
    closure_public_key_digest: str


@dataclass(frozen=True)
class ClosureSubmissionConformance:
    closure_submission: Mapping[str, Any]
    closure_receipt: Mapping[str, Any]
    closure_digest: str
    closure_submission_request_digest: str
    authorizes_closure: bool = False
    authorizes_state_transition: bool = False
    is_ack: bool = False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _expectations(value: ClosureExpectations) -> tuple[dict[str, str], bytes]:
    if type(value) is not ClosureExpectations:
        raise ClosureProtocolError("invalid closure expectations")
    out: dict[str, str] = {}
    for key in ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id", "gateway_generation", "task_generation"):
        out[key] = _id(getattr(value, key))
    for key in ("gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest", "closure_public_key_digest"):
        out[key] = _digest(getattr(value, key))
    out["thread_id"] = _bounded_text(value.thread_id, maximum=512)
    public = verify_public_key(value.closure_public_key_base64, out["closure_public_key_digest"])
    return out, public


def _receipt(value: object) -> dict[str, Any]:
    keys = {
        "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id",
        "gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest",
        "thread_id", "gateway_generation", "task_generation", "gate_receipt_digest",
        "learning_receipt_digest", "closed_at_ms",
    }
    out = _obj(value, keys)
    for key in ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id", "gateway_generation", "task_generation"):
        out[key] = _id(out[key])
    out["thread_id"] = _bounded_text(out["thread_id"], maximum=512)
    for key in ("gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest", "gate_receipt_digest", "learning_receipt_digest"):
        out[key] = _digest(out[key])
    out["closed_at_ms"] = safe_integer(out["closed_at_ms"])
    return out


def validate_closure_submission(
    value: object, *, expectations: ClosureExpectations, received_at_ms: int
) -> ClosureSubmissionConformance:
    """Validate a fresh candidate against supplied facts without granting authority."""
    try:
        expected, public = _expectations(expectations)
        received = safe_integer(received_at_ms)
        keys = {
            "submission_version", "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id",
            "closure_receipt", "closure_digest", "submitted_at_ms", "sidecar_signature_base64",
        }
        submission = _obj(value, keys)
        if safe_integer(submission["submission_version"]) != 1:
            raise ClosureProtocolError("invalid submission version")
        for key in ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
            submission[key] = _id(submission[key])
        submission["closure_digest"] = _digest(submission["closure_digest"])
        submission["submitted_at_ms"] = safe_integer(submission["submitted_at_ms"])
        receipt = _receipt(submission["closure_receipt"])
        submission["closure_receipt"] = receipt
        unsigned = {key: item for key, item in submission.items() if key != "sidecar_signature_base64"}
        closure_digest = domain_digest("anchor-amp/task-closure/v1", receipt)
        request_digest = domain_digest("anchor-amp/closure-submission-request/v1", unsigned)
        if submission["closure_digest"] != closure_digest:
            raise ClosureProtocolError("closure digest mismatch")
        identity_fields = ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id")
        if any(submission[key] != receipt[key] or receipt[key] != expected[key] for key in identity_fields):
            raise ClosureProtocolError("identity mismatch")
        fact_fields = (
            "gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest",
            "thread_id", "gateway_generation", "task_generation",
        )
        if any(receipt[key] != expected[key] for key in fact_fields):
            raise ClosureProtocolError("closure receipt differs from expectations")
        if abs(received - submission["submitted_at_ms"]) > 60_000:
            raise ClosureProtocolError("closure submission is stale")
        verify_ed25519(
            public,
            submission["sidecar_signature_base64"],
            domain_preimage("anchor-amp/closure-submission-signature/v1", unsigned),
        )
        return ClosureSubmissionConformance(
            _freeze(submission), _freeze(receipt), closure_digest, request_digest
        )
    except ClosureProtocolError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ClosureProtocolError("invalid closure submission") from exc
