"""Pure, peer-neutral LEDGER_QUERY_RESULT conformance validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._canonical import domain_digest, domain_preimage, safe_integer
from ._documents import _b64, _digest, _id, _obj
from ._ledger_query import LedgerQueryConformance
from ._verification import verify_ed25519, verify_public_key


class LedgerQueryResultProtocolError(ValueError):
    """A query result is malformed or differs from supplied comparison facts."""


@dataclass(frozen=True)
class LedgerQueryResultExpectations:
    outstanding_query: LedgerQueryConformance
    checkout_instance_id: str
    ledger_generation_id: str
    sidecar_instance_id: str
    gateway_handshake_digest: str
    readiness_receipt_digest: str
    deployment_attestation_digest: str
    expected_closure_digest: str | None
    attestor_public_key_base64: str
    attestor_public_key_digest: str


@dataclass(frozen=True)
class LedgerQueryResultConformance:
    ledger_query_result: Mapping[str, Any]
    query_digest: str
    authorizes_readiness: bool = False
    authorizes_closure: bool = False
    authorizes_state_transition: bool = False
    establishes_durable_state: bool = False
    establishes_durable_closure: bool = False
    verifies_live_fence: bool = False
    is_ack: bool = False
    is_authority_response: bool = False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _expectations(value: LedgerQueryResultExpectations) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    if type(value) is not LedgerQueryResultExpectations:
        raise LedgerQueryResultProtocolError("invalid ledger query result expectations")
    conformance = value.outstanding_query
    if type(conformance) is not LedgerQueryConformance:
        raise LedgerQueryResultProtocolError("invalid outstanding ledger query conformance")
    if not isinstance(conformance.ledger_query, Mapping):
        raise LedgerQueryResultProtocolError("invalid outstanding ledger query")

    query_keys = {
        "query_version", "query_instance_id", "checkout_instance_id", "ledger_generation_id",
        "sidecar_instance_id", "gateway_handshake_digest", "readiness_receipt_digest",
        "deployment_attestation_digest", "requested_at_ms", "sidecar_signature_base64",
    }
    query = _obj(dict(conformance.ledger_query), query_keys)
    if safe_integer(query["query_version"]) != 1:
        raise LedgerQueryResultProtocolError("invalid outstanding query version")
    for key in ("query_instance_id", "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
        query[key] = _id(query[key])
    for key in ("gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest"):
        query[key] = _digest(query[key])
    query["requested_at_ms"] = safe_integer(query["requested_at_ms"])
    query["sidecar_signature_base64"] = _b64(query["sidecar_signature_base64"], 64)
    unsigned_query = {key: item for key, item in query.items() if key != "sidecar_signature_base64"}
    request_digest = domain_digest("anchor-amp/ledger-query-request/v1", unsigned_query)
    if _digest(conformance.ledger_query_request_digest) != request_digest:
        raise LedgerQueryResultProtocolError("outstanding query digest mismatch")

    current: dict[str, Any] = {}
    for key in ("checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
        current[key] = _id(getattr(value, key))
    for key in ("gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest"):
        current[key] = _digest(getattr(value, key))
    if any(query[key] != expected for key, expected in current.items()):
        raise LedgerQueryResultProtocolError("outstanding query differs from current generation")
    current["expected_closure_digest"] = (
        None if value.expected_closure_digest is None else _digest(value.expected_closure_digest)
    )
    key_digest = _digest(value.attestor_public_key_digest)
    public = verify_public_key(value.attestor_public_key_base64, key_digest)
    return query, {**current, "query_request_digest": request_digest}, public


def validate_ledger_query_result(
    value: object, *, expectations: LedgerQueryResultExpectations, sidecar_received_at_ms: int
) -> LedgerQueryResultConformance:
    """Validate a query result against untrusted facts without granting authority."""
    try:
        query, expected, public = _expectations(expectations)
        received = safe_integer(sidecar_received_at_ms)
        keys = {
            "query_version", "query_instance_id", "state", "checkout_instance_id",
            "ledger_generation_id", "sidecar_instance_id", "gateway_handshake_digest",
            "readiness_receipt_digest", "query_request_digest", "deployment_attestation_digest",
            "closure_digest", "updated_at_ms", "responded_at_ms", "query_digest",
            "attestor_signature_base64",
        }
        result = _obj(value, keys)
        if safe_integer(result["query_version"]) != 1:
            raise LedgerQueryResultProtocolError("invalid query result version")
        if result["state"] not in {"OPEN", "CLOSED", "QUARANTINED", "CONSUMED"}:
            raise LedgerQueryResultProtocolError("invalid query result state")
        for key in ("query_instance_id", "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"):
            result[key] = _id(result[key])
        for key in (
            "gateway_handshake_digest", "readiness_receipt_digest", "query_request_digest",
            "deployment_attestation_digest", "query_digest",
        ):
            result[key] = _digest(result[key])
        if result["closure_digest"] is not None:
            result["closure_digest"] = _digest(result["closure_digest"])
        result["updated_at_ms"] = safe_integer(result["updated_at_ms"])
        result["responded_at_ms"] = safe_integer(result["responded_at_ms"])

        comparisons = {key: expected[key] for key in (
            "checkout_instance_id", "ledger_generation_id", "sidecar_instance_id",
            "gateway_handshake_digest", "readiness_receipt_digest", "query_request_digest",
            "deployment_attestation_digest",
        )}
        comparisons.update(query_version=query["query_version"], query_instance_id=query["query_instance_id"])
        if any(result[key] != item for key, item in comparisons.items()):
            raise LedgerQueryResultProtocolError("query result differs from expectations")

        closure = expected["expected_closure_digest"]
        if result["state"] in {"OPEN", "QUARANTINED"}:
            if result["closure_digest"] is not None:
                raise LedgerQueryResultProtocolError("invalid query result closure digest")
        elif closure is None or result["closure_digest"] != closure:
            raise LedgerQueryResultProtocolError("invalid query result closure digest")
        if result["updated_at_ms"] > result["responded_at_ms"]:
            raise LedgerQueryResultProtocolError("invalid query result time ordering")
        if not -5_000 <= received - result["responded_at_ms"] <= 60_000:
            raise LedgerQueryResultProtocolError("query result is stale")

        digest_object = {
            key: item for key, item in result.items()
            if key not in {"query_digest", "attestor_signature_base64"}
        }
        query_digest = domain_digest("anchor-amp/ledger-query-result/v1", digest_object)
        if result["query_digest"] != query_digest:
            raise LedgerQueryResultProtocolError("query digest mismatch")
        signed = {key: item for key, item in result.items() if key != "attestor_signature_base64"}
        verify_ed25519(
            public,
            result["attestor_signature_base64"],
            domain_preimage("anchor-amp/ledger-query-result-signature/v1", signed),
        )
        return LedgerQueryResultConformance(_freeze(result), query_digest)
    except LedgerQueryResultProtocolError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise LedgerQueryResultProtocolError("invalid ledger query result") from exc
