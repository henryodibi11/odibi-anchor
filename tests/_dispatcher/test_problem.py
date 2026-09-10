"""Tests for durable seven-stage Problem Records."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._problem import (
    _parse,
    _validate_recommendation_traceability,
    problem_action,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _create(tmp_path: Path, **changes):
    return problem_action(
        tmp_path,
        "create",
        title="Reduce queue processing delays",
        project_id="queue-automation",
        output_format="dict",
        **changes,
    )


def test_create_writes_all_seven_stages_to_one_markdown_file(tmp_path: Path) -> None:
    result = _create(
        tmp_path,
        definition="Queue jobs exceed the agreed processing window.",
        decision_needed="Decide which bottleneck to address first.",
    )

    path = Path(result["artifact_path"])
    text = path.read_text(encoding="utf-8")

    assert result["created"] is True
    assert result["project_id"] == "queue-automation"
    assert path.parent == tmp_path / "problems"
    for stage in range(1, 8):
        assert f"## {stage}." in text
    assert "## Decision and revision log" in text
    assert "## Linked implementation specs" in text


def test_parse_record_from_before_recommendation_evidence_heading(tmp_path: Path) -> None:
    result = _create(tmp_path)
    path = Path(result["artifact_path"])
    text = path.read_text(encoding="utf-8").replace(
        "### Recommendation\n[Not yet captured]\n\n"
        "### Supporting evidence IDs\n[Not yet captured]\n\n"
        "### Alternatives",
        "### Recommendation\nUse the smaller safe change.\n\n### Alternatives",
    )
    path.write_text(text, encoding="utf-8")

    record = _parse(path)

    assert record["recommendation"] == "Use the smaller safe change."
    assert record["recommendation_evidence"] == ""


def test_pytest_subprocess_problem_is_canonical_and_traceable() -> None:
    record = _parse(REPOSITORY_ROOT / "problems" / "PRB-2026-0008.md")

    assert record["meta"]["revision"] == 4
    assert all(
        record[field]
        for field in (
            "definition",
            "decision_needed",
            "scope",
            "constraints",
            "success_measures",
            "priorities",
            "synthesis",
            "uncertainty",
            "recommendation",
            "alternatives",
            "risks",
            "reversal_conditions",
        )
    )
    assert record["issues"] and record["hypotheses"] and record["workplan"]
    assert record["evidence"] and record["revisions"]
    assert _validate_recommendation_traceability(record) == [
        "E1",
        "E2",
        "E3",
        "E4",
        "E5",
        "E6",
        "E7",
        "E8",
    ]
    evidence_ids = {row["ID"] for row in record["evidence"]}
    for hypothesis in record["hypotheses"]:
        assert set(hypothesis["Evidence"].replace(",", " ").split()) <= evidence_ids
    assert record["linked_specs"] == [
        {"Spec": "PYTEST_SUBPROCESS_IMPORT", "Recommendation revision": "4"}
    ]


def test_ids_increment_and_remain_stable_across_updates(tmp_path: Path) -> None:
    created = _create(tmp_path)
    problem_id = created["problem_id"]

    problem_action(
        tmp_path,
        "update",
        problem_id,
        issue={"title": "Capacity", "priority": "high"},
        hypothesis={"issue_id": "I1", "hypothesis": "Workers are undersized"},
        evidence={
            "source": "databricks query history / query 42",
            "observation": "Worker CPU remains above 95%.",
            "interpretation": "Compute capacity is a plausible constraint.",
            "hypotheses": ["H1"],
        },
        output_format="dict",
    )
    problem_action(
        tmp_path,
        "update",
        problem_id,
        issue={"id": "I1", "rationale": "Largest measured delay"},
        hypothesis={"id": "H1", "expected_signal": "Latency falls with larger workers"},
        evidence={
            "source": "load test run 2026-07-20",
            "observation": "Larger workers reduced p95 latency.",
            "hypotheses": ["H1"],
        },
        output_format="dict",
    )

    shown = problem_action(tmp_path, "show", problem_id, output_format="dict")
    record = shown["record"]
    assert [row["ID"] for row in record["issues"]] == ["I1"]
    assert [row["ID"] for row in record["hypotheses"]] == ["H1"]
    assert [row["ID"] for row in record["evidence"]] == ["E1", "E2"]
    assert record["issues"][0]["Rationale"] == "Largest measured delay"
    assert record["hypotheses"][0]["Expected signal"] == "Latency falls with larger workers"


def test_evidence_requires_provenance_and_preserves_contradiction(tmp_path: Path) -> None:
    problem_id = _create(tmp_path)["problem_id"]

    with pytest.raises(ValueError, match="source provenance"):
        problem_action(
            tmp_path,
            "update",
            problem_id,
            evidence={"observation": "An unsupported observation"},
            output_format="dict",
        )

    problem_action(
        tmp_path,
        "update",
        problem_id,
        evidence={
            "source": "controlled load test",
            "observation": "Latency did not improve.",
            "interpretation": "Capacity may not be causal.",
            "hypotheses": ["H1"],
            "confidence": "high",
            "contradicts": ["E1"],
        },
        conflicting_evidence="E2 conflicts with the initial capacity signal.",
        output_format="dict",
    )

    record = problem_action(tmp_path, "show", problem_id, output_format="dict")["record"]
    assert record["evidence"][0]["Source"] == "controlled load test"
    assert record["evidence"][0]["Captured"]
    assert record["evidence"][0]["Contradicts"] == "E1"
    assert "conflicts" in record["conflicting_evidence"]


def test_markdown_round_trip_preserves_special_characters(tmp_path: Path) -> None:
    problem_id = _create(tmp_path)["problem_id"]
    problem_action(
        tmp_path,
        "update",
        problem_id,
        issue={"title": "Cost | latency", "rationale": "line one\nline two"},
        output_format="dict",
    )

    path = tmp_path / "problems" / f"{problem_id}.md"
    first = _parse(path)
    second = problem_action(tmp_path, "show", problem_id, output_format="dict")["record"]

    assert first == second
    assert first["issues"][0]["Issue"] == "Cost | latency"
    assert first["issues"][0]["Rationale"] == "line one\nline two"


def test_resume_is_bounded_for_large_record(tmp_path: Path) -> None:
    problem_id = _create(tmp_path)["problem_id"]
    for index in range(12):
        problem_action(
            tmp_path,
            "update",
            problem_id,
            issue={"title": f"Issue {index}"},
            hypothesis={"hypothesis": f"Hypothesis {index}"},
            evidence={"source": f"query-{index}", "observation": "x" * 200},
            output_format="dict",
        )

    result = problem_action(tmp_path, "resume", problem_id, output_format="dict")

    assert result["kind"] == "problem_resume_context"
    assert len(result["active_issues"]) == 5
    assert len(result["priority_hypotheses"]) == 5
    assert len(result["material_evidence"]) == 5
    assert "record" not in result


def test_link_multiple_specs_without_duplicates(tmp_path: Path) -> None:
    problem_id = _create(
        tmp_path,
        evidence={"source": "load test", "observation": "CPU exceeded 95%."},
        recommendation="Increase worker capacity first.",
        recommendation_evidence="E1",
        uncertainty="Demand growth may offset the improvement.",
    )["problem_id"]

    problem_action(tmp_path, "link_spec", problem_id, spec="WORKER_RESIZE", output_format="dict")
    problem_action(tmp_path, "link_spec", problem_id, spec="WORKER_RESIZE", output_format="dict")
    result = problem_action(
        tmp_path,
        "link_spec",
        problem_id,
        spec="QUEUE_METRICS",
        recommendation_revision=2,
        output_format="dict",
    )

    assert result["linked_specs"] == ["WORKER_RESIZE", "QUEUE_METRICS"]


def test_inconclusive_problem_can_close_without_recommendation(tmp_path: Path) -> None:
    problem_id = _create(tmp_path)["problem_id"]

    result = problem_action(
        tmp_path,
        "close",
        problem_id,
        outcome="inconclusive",
        output_format="dict",
    )

    assert result["closed"] is True
    assert result["status"] == "inconclusive"


def test_hypothesis_resolution_requires_linked_evidence(tmp_path: Path) -> None:
    problem_id = _create(
        tmp_path,
        hypothesis={"hypothesis": "Workers are undersized", "critical": True},
    )["problem_id"]

    with pytest.raises(ValueError, match="require linked evidence"):
        problem_action(
            tmp_path,
            "update",
            problem_id,
            hypothesis={"id": "H1", "status": "supported"},
            output_format="dict",
        )

    problem_action(
        tmp_path,
        "update",
        problem_id,
        hypothesis={"id": "H1", "status": "supported"},
        evidence={"source": "load test", "observation": "Scaling reduced latency", "hypotheses": ["H1"]},
        output_format="dict",
    )
    record = problem_action(tmp_path, "show", problem_id, output_format="dict")["record"]
    assert record["hypotheses"][0]["Evidence"] == "E1"


def test_recommendation_requires_existing_evidence_and_uncertainty(tmp_path: Path) -> None:
    problem_id = _create(tmp_path)["problem_id"]
    with pytest.raises(ValueError, match="supporting evidence"):
        problem_action(
            tmp_path,
            "update",
            problem_id,
            recommendation="Scale workers.",
            output_format="dict",
        )

    problem_action(
        tmp_path,
        "update",
        problem_id,
        evidence={"source": "load test", "observation": "Scaling reduced latency"},
        recommendation="Scale workers.",
        recommendation_evidence="E1",
        uncertainty="The test covered only peak-hour traffic.",
        output_format="dict",
    )


def test_later_updates_cannot_invalidate_recommendation_traceability(tmp_path: Path) -> None:
    problem_id = _create(
        tmp_path,
        evidence={"source": "load test", "observation": "Scaling reduced latency"},
        recommendation="Scale workers.",
        recommendation_evidence="E1",
        uncertainty="The test covered one traffic pattern.",
    )["problem_id"]

    with pytest.raises(ValueError, match="supporting evidence IDs"):
        problem_action(
            tmp_path,
            "update",
            problem_id,
            recommendation_evidence="E1bogus",
            output_format="dict",
        )
    with pytest.raises(ValueError, match="material uncertainty"):
        problem_action(
            tmp_path,
            "update",
            problem_id,
            uncertainty="",
            output_format="dict",
        )
    with pytest.raises(ValueError, match="non-blank"):
        problem_action(
            tmp_path,
            "update",
            problem_id,
            recommendation=None,
            output_format="dict",
        )


def test_evidence_normalizes_required_provenance(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source provenance"):
        _create(tmp_path, evidence={"source": "   ", "observation": "No source"})
    with pytest.raises(ValueError, match="source provenance"):
        _create(tmp_path, evidence={"source": None, "observation": "Null source"})

    problem_id = _create(
        tmp_path,
        evidence={"Source": "  query history  ", "Captured": None, "observation": "CPU was high"},
    )["problem_id"]
    evidence = problem_action(tmp_path, "show", problem_id, output_format="dict")["record"]["evidence"][0]
    assert evidence["Source"] == "query history"
    assert evidence["Captured"]


def test_close_requires_critical_hypothesis_disposition(tmp_path: Path) -> None:
    problem_id = _create(
        tmp_path,
        hypothesis={"hypothesis": "Workers are undersized", "critical": True},
    )["problem_id"]
    with pytest.raises(ValueError, match="critical hypotheses"):
        problem_action(tmp_path, "close", problem_id, output_format="dict")

    problem_action(
        tmp_path,
        "update",
        problem_id,
        hypothesis={"id": "H1", "status": "deferred"},
        output_format="dict",
    )
    result = problem_action(tmp_path, "close", problem_id, outcome="inconclusive", output_format="dict")
    assert result["closed"] is True


def test_list_is_stable_and_path_traversal_is_rejected(tmp_path: Path) -> None:
    first = _create(tmp_path)
    second = _create(tmp_path)
    result = problem_action(tmp_path, output_format="dict")

    year = datetime.now(timezone.utc).year
    assert first["problem_id"] == f"PRB-{year}-0001"
    assert second["problem_id"] == f"PRB-{year}-0002"
    assert [item["problem_id"] for item in result["problems"]] == [
        f"PRB-{year}-0001",
        f"PRB-{year}-0002",
    ]
    with pytest.raises(ValueError, match="problem ID"):
        problem_action(tmp_path, "show", "../escape", output_format="dict")


def test_markdown_output_is_compact_and_human_readable(tmp_path: Path) -> None:
    created = problem_action(
        tmp_path,
        "create",
        title="Choose a queue design",
        project_id="queue",
        output_format="markdown",
    )
    assert "# PRB-" in created
    assert "**Stage:** 1/7" in created
