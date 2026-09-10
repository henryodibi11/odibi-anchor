from __future__ import annotations

import base64
import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odibi_anchor._governance_protocol._canonical import domain_digest, domain_preimage
from odibi_anchor._governance_protocol._ledger_ack import (
    LedgerAckExpectations,
    LedgerAckProtocolError,
    validate_ledger_ack,
)

NOW = 1_800_000_000_000
D1, D2, D3 = "1" * 64, "2" * 64, "3" * 64


def _fixture(status: str = "BOUND") -> tuple[Ed25519PrivateKey, dict, LedgerAckExpectations]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    public_b64 = base64.b64encode(public).decode()
    closure = None if status == "BOUND" else D3
    expected = LedgerAckExpectations(
        status, "checkout-1", "ledger-1", D1, D2, closure, public_b64, hashlib.sha256(public).hexdigest()
    )
    ack = {
        "ack_version": 1,
        "status": status,
        "checkout_instance_id": "checkout-1",
        "ledger_generation_id": "ledger-1",
        "request_kind": "deployment_binding" if status == "BOUND" else "closure_submission",
        "request_digest": D1,
        "deployment_attestation_digest": D2,
        "closure_digest": closure,
        "committed_at_ms": NOW,
    }
    return private, _sign(private, ack), expected


def _sign(private: Ed25519PrivateKey, fields: dict, *, digest_domain: str = "anchor-amp/ledger-ack/v1",
          signature_domain: str = "anchor-amp/ledger-ack-signature/v1") -> dict:
    ack = dict(fields)
    ack.pop("acknowledgement_digest", None)
    ack.pop("attestor_signature_base64", None)
    ack["acknowledgement_digest"] = domain_digest(digest_domain, ack)
    signature = private.sign(domain_preimage(signature_domain, ack))
    ack["attestor_signature_base64"] = base64.b64encode(signature).decode()
    return ack


