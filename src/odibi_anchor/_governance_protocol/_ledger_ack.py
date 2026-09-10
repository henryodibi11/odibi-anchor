"""Pure, inert LEDGER_ACK conformance validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._canonical import domain_digest, domain_preimage, safe_integer
from ._documents import _digest, _id, _obj
from ._verification import verify_ed25519, verify_public_key


class LedgerAckProtocolError(ValueError):
    """A ledger acknowledgement is malformed or differs from supplied facts."""


@dataclass(frozen=True)
class LedgerAckExpectations:
    status: str
    checkout_instance_id: str
    ledger_generation_id: str
    request_digest: str
    deployment_attestation_digest: str
    closure_digest: str | None
    attestor_public_key_base64: str
    attestor_public_key_digest: str


@dataclass(frozen=True)
class LedgerAckConformance:
    ledger_ack: Mapping[str, Any]
    acknowledgement_digest: str
    authorizes_readiness: bool = False
    authorizes_closure: bool = False
    authorizes_state_transition: bool = False
    establishes_durable_commit: bool = False
    verifies_live_fence: bool = False
    is_ack: bool = False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _expectations(value: LedgerAckExpectations) -> tuple[dict[str, Any], bytes]:
    if type(value) is not LedgerAckExpectations:
        raise LedgerAckProtocolError("invalid ledger ACK expectations")
    if value.status not in {"BOUND", "CLOSED"}:
        raise LedgerAckProtocolError("invalid expected ACK status")
    expected: dict[str, Any] = {
        "status": value.status,
        "checkout_instance_id": _id(value.checkout_instance_id),
        "ledger_generation_id": _id(value.ledger_generation_id),
        "request_digest": _digest(value.request_digest),
        "deployment_attestation_digest": _digest(value.deployment_attestation_digest),
        "closure_digest": None if value.closure_digest is None else _digest(value.closure_digest),
        "attestor_public_key_digest": _digest(value.attestor_public_key_digest),
    }
    if (value.status == "BOUND") != (expected["closure_digest"] is None):
        raise LedgerAckProtocolError("expected closure digest does not match status")
    public = verify_public_key(value.attestor_public_key_base64, expected["attestor_public_key_digest"])
    return expected, public


def validate_ledger_ack(
    value: object, *, expectations: LedgerAckExpectations, received_at_ms: int
) -> LedgerAckConformance:
    """Validate a fresh ACK against untrusted comparison facts without granting authority."""
    try:
        expected, public = _expectations(expectations)
        received = safe_integer(received_at_ms)
        keys = {
            "ack_version", "status", "checkout_instance_id", "ledger_generation_id", "request_kind",
            "request_digest", "deployment_attestation_digest", "closure_digest", "committed_at_ms",
            "acknowledgement_digest", "attestor_signature_base64",
        }
        ack = _obj(value, keys)
        if safe_integer(ack["ack_version"]) != 1:
            raise LedgerAckProtocolError("invalid ACK version")
        ack["checkout_instance_id"] = _id(ack["checkout_instance_id"])
        ack["ledger_generation_id"] = _id(ack["ledger_generation_id"])
        for key in ("request_digest", "deployment_attestation_digest", "acknowledgement_digest"):
            ack[key] = _digest(ack[key])
        if ack["closure_digest"] is not None:
            ack["closure_digest"] = _digest(ack["closure_digest"])
        ack["committed_at_ms"] = safe_integer(ack["committed_at_ms"])
        request_kind = "deployment_binding" if expected["status"] == "BOUND" else "closure_submission"
        comparisons = {
            "status": expected["status"],
            "checkout_instance_id": expected["checkout_instance_id"],
            "ledger_generation_id": expected["ledger_generation_id"],
            "request_kind": request_kind,
            "request_digest": expected["request_digest"],
            "deployment_attestation_digest": expected["deployment_attestation_digest"],
            "closure_digest": expected["closure_digest"],
        }
        if any(ack[key] != item for key, item in comparisons.items()):
            raise LedgerAckProtocolError("ledger ACK differs from expectations")
        delta = received - ack["committed_at_ms"]
        if not -5_000 <= delta <= 60_000:
            raise LedgerAckProtocolError("ledger ACK is stale")
        digest_object = {
            key: item for key, item in ack.items()
            if key not in {"acknowledgement_digest", "attestor_signature_base64"}
        }
        acknowledgement_digest = domain_digest("anchor-amp/ledger-ack/v1", digest_object)
        if ack["acknowledgement_digest"] != acknowledgement_digest:
            raise LedgerAckProtocolError("acknowledgement digest mismatch")
        signed = {key: item for key, item in ack.items() if key != "attestor_signature_base64"}
        verify_ed25519(
            public,
            ack["attestor_signature_base64"],
            domain_preimage("anchor-amp/ledger-ack-signature/v1", signed),
        )
        return LedgerAckConformance(_freeze(ack), acknowledgement_digest)
    except LedgerAckProtocolError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise LedgerAckProtocolError("invalid ledger ACK") from exc
