"""Cross-peer conformance coverage for the inert binding validator."""

from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError

import pytest

from odibi_anchor._governance_protocol._canonical import domain_digest, domain_preimage
from odibi_anchor._governance_protocol._deployment_binding import (
    AuthorityProtocolError,
    BindingExpectations,
    validate_deployment_binding,
)
from odibi_anchor._governance_sidecar._crypto import sign_deployment_binding
from odibi_anchor._governance_sidecar._readiness import build_readiness_conformance_candidate
from tests._governance_sidecar.test_readiness import _signed_fixture


def _candidate(tmp_path, monkeypatch):
    identity, _, _, release, payload, inputs, now = _signed_fixture(tmp_path, monkeypatch)
    candidate = build_readiness_conformance_candidate(
        identity=identity,
        payload=payload,
        trusted_release_pin=release,
        trusted_attestor_pin=payload["attestor_pin"],
        now_ms=now,
        local_inputs=inputs,
    )
    expectations = BindingExpectations(
        identity.handshake,
        payload["deployment_claim"]["readiness_receipt"],
        release,
        candidate.deployment_attestation["repository_evidence"],
    )
    return identity, candidate, expectations, now


def _resign(identity, binding: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in binding.items() if key != "sidecar_signature_base64"}
    return sign_deployment_binding(identity, unsigned)


def _thaw(value):
    if hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _cascade(identity, candidate, expectations, *, handshake=None, receipt=None, release=None, repository=None, attestation=None):
    handshake = _thaw(expectations.sidecar_handshake) if handshake is None else handshake
    receipt = _thaw(expectations.readiness_receipt) if receipt is None else receipt
    release = _thaw(expectations.release_pin) if release is None else release
    repository = _thaw(expectations.repository_evidence) if repository is None else repository
    attestation = _thaw(candidate.deployment_attestation) if attestation is None else attestation
    binding = _thaw(candidate.deployment_binding)
    handshake_digest = domain_digest("anchor-amp/sidecar-handshake/v1", handshake)
    receipt["gateway_handshake_digest"] = handshake_digest
    receipt_digest = domain_digest("anchor-amp/readiness/v1", receipt)
    attestation["readiness_receipt_digest"] = receipt_digest
    binding["gateway_handshake_digest"] = handshake_digest
    binding["readiness_receipt_digest"] = receipt_digest
    binding["deployment_attestation"] = attestation
    binding["deployment_attestation_digest"] = domain_digest("anchor-amp/deployment/v1", attestation)
    return (
        _resign(identity, binding),
        BindingExpectations(handshake, receipt, release, repository),
    )


def test_sidecar_candidate_validates_without_authorizing(tmp_path, monkeypatch) -> None:
    _, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    result = validate_deployment_binding(
        dict(candidate.deployment_binding),
        expectations=expectations,
        received_at_ms=now,
    )
    assert result.deployment_attestation_digest == candidate.deployment_attestation_digest
    assert result.deployment_binding_request_digest == candidate.deployment_binding_request_digest
    assert result.authorizes_readiness is False
    assert result.is_ack is False
    with pytest.raises(FrozenInstanceError):
        result.is_ack = True
    with pytest.raises(TypeError):
        result.deployment_attestation["amp_workspace_id"] = "changed"
    with pytest.raises(TypeError):
        result.deployment_attestation["release_pin"]["anchor_version"] = "changed"


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_binding_keyset_is_exact(tmp_path, monkeypatch, change) -> None:
    _, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    binding = dict(candidate.deployment_binding)
    if change == "missing":
        binding.pop("binding_version")
    else:
        binding["extra"] = None
    with pytest.raises(AuthorityProtocolError):
        validate_deployment_binding(binding, expectations=expectations, received_at_ms=now)


@pytest.mark.parametrize("received", [True, 9_007_199_254_740_992])
def test_received_time_must_be_safe_integer(tmp_path, monkeypatch, received) -> None:
    _, candidate, expectations, _ = _candidate(tmp_path, monkeypatch)
    with pytest.raises(AuthorityProtocolError):
        validate_deployment_binding(dict(candidate.deployment_binding), expectations=expectations, received_at_ms=received)


@pytest.mark.parametrize("offset, accepted", [(-60_001, False), (-60_000, True)])
def test_binding_freshness_is_inclusive(tmp_path, monkeypatch, offset, accepted) -> None:
    _, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    def call():
        return validate_deployment_binding(
            dict(candidate.deployment_binding), expectations=expectations, received_at_ms=now + offset,
        )
    if accepted:
        call()
    else:
        with pytest.raises(AuthorityProtocolError, match="stale"):
            call()