@pytest.mark.parametrize("status", ["BOUND", "CLOSED"])
def test_valid_variants_are_inert_detached_and_immutable(status) -> None:
    _, ack, expected = _fixture(status)
    result = validate_ledger_ack(ack, expectations=expected, received_at_ms=NOW)
    assert dict(result.ledger_ack) == ack
    assert result.acknowledgement_digest == ack["acknowledgement_digest"]
    assert not any((result.authorizes_readiness, result.authorizes_closure, result.authorizes_state_transition,
                    result.establishes_durable_commit, result.verifies_live_fence, result.is_ack))
    ack["status"] = "changed"
    with pytest.raises(TypeError):
        result.ledger_ack["status"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.is_ack = True  # type: ignore[misc]


@pytest.mark.parametrize("delta,accepted", [(-5001, False), (-5000, True), (60000, True), (60001, False)])
@pytest.mark.parametrize("status", ["BOUND", "CLOSED"])
def test_exact_inclusive_freshness(status, delta, accepted) -> None:
    _, ack, expected = _fixture(status)
    def call():
        return validate_ledger_ack(ack, expectations=expected, received_at_ms=NOW + delta)

    if accepted:
        call()
    else:
        with pytest.raises(LedgerAckProtocolError):
            call()


@pytest.mark.parametrize("status,kind,closure,valid", [
    ("BOUND", "deployment_binding", None, True),
    ("BOUND", "closure_submission", None, False),
    ("BOUND", "deployment_binding", D3, False),
    ("CLOSED", "closure_submission", D3, True),
    ("CLOSED", "deployment_binding", D3, False),
    ("CLOSED", "closure_submission", None, False),
])
def test_status_request_closure_matrix(status, kind, closure, valid) -> None:
    private, ack, expected = _fixture(status)
    ack.update(request_kind=kind, closure_digest=closure)
    ack = _sign(private, ack)
    def call():
        return validate_ledger_ack(ack, expectations=expected, received_at_ms=NOW)

    if valid:
        call()
    else:
        with pytest.raises(LedgerAckProtocolError):
            call()


@pytest.mark.parametrize("status,field,bad", [
    ("BOUND", "status", "CLOSED"),
    ("BOUND", "checkout_instance_id", "checkout-2"),
    ("BOUND", "ledger_generation_id", "ledger-2"),
    ("BOUND", "request_digest", "4" * 64),
    ("BOUND", "deployment_attestation_digest", "5" * 64),
    ("CLOSED", "closure_digest", "4" * 64),
])
def test_every_operation_pairing_is_compared_after_authentic_resigning(status, field, bad) -> None:
    private, ack, expected = _fixture(status)
    ack[field] = bad
    ack = _sign(private, ack)
    with pytest.raises(LedgerAckProtocolError):
        validate_ledger_ack(ack, expectations=expected, received_at_ms=NOW)


def test_exact_keyset_and_strict_scalar_forms() -> None:
    private, ack, expected = _fixture()
    for candidate in ({**ack, "extra": None}, {key: value for key, value in ack.items() if key != "status"}):
        with pytest.raises(LedgerAckProtocolError):
            validate_ledger_ack(candidate, expectations=expected, received_at_ms=NOW)
    for field, bad in (("ack_version", True), ("committed_at_ms", 1.0),
                       ("request_digest", "A" * 64), ("checkout_instance_id", "bad id")):
        changed = {**ack, field: bad}
        if not isinstance(bad, float):
            changed = _sign(private, changed)
        with pytest.raises(LedgerAckProtocolError):
            validate_ledger_ack(changed, expectations=expected, received_at_ms=NOW)


def test_digest_excludes_exactly_digest_and_signature_and_signature_includes_digest() -> None:
    private, ack, expected = _fixture()
    unsigned_digest = {k: v for k, v in ack.items() if k not in {"acknowledgement_digest", "attestor_signature_base64"}}
    assert ack["acknowledgement_digest"] == domain_digest("anchor-amp/ledger-ack/v1", unsigned_digest)
    ack["acknowledgement_digest"] = "4" * 64
    signed = {key: value for key, value in ack.items() if key != "attestor_signature_base64"}
    ack["attestor_signature_base64"] = base64.b64encode(
        private.sign(domain_preimage("anchor-amp/ledger-ack-signature/v1", signed))
    ).decode()
    with pytest.raises(LedgerAckProtocolError):
        validate_ledger_ack(ack, expectations=expected, received_at_ms=NOW)
    private, valid, expected = _fixture()
    wrong_preimage = {
        key: value for key, value in valid.items()
        if key not in {"acknowledgement_digest", "attestor_signature_base64"}
    }
    valid["attestor_signature_base64"] = base64.b64encode(
        private.sign(domain_preimage("anchor-amp/ledger-ack-signature/v1", wrong_preimage))
    ).decode()
    with pytest.raises(LedgerAckProtocolError):
        validate_ledger_ack(valid, expectations=expected, received_at_ms=NOW)


def test_wrong_domains_key_digest_and_signature_are_stable_errors() -> None:
    private, ack, expected = _fixture()
    other, _, other_expected = _fixture()
    candidates = [
        (_sign(private, ack, digest_domain="anchor-amp/wrong/v1"), expected),
        (_sign(private, ack, signature_domain="anchor-amp/wrong/v1"), expected),
        (ack, replace(expected, attestor_public_key_base64=other_expected.attestor_public_key_base64,
                      attestor_public_key_digest=other_expected.attestor_public_key_digest)),
        (ack, replace(expected, attestor_public_key_digest=D1)),
        ({**ack, "attestor_signature_base64": base64.b64encode(other.sign(b"wrong")).decode()}, expected),
    ]
    for candidate, facts in candidates:
        with pytest.raises(LedgerAckProtocolError):
            validate_ledger_ack(candidate, expectations=facts, received_at_ms=NOW)


def test_noncanonical_public_key_base64_is_rejected_with_matching_digest() -> None:
    _, ack, expected = _fixture()
    decoded = b"\0" * 32
    noncanonical = "A" * 42 + "B="
    assert base64.b64decode(noncanonical, validate=True) == decoded
    facts = replace(
        expected,
        attestor_public_key_base64=noncanonical,
        attestor_public_key_digest=hashlib.sha256(decoded).hexdigest(),
    )
    with pytest.raises(LedgerAckProtocolError):
        validate_ledger_ack(ack, expectations=facts, received_at_ms=NOW)


def test_invalid_expectations_and_safe_received_time_use_protocol_error() -> None:
    _, ack, expected = _fixture()
    for facts, received in ((replace(expected, status="OPEN"), NOW),
                            (replace(expected, closure_digest=D3), NOW), (expected, True)):
        with pytest.raises(LedgerAckProtocolError):
            validate_ledger_ack(ack, expectations=facts, received_at_ms=received)
    with pytest.raises(LedgerAckProtocolError):
        validate_ledger_ack(ack, expectations={}, received_at_ms=NOW)  # type: ignore[arg-type]
