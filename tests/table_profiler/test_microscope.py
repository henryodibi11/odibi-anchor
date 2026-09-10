"""Tests for lib/microscope.py — column-level deep-dive tool."""

import sys

sys.dont_write_bytecode = True

import numpy as np
import pandas as pd
import pytest

from tools.table_profiler_tool.lib.microscope import microscope
from tools.table_profiler_tool.lib.models import ColumnProfile, TableProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def numeric_df():
    """DataFrame with a numeric column including nulls, outliers, zeros."""
    values: list[int | None] = list(range(1, 101))  # 1-100
    values[0] = None  # 1 null
    values[1] = 0  # 1 zero
    values[2] = -5  # 1 negative
    values[99] = 9999  # outlier
    return pd.DataFrame({"amount": values})


@pytest.fixture
def string_df():
    """DataFrame with string column including empty strings, whitespace, null-like."""
    values = [
        "Alice", "Bob", "Charlie", "  Dave  ", "Eve",
        "", "N/A", "null", "Frank", "Grace",
        "HENRY", "iris", "Jack Smith", "kate", "LEO",
        None, "Mike", "Nancy", "Oscar", "Pat",
    ]
    return pd.DataFrame({"name": values})


@pytest.fixture
def timestamp_df():
    """DataFrame with a date column spanning 2 years with a gap."""
    dates = pd.date_range("2022-01-01", periods=100, freq="D").tolist()
    # Insert a 60-day gap
    dates[50:] = pd.date_range("2022-04-01", periods=50, freq="D").tolist()
    dates.append(None)  # 1 null
    return pd.DataFrame({"created_at": dates})


@pytest.fixture
def boolean_df():
    """DataFrame with boolean-like values."""
    values = [True, False, True, True, False, None, True, False, True, True]
    return pd.DataFrame({"is_active": values})


@pytest.fixture
def constant_df():
    """DataFrame where a column has all the same value."""
    return pd.DataFrame({"status": ["ACTIVE"] * 50})


@pytest.fixture
def unique_df():
    """DataFrame where every value is unique (ID-like)."""
    return pd.DataFrame({"record_id": [f"REC-{i:05d}" for i in range(100)]})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMicroscopeNumeric:
    """Test microscope on numeric columns."""

    def test_numeric_basic_metrics(self, numeric_df):
        result = microscope(numeric_df, "amount")
        m = result["metrics"]
        assert m["row_count"] == 100
        assert m["null_count"] == 1
        assert m["null_pct"] == 0.01
        assert m["column_type_category"] == "numeric"

    def test_numeric_stats(self, numeric_df):
        result = microscope(numeric_df, "amount")
        m = result["metrics"]
        assert "min" in m
        assert "max" in m
        assert "mean" in m
        assert "median" in m
        assert "std" in m
        assert "skewness" in m

    def test_numeric_histogram(self, numeric_df):
        result = microscope(numeric_df, "amount", bin_count=10)
        samples = result["samples"]
        assert "histogram" in samples
        assert len(samples["histogram"]) == 10
        assert all("bin_start" in h and "count" in h for h in samples["histogram"])

    def test_numeric_outliers(self, numeric_df):
        result = microscope(numeric_df, "amount")
        m = result["metrics"]
        assert m["outlier_count"] > 0
        assert "outliers" in result["samples"]

    def test_numeric_zero_and_negative(self, numeric_df):
        result = microscope(numeric_df, "amount")
        m = result["metrics"]
        assert m["zero_count"] >= 1
        assert m["negative_count"] >= 1


class TestMicroscopeString:
    """Test microscope on string columns."""

    def test_string_length_distribution(self, string_df):
        result = microscope(string_df, "name")
        m = result["metrics"]
        assert "length_min" in m
        assert "length_max" in m
        assert "length_mean" in m
        samples = result["samples"]
        assert "length_distribution" in samples

    def test_string_null_like_detection(self, string_df):
        result = microscope(string_df, "name")
        m = result["metrics"]
        assert m.get("null_like_count", 0) > 0
        assert "null_like_values" in result["samples"]

    def test_string_pattern_fingerprints(self, string_df):
        result = microscope(string_df, "name")
        samples = result["samples"]
        assert "pattern_fingerprints" in samples
        assert len(samples["pattern_fingerprints"]) > 0

    def test_string_whitespace_detection(self, string_df):
        result = microscope(string_df, "name")
        m = result["metrics"]
        # '  Dave  ' has leading/trailing whitespace
        assert m.get("whitespace_issues", 0) > 0


