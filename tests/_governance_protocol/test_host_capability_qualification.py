from __future__ import annotations

import copy
import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest

from odibi_anchor._governance_protocol._canonical import dumps
from odibi_anchor._governance_protocol._host_capability_qualification import (
    CAPABILITY_IDS,
    EVIDENCE_CLASSES,
    MAX_VALIDITY_MS,
    HostCapabilityQualificationExpectations,
    HostCapabilityQualificationProtocolError,
    validate_host_capability_qualification,
)

NOW = 1_800_000_000_000
D1, D2, D3, D4 = "1" * 64, "2" * 64, "3" * 64, "4" * 64
PROVIDER_ID, AUTHORITY_ID = "provider-1", "qualification-authority-1"
AUTHORITY_DIGEST = "6" * 64
TARGET_DOMAIN = b"anchor-governance/host-capability-target/v1"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _target(*, build: str = "amp-build-1") -> dict:
    return {
        "capability_contract_digest": D1,
        "host_product_identity": {"product_id": "host-product-1", "executable_build_id": build},
        "host_adapter_build_digest": D2,
        "host_environment_build_id": "host-image-1",
        "resolved_host_configuration_digest": D3,
        "deployment_topology_digest": D4,
        "qualification_suite_digest": "5" * 64,
    }


def _target_digest(target: dict) -> str:
    return hashlib.sha256(TARGET_DOMAIN + b"\0" + dumps(target)).hexdigest()


def _evidence(capability: str, evidence_class: str, target_digest: str) -> dict:
    provider = evidence_class == "supported_interface_commitment"
    return {
        "evidence_class": evidence_class,
        "record_digest": _digest(f"{capability}:{evidence_class}"),
        "qualification_target_digest": target_digest,
        "issuer_id": PROVIDER_ID if provider else AUTHORITY_ID,
        "issuer_key_or_artifact_digest": D1 if provider else AUTHORITY_DIGEST,
        "conclusion": "PASS",
        "observed_at_ms": NOW - 1_000,
        "valid_until_ms": NOW + 1_000,
    }


def _capability(capability: str, target_digest: str) -> dict:
    return {
        "capability_id": capability,
        "provider_response": "SUPPORTED",
        "evidence": [_evidence(capability, evidence_class, target_digest) for evidence_class in EVIDENCE_CLASSES],
        "material_limitation_digests": [],
        "conflict_digests": [],
    }


def _fixture() -> tuple[dict, HostCapabilityQualificationExpectations]:
    target = _target()
    target_digest = _target_digest(target)
    candidate = {
        "qualification_version": 1,
        "target": target,
        "capabilities": [_capability(capability, target_digest) for capability in CAPABILITY_IDS],
    }
    expected = HostCapabilityQualificationExpectations(
        expected_target=copy.deepcopy(target),
        evaluated_at_ms=NOW,
        provider_issuer_id=PROVIDER_ID,
        provider_issuer_key_or_artifact_digest=D1,
        qualification_authority_id=AUTHORITY_ID,
        qualification_authority_key_or_artifact_digest=AUTHORITY_DIGEST,
        invalidated_target_digests=(),
        invalidated_evidence_record_digests=(),
        invalidation_view_complete=True,
        invalidation_view_observed_at_ms=NOW,
    )
    return candidate, expected


def _record(candidate: dict, capability: str) -> dict:
    return next(item for item in candidate["capabilities"] if item["capability_id"] == capability)


def _validate(candidate: dict, expected: HostCapabilityQualificationExpectations):
    return validate_host_capability_qualification(candidate, expectations=expected)


