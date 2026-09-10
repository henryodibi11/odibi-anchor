"""Focused evaluation tests for exactly the five T1 core controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from odibi_anchor.assurance import (
    T1_CONTROLS,
    CommandResultEvidence,
    EvidenceState,
    evaluate_t1_controls,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
SUBJECT = "sha256:" + "1" * 64
SCOPE = "sha256:" + "2" * 64


def _evidence(check_id, result, *, state: EvidenceState = "satisfied", evidence_id=None,
              subject=SUBJECT, scope=SCOPE, observed="2026-08-20T10:59:00+00:00",
              completed="2026-08-20T11:00:00+00:00"):
    return CommandResultEvidence(
        evidence_id=evidence_id or f"result:{check_id}",
        check_id=check_id,
        state=state,
        observed_at=observed,
        completed_at=completed,
        subject_digest=subject,
        scope_digest=scope,
        producer=f"producer:{check_id}",
        producer_version="1",
        result=result,
        provenance={"command": [check_id]},
    )


PASSING = (
    _evidence("tests", {"exit_code": 0, "failed": 0, "errors": 0, "expected_scope_ran": True}),
    _evidence("static-ruff", {"findings": {"existing": ["old"], "introduced": [], "unknown": []}}),
    _evidence("static-pyright", {"findings": {"existing": [], "introduced": [], "unknown": []}}),
    _evidence("output-contracts", {"exit_code": 0, "probes": {"json": "pass", "markdown": "pass"}}),
    _evidence("distribution", {"subchecks": {
        "build": "pass", "metadata": "pass", "isolated-install": "pass",
        "import": "pass", "cli": "pass", "installed-distribution": "pass",
    }}),
    _evidence("scope-integrity", {
        "intended_paths_known": True, "reconciled": True,
        "denied_paths": [], "unattested_paths": [],
    }),
)


def _evaluate(paths, evidence=PASSING, **kwargs):
    return evaluate_t1_controls(
        paths, evidence, subject_digest=SUBJECT, scope_digest=SCOPE, now=NOW, **kwargs,
    )


def test_applicability_matrix_and_deterministic_order():
    results = _evaluate([
        "src/odibi_anchor/cli.py", "scripts/verify_outputs.py", "pyproject.toml",
    ])

    assert tuple(result.control_id for result in results) == tuple(
        control.control_id for control in T1_CONTROLS
    )
    assert all(result.applicability == "applicable" for result in results)
    docs = _evaluate(["README.md"], evidence=())
    assert [result.control_id for result in docs if result.applicability == "applicable"] == [
        "Anchor-T1-SCOPE-INTEGRITY",
    ]


def test_each_t1_control_satisfied_path_uses_explicit_decisive_results():
    results = _evaluate([
        "src/odibi_anchor/cli.py", "scripts/verify_outputs.py", "pyproject.toml",
    ])
    assert {result.evidence_state for result in results} == {"satisfied"}
    assert all(result.evidence_ids for result in results)


def test_failed_dominates_stale_unavailable_missing_and_satisfied():
    failed = _evidence(
        "tests", {"exit_code": 1, "failed": 1, "errors": 0, "expected_scope_ran": True},
        state="failed", evidence_id="result:tests:failed",
    )
    stale = _evidence(
        "tests", {"exit_code": 0, "failed": 0, "errors": 0, "expected_scope_ran": True},
        observed="2026-08-18T10:59:00+00:00", completed="2026-08-18T11:00:00+00:00",
        evidence_id="result:tests:stale",
    )
    result = _evaluate(["src/mod.py"], [failed, stale])[0]
    assert result.control_id == "Anchor-T1-TESTS"
    assert result.evidence_state == "failed"


def test_stale_wrong_subject_and_malformed_success_are_not_satisfied():
    stale = replace(
        PASSING[0], observed_at="2026-08-18T10:59:00+00:00",
        completed_at="2026-08-18T11:00:00+00:00",
    )
    wrong_subject = replace(PASSING[1], subject_digest="sha256:" + "3" * 64)
    malformed_static = replace(PASSING[2], result={"findings": {"introduced": []}})
    results = _evaluate(["src/mod.py"], [stale, wrong_subject, malformed_static])
    indexed = {result.control_id: result for result in results}
    assert indexed["Anchor-T1-TESTS"].evidence_state == "stale"
    assert indexed["Anchor-T1-STATIC-RATCHET"].evidence_state == "unavailable"


def test_each_failed_pass_rule_and_unavailable_payload_is_rejected():
    failing = (
        replace(PASSING[0], result={"exit_code": 0, "failed": 1, "errors": 0, "expected_scope_ran": True}),
        replace(PASSING[1], result={"findings": {"existing": [], "introduced": ["new"], "unknown": []}}),
        PASSING[2],
        replace(PASSING[3], result={"exit_code": 0, "probes": {"json": "fail"}}),
        replace(PASSING[4], result={"subchecks": {"build": "pass"}}),
        replace(PASSING[5], result={
            "intended_paths_known": True, "reconciled": False,
            "denied_paths": [], "unattested_paths": [],
        }),
    )
    states = {result.control_id: result.evidence_state for result in _evaluate([
        "src/odibi_anchor/cli.py", "scripts/verify_outputs.py", "pyproject.toml",
    ], failing)}
    assert states == {
        "Anchor-T1-TESTS": "failed",
        "Anchor-T1-STATIC-RATCHET": "failed",
        "Anchor-T1-OUTPUT-CONTRACT": "failed",
        "Anchor-T1-DISTRIBUTION": "unavailable",
        "Anchor-T1-SCOPE-INTEGRITY": "failed",
    }


def test_catalog_disposition_is_preserved_for_advisory_and_blocking_modes():
    advisory = _evaluate(["src/mod.py"], evidence=())
    blocking_controls = tuple(replace(control, disposition="blocking") for control in T1_CONTROLS)
    blocking = _evaluate(["src/mod.py"], evidence=(), controls=blocking_controls)

    assert {result.disposition for result in advisory} == {"advisory"}
    assert {result.disposition for result in blocking} == {"blocking"}
    assert [result.evidence_state for result in advisory] == [
        result.evidence_state for result in blocking
    ]
