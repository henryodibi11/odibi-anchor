"""Unit tests for freshness detection module."""

from __future__ import annotations

import pandas as pd
import pytest

from tools.table_profiler_tool.lib.freshness import detect_freshness
from tools.table_profiler_tool.lib.models import FreshnessAnalysis, Inference

# Fixed base timestamp makes tests deterministic (staleness will be large but >= 0)
BASE = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hourly_df() -> pd.DataFrame:
    """24 rows at 1-hour intervals (hourly cadence)."""
    ts = [BASE + pd.Timedelta(hours=i) for i in range(24)]
    return pd.DataFrame({"loaded_at": ts, "val": range(24)})


@pytest.fixture
def daily_df() -> pd.DataFrame:
    """30 rows at 1-day intervals (daily cadence)."""
    ts = [BASE + pd.Timedelta(days=i) for i in range(30)]
    return pd.DataFrame({"created_date": ts, "amount": [float(i) for i in range(30)]})


@pytest.fixture
def weekly_df() -> pd.DataFrame:
    """8 rows at 7-day intervals (weekly cadence)."""
    ts = [BASE + pd.Timedelta(weeks=i) for i in range(8)]
    return pd.DataFrame({"report_date": ts, "score": range(8)})


@pytest.fixture
def irregular_df() -> pd.DataFrame:
    """Sparse timestamps whose median interval exceeds 31 days (irregular)."""
    offsets = [0, 3, 90, 95, 200, 210, 400, 420, 700]  # days
    ts = [BASE + pd.Timedelta(days=d) for d in offsets]
    return pd.DataFrame({"event_time": ts})


@pytest.fixture
def single_row_df() -> pd.DataFrame:
    """Single-row DataFrame — no interval can be computed."""
    return pd.DataFrame({"ts": [BASE], "val": [42]})


@pytest.fixture
def all_null_temporal_df() -> pd.DataFrame:
    """Timestamp column is entirely null."""
    return pd.DataFrame({
        "ts": pd.Series([None, None, None], dtype="datetime64[us, UTC]"),
        "val": [1, 2, 3],
    })


@pytest.fixture
def no_temporal_df() -> pd.DataFrame:
    """No date/timestamp columns — only numeric and text."""
    return pd.DataFrame({"name": ["Alice", "Bob", "Carol"], "score": [85, 92, 78]})


@pytest.fixture
def gapped_daily_df() -> pd.DataFrame:
    """Daily data with a 3-day gap (days 10-12 absent)."""
    days = list(range(10)) + list(range(13, 23))
    ts = [BASE + pd.Timedelta(days=d) for d in days]
    return pd.DataFrame({"ts": ts, "val": range(len(ts))})


@pytest.fixture
def contiguous_daily_df() -> pd.DataFrame:
    """Daily data with no gaps."""
    ts = [BASE + pd.Timedelta(days=i) for i in range(14)]
    return pd.DataFrame({"ts": ts, "val": range(14)})


@pytest.fixture
def string_date_df() -> pd.DataFrame:
    """Dates stored as ISO strings (auto-detected via parse-rate fallback)."""
    dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    return pd.DataFrame({"event_date": dates, "count": range(5)})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoTemporalColumn:
    """Returns None when no temporal column exists."""

    def test_returns_none(self, no_temporal_df: pd.DataFrame) -> None:
        assert detect_freshness(no_temporal_df) is None

    def test_numeric_only_returns_none(self) -> None:
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
        assert detect_freshness(df) is None


class TestAllNullTemporal:
    """Returns None when the temporal column is entirely null."""

    def test_all_null_returns_none(self, all_null_temporal_df: pd.DataFrame) -> None:
        assert detect_freshness(all_null_temporal_df) is None


