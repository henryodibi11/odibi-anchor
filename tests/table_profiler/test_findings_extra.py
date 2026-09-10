"""Tests for lib/findings_extra.py — Backlog F19-F24 findings."""

from __future__ import annotations

import sys

import pandas as pd
import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.findings_extra import (
    detect_extra_findings,
    _detect_monotonicity,
    _detect_value_concentration,
    _detect_empty_columns,
    _detect_ordering_signal,
    _detect_conditional_sparsity,
)
from tools.table_profiler_tool.lib.models import ColumnProfile, ColumnRole, SemanticType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(
    name, spark_type="string", null_pct=0.0, distinct_count=10,
    is_unique=False, role=ColumnRole.DIMENSION, min_value=None,
    max_value=None, top_values=None, row_count=100, correlated_nulls=None,
):
    p = ColumnProfile(
        name=name,
        position=0,
        spark_type=spark_type,
        null_pct=null_pct,
        null_count=int(null_pct * row_count),
        non_null_count=int((1 - null_pct) * row_count),
        row_count=row_count,
        distinct_count=distinct_count,
        semantic_type=SemanticType.UNKNOWN,
        role=role,
        is_unique=is_unique,
        min_value=min_value,
        max_value=max_value,
        top_values=top_values or [],
    )
    if correlated_nulls:
        p.correlated_nulls = correlated_nulls
    return p


# ---------------------------------------------------------------------------
# F19: Monotonicity
# ---------------------------------------------------------------------------


class TestMonotonicity:
    """F19 — watermark candidate detection."""

    def test_detects_unique_integer_column(self):
        profiles = [
            _make_profile("id", spark_type="long", is_unique=True,
                         min_value=1, max_value=1000, role=ColumnRole.PRIMARY_KEY),
        ]
        result = _detect_monotonicity(profiles, 1000)
        assert len(result) == 1
        assert "id" in result[0]
        assert "watermark" in result[0].lower()

    def test_skips_non_unique(self):
        profiles = [
            _make_profile("amount", spark_type="long", is_unique=False,
                         min_value=0, max_value=1000),
        ]
        result = _detect_monotonicity(profiles, 1000)
        assert result == []

    def test_skips_dimension_role(self):
        profiles = [
            _make_profile("category", spark_type="long", is_unique=True,
                         min_value=1, max_value=100, role=ColumnRole.DIMENSION),
        ]
        result = _detect_monotonicity(profiles, 100)
        assert result == []

    def test_skips_string_type(self):
        profiles = [
            _make_profile("code", spark_type="string", is_unique=True,
                         min_value="A", max_value="Z"),
        ]
        result = _detect_monotonicity(profiles, 26)
        assert result == []

    def test_detects_timestamp_column(self):
        profiles = [
            _make_profile("created_at", spark_type="timestamp", is_unique=True,
                         min_value="2020-01-01", max_value="2025-12-31",
                         role=ColumnRole.TIMESTAMP),
        ]
        result = _detect_monotonicity(profiles, 100)
        assert len(result) == 1
        assert "created_at" in result[0]


# ---------------------------------------------------------------------------
# F20: Conditional Sparsity
# ---------------------------------------------------------------------------


class TestConditionalSparsity:
    """F20 — conditional sparsity explanation."""

    def test_detects_explained_nulls(self):
        profiles = [
            _make_profile("cancel_reason", null_pct=0.60, distinct_count=5,
                         correlated_nulls=["status"]),
        ]
        result = _detect_conditional_sparsity(profiles)
        assert len(result) == 1
        assert "cancel_reason" in result[0]
        assert "structurally expected" in result[0]

    def test_skips_low_null_rate(self):
        profiles = [
            _make_profile("name", null_pct=0.05, correlated_nulls=["type"]),
        ]
        result = _detect_conditional_sparsity(profiles)
        assert result == []

    def test_skips_no_correlated_nulls(self):
        profiles = [
            _make_profile("detail", null_pct=0.50),
        ]
        result = _detect_conditional_sparsity(profiles)
        assert result == []


# ---------------------------------------------------------------------------
# F21: Value Concentration
# ---------------------------------------------------------------------------


