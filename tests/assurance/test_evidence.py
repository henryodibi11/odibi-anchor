"""Focused contracts for normalized command-result evidence."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from odibi_anchor.assurance import (
    CommandResultEvidence,
    evaluate_t1_controls,
    normalize_command_result_evidence,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def evidence_payload(**overrides):
    value = {
        "evidence_id": "result:tests",
        "check_id": "tests",
        "state": "satisfied",
        "observed_at": "2026-08-20T11:00:00+00:00",
        "completed_at": "2026-08-20T11:05:00+00:00",
        "subject_digest": DIGEST_A,
        "scope_digest": DIGEST_B,
        "producer": "scripts/run_tests.py",
        "producer_version": "1",
        "result": {"exit_code": 0, "failed": 0, "errors": 0, "expected_scope_ran": True,
                   "nested": {"items": ["tests/"]}},
        "provenance": {"command": ["python", "scripts/run_tests.py", "tests/"]},
    }
    value.update(overrides)
    return value


def test_exact_schema_round_trip_and_deep_immutability():
    evidence = CommandResultEvidence.from_dict(evidence_payload())

    assert CommandResultEvidence.from_dict(evidence.to_dict()) == evidence
    with pytest.raises(TypeError):
        evidence.result["exit_code"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        evidence.result["nested"]["items"] = ()
    with pytest.raises(FrozenInstanceError):
        evidence.state = "failed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="Unexpected CommandResultEvidence fields"):
        CommandResultEvidence.from_dict({**evidence_payload(), "claim": "passed"})


def test_timestamp_ordering_and_freshness_boundary():
    with pytest.raises(ValueError, match="cannot be before"):
        CommandResultEvidence.from_dict(evidence_payload(
            completed_at="2026-08-20T10:59:59+00:00",
        ))
    evidence = CommandResultEvidence.from_dict(evidence_payload(
        completed_at="2026-08-20T11:00:00+00:00",
    ))
    assert evidence.is_fresh(NOW, timedelta(hours=1))
    assert not evidence.is_fresh(NOW, timedelta(minutes=59, seconds=59))


def test_digest_and_scope_mismatch_normalize_to_unavailable():
    evidence = normalize_command_result_evidence(
        evidence_payload(),
        expected_check_id="tests",
        expected_subject_digest=DIGEST_B,
        expected_scope_digest=DIGEST_A,
    )

    assert evidence.state == "unavailable"
    assert evidence.subject_digest == DIGEST_B
    assert evidence.scope_digest == DIGEST_A
    assert set(evidence.provenance["normalization_errors"]) == {
        "subject-digest", "scope-digest",
    }
    with pytest.raises(ValueError, match="SHA-256"):
        CommandResultEvidence.from_dict(evidence_payload(subject_digest="abc"))


def test_bounded_payload_unknown_state_and_malformed_adapter():
    with pytest.raises(ValueError, match="oversized string"):
        CommandResultEvidence.from_dict(evidence_payload(result={"text": "x" * 4_097}))
    with pytest.raises(ValueError, match="Unsupported evidence state"):
        CommandResultEvidence.from_dict(evidence_payload(state="pass"))

    unavailable = normalize_command_result_evidence(
        {"evidence_id": "broken", "state": "pass"},
        expected_check_id="tests",
        expected_subject_digest=DIGEST_A,
        expected_scope_digest=DIGEST_B,
    )
    assert unavailable.state == "unavailable"
    assert unavailable.evidence_id == "broken"


def test_invocation_timing_without_normalized_result_satisfies_no_t1_control():
    results = evaluate_t1_controls(
        ["src/odibi_anchor/example.py"],
        [],
        subject_digest=DIGEST_A,
        scope_digest=DIGEST_B,
        now=NOW,
    )

    applicable = {result.control_id: result for result in results if result.applicability == "applicable"}
    assert applicable["Anchor-T1-TESTS"].evidence_state == "missing"
    assert applicable["Anchor-T1-STATIC-RATCHET"].evidence_state == "missing"
    assert applicable["Anchor-T1-SCOPE-INTEGRITY"].evidence_state == "missing"