def test_all_six_supported_is_detached_immutable_order_independent_and_inert() -> None:
    candidate, expected = _fixture()
    candidate["capabilities"].reverse()
    for capability in candidate["capabilities"]:
        capability["evidence"].reverse()
    result = _validate(candidate, expected)
    assert result.effective_status == "SUPPORTED"
    assert set(result.capability_statuses) == set(CAPABILITY_IDS)
    assert set(result.capability_statuses.values()) == {"SUPPORTED"}
    assert result.qualification_target_digest == _target_digest(_target())
    assert [item["capability_id"] for item in result.qualification["capabilities"]] == list(CAPABILITY_IDS)
    assert not any((result.authorizes_readiness, result.production_ready,
                    result.establishes_authority, result.activates_adapter))

    candidate["target"]["host_product_identity"]["product_id"] = "changed"
    candidate["capabilities"][0]["evidence"][0]["conclusion"] = "FAIL"
    assert result.qualification["target"]["host_product_identity"]["product_id"] == "host-product-1"
    assert result.qualification["capabilities"][0]["evidence"][0]["conclusion"] == "PASS"
    with pytest.raises(TypeError):
        result.capability_statuses[CAPABILITY_IDS[0]] = "UNKNOWN"  # type: ignore[index]
    with pytest.raises(TypeError):
        result.qualification["target"]["host_environment_build_id"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.authorizes_readiness = True  # type: ignore[misc]


@pytest.mark.parametrize("response", ["ROADMAP", "UNSUPPORTED"])
def test_provider_roadmap_or_unsupported_reduces_to_unsupported(response: str) -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    capability["provider_response"] = response
    capability["evidence"] = []
    result = _validate(candidate, expected)
    assert result.capability_statuses["effective_inventory"] == "UNSUPPORTED"
    assert result.effective_status == "UNSUPPORTED"


def test_current_independently_issued_fail_precedes_conflict_and_incomplete_invalidation() -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "terminal_lifecycle")
    capability["evidence"][0]["conclusion"] = "FAIL"
    capability["conflict_digests"] = ["6" * 64]
    expected = replace(expected, invalidation_view_complete=False)
    result = _validate(candidate, expected)
    assert result.capability_statuses["terminal_lifecycle"] == "UNSUPPORTED"


def test_temporally_current_pinned_fail_precedes_historical_target_and_record_invalidation() -> None:
    candidate, expected = _fixture()
    historical = _target(build="amp-build-old")
    historical_digest = _target_digest(historical)
    candidate["target"] = historical
    for capability in candidate["capabilities"]:
        for item in capability["evidence"]:
            item["qualification_target_digest"] = historical_digest
    _record(candidate, "terminal_lifecycle")["evidence"][0]["conclusion"] = "FAIL"
    result = _validate(candidate, expected)
    assert result.capability_statuses["terminal_lifecycle"] == "UNSUPPORTED"

    candidate, expected = _fixture()
    item = _record(candidate, "terminal_lifecycle")["evidence"][0]
    item["conclusion"] = "FAIL"
    expected = replace(expected, invalidated_evidence_record_digests=(item["record_digest"],))
    result = _validate(candidate, expected)
    assert result.capability_statuses["terminal_lifecycle"] == "UNSUPPORTED"


@pytest.mark.parametrize("source", ["provider", "authority"])
def test_wrong_pinned_issuer_is_unknown_not_self_attested(source: str) -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    evidence_class = "supported_interface_commitment" if source == "provider" else "threat_review"
    item = next(item for item in capability["evidence"] if item["evidence_class"] == evidence_class)
    item["issuer_id"] = "other-issuer"
    result = _validate(candidate, expected)
    assert result.capability_statuses["effective_inventory"] == "UNKNOWN"


@pytest.mark.parametrize(
    "change",
    [
        {"qualification_authority_id": PROVIDER_ID},
        {"qualification_authority_key_or_artifact_digest": D1},
        {"qualification_authority_key_or_artifact_digest": D2},
    ],
)
def test_qualification_authority_pin_must_be_independent(change: dict) -> None:
    candidate, expected = _fixture()
    with pytest.raises(HostCapabilityQualificationProtocolError):
        _validate(candidate, replace(expected, **change))


