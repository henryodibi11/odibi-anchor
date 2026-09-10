from __future__ import annotations

import base64
import hashlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odibi_anchor._governance_protocol._canonical import domain_digest, domain_preimage
from odibi_anchor._governance_protocol._ledger_query import LedgerQueryConformance
from odibi_anchor._governance_protocol._ledger_query_result import (
    LedgerQueryResultExpectations,
    LedgerQueryResultProtocolError,
    validate_ledger_query_result,
)

NOW = 1_800_000_000_000
D1, D2, D3, D4 = "1" * 64, "2" * 64, "3" * 64, "4" * 64


def _seal(private: Ed25519PrivateKey, fields: dict, *, digest_domain: str = "anchor-amp/ledger-query-result/v1",
          signature_domain: str = "anchor-amp/ledger-query-result-signature/v1") -> dict:
    result = dict(fields)
    result.pop("query_digest", None)
    result.pop("attestor_signature_base64", None)
    result["query_digest"] = domain_digest(digest_domain, result)
    result["attestor_signature_base64"] = base64.b64encode(
        private.sign(domain_preimage(signature_domain, result))
    ).decode()
    return result


def _fixture(state: str = "OPEN", *, expected_closure: str | None = D4):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    query = {
        "query_version": 1, "query_instance_id": "query-1", "checkout_instance_id": "checkout-1",
        "ledger_generation_id": "ledger-1", "sidecar_instance_id": "sidecar-1",
        "gateway_handshake_digest": D1, "readiness_receipt_digest": D2,
        "deployment_attestation_digest": D3, "requested_at_ms": NOW - 100,
        "sidecar_signature_base64": base64.b64encode(b"x" * 64).decode(),
    }
    request_digest = domain_digest(
        "anchor-amp/ledger-query-request/v1",
        {key: item for key, item in query.items() if key != "sidecar_signature_base64"},
    )
    conformance = LedgerQueryConformance(query, request_digest)
    expected = LedgerQueryResultExpectations(
        conformance, "checkout-1", "ledger-1", "sidecar-1", D1, D2, D3, expected_closure,
        base64.b64encode(public).decode(), hashlib.sha256(public).hexdigest(),
    )
    result = {
        "query_version": 1, "query_instance_id": "query-1", "state": state,
        "checkout_instance_id": "checkout-1", "ledger_generation_id": "ledger-1",
        "sidecar_instance_id": "sidecar-1", "gateway_handshake_digest": D1,
        "readiness_receipt_digest": D2, "query_request_digest": request_digest,
        "deployment_attestation_digest": D3,
        "closure_digest": D4 if state in {"CLOSED", "CONSUMED"} else None,
        "updated_at_ms": NOW - 1_000, "responded_at_ms": NOW,
    }
    return private, _seal(private, result), expected


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "QUARANTINED", "CONSUMED"])
def test_all_states_are_valid_and_result_is_detached_immutable_and_inert(state: str) -> None:
    _, candidate, expected = _fixture(state)
    conformance = validate_ledger_query_result(candidate, expectations=expected, sidecar_received_at_ms=NOW)
    assert dict(conformance.ledger_query_result) == candidate
    assert conformance.query_digest == candidate["query_digest"]
    assert not any(value for name, value in conformance.__dict__.items() if name not in {"ledger_query_result", "query_digest"})
    candidate["state"] = "OPEN"
    assert conformance.ledger_query_result["state"] == state
    with pytest.raises(TypeError):
        conformance.ledger_query_result["state"] = "OPEN"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        conformance.is_ack = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "state,expected_closure,result_closure,accepted",
    [
        ("OPEN", None, None, True), ("OPEN", D4, None, True), ("OPEN", D4, D4, False),
        ("QUARANTINED", None, None, True), ("QUARANTINED", D4, None, True),
        ("QUARANTINED", D4, D4, False), ("CLOSED", None, D4, False), ("CLOSED", D4, D4, True),
        ("CLOSED", D4, None, False), ("CONSUMED", None, D4, False), ("CONSUMED", D4, D4, True),
        ("CONSUMED", D4, None, False),
    ],
)
def test_full_closure_matrix(state: str, expected_closure: str | None, result_closure: str | None,
                             accepted: bool) -> None:
    private, candidate, expected = _fixture(state, expected_closure=expected_closure)
    candidate["closure_digest"] = result_closure
    candidate = _seal(private, candidate)
    if accepted:
        validate_ledger_query_result(candidate, expectations=expected, sidecar_received_at_ms=NOW)
    else:
        with pytest.raises(LedgerQueryResultProtocolError):
            validate_ledger_query_result(candidate, expectations=expected, sidecar_received_at_ms=NOW)


@pytest.mark.parametrize("delta,accepted", [(-5_001, False), (-5_000, True), (60_000, True), (60_001, False)])
def test_exact_inclusive_freshness(delta: int, accepted: bool) -> None:
    _, candidate, expected = _fixture()

    def call() -> None:
        validate_ledger_query_result(candidate, expectations=expected, sidecar_received_at_ms=NOW + delta)

    if accepted:
        call()
    else:
        with pytest.raises(LedgerQueryResultProtocolError):
            call()


