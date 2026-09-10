"""Pure, peer-neutral LEDGER_QUERY request conformance validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._canonical import domain_digest, domain_preimage, safe_integer
from ._documents import _digest, _id, _obj
from ._verification import verify_ed25519, verify_public_key


class LedgerQueryProtocolError(ValueError):
    """A ledger query request is malformed or differs from supplied facts."""


@dataclass(frozen=True)
class LedgerQueryExpectations:
    checkout_instance_id: str
    ledger_generation_id: str
    sidecar_instance_id: str
    gateway_handshake_digest: str
    readiness_receipt_digest: str
    deployment_attestation_digest: str
    closure_public_key_base64: str
    closure_public_key_digest: str


@dataclass(frozen=True)
class LedgerQueryConformance:
    ledger_query: Mapping[str, Any]
    ledger_query_request_digest: str
    authorizes_readiness: bool = False
    authorizes_closure: bool = False
    authorizes_state_transition: bool = False
    proves_randomness: bool = False
    proves_replay_uniqueness: bool = False
    records_replay_state: bool = False
    is_response: bool = False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _expectations(value: LedgerQueryExpectations) -> tuple[dict[str, str], bytes]:
    if type(value) is not LedgerQueryExpectations:
        raise LedgerQueryProtocolError("invalid ledger query expectations")
    expected: dict[str, str] = {}
    for key in ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
        expected[key] = _id(getattr(value, key))
    for key in (
        "gateway_handshake_digest",
        "readiness_receipt_digest",
        "deployment_attestation_digest",
        "closure_public_key_digest",
    ):
        expected[key] = _digest(getattr(value, key))
    public = verify_public_key(value.closure_public_key_base64, expected["closure_public_key_digest"])
    return expected, public


def validate_ledger_query(
    value: object, *, expectations: LedgerQueryExpectations, attestor_received_at_ms: int
) -> LedgerQueryConformance:
    """Validate a fresh query request against supplied facts without granting authority."""
    try:
        expected, public = _expectations(expectations)
        received = safe_integer(attestor_received_at_ms)
        keys = {
            "query_version",
            "query_instance_id",
            "checkout_instance_id",
            "ledger_generation_id",
            "sidecar_instance_id",
            "gateway_handshake_digest",
            "readiness_receipt_digest",
            "deployment_attestation_digest",
            "requested_at_ms",
            "sidecar_signature_base64",
        }
        query = _obj(value, keys)
        if safe_integer(query["query_version"]) != 1:
            raise LedgerQueryProtocolError("invalid query version")
        for key in ("query_instance_id", "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
            query[key] = _id(query[key])
        for key in ("gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest"):
            query[key] = _digest(query[key])
        query["requested_at_ms"] = safe_integer(query["requested_at_ms"])
        if any(query[key] != item for key, item in expected.items() if key != "closure_public_key_digest"):
            raise LedgerQueryProtocolError("ledger query differs from expectations")
        if abs(received - query["requested_at_ms"]) > 60_000:
            raise LedgerQueryProtocolError("ledger query is stale")
        unsigned = {key: item for key, item in query.items() if key != "sidecar_signature_base64"}
        request_digest = domain_digest("anchor-amp/ledger-query-request/v1", unsigned)
        verify_ed25519(
            public,
            query["sidecar_signature_base64"],
            domain_preimage("anchor-amp/ledger-query-signature/v1", unsigned),
        )
        return LedgerQueryConformance(_freeze(query), request_digest)
    except LedgerQueryProtocolError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise LedgerQueryProtocolError("invalid ledger query") from exc