class TestSingleRow:
    """Single-row DataFrame: cadence=None, gap_detected=False."""

    def test_returns_freshness_analysis(self, single_row_df: pd.DataFrame) -> None:
        result = detect_freshness(single_row_df)
        assert isinstance(result, FreshnessAnalysis)

    def test_cadence_is_none(self, single_row_df: pd.DataFrame) -> None:
        result = detect_freshness(single_row_df)
        assert result is not None
        assert result.cadence is None

    def test_no_gap(self, single_row_df: pd.DataFrame) -> None:
        result = detect_freshness(single_row_df)
        assert result is not None
        assert result.gap_detected is False
        assert result.gap_description is None

    def test_staleness_non_negative(self, single_row_df: pd.DataFrame) -> None:
        result = detect_freshness(single_row_df)
        assert result is not None
        assert result.staleness_hours >= 0.0


class TestBasicFreshness:
    """Happy-path freshness detection with a clear timestamp column."""

    def test_returns_analysis(self, daily_df: pd.DataFrame) -> None:
        assert isinstance(detect_freshness(daily_df), FreshnessAnalysis)

    def test_freshness_column_identified(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.freshness_column == "created_date"

    def test_staleness_hours_non_negative(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert isinstance(result.staleness_hours, float)
        assert result.staleness_hours >= 0.0

    def test_staleness_string_ends_with_ago(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.staleness.endswith("ago")

    def test_staleness_uses_valid_unit(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert any(
            result.staleness.endswith(sfx) for sfx in ("m ago", "h ago", "d ago")
        )

    def test_latest_earliest_populated(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.latest_value and len(result.latest_value) > 0
        assert result.earliest_value and len(result.earliest_value) > 0


class TestCadenceDetection:
    """Cadence is inferred from the median inter-arrival interval."""

    def test_hourly(self, hourly_df: pd.DataFrame) -> None:
        result = detect_freshness(hourly_df)
        assert result is not None
        assert result.cadence == "hourly"

    def test_daily(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.cadence == "daily"

    def test_weekly(self, weekly_df: pd.DataFrame) -> None:
        result = detect_freshness(weekly_df)
        assert result is not None
        assert result.cadence == "weekly"

    def test_irregular(self, irregular_df: pd.DataFrame) -> None:
        result = detect_freshness(irregular_df)
        assert result is not None
        assert result.cadence == "irregular"

    def test_cadence_is_valid_enum(self, hourly_df: pd.DataFrame) -> None:
        result = detect_freshness(hourly_df)
        assert result is not None
        assert result.cadence in ("hourly", "daily", "weekly", "monthly", "irregular", None)

    def test_avg_rows_per_period_populated_for_regular(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.avg_rows_per_period is not None
        assert result.avg_rows_per_period > 0.0

    def test_avg_rows_per_period_none_for_irregular(self, irregular_df: pd.DataFrame) -> None:
        result = detect_freshness(irregular_df)
        assert result is not None
        assert result.avg_rows_per_period is None


class TestGapDetection:
    """Missing time periods are flagged with a human-readable description."""

    def test_gap_detected(self, gapped_daily_df: pd.DataFrame) -> None:
        result = detect_freshness(gapped_daily_df)
        assert result is not None
        assert result.gap_detected is True

    def test_gap_description_populated(self, gapped_daily_df: pd.DataFrame) -> None:
        result = detect_freshness(gapped_daily_df)
        assert result is not None
        assert result.gap_description is not None
        assert "No data between" in result.gap_description

    def test_no_gap_when_contiguous(self, contiguous_daily_df: pd.DataFrame) -> None:
        result = detect_freshness(contiguous_daily_df)
        assert result is not None
        assert result.gap_detected is False
        assert result.gap_description is None


class TestStringTemporalDetection:
    """String columns with parseable dates are auto-detected."""

    def test_string_date_detected(self, string_date_df: pd.DataFrame) -> None:
        result = detect_freshness(string_date_df)
        assert result is not None
        assert result.freshness_column == "event_date"

    def test_staleness_valid(self, string_date_df: pd.DataFrame) -> None:
        result = detect_freshness(string_date_df)
        assert result is not None
        assert result.staleness_hours >= 0.0


class TestInferenceContract:
    """Inference metadata is correctly attached."""

    def test_inference_present(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert isinstance(result.inference, Inference)

    def test_inference_method(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.inference is not None
        assert result.inference.method == "freshness_detection"

    def test_inference_value(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.inference is not None
        assert result.inference.value == "freshness_detected"

    def test_inference_sample_size(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.inference is not None
        assert result.inference.sample_size == len(daily_df)

    def test_inference_confidence_in_range(self, daily_df: pd.DataFrame) -> None:
        result = detect_freshness(daily_df)
        assert result is not None
        assert result.inference is not None
        assert 0.0 <= result.inference.confidence <= 1.0

    def test_no_result_when_no_temporal(self, no_temporal_df: pd.DataFrame) -> None:
        assert detect_freshness(no_temporal_df) is None


class TestAuditColumnPreference:
    """Regression: audit/load columns must be preferred over business-date columns.

    Mirrors the queue_ercot dogfood failure where freshness picked Approval_Date__
    (a business date that appears first in the schema) instead of _extracted_at
    or Created_Timestamp (audit/ETL provenance columns that appear later).
    """

    def test_prefers_extracted_at_over_business_date(self) -> None:
        """_extracted_at must win even when a business date col appears first."""
        df = pd.DataFrame({
            "approval_date": pd.date_range("2024-01-01", periods=30, freq="MS"),
            "_extracted_at": pd.date_range("2025-01-01", periods=30, freq="D"),
            "value": range(30),
        })
        result = detect_freshness(df)
        assert result is not None
        assert result.freshness_column == "_extracted_at", (
            f"Expected '_extracted_at', got '{result.freshness_column}'"
        )

    def test_prefers_created_at_over_business_date(self) -> None:
        """created_at must win over a plain event_date column."""
        df = pd.DataFrame({
            "event_date": pd.date_range("2023-01-01", periods=30, freq="MS"),
            "created_at": pd.date_range("2025-03-01", periods=30, freq="D"),
        })
        result = detect_freshness(df)
        assert result is not None
        assert result.freshness_column == "created_at"

    def test_prefers_loaded_at(self) -> None:
        """loaded_at is an audit column and should be preferred."""
        df = pd.DataFrame({
            "start_date": pd.date_range("2022-01-01", periods=20, freq="MS"),
            "loaded_at": pd.date_range("2025-04-01", periods=20, freq="D"),
        })
        result = detect_freshness(df)
        assert result is not None
        assert result.freshness_column == "loaded_at"

    def test_prefers_updated_timestamp_case_insensitive(self) -> None:
        """Mixed-case 'Updated_Timestamp' (as in queue_ercot) must be matched."""
        df = pd.DataFrame({
            "Approval_Date": pd.date_range("2024-06-01", periods=20, freq="MS"),
            "Updated_Timestamp": pd.date_range("2025-05-01", periods=20, freq="D"),
        })
        result = detect_freshness(df)
        assert result is not None
        assert result.freshness_column == "Updated_Timestamp"

    def test_falls_back_to_first_when_no_audit_col(self) -> None:
        """When no audit-named col exists the first temporal col is used."""
        df = pd.DataFrame({
            "start_date": pd.date_range("2024-01-01", periods=20, freq="MS"),
            "end_date": pd.date_range("2024-02-01", periods=20, freq="MS"),
        })
        result = detect_freshness(df)
        assert result is not None
        # No audit col: schema-first fallback
        assert result.freshness_column == "start_date"

    def test_etl_prefix_col_preferred(self) -> None:
        """Column starting with 'etl_' is recognised as an audit column."""
        df = pd.DataFrame({
            "report_date": pd.date_range("2024-01-01", periods=20, freq="MS"),
            "etl_timestamp": pd.date_range("2025-01-01", periods=20, freq="D"),
        })
        result = detect_freshness(df)
        assert result is not None
        assert result.freshness_column == "etl_timestamp"