def test_time_ordering_equality_old_updated_and_no_request_cross_clock_order() -> None:
    private, candidate, expected = _fixture()
    for updated, responded in ((NOW, NOW), (0, NOW)):
        changed = _seal(private, {**candidate, "updated_at_ms": updated, "responded_at_ms": responded})
        validate_ledger_query_result(changed, expectations=expected, sidecar_received_at_ms=responded)
    future_query = dict(expected.outstanding_query.ledger_query)
    future_query["requested_at_ms"] = NOW + 1_000_000
    future_request_digest = domain_digest(
        "anchor-amp/ledger-query-request/v1",
        {key: item for key, item in future_query.items() if key != "sidecar_signature_base64"},
    )
    future_expected = replace(
        expected,
        outstanding_query=LedgerQueryConformance(future_query, future_request_digest),
    )
    future_candidate = _seal(private, {**candidate, "query_request_digest": future_request_digest})
    validate_ledger_query_result(future_candidate, expectations=future_expected, sidecar_received_at_ms=NOW)
    changed = _seal(private, {**candidate, "updated_at_ms": NOW + 1})
    with pytest.raises(LedgerQueryResultProtocolError):
        validate_ledger_query_result(changed, expectations=expected, sidecar_received_at_ms=NOW)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("query_instance_id", "query-2"), ("checkout_instance_id", "checkout-2"),
        ("ledger_generation_id", "ledger-2"), ("sidecar_instance_id", "sidecar-2"),
        ("gateway_handshake_digest", "5" * 64), ("readiness_receipt_digest", "6" * 64),
        ("query_request_digest", "7" * 64), ("deployment_attestation_digest", "8" * 64),
    ],
)
def test_every_result_pairing_rejected_after_coherent_digest_and_authentic_resigning(field: str, bad: str) -> None:
    private, candidate, expected = _fixture()
    with pytest.raises(LedgerQueryResultProtocolError):
        validate_ledger_query_result(_seal(private, {**candidate, field: bad}), expectations=expected,
                                     sidecar_received_at_ms=NOW)


@pytest.mark.parametrize("field", ["checkout_instance_id", "ledger_generation_id", "sidecar_instance_id",
                                    "gateway_handshake_digest", "readiness_receipt_digest",
                                    "deployment_attestation_digest"])
def test_each_current_fact_is_independently_compared(field: str) -> None:
    _, candidate, expected = _fixture()
    bad = "other" if field.endswith("instance_id") else "9" * 64
    with pytest.raises(LedgerQueryResultProtocolError):
        validate_ledger_query_result(candidate, expectations=replace(expected, **{field: bad}),
                                     sidecar_received_at_ms=NOW)


def test_outstanding_digest_is_recomputed_and_exact_types_required() -> None:
    _, candidate, expected = _fixture()
    corrupt = replace(expected.outstanding_query, ledger_query_request_digest="a" * 64)
    class QuerySubclass(LedgerQueryConformance):
        pass
    class ExpectationsSubclass(LedgerQueryResultExpectations):
        pass
    for facts in (
        replace(expected, outstanding_query=corrupt),
        replace(expected, outstanding_query=QuerySubclass(**expected.outstanding_query.__dict__)),
        ExpectationsSubclass(**expected.__dict__),
    ):
        with pytest.raises(LedgerQueryResultProtocolError):
            validate_ledger_query_result(candidate, expectations=facts, sidecar_received_at_ms=NOW)


@pytest.mark.parametrize("signature", [None, "not-base64", base64.b64encode(b"x").decode()])
def test_outstanding_query_signature_shape_is_revalidated(signature: object) -> None:
    _, candidate, expected = _fixture()
    query = dict(expected.outstanding_query.ledger_query)
    query["sidecar_signature_base64"] = signature
    facts = replace(
        expected,
        outstanding_query=LedgerQueryConformance(
            query,
            expected.outstanding_query.ledger_query_request_digest,
        ),
    )
    with pytest.raises(LedgerQueryResultProtocolError):
        validate_ledger_query_result(candidate, expectations=facts, sidecar_received_at_ms=NOW)


def test_noncanonical_outstanding_signature_and_non_mapping_query_are_rejected() -> None:
    _, candidate, expected = _fixture()
    query = dict(expected.outstanding_query.ledger_query)
    canonical = query["sidecar_signature_base64"]
    decoded = base64.b64decode(canonical, validate=True)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    index = alphabet.index(canonical[-3])
    query["sidecar_signature_base64"] = canonical[:-3] + alphabet[index ^ 1] + "=="
    assert base64.b64decode(query["sidecar_signature_base64"], validate=True) == decoded
    noncanonical = replace(
        expected,
        outstanding_query=LedgerQueryConformance(
            query,
            expected.outstanding_query.ledger_query_request_digest,
        ),
    )
    list_of_pairs = replace(
        expected,
        outstanding_query=LedgerQueryConformance(
            list(expected.outstanding_query.ledger_query.items()),  # type: ignore[arg-type]
            expected.outstanding_query.ledger_query_request_digest,
        ),
    )
    for facts in (noncanonical, list_of_pairs):
        with pytest.raises(LedgerQueryResultProtocolError):
            validate_ledger_query_result(candidate, expectations=facts, sidecar_received_at_ms=NOW)