class TestMicroscopeTimestamp:
    """Test microscope on timestamp columns."""

    def test_timestamp_range(self, timestamp_df):
        result = microscope(timestamp_df, "created_at")
        m = result["metrics"]
        assert "earliest" in m
        assert "latest" in m
        assert "span_days" in m
        assert m["span_days"] > 0

    def test_timestamp_cadence(self, timestamp_df):
        result = microscope(timestamp_df, "created_at")
        m = result["metrics"]
        assert "inferred_cadence" in m
        assert m["inferred_cadence"] == "daily"

    def test_timestamp_gap(self, timestamp_df):
        result = microscope(timestamp_df, "created_at")
        m = result["metrics"]
        assert "max_gap_days" in m


class TestMicroscopeBoolean:
    """Test microscope on boolean columns."""

    def test_boolean_distribution(self, boolean_df):
        result = microscope(boolean_df, "is_active")
        m = result["metrics"]
        assert "true_count" in m
        assert "false_count" in m
        assert "balance_ratio" in m
        assert m["column_type_category"] == "boolean"

    def test_boolean_balance(self, boolean_df):
        result = microscope(boolean_df, "is_active")
        m = result["metrics"]
        # 7 True, 3 False -> balance = 3/10 = 0.3
        assert m["balance_ratio"] > 0
        assert m["balance_ratio"] <= 0.5


class TestMicroscopeEdgeCases:
    """Test edge cases."""

    def test_constant_column(self, constant_df):
        result = microscope(constant_df, "status")
        m = result["metrics"]
        assert m["is_constant"] is True
        assert m["distinct_count"] == 1

    def test_unique_column(self, unique_df):
        result = microscope(unique_df, "record_id")
        m = result["metrics"]
        assert m["is_unique"] is True
        assert m["distinct_pct"] == 1.0

    def test_with_precomputed_profile(self, numeric_df):
        """When a TableProfile is passed, reuse ColumnProfile."""
        col_prof = ColumnProfile(
            name="amount", position=0, spark_type="double",
            null_count=1, null_pct=0.01, row_count=100,
        )
        prof = TableProfile(
            subject="test", row_count=100, column_count=1,
            columns=[col_prof],
        )
        result = microscope(numeric_df, "amount", profile=prof)
        assert result["kind"] == "microscope"
        # Should still compute full analysis
        assert result["metrics"]["row_count"] == 100

    def test_unknown_column_raises(self, numeric_df):
        with pytest.raises(ValueError, match="not found"):
            microscope(numeric_df, "nonexistent_column")

    def test_output_shape(self, numeric_df):
        """Verify all Anchor contract keys are present."""
        result = microscope(numeric_df, "amount")
        required_keys = {
            "kind", "subject", "summary", "metrics",
            "findings", "risks", "samples", "suggested_next_actions",
        }
        assert required_keys == set(result.keys())
        assert result["kind"] == "microscope"
        assert isinstance(result["metrics"], dict)
        assert isinstance(result["findings"], list)
        assert isinstance(result["risks"], list)
        assert isinstance(result["samples"], dict)
        assert isinstance(result["suggested_next_actions"], list)


def test_sampling_metadata_small_df():
    """Small DataFrames should not be sampled."""
    df = pd.DataFrame({"x": range(100)})
    result = microscope(df, "x")
    assert result["metrics"]["sampled"] is False
    assert "source_row_count" not in result["metrics"]


def test_sampling_metadata_large_df():
    """Large DataFrames should report sampling metadata."""
    # Create a DataFrame larger than _MAX_SAMPLE_FOR_DISTRIBUTION (100_000)
    df = pd.DataFrame({"x": range(200_000)})
    result = microscope(df, "x")
    assert result["metrics"]["sampled"] is True
    assert result["metrics"]["source_row_count"] == 200_000
    assert result["metrics"]["sample_size"] <= 100_000


def test_profile_reuse_overrides_sampled_counts():
    """When profile is provided and sampling occurred, exact counts from profile override."""
    df = pd.DataFrame({"x": range(200_000)})
    col_profile = ColumnProfile(
        name="x", position=0, spark_type="long",
        row_count=200_000, null_count=0, null_pct=0.0,
        distinct_count=200_000, distinct_pct=1.0, is_unique=True,
    )
    profile = TableProfile(subject="test", columns=[col_profile])
    result = microscope(df, "x", profile=profile)
    # Exact count from profile should override sampled approximations
    assert result["metrics"]["row_count"] == 200_000
    assert result["metrics"]["distinct_count"] == 200_000
