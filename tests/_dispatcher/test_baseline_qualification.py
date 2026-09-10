from __future__ import annotations

import json
from dataclasses import replace

import pytest

from odibi_anchor._dispatcher._baseline_qualification import (
    OUTCOMES,
    baseline_fingerprint,
    project_baseline_qualification,
    qualify_task_baseline,
)
from odibi_anchor._repository_snapshot import TaskRepositoryBaseline


@pytest.fixture
def baseline():
    return TaskRepositoryBaseline(
        "/repo", "feature", "main", "target", "base", "head",
        "2026-09-10T00:00:00+00:00",
    )


@pytest.mark.parametrize("outcome", sorted(OUTCOMES))
def test_all_six_truthful_outcomes_are_bound_to_exact_authority(baseline, outcome) -> None:
    request = {"outcome": outcome, "reason": f"qualified as {outcome}"}
    if outcome in {"smoke_passed", "existing_failure_reproduced", "unrelated_failure"}:
        request.update(evidence_ref="evidence://baseline/1", command_identity=["pytest", "-q"])

    result = qualify_task_baseline(
        baseline, task_window_id="ltw_exact", execution_mode="source_change",
        target_root="/absent", request=request,
    )

    assert result.outcome == outcome
    assert result.task_window_id == "ltw_exact"
    assert result.baseline_sha256 == baseline_fingerprint(baseline)


def test_missing_safe_check_records_unavailable_instead_of_guessing(baseline) -> None:
    result = qualify_task_baseline(
        baseline, task_window_id="ltw_exact", execution_mode="source_change",
        target_root="/absent",
    )
    assert result.outcome == "unavailable"
    assert result.command_identity == ()


def test_non_source_task_is_not_applicable() -> None:
    result = qualify_task_baseline(
        None, task_window_id="ltw_exact", execution_mode="analysis", target_root=None,
    )
    assert result.outcome == "not_applicable"
    assert result.baseline_sha256 is None


def test_positive_outcome_requires_retained_evidence_and_command_identity(baseline) -> None:
    with pytest.raises(ValueError, match="retained evidence_ref"):
        qualify_task_baseline(
            baseline, task_window_id="ltw_exact", execution_mode="source_change",
            target_root="/absent", request={"outcome": "smoke_passed", "reason": "passed"},
        )


def test_source_or_task_drift_makes_projection_stale(baseline) -> None:
    qualification = qualify_task_baseline(
        baseline, task_window_id="ltw_exact", execution_mode="source_change",
        target_root="/absent",
    )
    changed = replace(baseline, task_start_head_sha="other")
    assert project_baseline_qualification(
        qualification, changed, task_window_id="ltw_exact",
    )["status"] == "stale"
    assert project_baseline_qualification(
        qualification, baseline, task_window_id="ltw_other",
    )["status"] == "stale"


def test_checked_in_positive_result_is_configuration_not_new_task_attestation(
    baseline, tmp_path,
) -> None:
    authority = tmp_path / ".odibi-anchor"
    authority.mkdir()
    (authority / "baseline.json").write_text(
        json.dumps({
            "outcome": "smoke_passed",
            "reason": "the prior run passed",
            "command_identity": ["pytest", "-q"],
        }),
        encoding="utf-8",
    )

    result = qualify_task_baseline(
        baseline,
        task_window_id="ltw_new_task",
        execution_mode="source_change",
        target_root=str(tmp_path),
    )

    assert result.outcome == "unavailable"
    assert result.evidence_ref is None
    assert "identifies a check method, not a result" in result.reason