def test_exact_schema_strict_values_and_stable_protocol_error() -> None:
    private, candidate, expected = _fixture()
    assert len(candidate) == 15
    cases = [{**candidate, "extra": None}, {k: v for k, v in candidate.items() if k != "state"}]
    for field, bad in (("query_version", True), ("state", "closed"), ("updated_at_ms", -1),
                       ("responded_at_ms", 1.0), ("query_instance_id", "bad id"),
                       ("deployment_attestation_digest", None)):
        cases.append(_seal(private, {**candidate, field: bad}) if field != "responded_at_ms" else {**candidate, field: bad})
    for value in cases:
        with pytest.raises(LedgerQueryResultProtocolError):
            validate_ledger_query_result(value, expectations=expected, sidecar_received_at_ms=NOW)
    with pytest.raises(LedgerQueryResultProtocolError):
        validate_ledger_query_result(candidate, expectations=expected, sidecar_received_at_ms=True)


def test_digest_excludes_digest_and_signature_while_signature_includes_digest() -> None:
    private, candidate, expected = _fixture()
    unsigned_digest = {k: v for k, v in candidate.items() if k not in {"query_digest", "attestor_signature_base64"}}
    assert candidate["query_digest"] == domain_digest("anchor-amp/ledger-query-result/v1", unsigned_digest)
    wrong_signature_object = {k: v for k, v in candidate.items() if k not in {"query_digest", "attestor_signature_base64"}}
    candidate["attestor_signature_base64"] = base64.b64encode(private.sign(
        domain_preimage("anchor-amp/ledger-query-result-signature/v1", wrong_signature_object))).decode()
    with pytest.raises(LedgerQueryResultProtocolError):
        validate_ledger_query_result(candidate, expectations=expected, sidecar_received_at_ms=NOW)


def test_wrong_domains_key_digest_signature_and_noncanonical_key() -> None:
    private, candidate, expected = _fixture()
    other, _, other_expected = _fixture()
    decoded = base64.b64decode(expected.attestor_public_key_base64)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    index = alphabet.index(expected.attestor_public_key_base64[-2])
    noncanonical = expected.attestor_public_key_base64[:-2] + alphabet[index ^ 1] + "="
    assert base64.b64decode(noncanonical, validate=True) == decoded
    cases = [
        (_seal(private, candidate, digest_domain="anchor-amp/wrong/v1"), expected),
        (_seal(private, candidate, signature_domain="anchor-amp/wrong/v1"), expected),
        (candidate, replace(expected, attestor_public_key_digest=D1)),
        (candidate, replace(expected, attestor_public_key_base64=other_expected.attestor_public_key_base64,
                            attestor_public_key_digest=other_expected.attestor_public_key_digest)),
        ({**candidate, "attestor_signature_base64": base64.b64encode(other.sign(b"bad")).decode()}, expected),
        (candidate, replace(expected, attestor_public_key_base64=noncanonical)),
    ]
    for value, facts in cases:
        with pytest.raises(LedgerQueryResultProtocolError):
            validate_ledger_query_result(value, expectations=facts, sidecar_received_at_ms=NOW)


def test_noncanonical_signature_base64_rejected_with_decoded_bytes_held_constant() -> None:
    _, candidate, expected = _fixture()
    canonical = candidate["attestor_signature_base64"]
    decoded = base64.b64decode(canonical, validate=True)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    index = alphabet.index(canonical[-3])
    noncanonical = canonical[:-3] + alphabet[index ^ 1] + "=="
    assert base64.b64decode(noncanonical, validate=True) == decoded
    candidate["attestor_signature_base64"] = noncanonical
    with pytest.raises(LedgerQueryResultProtocolError):
        validate_ledger_query_result(candidate, expectations=expected, sidecar_received_at_ms=NOW)


def test_source_is_pure_peer_neutral_and_has_no_prohibited_callers() -> None:
    root = Path(__file__).parents[2]
    source = (root / "src/odibi_anchor/_governance_protocol/_ledger_query_result.py").read_text()
    for token in ("_governance_ledger", "_kernel", "_sidecar", "plugin", "socket", "subprocess", "open(",
                  "Ed25519PrivateKey"):
        assert token not in source
    callers = []
    for path in (root / "src").rglob("*.py"):
        if path.name != "_ledger_query_result.py" and "_ledger_query_result" in path.read_text():
            callers.append(path)
    assert callers == []
