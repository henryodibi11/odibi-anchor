from __future__ import annotations

import base64
import hashlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odibi_anchor._governance_protocol._canonical import domain_digest, domain_preimage
from odibi_anchor._governance_protocol._ledger_query import (
    LedgerQueryExpectations,
    LedgerQueryProtocolError,
    validate_ledger_query,
)

NOW = 1_800_000_000_000
D1, D2, D3 = "1" * 64, "2" * 64, "3" * 64


def _sign(
    private: Ed25519PrivateKey,
    fields: dict,
    *,
    signature_domain: str = "anchor-amp/ledger-query-signature/v1",
) -> dict:
    query = dict(fields)
    query.pop("sidecar_signature_base64", None)
    signature = private.sign(domain_preimage(signature_domain, query))
    query["sidecar_signature_base64"] = base64.b64encode(signature).decode()
    return query


def _fixture() -> tuple[Ed25519PrivateKey, dict, LedgerQueryExpectations]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    expected = LedgerQueryExpectations(
        "checkout-1",
        "ledger-1",
        "sidecar-1",
        D1,
        D2,
        D3,
        base64.b64encode(public).decode(),
        hashlib.sha256(public).hexdigest(),
    )
    query = {
        "query_version": 1,
        "query_instance_id": "query-1",
        "checkout_instance_id": expected.checkout_instance_id,
        "ledger_generation_id": expected.ledger_generation_id,
        "sidecar_instance_id": expected.sidecar_instance_id,
        "gateway_handshake_digest": expected.gateway_handshake_digest,
        "readiness_receipt_digest": expected.readiness_receipt_digest,
        "deployment_attestation_digest": expected.deployment_attestation_digest,
        "requested_at_ms": NOW,
    }
    return private, _sign(private, query), expected


