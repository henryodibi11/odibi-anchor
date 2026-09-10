"""Conformance coverage for the private, inert closure validator."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from odibi_anchor._governance_protocol._canonical import domain_digest, domain_preimage
from odibi_anchor._governance_protocol._closure_submission import (
    ClosureExpectations,
    ClosureProtocolError,
    validate_closure_submission,
)

NOW = 1_900_000_000_000
DIGEST = "1" * 64


def _fixture():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    expectations = ClosureExpectations(
        "checkout-1", "ledger-1", "sidecar-1", "2" * 64, "3" * 64, "4" * 64,
        "opaque thread / 你好", "gateway-1", "task-1", base64.b64encode(public).decode(),
        hashlib.sha256(public).hexdigest(),
    )
    receipt = {
        "checkout_instance_id": expectations.checkout_instance_id,
        "ledger_generation_id": expectations.ledger_generation_id,
        "sidecar_instance_id": expectations.sidecar_instance_id,
        "gateway_handshake_digest": expectations.gateway_handshake_digest,
        "readiness_receipt_digest": expectations.readiness_receipt_digest,
        "deployment_attestation_digest": expectations.deployment_attestation_digest,
        "thread_id": expectations.thread_id,
        "gateway_generation": expectations.gateway_generation,
        "task_generation": expectations.task_generation,
        "gate_receipt_digest": "5" * 64,
        "learning_receipt_digest": "6" * 64,
        "closed_at_ms": NOW + 500_000,
    }
    submission = {
        "submission_version": 1,
        "checkout_instance_id": expectations.checkout_instance_id,
        "ledger_generation_id": expectations.ledger_generation_id,
        "sidecar_instance_id": expectations.sidecar_instance_id,
        "closure_receipt": receipt,
        "closure_digest": domain_digest("anchor-amp/task-closure/v1", receipt),
        "submitted_at_ms": NOW,
        "sidecar_signature_base64": "",
    }
    return private, expectations, _resign(private, submission)


def _resign(private, submission, *, domain="anchor-amp/closure-submission-signature/v1"):
    unsigned = {key: value for key, value in submission.items() if key != "sidecar_signature_base64"}
    submission["sidecar_signature_base64"] = base64.b64encode(
        private.sign(domain_preimage(domain, unsigned))
    ).decode()
    return submission


def _cascade(private, submission):
    submission["closure_digest"] = domain_digest("anchor-amp/task-closure/v1", submission["closure_receipt"])
    return _resign(private, submission)


def test_valid_opaque_thread_result_is_deeply_immutable_and_non_authorizing() -> None:
    _, expected, submission = _fixture()
    result = validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)
    assert result.closure_digest == submission["closure_digest"]
    unsigned = {key: value for key, value in submission.items() if key != "sidecar_signature_base64"}
    assert result.closure_submission_request_digest == domain_digest(
        "anchor-amp/closure-submission-request/v1", unsigned
    )
    assert (result.authorizes_closure, result.authorizes_state_transition, result.is_ack) == (False, False, False)
    with pytest.raises(FrozenInstanceError):
        result.is_ack = True
    with pytest.raises(TypeError):
        result.closure_submission["submitted_at_ms"] = 0
    with pytest.raises(TypeError):
        result.closure_receipt["thread_id"] = "other"


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("change", ["missing", "extra"])
def test_outer_and_nested_keysets_are_exact(nested, change) -> None:
    _, expected, submission = _fixture()
    target = submission["closure_receipt"] if nested else submission
    if change == "missing":
        target.pop("closed_at_ms" if nested else "submission_version")
    else:
        target["extra"] = None
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


@pytest.mark.parametrize("location,field", [("outer", "submission_version"), ("outer", "submitted_at_ms"), ("receipt", "closed_at_ms")])
@pytest.mark.parametrize("bad", [True, 9_007_199_254_740_992])
def test_document_integer_type_and_safe_boundaries(location, field, bad) -> None:
    _, expected, submission = _fixture()
    (submission if location == "outer" else submission["closure_receipt"])[field] = bad
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


@pytest.mark.parametrize("bad", [True, -1, 9_007_199_254_740_992])
def test_received_time_is_a_nonnegative_safe_integer(bad) -> None:
    _, expected, submission = _fixture()
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=expected, received_at_ms=bad)


@pytest.mark.parametrize("offset,accepted", [(-60_001, False), (-60_000, True), (60_000, True), (60_001, False)])
def test_freshness_is_exact_and_inclusive(offset, accepted) -> None:
    _, expected, submission = _fixture()

    def call():
        return validate_closure_submission(submission, expectations=expected, received_at_ms=NOW + offset)

    if accepted:
        call()
    else:
        with pytest.raises(ClosureProtocolError, match="stale"):
            call()


@pytest.mark.parametrize("field", ["checkout_instance_id", "ledger_generation_id", "sidecar_instance_id"])
@pytest.mark.parametrize("location", ["outer", "receipt", "expectation"])
def test_every_identity_pairing_is_checked_after_authentic_resigning(field, location) -> None:
    private, expected, submission = _fixture()
    if location == "outer":
        submission[field] = "alternate"
        _resign(private, submission)
    elif location == "receipt":
        submission["closure_receipt"][field] = "alternate"
        _cascade(private, submission)
    else:
        expected = replace(expected, **{field: "alternate"})
    with pytest.raises(ClosureProtocolError, match="identity"):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


@pytest.mark.parametrize("field", [
    "gateway_handshake_digest", "readiness_receipt_digest", "deployment_attestation_digest",
    "thread_id", "gateway_generation", "task_generation",
])
def test_every_supplied_receipt_fact_pairing_is_checked_with_coherent_hostile_document(field) -> None:
    private, expected, submission = _fixture()
    replacement = "alternate" if field in {"thread_id", "gateway_generation", "task_generation"} else DIGEST
    submission["closure_receipt"][field] = replacement
    _cascade(private, submission)
    with pytest.raises(ClosureProtocolError, match="expectations"):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


def test_closure_digest_mismatch_is_rejected() -> None:
    private, expected, submission = _fixture()
    submission["closure_digest"] = DIGEST
    _resign(private, submission)
    with pytest.raises(ClosureProtocolError, match="digest"):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


def test_wrong_signature_domain_and_alternate_key_are_rejected() -> None:
    private, expected, submission = _fixture()
    _resign(private, submission, domain="anchor-amp/deployment-binding-signature/v1")
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)
    alternate = Ed25519PrivateKey.generate()
    _resign(alternate, submission)
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


def test_malformed_signature_base64_is_rejected() -> None:
    _, expected, submission = _fixture()
    submission["sidecar_signature_base64"] = "A" * 85 + "==="
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


def test_noncanonical_public_key_base64_is_rejected_with_matching_digest() -> None:
    _, expected, submission = _fixture()
    decoded = b"\0" * 32
    # The low two pad bits in "B" are non-zero, but Python's decoder still yields
    # the same bytes as the canonical final "A". Exact re-encoding must reject it.
    noncanonical = "A" * 42 + "B="
    assert base64.b64decode(noncanonical, validate=True) == decoded
    expected = replace(
        expected,
        closure_public_key_base64=noncanonical,
        closure_public_key_digest=hashlib.sha256(decoded).hexdigest(),
    )
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=expected, received_at_ms=NOW)


def test_expectations_require_exact_type_and_normalize_without_attribute_leaks() -> None:
    _, expected, submission = _fixture()
    with pytest.raises(ClosureProtocolError, match="expectations"):
        validate_closure_submission(submission, expectations={}, received_at_ms=NOW)  # type: ignore[arg-type]
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(
            submission, expectations=replace(expected, thread_id="bad\x00thread"), received_at_ms=NOW
        )
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(submission, expectations=replace(expected, thread_id="x" * 513), received_at_ms=NOW)


def test_public_key_digest_and_canonical_key_are_verified() -> None:
    _, expected, submission = _fixture()
    with pytest.raises(ClosureProtocolError):
        validate_closure_submission(
            submission, expectations=replace(expected, closure_public_key_digest=DIGEST), received_at_ms=NOW
        )
