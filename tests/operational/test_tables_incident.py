from __future__ import annotations

import json

from odibi_anchor.operational import (
    CollectorResult,
    incident_snapshot,
    observe_table,
    table_trend,
)


def _profile(row_count: int, observed_at: str) -> dict:
    return {
        "row_count": row_count,
        "column_count": 2,
        "profiled_at": observed_at,
        "profiling_level": "aggregate",
        "columns": [
            {"name": "id", "dtype": "int64", "null_count": 0, "distinct_count": row_count},
            {"name": "value", "dtype": "string", "null_count": 1, "distinct_count": 3},
        ],
        "duplicate_forensics": {"duplicate_count": 0},
        "freshness": {"status": "current"},
    }


def test_observation_persists_aggregates_without_rows_and_trends_artifacts(tmp_path):
    first = observe_table(
        _profile(10, "2026-01-01T00:00:00Z"), subject="main.default.events",
        persist=True, artifact_root=tmp_path,
    )
    second = observe_table(
        _profile(14, "2026-01-02T00:00:00Z"), subject="main.default.events",
        persist=True, artifact_root=tmp_path,
    )

    paths = [item["managed_writes"][0]["relative_path"] for item in (first, second)]
    persisted = (tmp_path / paths[0]).read_text(encoding="utf-8")
    assert '"rows"' not in persisted
    document = json.loads(persisted)
    assert document["payload"] == first["evidence"]
    trend = table_trend(paths, artifact_root=tmp_path)
    assert trend["status"] == "compared"
    assert {tuple(change.items()) for change in trend["point_changes"]} >= {
        tuple({"metric": "row_count", "from": 10, "to": 14, "change": 4}.items())
    }


def test_trend_refuses_cross_method_summary():
    first = observe_table(_profile(10, "2026-01-01T00:00:00Z"), subject="events")
    second = observe_table(
        _profile(12, "2026-01-02T00:00:00Z"), subject="events",
        collection_method="sampled",
    )
    trend = table_trend([first, second])
    assert "summaries" not in trend
    assert any("not comparable" in item for item in trend["limitations"])
    assert trend["status"] == "incomparable"
    assert trend["point_changes"] == []


def test_trend_refuses_cross_subject_schema_and_invalid_timestamp():
    first = observe_table(_profile(10, "2026-01-01T00:00:00Z"), subject="events")
    other = observe_table(_profile(12, "2026-01-02T00:00:00Z"), subject="customers")
    assert table_trend([first, other])["status"] == "incomparable"

    schema_changed = observe_table(_profile(12, "2026-01-02T00:00:00Z"), subject="events")
    schema_changed["facts"]["schema_fingerprint"] = "different"
    assert table_trend([first, schema_changed])["status"] == "incomparable"

    invalid = dict(first)
    invalid["observed_at"] = "2026-01-03T00:00:00"
    trend = table_trend([first, invalid])
    assert trend["status"] == "incomparable"
    assert trend["point_changes"] == []
    assert any("non-UTC" in item for item in trend["limitations"])


def test_incident_fails_forward_persists_redacted_evidence_and_attaches(tmp_path):
    attached = []

    def broken():
        raise RuntimeError("authorization: Bearer top-secret")

    def attach(problem_id, evidence):
        attached.append((problem_id, evidence))
        return {
            "write_performed": True,
            "accepted_evidence": [{"id": "E1"}],
            "managed_writes": [{"kind": "problem", "path": "problems/PRB.md"}],
        }

    incident = incident_snapshot(
        [CollectorResult("healthy", "collected", facts={"rows": 4}), broken],
        artifact_root=tmp_path,
        problem_id="PRB-2026-0001",
        attach=attach,
    )

    assert [item["status"] for item in incident["collectors"]][1:] == ["collected", "failed"]
    assert incident["attachment"]["status"] == "attached"
    assert attached[0][0] == "PRB-2026-0001"
    assert attached[0][1]["source"] == "operational_evidence"
    artifact = tmp_path / incident["managed_writes"][0]["relative_path"]
    document = json.loads(artifact.read_text(encoding="utf-8"))
    assert "top-secret" not in artifact.read_text(encoding="utf-8")
    assert document["payload"]["kind"] == "incident_snapshot"
    assert document["payload"] == incident["evidence"]
    assert incident["collectors"][1]["facts"] == {}
    assert any("omitted" in item for item in incident["collectors"][1]["limitations"])


def test_observation_projects_profiler_forensics_to_aggregate_allowlist():
    profile = _profile(10, "2026-01-01T00:00:00Z")
    profile["duplicate_forensics"] = {
        "duplicate_count": 2,
        "concentration_values": ["sensitive-customer"],
        "description": "sensitive-customer is duplicated",
    }
    profile["delta_metadata"] = {"version": 2, "raw_rows": [{"ssn": "123"}]}
    observation = observe_table(profile, subject="events")
    assert observation["facts"]["duplicates"] == {"duplicate_count": 2}
    assert observation["facts"]["delta"] == {"version": 2}
    assert "sensitive-customer" not in str(observation)


def test_observation_rejects_non_scalar_counts_and_returns_exact_redacted_evidence(tmp_path):
    profile = _profile(10, "2026-01-01T00:00:00Z")
    profile["row_count"] = {"rows": [{"ssn": "123"}]}
    profile["degraded_features"] = ["safe_identifier", "token=https://host/path?sig=secret"]
    result = observe_table(profile, subject="https://user:pass@host/table?sig=secret", persist=True,
                           artifact_root=tmp_path)
    document = json.loads((tmp_path / result["managed_writes"][0]["relative_path"]).read_text(encoding="utf-8"))
    assert result["facts"]["row_count"] is None
    assert result["limitations"] == ["safe_identifier"]
    assert document["payload"] == result["evidence"]
    assert "secret" not in str(result)


def test_registered_incident_projectors_omit_nested_raw_records(tmp_path):
    run = CollectorResult(
        "run_context", "collected",
        facts={"run": {"run_id": 7, "status": "FAILED", "inputs": {"raw_rows": [{"ssn": "123"}]}}},
    )
    uc = CollectorResult(
        "uc_context", "collected",
        facts={"detail": {"table_name": "events", "raw_rows": [{"ssn": "456"}]}, "direct_grants": []},
    )
    delta = CollectorResult(
        "delta_changes", "collected",
        facts={"history": [{"version": 1, "operation": "WRITE",
                             "operation_parameters": {"raw_rows": [{"ssn": "789"}]}}]},
    )
    result = incident_snapshot([run, uc, delta], artifact_root=tmp_path)
    persisted = json.loads(
        (tmp_path / result["managed_writes"][0]["relative_path"]).read_text(encoding="utf-8")
    )["payload"]
    rendered = json.dumps(persisted)
    assert "raw_rows" not in rendered and "ssn" not in rendered
    assert persisted["collectors"][1]["facts"]["run"]["inputs_summary"]["member_count"] == 1
    assert persisted["collectors"][2]["facts"]["detail"] == {"table_name": "events", "columns": []}
    assert persisted["collectors"][3]["facts"]["history"] == [{"version": 1, "operation": "WRITE"}]