def test_conflict_and_incomplete_or_stale_invalidation_view_are_unknown() -> None:
    for mutate, expected_change in (
        (lambda capability: capability["conflict_digests"].append("6" * 64), {}),
        (lambda capability: None, {"invalidation_view_complete": False}),
        (lambda capability: None, {"invalidation_view_observed_at_ms": NOW - 1}),
    ):
        candidate, expected = _fixture()
        capability = _record(candidate, "gateway_admission_revocation")
        mutate(capability)
        expected = replace(expected, **expected_change)
        assert _validate(candidate, expected).capability_statuses["gateway_admission_revocation"] == "UNKNOWN"


def test_historical_homogeneous_target_known_invalidation_and_expired_record_are_expired() -> None:
    candidate, expected = _fixture()
    historical = _target(build="amp-build-old")
    historical_digest = _target_digest(historical)
    candidate["target"] = historical
    for capability in candidate["capabilities"]:
        for item in capability["evidence"]:
            item["qualification_target_digest"] = historical_digest
    result = _validate(candidate, expected)
    assert set(result.capability_statuses.values()) == {"EXPIRED"}

    candidate, expected = _fixture()
    expected = replace(expected, invalidated_target_digests=(_target_digest(_target()),))
    assert set(_validate(candidate, expected).capability_statuses.values()) == {"EXPIRED"}

    candidate, expected = _fixture()
    capability = _record(candidate, "checkout_creation_evidence")
    capability["evidence"][0]["valid_until_ms"] = NOW - 1
    assert _validate(candidate, expected).capability_statuses["checkout_creation_evidence"] == "EXPIRED"


def test_record_invalidation_is_expired_but_untrusted_expired_record_is_unknown() -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    item = capability["evidence"][0]
    expected = replace(expected, invalidated_evidence_record_digests=(item["record_digest"],))
    assert _validate(candidate, expected).capability_statuses["effective_inventory"] == "EXPIRED"

    candidate, expected = _fixture()
    item = _record(candidate, "effective_inventory")["evidence"][0]
    item["issuer_id"] = "other-issuer"
    item["valid_until_ms"] = NOW - 1
    assert _validate(candidate, expected).capability_statuses["effective_inventory"] == "UNKNOWN"


def test_zero_pass_is_unknown_one_to_three_pass_or_limitation_is_partial() -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    capability["evidence"] = []
    assert _validate(candidate, expected).capability_statuses["effective_inventory"] == "UNKNOWN"

    for count in (1, 2, 3):
        candidate, expected = _fixture()
        capability = _record(candidate, "effective_inventory")
        capability["evidence"] = capability["evidence"][:count]
        assert _validate(candidate, expected).capability_statuses["effective_inventory"] == "PARTIAL"

    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    capability["material_limitation_digests"] = ["6" * 64]
    assert _validate(candidate, expected).capability_statuses["effective_inventory"] == "PARTIAL"


def test_provider_unknown_is_unknown_even_with_four_pass_records() -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    capability["provider_response"] = "UNKNOWN"
    assert _validate(candidate, expected).capability_statuses["effective_inventory"] == "UNKNOWN"


@pytest.mark.parametrize(
    "observed,valid,accepted,status",
    [
        (NOW, NOW + MAX_VALIDITY_MS, True, "SUPPORTED"),
        (NOW - MAX_VALIDITY_MS, NOW, True, "SUPPORTED"),
        (NOW + 1, NOW + MAX_VALIDITY_MS, True, "EXPIRED"),
        (NOW - MAX_VALIDITY_MS, NOW - 1, True, "EXPIRED"),
        (NOW, NOW + MAX_VALIDITY_MS + 1, False, None),
        (NOW + 1, NOW, False, None),
    ],
)
def test_inclusive_freshness_and_exact_maximum_interval(
    observed: int, valid: int, accepted: bool, status: str | None
) -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    for item in capability["evidence"]:
        item["observed_at_ms"] = observed
        item["valid_until_ms"] = valid
    if accepted:
        assert _validate(candidate, expected).capability_statuses["effective_inventory"] == status
    else:
        with pytest.raises(HostCapabilityQualificationProtocolError):
            _validate(candidate, expected)


