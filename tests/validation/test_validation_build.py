"""Tests for validation/_validation_build.py.

Tests cover _build_context, _rule_columns, _df_to_records, 
_build_summary, _build_recommendation, _build_findings, _build_risks,
_build_suggested_next_actions.
"""

import pandas as pd
import numpy as np
import pytest

from odibi_anchor.validation._validation_build import (
    _build_context,
    _rule_columns,
    _df_to_records,
    _build_summary,
    _build_recommendation,
    _build_findings,
    _build_risks,
    _build_suggested_next_actions,
)


class TestRuleColumns:
    """Test _rule_columns extraction."""

    def test_single_column(self):
        rule = {"column": "name"}
        assert _rule_columns(rule) == ["name"]

    def test_multiple_columns(self):
        rule = {"columns": ["a", "b", "c"]}
        assert _rule_columns(rule) == ["a", "b", "c"]

    def test_column_takes_priority(self):
        rule = {"column": "x", "columns": ["a", "b"]}
        assert _rule_columns(rule) == ["x"]

    def test_no_columns(self):
        rule = {"type": "custom_sql"}
        assert _rule_columns(rule) == []


class TestDfToRecords:
    """Test _df_to_records conversion."""

    def test_basic_conversion(self):
        df = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})
        records = _df_to_records(df)
        assert records == [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

    def test_nan_converted_to_none(self):
        df = pd.DataFrame({"val": [1.0, float("nan"), 3.0]})
        records = _df_to_records(df)
        assert records[1]["val"] is None

    def test_none_converted_to_none(self):
        df = pd.DataFrame({"val": [None, "x"]})
        records = _df_to_records(df)
        assert records[0]["val"] is None

    def test_numpy_types_converted(self):
        df = pd.DataFrame({"val": pd.array([10, 20], dtype="int64")})
        records = _df_to_records(df)
        assert records[0]["val"] == 10
        assert isinstance(records[0]["val"], int)

    def test_timestamp_converted(self):
        df = pd.DataFrame({"ts": pd.to_datetime(["2026-01-01"])})
        records = _df_to_records(df)
        assert "2026-01-01" in records[0]["ts"]

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["a", "b"])
        records = _df_to_records(df)
        assert records == []


class TestBuildSummary:
    """Test _build_summary."""

    def test_all_pass(self):
        result = _build_summary(1000, 5, 0, True, 0)
        assert "5" in result
        assert "write-ready" in result
        assert "1,000" in result

    def test_structural_fail(self):
        rule_results = [
            {"passed": False, "rule_type": "not_null", "rule_id": "not_null:id",
             "columns": ["id"], "failed_count": 150, "failed_rate": 0.15},
        ]
        result = _build_summary(1000, 3, 2, False, 150, rule_results=rule_results)
        assert "blocked" in result.lower()
        assert "150" in result

    def test_domain_only_fail(self):
        rule_results = [
            {"passed": False, "rule_type": "range", "rule_id": "range:amount",
             "columns": ["amount"], "failed_count": 50, "failed_rate": 0.05},
        ]
        result = _build_summary(1000, 3, 1, True, 50, rule_results=rule_results)
        assert "domain" in result.lower()
        assert "50" in result

    def test_zero_rows(self):
        result = _build_summary(0, 1, 0, True, 0)
        assert "0" in result


class TestBuildRecommendation:
    """Test _build_recommendation."""

    def test_all_pass_no_warnings(self):
        result = _build_recommendation(True, [], [])
        assert "OK to promote" in result

    def test_safe_with_warnings(self):
        result = _build_recommendation(True, [], [{"rule_id": "x"}])
        assert "OK to promote" in result
        assert "warning" in result

    def test_blocked(self):
        result = _build_recommendation(False, [{"rule_id": "x"}, {"rule_id": "y"}], [])
        assert "BLOCKED" in result
        assert "2" in result


class TestBuildFindings:
    """Test _build_findings."""

    def test_empty_df_finding(self):
        findings = _build_findings([], 0)
        assert any(f["check_type"] == "empty_dataframe" for f in findings)

    def test_high_failure_rate_finding(self):
        rule_results = [{"passed": False, "rule_id": "r1", "rule_type": "range", "failed_rate": 0.8, "columns": ["x"]}]
        findings = _build_findings(rule_results, 100)
        assert any(f["check_type"] == "high_failure_rate" for f in findings)

    def test_structural_finding(self):
        rule_results = [{"passed": False, "rule_id": "not_null:id", "rule_type": "not_null", "failed_rate": 0.1, "columns": ["id"]}]
        findings = _build_findings(rule_results, 100)
        assert any(f["check_type"] == "structural_constraint_violated" for f in findings)
        assert any(f.get("category") == "structural" for f in findings)

    def test_domain_finding(self):
        rule_results = [{"passed": False, "rule_id": "range:amount", "rule_type": "range", "failed_rate": 0.05, "columns": ["amount"]}]
        findings = _build_findings(rule_results, 100)
        assert any(f["check_type"] == "domain_rule_failure" for f in findings)
        assert any(f.get("category") == "domain" for f in findings)

    def test_no_findings_when_all_pass(self):
        rule_results = [{"passed": True, "rule_id": "r1", "rule_type": "not_null", "failed_rate": 0.0}]
        findings = _build_findings(rule_results, 100)
        assert len(findings) == 0