class TestValueConcentration:
    """F21 — Pareto / single-value dominance."""

    def test_detects_dominant_value(self):
        profiles = [
            _make_profile("status", distinct_count=3, is_unique=False,
                         row_count=100,
                         top_values=[{"value": "Active", "count": 90}]),
        ]
        result = _detect_value_concentration(profiles, 100)
        assert len(result) == 1
        assert "Active" in result[0]
        assert "90%" in result[0]

    def test_skips_below_threshold(self):
        profiles = [
            _make_profile("category", distinct_count=5, is_unique=False,
                         row_count=100,
                         top_values=[{"value": "A", "count": 50}]),
        ]
        result = _detect_value_concentration(profiles, 100)
        assert result == []

    def test_skips_unique_columns(self):
        profiles = [
            _make_profile("id", is_unique=True, row_count=100,
                         top_values=[{"value": "1", "count": 1}]),
        ]
        result = _detect_value_concentration(profiles, 100)
        assert result == []

    def test_skips_key_roles(self):
        profiles = [
            _make_profile("pk", distinct_count=100, is_unique=False,
                         role=ColumnRole.PRIMARY_KEY, row_count=100,
                         top_values=[{"value": "X", "count": 85}]),
        ]
        result = _detect_value_concentration(profiles, 100)
        assert result == []


# ---------------------------------------------------------------------------
# F23: Empty / Near-Empty Columns
# ---------------------------------------------------------------------------


class TestEmptyColumns:
    """F23 — dead and near-empty column detection."""

    def test_detects_dead_column(self):
        profiles = [_make_profile("unused_col", null_pct=1.0)]
        result = _detect_empty_columns(profiles)
        assert len(result) == 1
        assert "unused_col" in result[0]
        assert "Dead" in result[0]

    def test_detects_near_empty(self):
        profiles = [_make_profile("rare_col", null_pct=0.995)]
        result = _detect_empty_columns(profiles)
        assert len(result) == 1
        assert "rare_col" in result[0]
        assert "Near-empty" in result[0]

    def test_detects_single_value(self):
        profiles = [_make_profile("constant", null_pct=0.0, distinct_count=1)]
        result = _detect_empty_columns(profiles)
        assert len(result) == 1
        assert "constant" in result[0]
        assert "Single-value" in result[0]

    def test_skips_normal_columns(self):
        profiles = [_make_profile("name", null_pct=0.05, distinct_count=50)]
        result = _detect_empty_columns(profiles)
        assert result == []

    def test_multiple_dead_columns(self):
        profiles = [
            _make_profile("dead1", null_pct=1.0),
            _make_profile("dead2", null_pct=1.0),
            _make_profile("dead3", null_pct=1.0),
        ]
        result = _detect_empty_columns(profiles)
        assert len(result) == 1  # Single finding listing all
        assert "dead1" in result[0]
        assert "dead2" in result[0]


# ---------------------------------------------------------------------------
# F24: Row Ordering Signal
# ---------------------------------------------------------------------------


class TestOrderingSignal:
    """F24 — natural sort signal detection."""

    def test_detects_sequence_column(self):
        profiles = [
            _make_profile("row_num", spark_type="long", is_unique=True,
                         min_value=1, max_value=100),
        ]
        result = _detect_ordering_signal(profiles, 100)
        assert len(result) == 1
        assert "row_num" in result[0]

    def test_detects_unique_timestamp(self):
        profiles = [
            _make_profile("event_ts", spark_type="timestamp", is_unique=True,
                         role=ColumnRole.TIMESTAMP),
        ]
        result = _detect_ordering_signal(profiles, 100)
        assert len(result) == 1
        assert "event_ts" in result[0]

    def test_skips_non_ordering_columns(self):
        profiles = [
            _make_profile("name", spark_type="string", distinct_count=50),
            _make_profile("amount", spark_type="double", distinct_count=80),
        ]
        result = _detect_ordering_signal(profiles, 100)
        assert result == []


# ---------------------------------------------------------------------------
# Integration: detect_extra_findings
# ---------------------------------------------------------------------------


class TestIntegration:
    """Verify detect_extra_findings combines all sub-detections."""

    def test_returns_empty_for_none_profiles(self):
        assert detect_extra_findings(None) == []

    def test_returns_empty_for_empty_profiles(self):
        assert detect_extra_findings([]) == []

    def test_combines_multiple_findings(self):
        profiles = [
            _make_profile("id", spark_type="long", is_unique=True,
                         min_value=1, max_value=100, role=ColumnRole.PRIMARY_KEY,
                         row_count=100),
            _make_profile("dead_col", null_pct=1.0, row_count=100),
            _make_profile("status", distinct_count=2, is_unique=False,
                         row_count=100,
                         top_values=[{"value": "Active", "count": 95}]),
        ]
        result = detect_extra_findings(profiles, 100)
        # Should have at least: dead column + concentration + watermark
        assert len(result) >= 2

    def test_profiler_integration_no_crash(self):
        """profile_table should not crash with extra findings."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        df = pd.DataFrame({
            "id": range(50),
            "name": [f"N_{i}" for i in range(50)],
            "status": ["Active"] * 45 + ["Inactive"] * 5,
        })
        result = profile_table(df, "test_extra", level="standard")
        assert result is not None
        assert "findings_extra" not in result.degraded_features