def test_receipt_expiry_is_inclusive(tmp_path, monkeypatch) -> None:
    _, candidate, expectations, _ = _candidate(tmp_path, monkeypatch)
    expires = expectations.readiness_receipt["expires_at_ms"]
    validate_deployment_binding(
        dict(candidate.deployment_binding), expectations=expectations, received_at_ms=expires,
    )
    with pytest.raises(AuthorityProtocolError, match="stale"):
        validate_deployment_binding(
            dict(candidate.deployment_binding), expectations=expectations, received_at_ms=expires + 1,
        )


def test_coherent_handshake_challenge_substitution_reaches_pairing_check(tmp_path, monkeypatch) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    handshake = _thaw(expectations.sidecar_handshake)
    handshake["challenge"] = base64.b64encode(b"x" * 32).decode()
    binding, altered = _cascade(identity, candidate, expectations, handshake=handshake)
    with pytest.raises(AuthorityProtocolError, match="handshake mismatch"):
        validate_deployment_binding(binding, expectations=altered, received_at_ms=now)


def test_coherent_closure_key_substitution_reaches_pairing_check(tmp_path, monkeypatch) -> None:
    import hashlib

    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    receipt = _thaw(expectations.readiness_receipt)
    alternate_key = b"k" * 32
    receipt["closure_public_key_base64"] = base64.b64encode(alternate_key).decode()
    receipt["closure_public_key_digest"] = hashlib.sha256(alternate_key).hexdigest()
    attestation = _thaw(candidate.deployment_attestation)
    attestation["closure_public_key_digest"] = receipt["closure_public_key_digest"]
    binding, altered = _cascade(
        identity, candidate, expectations, receipt=receipt, attestation=attestation,
    )
    with pytest.raises(AuthorityProtocolError, match="handshake mismatch"):
        validate_deployment_binding(binding, expectations=altered, received_at_ms=now)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("setup_plugin_digest", "0" * 64), ("setup_wheel_digest", "0" * 64), ("amp_version", "other-amp")],
)
def test_coherent_release_substitution_reaches_pairing_check(tmp_path, monkeypatch, field, replacement) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    receipt = _thaw(expectations.readiness_receipt)
    receipt[field] = replacement
    binding, altered = _cascade(identity, candidate, expectations, receipt=receipt)
    with pytest.raises(AuthorityProtocolError, match="release mismatch"):
        validate_deployment_binding(binding, expectations=altered, received_at_ms=now)


@pytest.mark.parametrize(("field", "replacement"), [("repository_id", "https://github.com/other/project"), ("canonical_root", "/other/root")])
def test_coherent_repository_substitution_reaches_pairing_check(tmp_path, monkeypatch, field, replacement) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    receipt = _thaw(expectations.readiness_receipt)
    receipt[field] = replacement
    binding, altered = _cascade(identity, candidate, expectations, receipt=receipt)
    with pytest.raises(AuthorityProtocolError, match="repository mismatch"):
        validate_deployment_binding(binding, expectations=altered, received_at_ms=now)


def test_noncanonical_repository_evidence_is_not_normalized_before_verification(tmp_path, monkeypatch) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    repository = _thaw(expectations.repository_evidence)
    repository["fingerprint"]["repository_id"] = "HTTPS://GitHub.COM/Example/Project.git/"
    attestation = _thaw(candidate.deployment_attestation)
    attestation["repository_evidence"] = repository
    binding, altered = _cascade(
        identity, candidate, expectations, repository=repository, attestation=attestation,
    )
    with pytest.raises(AuthorityProtocolError):
        validate_deployment_binding(binding, expectations=altered, received_at_ms=now)


def test_non_git_null_repository_pair_is_accepted(tmp_path, monkeypatch) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    receipt = _thaw(expectations.readiness_receipt)
    receipt["repository_id"] = None
    repository = {"outcome": "unavailable", "reason": "non_git_read_only"}
    attestation = _thaw(candidate.deployment_attestation)
    attestation["repository_evidence"] = repository
    binding, altered = _cascade(
        identity, candidate, expectations, receipt=receipt, repository=repository,
        attestation=attestation,
    )
    assert validate_deployment_binding(
        binding, expectations=altered, received_at_ms=now,
    ).authorizes_readiness is False


def test_positive_freshness_boundary_is_inclusive(tmp_path, monkeypatch) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    receipt = _thaw(expectations.readiness_receipt)
    receipt["expires_at_ms"] = now + 60_000
    binding, altered = _cascade(identity, candidate, expectations, receipt=receipt)
    validate_deployment_binding(binding, expectations=altered, received_at_ms=now + 60_000)
    with pytest.raises(AuthorityProtocolError, match="stale"):
        validate_deployment_binding(binding, expectations=altered, received_at_ms=now + 60_001)