def test_mixed_targets_duplicate_capability_and_duplicate_evidence_class_reject() -> None:
    candidate, expected = _fixture()
    _record(candidate, "effective_inventory")["evidence"][0]["qualification_target_digest"] = "9" * 64
    with pytest.raises(HostCapabilityQualificationProtocolError):
        _validate(candidate, expected)

    candidate, expected = _fixture()
    candidate["capabilities"][-1] = copy.deepcopy(candidate["capabilities"][0])
    with pytest.raises(HostCapabilityQualificationProtocolError):
        _validate(candidate, expected)

    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    capability["evidence"].append(copy.deepcopy(capability["evidence"][0]))
    with pytest.raises(HostCapabilityQualificationProtocolError):
        _validate(candidate, expected)


def test_duplicate_digest_sets_and_invalidation_sets_reject() -> None:
    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    capability["material_limitation_digests"] = [D3, D3]
    with pytest.raises(HostCapabilityQualificationProtocolError):
        _validate(candidate, expected)

    for field in ("invalidated_target_digests", "invalidated_evidence_record_digests"):
        candidate, expected = _fixture()
        with pytest.raises(HostCapabilityQualificationProtocolError):
            _validate(candidate, replace(expected, **{field: (D3, D3)}))


def test_exact_keysets_strict_types_and_stable_error_boundary() -> None:
    candidate, expected = _fixture()

    class ExpectationsSubclass(HostCapabilityQualificationExpectations):
        pass

    cases: list[tuple[object, HostCapabilityQualificationExpectations]] = [
        ({**candidate, "extra": None}, expected),
        ({key: value for key, value in candidate.items() if key != "target"}, expected),
        ({**candidate, "qualification_version": True}, expected),
        ({**candidate, "target": {**candidate["target"], "extra": None}}, expected),
        ({**candidate, "capabilities": tuple(candidate["capabilities"])}, expected),
        (candidate, ExpectationsSubclass(**expected.__dict__)),
        (candidate, replace(expected, evaluated_at_ms=True)),
        (candidate, replace(expected, invalidated_target_digests=[])),  # type: ignore[arg-type]
        (candidate, replace(expected, invalidation_view_complete=1)),  # type: ignore[arg-type]
    ]
    for value, facts in cases:
        with pytest.raises(HostCapabilityQualificationProtocolError):
            _validate(value, facts)

    candidate, expected = _fixture()
    capability = _record(candidate, "effective_inventory")
    capability["evidence"][0]["extra"] = None
    with pytest.raises(HostCapabilityQualificationProtocolError):
        _validate(candidate, expected)


def test_dict_subclasses_are_rejected_at_every_object_boundary() -> None:
    class DictSubclass(dict):
        pass

    mutations = []
    candidate, expected = _fixture()
    mutations.append((DictSubclass(candidate), expected))
    candidate, expected = _fixture()
    candidate["target"] = DictSubclass(candidate["target"])
    mutations.append((candidate, expected))
    candidate, expected = _fixture()
    candidate["target"]["host_product_identity"] = DictSubclass(
        candidate["target"]["host_product_identity"]
    )
    mutations.append((candidate, expected))
    candidate, expected = _fixture()
    candidate["capabilities"][0] = DictSubclass(candidate["capabilities"][0])
    mutations.append((candidate, expected))
    candidate, expected = _fixture()
    candidate["capabilities"][0]["evidence"][0] = DictSubclass(
        candidate["capabilities"][0]["evidence"][0]
    )
    mutations.append((candidate, expected))

    for value, facts in mutations:
        with pytest.raises(HostCapabilityQualificationProtocolError):
            _validate(value, facts)


def test_private_module_is_not_reexported_and_has_no_io_or_activation_imports() -> None:
    import odibi_anchor._governance_protocol as package
    from odibi_anchor._governance_protocol import _host_capability_qualification as module

    assert not hasattr(package, "validate_host_capability_qualification")
    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    for prohibited in ("import os", "import socket", "import sqlite3", "import subprocess",
                       "_governance_sidecar", "_governance_ledger", "cryptography"):
        assert prohibited not in source
