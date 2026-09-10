"""Outcome records, raw-count scorecard, and fail-closed comparison tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from odibi_anchor.assurance import (
    QualificationRun,
    ScenarioJudgement,
    build_scorecard,
    compare_cohorts,
    load_scenario_manifest,
    render_scorecard_markdown,
    validate_run,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "src/odibi_anchor/assurance/resources/scenario_manifest.json"
DIGEST = "sha256:" + "a" * 64


def _run(**changes) -> QualificationRun:
    values = {
        "run_id": "run-1",
        "scenario_id": "public-t1-localized-fix",
        "scenario_version": "1.0",
        "source_commit": "a" * 40,
        "distribution_version": "0.9.0",
        "distribution_digest": DIGEST,
        "agent_family": "synthetic-family",
        "agent_version": "1",
        "producer_id": "producer",
        "host": "direct-python",
        "transport": "direct",
        "tier": "T1",
        "scope": "localized",
        "selected_profile": "source-change",
        "control_version": "Anchor-T1-TESTS@1.0",
        "evaluator_digest": "sha256:" + "0" * 63 + "5",
        "started_at": "2026-08-20T00:00:00Z",
        "ended_at": "2026-08-20T00:01:00Z",
        "terminal_status": "completed",
        "artifact_digests": (DIGEST,),
        "protocol_violations": (),
        "attempt": 1,
    }
    values.update(changes)
    return QualificationRun(**values)


def _judgement(**changes) -> ScenarioJudgement:
    values = {
        "judgement_id": "judgement-1",
        "run_id": "run-1",
        "scenario_id": "public-t1-localized-fix",
        "behavior_id": "behavior-1",
        "outcome": "pass",
        "defect_severity": "none",
        "escaped_defect": False,
        "false_positive": False,
        "unsupported_claim": False,
        "reviewer_corrections": 0,
        "evidence_references": ("evidence-1",),
        "reviewer_id": "reviewer",
        "reviewer_blinded": True,
        "first_attempt": True,
        "independently_reviewed": True,
        "material_claims": 1,
        "detectable_defects": 1,
        "emitted_findings": 1,
        "applicable_evidence_requirements": 1,
        "ceremony_wall_seconds": 5,
        "ceremony_calls": 1,
        "ceremony_prompts": 1,
        "ceremony_evidence_items": 1,
        "ceremony_reviewer_actions": 0,
    }
    values.update(changes)
    return ScenarioJudgement(**values)


def test_manifest_and_run_bind_exact_digest_tier_and_scope() -> None:
    manifest = load_scenario_manifest(MANIFEST)
    assert len(manifest) == 16
    validate_run(_run(), manifest)
    for changed, message in (({"scope": "systemic"}, "tier/scope"), ({"evaluator_digest": DIGEST}, "digest mismatch")):
        with pytest.raises(ValueError, match=message):
            validate_run(_run(**changed), manifest)


def test_strict_records_reject_unknown_fields_and_producer_review() -> None:
    payload = _run().to_dict()
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="Unexpected QualificationRun"):
        QualificationRun.from_dict(payload)
    with pytest.raises(ValueError, match="producing agent"):
        build_scorecard((_run(),), (_judgement(reviewer_id="producer"),))


def test_scorecard_preserves_counts_retries_unavailable_and_exclusions() -> None:
    retry = _run(run_id="run-2", attempt=2)
    unavailable = _judgement(outcome="unavailable", defect_severity="high")
    scorecard = build_scorecard((_run(), retry), (unavailable,))
    assert scorecard.counts["first_pass_success"] == {"numerator": 0, "denominator": 1}
    assert scorecard.counts["unavailable_evidence"] == {"numerator": 1, "denominator": 1}
    assert scorecard.rate("unavailable_evidence") == 1
    assert scorecard.judgement_ids == ("judgement-1",)

    incomplete = _run(terminal_status="incomplete")
    excluded = build_scorecard((incomplete,), (_judgement(),))
    assert excluded.counts["first_pass_success"]["denominator"] == 0
    assert excluded.exclusions == ({"reason": "terminal_status:incomplete", "run_id": "run-1"},)


def test_scenario_metrics_do_not_count_each_expected_behavior_as_a_scenario() -> None:
    second = _judgement(judgement_id="judgement-2", behavior_id="behavior-2")
    scorecard = build_scorecard((_run(),), (_judgement(), second))

    assert scorecard.counts["first_pass_success"] == {"numerator": 1, "denominator": 1}
    assert scorecard.counts["review_rework_scenarios"] == {"numerator": 0, "denominator": 1}
    assert scorecard.ceremony_samples["calls"] == (2,)


def test_critical_failure_precedes_metrics_and_cannot_be_averaged_away() -> None:
    critical = _judgement(outcome="fail", defect_severity="critical", escaped_defect=True)
    candidate = build_scorecard((_run(),), (critical,))
    baseline = build_scorecard((_run(),), (_judgement(),))
    comparison = compare_cohorts(candidate, baseline)
    assert comparison.comparable is False
    assert comparison.critical_failures == ("judgement-1",)
    report = render_scorecard_markdown(candidate)
    assert report.index("## Critical failures") < report.index("## Metrics")


def test_missing_denominator_is_not_a_pass() -> None:
    no_claims = _judgement(
        material_claims=0, detectable_defects=0, emitted_findings=0, applicable_evidence_requirements=0
    )
    scorecard = build_scorecard((_run(),), (no_claims,))
    comparison = compare_cohorts(scorecard, replace(scorecard))
    assert comparison.comparable is False
    assert comparison.metric_deltas["unsupported_claims"] is None


def test_mismatched_cohorts_are_not_comparable() -> None:
    scorecard = build_scorecard((_run(),), (_judgement(),))
    different_scope = replace(
        scorecard,
        cohort_keys={**scorecard.cohort_keys, "scope": "systemic"},
    )

    comparison = compare_cohorts(scorecard, different_scope)

    assert comparison.comparable is False
    assert "cohort:mismatch" in comparison.regressions