@pytest.mark.parametrize("field", ["checkout_instance_id", "gateway_handshake_digest", "readiness_receipt_digest"])
def test_authentically_resigned_binding_substitution_fails_closed(tmp_path, monkeypatch, field) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    binding = _thaw(candidate.deployment_binding)
    binding[field] = "other" if field.endswith("_id") else "0" * 64
    with pytest.raises(AuthorityProtocolError):
        validate_deployment_binding(
            _resign(identity, binding), expectations=expectations, received_at_ms=now,
        )


def test_authentically_resigned_attestation_substitution_fails_closed(tmp_path, monkeypatch) -> None:
    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    binding = _thaw(candidate.deployment_binding)
    binding["deployment_attestation"]["amp_workspace_id"] = "other-workspace"
    binding["deployment_attestation_digest"] = domain_digest(
        "anchor-amp/deployment/v1", binding["deployment_attestation"],
    )
    with pytest.raises(AuthorityProtocolError, match="expectations"):
        validate_deployment_binding(
            _resign(identity, binding), expectations=expectations, received_at_ms=now,
        )


def test_wrong_signature_and_malformed_expectations_fail_closed(tmp_path, monkeypatch) -> None:
    _, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    binding = _thaw(candidate.deployment_binding)
    binding["sidecar_signature_base64"] = "A" * 86 + "=="
    with pytest.raises(AuthorityProtocolError):
        validate_deployment_binding(binding, expectations=expectations, received_at_ms=now)
    with pytest.raises(AuthorityProtocolError, match="expectations"):
        validate_deployment_binding(
            dict(candidate.deployment_binding), expectations={}, received_at_ms=now,  # type: ignore[arg-type]
        )


def test_wrong_signature_domain_and_alternate_key_fail_closed(tmp_path, monkeypatch) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    identity, candidate, expectations, now = _candidate(tmp_path, monkeypatch)
    binding = _thaw(candidate.deployment_binding)
    unsigned = {key: value for key, value in binding.items() if key != "sidecar_signature_base64"}
    for signature in (
        identity._private_key.sign(domain_preimage("anchor-amp/closure-submission-signature/v1", unsigned)),
        Ed25519PrivateKey.generate().sign(
            domain_preimage("anchor-amp/deployment-binding-signature/v1", unsigned),
        ),
    ):
        binding["sidecar_signature_base64"] = base64.b64encode(signature).decode()
        with pytest.raises(AuthorityProtocolError):
            validate_deployment_binding(binding, expectations=expectations, received_at_ms=now)


def test_compatibility_modules_retain_historical_symbols() -> None:
    from odibi_anchor._governance_sidecar import _canonical, _crypto, _readiness_protocol

    assert set(_canonical.__all__) == {
        "Any", "CanonicalError", "JS_SAFE_INTEGER", "MAX_DEPTH", "MAX_ITEMS",
        "MAX_STRING_BYTES", "Mapping", "Sequence", "domain_digest", "domain_preimage",
        "annotations", "dumps", "hashlib", "normalize", "safe_integer", "unicodedata",
    }
    assert set(_readiness_protocol.__all__) == {
        "Any", "COMMIT", "Callable", "CanonicalError", "EFFECTS", "ID_PATTERN", "PCHAR",
        "PYTHON_MINOR", "PurePath", "ReadinessProtocolError", "SCOPES", "SHA", "SOURCES",
        "UNRESERVED", "annotations", "base64", "normalize", "normalize_repository_id", "re",
        "safe_integer", "string", "urlsplit", "urlunsplit", "validate_bootstrap_payload",
    }
    assert {
        "_absolute", "_adapter", "_array", "_attestor", "_b64", "_bounded_text",
        "_confirmation", "_digest", "_fail", "_fence", "_id", "_manifest", "_modifiers",
        "_obj", "_provenance", "_receipt", "_release", "_sorted_unique", "_text", "_tool",
        "_validate_bootstrap_payload",
    } <= set(vars(_readiness_protocol))
    assert callable(_canonical._string)
    assert callable(_readiness_protocol._obj)
    assert callable(_readiness_protocol._validate_bootstrap_payload)
    assert _readiness_protocol.ID_PATTERN.fullmatch("protocol-id")
    assert _crypto.hashlib.sha256(b"compatibility").hexdigest()
    assert _canonical.annotations is _readiness_protocol.annotations
    canonical_wildcard: dict[str, object] = {}
    readiness_wildcard: dict[str, object] = {}
    exec("from odibi_anchor._governance_sidecar._canonical import *", canonical_wildcard)
    exec(
        "from odibi_anchor._governance_sidecar._readiness_protocol import *",
        readiness_wildcard,
    )
    assert canonical_wildcard["annotations"] is _canonical.annotations
    assert readiness_wildcard["annotations"] is _readiness_protocol.annotations