class TestBuildRisks:
    """Test _build_risks."""

    def test_blocker_creates_data_loss_risk(self):
        rule_results = [{"passed": False, "severity": "blocker", "rule_type": "range",
                         "rule_id": "range:x", "columns": [], "failed_rate": 0.05}]
        risks = _build_risks(rule_results, 100)
        assert any(r["risk"] == "data_loss_on_write" for r in risks)

    def test_null_rule_creates_null_key_risk(self):
        rule_results = [{"passed": False, "severity": "blocker", "rule_type": "not_null",
                         "rule_id": "not_null:id", "columns": ["id"], "failed_rate": 0.1}]
        risks = _build_risks(rule_results, 100)
        assert any(r["risk"] == "null_key_columns" for r in risks)

    def test_unique_rule_creates_duplicate_risk(self):
        rule_results = [{"passed": False, "severity": "blocker", "rule_type": "unique",
                         "rule_id": "unique:id", "columns": ["id"], "failed_rate": 0.05}]
        risks = _build_risks(rule_results, 100)
        assert any(r["risk"] == "duplicate_keys" for r in risks)

    def test_rate_aware_interpretation_low(self):
        rule_results = [{"passed": False, "severity": "warning", "rule_type": "range",
                         "rule_id": "range:x", "columns": ["x"], "failed_rate": 0.005}]
        risks = _build_risks(rule_results, 1000)
        rate_risks = [r for r in risks if r["risk"].startswith("failure_rate_")]
        assert len(rate_risks) == 1
        assert rate_risks[0]["severity"] == "low"
        assert "edge cases" in rate_risks[0]["message"]

    def test_rate_aware_interpretation_high(self):
        rule_results = [{"passed": False, "severity": "blocker", "rule_type": "not_null",
                         "rule_id": "not_null:id", "columns": ["id"], "failed_rate": 0.25}]
        risks = _build_risks(rule_results, 100)
        rate_risks = [r for r in risks if r["risk"].startswith("failure_rate_")]
        assert len(rate_risks) == 1
        assert rate_risks[0]["severity"] == "high"
        assert "systemic" in rate_risks[0]["message"]

    def test_no_risks_when_all_pass(self):
        rule_results = [{"passed": True, "severity": "blocker", "rule_type": "not_null",
                         "rule_id": "not_null:id", "columns": ["id"], "failed_rate": 0.0}]
        risks = _build_risks(rule_results, 100)
        assert risks == []


class TestBuildSuggestedNextActions:
    """Test _build_suggested_next_actions."""

    def test_safe_no_warnings(self):
        rule_results = [{"passed": True, "severity": "blocker", "rule_type": "not_null"}]
        actions = _build_suggested_next_actions(rule_results, True)
        assert isinstance(actions, list)
        assert len(actions) >= 1
        # Should mention promotion or proceed
        assert any("promotion" in a.lower() or "proceed" in a.lower() for a in actions)

    def test_not_safe(self):
        rule_results = [
            {"passed": False, "severity": "blocker", "rule_type": "not_null",
             "rule_id": "not_null:id", "columns": ["id"],
             "failed_count": 5, "failed_rate": 0.5}
        ]
        actions = _build_suggested_next_actions(rule_results, False)
        assert len(actions) > 0


class TestBuildContext:
    """Test _build_context full assembly."""

    def test_basic_assembly(self):
        rule_results = [
            {"rule_id": "r1", "rule_type": "not_null", "columns": ["id"],
             "severity": "blocker", "passed": True, "failed_count": 0,
             "failed_rate": 0.0, "detail": "ok", "fix_expr": "",
             "sample_failures": [], "source": "explicit"},
        ]
        df = pd.DataFrame({"id": [1, 2, 3]})
        mask = pd.Series([True, True, True])
        ctx = _build_context(
            rule_results=rule_results,
            all_pass_masks=[mask],
            total_rows=3,
            engine_name="pandas",
            subject="test",
            df=df,
        )
        assert ctx["kind"] == "validation_summary_context"
        assert ctx["metrics"]["total_rows"] == 3
        assert ctx["metrics"]["rules_passed"] == 1
        assert ctx["metrics"]["is_promotion_safe"] is True

    def test_context_with_failures(self):
        rule_results = [
            {"rule_id": "r1", "rule_type": "not_null", "columns": ["id"],
             "severity": "blocker", "passed": False, "failed_count": 2,
             "failed_rate": 0.5, "detail": "nulls found", "fix_expr": "fix",
             "sample_failures": [], "source": "explicit"},
        ]
        df = pd.DataFrame({"id": [1, None, None]})
        mask = pd.Series([True, False, False])
        ctx = _build_context(
            rule_results=rule_results,
            all_pass_masks=[mask],
            total_rows=3,
            engine_name="pandas",
            subject="test",
            df=df,
        )
        assert ctx["metrics"]["rules_failed"] == 1
        assert ctx["metrics"]["is_promotion_safe"] is False
        assert ctx["metrics"]["rows_with_any_failure"] == 2