def test_valid_request_is_detached_deeply_immutable_and_explicitly_inert() -> None:
    _, query, expected = _fixture()
    result = validate_ledger_query(query, expectations=expected, attestor_received_at_ms=NOW)
    unsigned = {key: item for key, item in query.items() if key != "sidecar_signature_base64"}
    assert dict(result.ledger_query) == query
    assert result.ledger_query_request_digest == domain_digest("anchor-amp/ledger-query-request/v1", unsigned)
    assert not any(
        (
            result.authorizes_readiness,
            result.authorizes_closure,
            result.authorizes_state_transition,
            result.proves_randomness,
            result.proves_replay_uniqueness,
            result.records_replay_state,
            result.is_response,
        )
    )
    query["query_instance_id"] = "query-mutated"
    assert result.ledger_query["query_instance_id"] == "query-1"
    with pytest.raises(TypeError):
        result.ledger_query["query_instance_id"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.is_response = True  # type: ignore[misc]


@pytest.mark.parametrize("delta, accepted", [(-60_001, False), (-60_000, True), (60_000, True), (60_001, False)])
def test_exact_symmetric_inclusive_freshness(delta: int, accepted: bool) -> None:
    _, query, expected = _fixture()

    def call() -> None:
        validate_ledger_query(query, expectations=expected, attestor_received_at_ms=NOW + delta)

    if accepted:
        call()
    else:
        with pytest.raises(LedgerQueryProtocolError):
            call()


@pytest.mark.parametrize(
    "field,bad",
    [
        ("checkout_instance_id", "checkout-2"),
        ("ledger_generation_id", "ledger-2"),
        ("sidecar_instance_id", "sidecar-2"),
        ("gateway_handshake_digest", "4" * 64),
        ("readiness_receipt_digest", "5" * 64),
        ("deployment_attestation_digest", "6" * 64),
    ],
)
def test_every_supplied_generation_pairing_rejects_after_authentic_resigning(field: str, bad: str) -> None:
    private, query, expected = _fixture()
    query[field] = bad
    with pytest.raises(LedgerQueryProtocolError):
        validate_ledger_query(_sign(private, query), expectations=expected, attestor_received_at_ms=NOW)


def test_query_instance_requires_only_id_syntax_and_is_not_an_expected_pairing() -> None:
    private, query, expected = _fixture()
    query["query_instance_id"] = "caller:selected.id:2"
    validate_ledger_query(_sign(private, query), expectations=expected, attestor_received_at_ms=NOW)


def test_exact_ten_key_schema_and_safe_scalar_forms() -> None:
    private, query, expected = _fixture()
    assert len(query) == 10
    candidates = [{**query, "extra": None}, {key: value for key, value in query.items() if key != "query_instance_id"}]
    for candidate in candidates:
        with pytest.raises(LedgerQueryProtocolError):
            validate_ledger_query(candidate, expectations=expected, attestor_received_at_ms=NOW)
    for field, bad in (
        ("query_version", True),
        ("query_version", 2),
        ("requested_at_ms", 1.0),
        ("requested_at_ms", -1),
        ("query_instance_id", "bad id"),
        ("gateway_handshake_digest", "A" * 64),
    ):
        changed = {**query, field: bad}
        if type(bad) is not float:
            changed = _sign(private, changed)
        with pytest.raises(LedgerQueryProtocolError):
            validate_ledger_query(changed, expectations=expected, attestor_received_at_ms=NOW)
    with pytest.raises(LedgerQueryProtocolError):
        validate_ledger_query(query, expectations=expected, attestor_received_at_ms=True)


def test_exact_expectations_runtime_type_and_safe_expected_fields() -> None:
    _, query, expected = _fixture()

    class Subclass(LedgerQueryExpectations):
        pass

    for facts in ({}, Subclass(**expected.__dict__), replace(expected, checkout_instance_id="bad id")):
        with pytest.raises(LedgerQueryProtocolError):
            validate_ledger_query(query, expectations=facts, attestor_received_at_ms=NOW)  # type: ignore[arg-type]


def test_digest_and_signature_use_exact_domains_and_exclude_only_signature() -> None:
    private, query, expected = _fixture()
    unsigned = {key: item for key, item in query.items() if key != "sidecar_signature_base64"}
    result = validate_ledger_query(query, expectations=expected, attestor_received_at_ms=NOW)
    assert result.ledger_query_request_digest == domain_digest("anchor-amp/ledger-query-request/v1", unsigned)
    assert result.ledger_query_request_digest != domain_digest("anchor-amp/wrong/v1", unsigned)
    signed_with_signature = dict(query)
    query["sidecar_signature_base64"] = base64.b64encode(
        private.sign(domain_preimage("anchor-amp/ledger-query-signature/v1", signed_with_signature))
    ).decode()
    with pytest.raises(LedgerQueryProtocolError):
        validate_ledger_query(query, expectations=expected, attestor_received_at_ms=NOW)


def test_wrong_signature_domain_key_digest_alternate_key_and_malformed_signature() -> None:
    private, query, expected = _fixture()
    other_private, _, other_expected = _fixture()
    candidates = [
        (_sign(private, query, signature_domain="anchor-amp/wrong/v1"), expected),
        (query, replace(expected, closure_public_key_digest=D1)),
        (
            query,
            replace(
                expected,
                closure_public_key_base64=other_expected.closure_public_key_base64,
                closure_public_key_digest=other_expected.closure_public_key_digest,
            ),
        ),
        ({**query, "sidecar_signature_base64": base64.b64encode(other_private.sign(b"wrong")).decode()}, expected),
        ({**query, "sidecar_signature_base64": "not-base64"}, expected),
    ]
    for candidate, facts in candidates:
        with pytest.raises(LedgerQueryProtocolError):
            validate_ledger_query(candidate, expectations=facts, attestor_received_at_ms=NOW)


def test_noncanonical_public_key_base64_rejected_with_decoded_bytes_and_digest_held_constant() -> None:
    _, query, expected = _fixture()
    decoded = base64.b64decode(expected.closure_public_key_base64)
    # Alter only discarded pad bits, preserving the decoded key and its expected digest.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    canonical_index = alphabet.index(expected.closure_public_key_base64[-2])
    noncanonical = expected.closure_public_key_base64[:-2] + alphabet[canonical_index ^ 1] + "="
    assert base64.b64decode(noncanonical, validate=True) == decoded
    with pytest.raises(LedgerQueryProtocolError):
        validate_ledger_query(
            query,
            expectations=replace(expected, closure_public_key_base64=noncanonical),
            attestor_received_at_ms=NOW,
        )


def test_source_is_peer_neutral_and_has_no_prohibited_integration() -> None:
    source = (Path(__file__).parents[2] / "src/odibi_anchor/_governance_protocol/_ledger_query.py").read_text()
    prohibited = ("_governance_ledger", "_kernel", "sidecar", "plugin", "socket", "subprocess", "open(", "Ed25519PrivateKey")
    # "sidecar" is a protocol field, so prove it is never imported rather than banning the field name.
    for token in prohibited:
        if token == "sidecar":
            assert "import sidecar" not in source and "from ._sidecar" not in source
        else:
            assert token not in source
