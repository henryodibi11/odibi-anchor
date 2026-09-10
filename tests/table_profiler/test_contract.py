"""Tests for lib/contract.py — Anchor Standard Output Contract (Phase 4)."""

from __future__ import annotations

import json
import sys

import pandas as pd
import numpy as np
import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.contract import serialize_profile
from tools.table_profiler_tool.lib.profiler import profile_table
from tools.table_profiler_tool.lib.models import (
    ColumnProfile,
    ColumnRole,
    FormatIssue,
    GrainAnalysis,
    Inference,
    JoinProfile,
    SemanticType,
    TableClassification,
    TableProfile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_profile():
    """Minimal clean profile for contract testing."""
    df = pd.DataFrame({
        "id": range(100),
        "name": [f"Person_{i}" for i in range(100)],
        "amount": [float(i * 10) for i in range(100)],
    })
    return profile_table(df, "test_basic", level="standard")


@pytest.fixture
def messy_profile():
    """Profile with nulls, duplicates, format issues."""
    n = 100
    df = pd.DataFrame({
        "customer_id": [f"CUST_{i % 30}" for i in range(n)],
        "email": [f"user{i}@example.com" if i % 5 != 0 else None for i in range(n)],
        "amount": [float(i) for i in range(n)],
        "status": ["Active"] * 60 + ["Inactive"] * 40,
        "notes": ["  leading space" if i % 10 == 0 else f"note_{i}" for i in range(n)],
    })
    return profile_table(df, "customer_data", level="standard")


# ---------------------------------------------------------------------------
# Contract Shape
# ---------------------------------------------------------------------------


class TestContractShape:
    """Verify the output has exactly the required top-level keys."""

    def test_has_all_required_keys(self, basic_profile):
        result = serialize_profile(basic_profile)
        required_keys = {
            "kind", "subject", "summary", "metrics", "column_profiles",
            "findings", "risks", "samples", "suggested_next_actions",
        }
        assert set(result.keys()) == required_keys

    def test_kind_is_profile_table(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert result["kind"] == "profile_table"

    def test_subject_matches_input(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert result["subject"] == "test_basic"

    def test_summary_is_single_line_string(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert isinstance(result["summary"], str)
        assert "\n" not in result["summary"]
        assert len(result["summary"]) > 0

    def test_summary_contains_key_numbers(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert "100" in result["summary"]  # row count
        assert "3" in result["summary"]    # column count


# ---------------------------------------------------------------------------
# Metrics Contract
# ---------------------------------------------------------------------------


class TestMetricsContract:
    """Verify metrics contains only numeric/bool values."""

    def test_metrics_is_flat_dict(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert isinstance(result["metrics"], dict)

    def test_metrics_values_are_numeric_or_bool(self, basic_profile):
        result = serialize_profile(basic_profile)
        for key, value in result["metrics"].items():
            assert isinstance(value, (int, float, bool)), (
                f"metrics['{key}'] is {type(value).__name__}, expected int/float/bool"
            )

    def test_metrics_no_nested_dicts(self, basic_profile):
        result = serialize_profile(basic_profile)
        for key, value in result["metrics"].items():
            assert not isinstance(value, (dict, list)), (
                f"metrics['{key}'] is {type(value).__name__}, expected flat"
            )

    def test_metrics_has_core_fields(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert "row_count" in result["metrics"]
        assert "column_count" in result["metrics"]
        assert "quality_score" in result["metrics"]
        assert "profiling_duration_ms" in result["metrics"]

    def test_metrics_row_count_correct(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert result["metrics"]["row_count"] == 100

    def test_metrics_column_count_correct(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert result["metrics"]["column_count"] == 3

    def test_metrics_quality_score_in_range(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert 0.0 <= result["metrics"]["quality_score"] <= 1.0

    def test_metrics_grain_fields_present(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert "grain_is_unique" in result["metrics"]
        assert "grain_duplicate_rate" in result["metrics"]
        assert isinstance(result["metrics"]["grain_is_unique"], bool)


# ---------------------------------------------------------------------------
# Findings Contract
# ---------------------------------------------------------------------------


class TestFindingsContract:
    """Verify findings is a flat list of strings."""

    def test_findings_is_list(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert isinstance(result["findings"], list)

    def test_findings_all_strings(self, basic_profile):
        result = serialize_profile(basic_profile)
        for f in result["findings"]:
            assert isinstance(f, str)

    def test_findings_nonempty_for_classified_table(self, basic_profile):
        result = serialize_profile(basic_profile)
        # Should at least have classification finding
        assert len(result["findings"]) >= 1


# ---------------------------------------------------------------------------
# Risks Contract
# ---------------------------------------------------------------------------


class TestRisksContract:
    """Verify risks is a flat list of strings."""

    def test_risks_is_list(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert isinstance(result["risks"], list)

    def test_risks_all_strings(self, basic_profile):
        result = serialize_profile(basic_profile)
        for r in result["risks"]:
            assert isinstance(r, str)

    def test_risks_includes_format_errors(self):
        """Format issues with severity='error' should appear in risks."""
        profile = TableProfile(subject="test")
        profile.format_issues = [
            FormatIssue(
                issue_type="test_error",
                column="col_a",
                severity="error",
                description="Critical format problem",
                affected_count=10,
                affected_pct=0.1,
            )
        ]
        result = serialize_profile(profile)
        assert any("col_a" in r for r in result["risks"])

    def test_risks_includes_degraded_features(self):
        """Degraded features should appear as a risk."""
        profile = TableProfile(subject="test")
        profile.degraded_features = ["freshness", "grain"]
        result = serialize_profile(profile)
        assert any("degraded" in r.lower() for r in result["risks"])


# ---------------------------------------------------------------------------
# Samples Contract
# ---------------------------------------------------------------------------


class TestSamplesContract:
    """Verify samples is a dict of bounded collections."""

    def test_samples_is_dict(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert isinstance(result["samples"], dict)

    def test_samples_columns_bounded(self, basic_profile):
        result = serialize_profile(basic_profile)
        if "columns" in result["samples"]:
            assert len(result["samples"]["columns"]) <= 10

    def test_samples_custom_limit(self, basic_profile):
        result = serialize_profile(basic_profile, sample_limit=2)
        if "columns" in result["samples"]:
            assert len(result["samples"]["columns"]) <= 2

    def test_samples_grain_present(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert "grain" in result["samples"]
        assert "columns" in result["samples"]["grain"]
        assert "is_unique" in result["samples"]["grain"]

    def test_samples_column_has_name_and_type(self, basic_profile):
        result = serialize_profile(basic_profile)
        if result["samples"].get("columns"):
            col = result["samples"]["columns"][0]
            assert "name" in col
            assert "spark_type" in col
            assert "null_pct" in col

    def test_samples_format_issues_bounded(self, messy_profile):
        result = serialize_profile(messy_profile, sample_limit=3)
        if "format_issues" in result["samples"]:
            assert len(result["samples"]["format_issues"]) <= 3

    def test_samples_include_classification_ambiguity_metadata(self):
        profile = TableProfile(subject="classification")
        profile.classification = TableClassification.FACT
        profile.classification_confidence = 0.72
        profile.classification_reasoning = "mostly measures"
        profile.classification_inference = Inference(
            value=TableClassification.FACT,
            confidence=0.72,
            evidence=["measure_heavy"],
            runner_ups=[
                Inference(
                    value=TableClassification.DIMENSION,
                    confidence=0.55,
                    evidence=["descriptive_fields"],
                )
            ],
            verification_hint="confirm whether rows append over time",
            blocker_reason="mixed fact and descriptive signals",
        )

        result = serialize_profile(profile, sample_limit=5)

        assert result["samples"]["classification"]["runner_ups"][0]["value"] == "dimension"
        assert result["samples"]["classification"]["verification_hint"]
        assert result["samples"]["classification"]["blocker_reason"]

    def test_samples_include_column_ambiguity_metadata(self):
        profile = TableProfile(subject="ambiguity")
        profile.columns = [
            ColumnProfile(
                name="zip_code",
                position=0,
                spark_type="string",
                semantic_type=SemanticType.ZIP_CODE,
                semantic_type_inference=Inference(
                    value=SemanticType.ZIP_CODE,
                    confidence=0.85,
                    evidence=["pattern_match"],
                    runner_ups=[
                        Inference(
                            value=SemanticType.NUMERIC_STRING,
                            confidence=0.55,
                            evidence=["all_digits"],
                            verification_hint="check whether leading zeros matter",
                        )
                    ],
                    verification_hint="check whether leading zeros matter",
                ),
            )
        ]

        result = serialize_profile(profile, sample_limit=5)

        assert "ambiguity" in result["samples"]
        ambiguity = result["samples"]["ambiguity"][0]
        assert ambiguity["name"] == "zip_code"
        assert ambiguity["semantic_inference"]["runner_ups"][0]["value"] == "numeric_string"
        assert ambiguity["semantic_inference"]["verification_hint"]

    def test_samples_include_grain_runner_ups_and_verification(self):
        profile = TableProfile(subject="grain_case")
        profile.grain = GrainAnalysis(
            best_grain=["order_id"],
            is_unique=False,
            duplicate_rate=0.1,
            runner_up_grains=[
                {
                    "columns": ["order_id", "line_number"],
                    "duplicate_rate": 0.0,
                    "null_exclusion_rate": 0.0,
                    "is_unique": True,
                }
            ],
            verification_hint="check whether line_number is required for row-level grain",
        )

        result = serialize_profile(profile, sample_limit=5)

        assert result["samples"]["grain"]["runner_up_grains"][0]["columns"] == ["order_id", "line_number"]
        assert result["samples"]["grain"]["verification_hint"]


# ---------------------------------------------------------------------------
# Suggested Next Actions Contract
# ---------------------------------------------------------------------------


class TestSuggestedActionsContract:
    """Verify suggested_next_actions is a flat list of strings."""

    def test_actions_is_list(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert isinstance(result["suggested_next_actions"], list)

    def test_actions_all_strings(self, basic_profile):
        result = serialize_profile(basic_profile)
        for a in result["suggested_next_actions"]:
            assert isinstance(a, str)

    def test_actions_nonempty(self, basic_profile):
        result = serialize_profile(basic_profile)
        assert len(result["suggested_next_actions"]) >= 1

    def test_actions_reference_duplicates_when_present(self):
        """When grain has duplicates, actions should mention it."""
        profile = TableProfile(subject="test")
        profile.grain = GrainAnalysis(
            best_grain=["id"], is_unique=False, duplicate_rate=0.15
        )
        result = serialize_profile(profile)
        assert any("duplicate" in a.lower() for a in result["suggested_next_actions"])


# ---------------------------------------------------------------------------
# JSON Serializability
# ---------------------------------------------------------------------------


class TestJsonSerializability:
    """Verify entire output is JSON-serializable."""

    def test_basic_profile_serializable(self, basic_profile):
        result = serialize_profile(basic_profile)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        # Round-trip
        deserialized = json.loads(serialized)
        assert deserialized["kind"] == "profile_table"

    def test_messy_profile_serializable(self, messy_profile):
        result = serialize_profile(messy_profile)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)

    def test_no_numpy_types_in_output(self, basic_profile):
        """Ensure no numpy types leak into the output."""
        result = serialize_profile(basic_profile)
        serialized = json.dumps(result)
        # If numpy types leaked, json.dumps would raise TypeError
        assert len(serialized) > 0

    def test_empty_profile_serializable(self):
        """Minimal empty profile should still serialize."""
        profile = TableProfile(subject="empty")
        result = serialize_profile(profile)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Verify output is deterministic given same input."""

    def test_same_input_same_output(self, basic_profile):
        result1 = serialize_profile(basic_profile)
        result2 = serialize_profile(basic_profile)
        # Compare JSON strings for exact equality
        assert json.dumps(result1, sort_keys=True) == json.dumps(result2, sort_keys=True)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_profile_with_no_columns(self):
        profile = TableProfile(subject="empty_table")
        result = serialize_profile(profile)
        assert result["metrics"]["column_count"] == 0
        assert "columns" not in result["samples"]

    def test_profile_with_no_grain(self):
        profile = TableProfile(subject="no_grain")
        result = serialize_profile(profile)
        # Should still serialize without grain metrics
        assert "grain_is_unique" in result["metrics"]

    def test_profile_with_numpy_int_values(self):
        """Numpy ints should be converted to Python int."""
        profile = TableProfile(subject="numpy_test")
        profile.row_count = np.int64(1000)
        profile.column_count = np.int32(5)
        result = serialize_profile(profile)
        assert isinstance(result["metrics"]["row_count"], int)
        assert isinstance(result["metrics"]["column_count"], int)
        json.dumps(result)  # Should not raise

    def test_profile_with_numpy_float_values(self):
        """Numpy floats should be converted to Python float."""
        profile = TableProfile(subject="numpy_float_test")
        profile.overall_quality_score = np.float64(0.95)
        result = serialize_profile(profile)
        assert isinstance(result["metrics"]["quality_score"], float)
        json.dumps(result)  # Should not raise


def test_serialize_profile_with_joins():
    """serialize_profile must not crash when joins are populated."""
    profile = TableProfile(
        subject="test_joins",
        row_count=1000,
        column_count=3,
        columns=[
            ColumnProfile(name="user_id", position=0, spark_type="string"),
        ],
        joins=[
            JoinProfile(
                source_column="user_id",
                target_table="dim_users",
                target_column="id",
                cardinality="many-to-one",
                overlap_pct=0.95,
                orphan_count=50,
                orphan_pct=0.05,
                format_compatible=True,
                safe_join_type="LEFT",
            ),
        ],
    )
    result = serialize_profile(profile)
    assert "joins" in result["samples"]
    assert result["samples"]["joins"][0]["safe_join_type"] == "LEFT"
    # Verify it's JSON-serializable
    import json
    json.dumps(result)
