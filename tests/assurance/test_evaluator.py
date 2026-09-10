"""Deterministic contract tests for the shadow assurance kernel."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from odibi_anchor.assurance import (
    CATALOG_VERSION,
    CONTROL_CATALOG,
    CORE_CONTROLS,
    AssuranceAssessment,
    AssuranceException,
    AssurancePlan,
    ControlDefinition,
    build_assurance_plan,
    catalog_digest,
    evaluate_assurance,
    normalize_assurance_evidence,
)
from odibi_anchor.planning._task_policy import (
    BpsKernel,
    EvidenceEntry,
    TaskPolicyContext,
)
from odibi_anchor.planning._task_profile import EvidenceRequest, normalize_task_profile

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _profile(**overrides):
    values = {
        "work_type": "investigate",
        "execution_mode": "read_only",
        "risk": "low",
        "rigor": "direct",
        "domains": ["general"],
        "traits": [],
    }
    values.update(overrides)
    return normalize_task_profile(**values)


def _context(profile, *, entries=(), requests=(), epoch=1):
    return TaskPolicyContext(
        profile=profile,
        bps_kernel=BpsKernel("assure the task", "return advisory evidence"),
        caller_required_evidence=tuple(requests),
        current_evidence_entries=tuple(entries),
        task_verification_epoch=epoch,
    )


@pytest.mark.parametrize(
    ("profile", "tier"),
    [
        (_profile(), "T0"),
        (_profile(risk="medium"), "T1"),
        (_profile(execution_mode="artifact_only"), "T1"),
        (_profile(execution_mode="source_change"), "T2"),
        (_profile(risk="high"), "T2"),
        (_profile(execution_mode="data_change", risk="high"), "T3"),
        (_profile(traits=["irreversible"]), "T3"),
        (_profile(traits=["critical-impact"]), "T3"),
    ],
)
def test_tier_matrix_uses_consequence_not_scope(profile, tier):
    assert build_assurance_plan(profile).tier == tier


def test_one_line_irreversible_and_large_read_only_counterexamples_are_scope_independent():
    one_line = _profile(traits=["irreversible"])
    hundred_file_inventory = _profile()

    assert build_assurance_plan(one_line).tier == "T3"
    assert build_assurance_plan(hundred_file_inventory).tier == "T0"
    assert "scope" not in json.dumps(build_assurance_plan(one_line).to_dict())
    assert "file" not in json.dumps(build_assurance_plan(hundred_file_inventory).to_dict())


def test_attributes_and_controls_have_deterministic_order_and_applicability():
    plan = build_assurance_plan(
        _profile(
            execution_mode="source_change",
            traits=["public-contract-change"],
            domains=["security"],
        )
    )

    assert plan.quality_attributes == (
        "functional-suitability",
        "reliability",
        "safety",
        "maintainability",
        "adaptability",
        "performance-efficiency",
        "compatibility",
        "security",
    )
    assert plan.controls == ("AK-001", "AK-002", "AK-003", "AK-004", "AK-005")


def test_t3_mutation_selects_recovery_control():
    plan = build_assurance_plan(
        _profile(execution_mode="data_change", risk="high")
    )
    assert plan.tier == "T3"
    assert plan.controls == ("AK-001", "AK-004", "AK-005", "AK-006")


def test_models_are_frozen_strict_and_round_trip_through_json():
    plan = build_assurance_plan(_profile(execution_mode="source_change"))
    restored = AssurancePlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assessment = evaluate_assurance(plan, {"caller": {}, "control": {}})
    restored_assessment = AssuranceAssessment.from_dict(
        json.loads(json.dumps(assessment.to_dict()))
    )

    assert restored == plan
    assert restored_assessment == assessment
    canonical = json.dumps(plan.to_dict(), sort_keys=True, separators=(",", ":"))
    assert json.dumps(build_assurance_plan(_profile(execution_mode="source_change")).to_dict(),
                      sort_keys=True, separators=(",", ":")) == canonical
    with pytest.raises(FrozenInstanceError):
        plan.tier = "T0"  # type: ignore[misc]
    with pytest.raises(ValueError, match="Unexpected AssurancePlan fields"):
        AssurancePlan.from_dict({**plan.to_dict(), "score": 100})
    with pytest.raises(ValueError, match="Unknown assurance control"):
        AssurancePlan.from_dict({**plan.to_dict(), "controls": ["AK-999"]})


def test_catalog_is_closed_ordered_advisory_and_digestible():
    assert CATALOG_VERSION == "anchor-assurance-core/1.1"
    assert tuple(control.control_id for control in CORE_CONTROLS) == (
        "AK-001", "AK-002", "AK-003", "AK-004", "AK-005", "AK-006",
    )
    assert {control.disposition for control in CORE_CONTROLS} == {"advisory"}
    assert len(CONTROL_CATALOG) == 35
    assert catalog_digest() == "db82b8c8064295f44bd5293ba44320b22935fdaa83c4b25cc745d7d766356566"
    with pytest.raises(ValueError, match="accepted evidence"):
        ControlDefinition(
            "AK-001", "functional-suitability", "all-plans", ["mutable"],  # type: ignore[arg-type]
        )


def test_every_assessment_has_six_results_and_non_applicable_results_have_no_evidence():
    plan = build_assurance_plan(_profile())
    assessment = evaluate_assurance(plan, {"caller": {}, "control": {}})

    assert tuple(result.control_id for result in assessment.results) == tuple(
        control.control_id for control in CORE_CONTROLS
    )
    assert len(assessment.results) == 6
    assert all(
        not result.evidence_ids
        for result in assessment.results
        if result.applicability == "not_applicable"
    )


def test_normalization_separates_caller_evidence_and_marks_prior_epoch_stale():
    request = EvidenceRequest("caller-compat", "compatibility", "caller proof", "gate")
    entry = EvidenceEntry(
        "caller-compat",
        "compatibility",
        "pass",
        "reviewer",
        "2026-08-20T11:00:00+00:00",
        {"referenced_ids": []},
    )
    context = _context(
        _profile(traits=["public-contract-change"]),
        entries=[entry],
        requests=[request],
        epoch=2,
    )
    timings = [
        {"action": "test", "passed": True, "error": None},
        {"action": "task", "passed": True, "error": None},
        {
            "action": "test",
            "passed": True,
            "error": None,
            "observed_at": "2026-08-20T11:30:00+00:00",
            "test_target": "tests/",
            "exit_code": 0,
        },
    ]

    normalized = normalize_assurance_evidence(context, timings)

    assert normalized["caller"]["caller-compat"]["state"] == "satisfied"
    assert normalized["control"]["dispatch:test:0"]["state"] == "stale"
    assert normalized["control"]["dispatch:test:2"]["state"] == "satisfied"


def test_current_evidence_supersedes_stale_evidence_without_hiding_current_failures():
    profile = _profile(execution_mode="source_change")
    normalized = normalize_assurance_evidence(
        _context(profile, epoch=1),
        [
            {
                "action": "test", "passed": True, "error": None,
                "test_target": "old-tests/", "exit_code": 0,
            },
            {
                "action": "test", "passed": True, "error": None,
                "test_target": "tests/", "exit_code": 0,
            },
        ],
    )

    results = {
        item.control_id: item
        for item in evaluate_assurance(build_assurance_plan(profile), normalized).results
    }

    assert normalized["control"]["dispatch:test:0"]["state"] == "stale"
    assert results["AK-001"].evidence_state == "satisfied"
    assert results["AK-003"].evidence_state == "satisfied"
    assert results["AK-005"].evidence_state == "satisfied"
    assert all("dispatch:test:0" not in result.evidence_ids for result in results.values())


def test_failed_evidence_precedes_satisfied_and_malformed_never_passes():
    plan = build_assurance_plan(_profile(execution_mode="source_change"))
    evidence = {
        "caller": {},
        "control": {
            "test-pass": {
                "id": "test-pass", "kind": "test", "state": "satisfied",
                "source": "anchor:test", "observed_at": "2026-08-20T11:00:00+00:00",
                "provenance": {"target": "tests/", "exit_code": 0},
            },
            "test-fail": {
                "id": "test-fail", "kind": "test", "state": "failed",
                "source": "anchor:test", "observed_at": "2026-08-20T11:01:00+00:00",
                "provenance": {"target": "tests/", "exit_code": 1},
            },
            "malformed": {"state": "satisfied"},
        },
    }

    results = {item.control_id: item for item in evaluate_assurance(plan, evidence).results}

    assert results["AK-003"].evidence_state == "failed"
    assert "malformed" not in results["AK-003"].evidence_ids


def test_latest_current_test_is_selected_and_duplicate_ledger_ids_use_upsert_order():
    profile = _profile(execution_mode="source_change")
    duplicate_old = EvidenceEntry(
        "caller-proof", "compatibility", "fail", "reviewer",
        "2026-08-20T10:00:00+00:00", {},
    )
    duplicate_new = EvidenceEntry(
        "caller-proof", "compatibility", "pass", "reviewer",
        "2026-08-20T10:01:00+00:00", {},
    )
    normalized = normalize_assurance_evidence(
        _context(profile, entries=(duplicate_old, duplicate_new), epoch=0),
        [
            {
                "action": "test", "passed": False, "error": "old failure",
                "observed_at": "2026-08-20T10:00:00+00:00",
                "test_target": "tests/", "exit_code": 1,
            },
            {
                "action": "test", "passed": True, "error": None,
                "observed_at": "2026-08-20T10:01:00+00:00",
                "test_target": "tests/", "exit_code": 0,
            },
        ],
    )

    assessment = evaluate_assurance(build_assurance_plan(profile), normalized)
    maintainability = next(
        result for result in assessment.results if result.control_id == "AK-003"
    )
    assert normalized["control"]["caller-proof"]["state"] == "satisfied"
    assert maintainability.evidence_state == "satisfied"
    assert maintainability.evidence_ids == ("dispatch:test:1",)


def test_invocation_only_and_explicit_unavailable_evidence_never_satisfy_reliability():
    profile = _profile(execution_mode="source_change")
    normalized = normalize_assurance_evidence(
        _context(profile, epoch=0),
        [
            {"action": "preflight", "passed": True, "error": None},
            {"action": "test", "passed": True, "error": None, "unavailable": True},
        ],
    )

    reliability = next(
        result for result in evaluate_assurance(
            build_assurance_plan(profile), normalized,
        ).results
        if result.control_id == "AK-005"
    )
    assert normalized["control"]["dispatch:preflight:0"]["state"] == "unavailable"
    assert reliability.evidence_state == "unavailable"


def test_active_exception_is_recorded_without_rewriting_missing_evidence():
    plan = build_assurance_plan(
        _profile(execution_mode="data_change", risk="high")
    )
    active = AssuranceException(
        exception_id="EX-001",
        control_id="AK-006",
        reason="Recovery evidence is temporarily unavailable.",
        approved_by="release-owner",
        created_at="2026-08-20T10:00:00+00:00",
        expires_at="2026-08-20T14:00:00+00:00",
        status="active",
    )
    expired = AssuranceException(
        exception_id="EX-002",
        control_id="AK-006",
        reason="Old exception.",
        approved_by="release-owner",
        created_at="2026-08-19T10:00:00+00:00",
        expires_at="2026-08-19T14:00:00+00:00",
        status="active",
    )

    active_result = {
        result.control_id: result
        for result in evaluate_assurance(
            plan, {"caller": {}, "control": {}}, exceptions=[active], now=NOW,
        ).results
    }["AK-006"]
    expired_result = {
        result.control_id: result
        for result in evaluate_assurance(
            plan, {"caller": {}, "control": {}}, exceptions=[expired], now=NOW,
        ).results
    }["AK-006"]

    assert active_result.evidence_state == "missing"
    assert active_result.exception_id == "EX-001"
    assert expired_result.evidence_state == "missing"
    assert expired_result.exception_id is None


def test_v1_output_has_no_score_overall_pass_or_blocking_disposition():
    assessment = evaluate_assurance(
        build_assurance_plan(_profile(execution_mode="source_change")),
        {"caller": {}, "control": {}},
    ).to_dict()
    serialized = json.dumps(assessment, sort_keys=True)

    assert "score" not in serialized
    assert "overall_pass" not in serialized
    assert "blocking" not in serialized
