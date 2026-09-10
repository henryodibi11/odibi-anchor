"""Tests for watermark debugger tool.

Pandas-first — no Spark dependency required.
Covers all 5 staleness scenarios, gap detection, cadence inference, and markdown output.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

# Ensure tool and src are importable
_ROOT = Path(__file__).resolve().parents[2]
_TOOL_DIR = _ROOT / "tools" / "watermark_tool"
_SRC_DIR = _ROOT / "src"

if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from watermark_impl import (
    _classify_staleness,
    _compute_stats_pandas,
    _count_pending_pandas,
    _detect_gaps,
    _infer_cadence,
    watermark_debug_context,
)


# ============================================================================
# Fixtures — reusable test data
# ============================================================================


@pytest.fixture
def base_date():
    """Base date for test data generation."""
    return datetime(2026, 6, 3, 14, 30, 0)


@pytest.fixture
def source_df(base_date):
    """Source DataFrame with data through base_date."""
    dates = [base_date - timedelta(hours=i) for i in range(100)]
    return pd.DataFrame({
        "id": range(100),
        "updated_at": dates,
        "value": range(100),
    })


@pytest.fixture
def target_up_to_date(base_date):
    """Target DataFrame that is up to date with source."""
    dates = [base_date - timedelta(hours=i) for i in range(80)]
    return pd.DataFrame({
        "id": range(80),
        "updated_at": dates,
        "value": range(80),
    })


@pytest.fixture
def target_stale(base_date):
    """Target DataFrame 2 days behind source."""
    stale_max = base_date - timedelta(days=2)
    dates = [stale_max - timedelta(hours=i) for i in range(50)]
    return pd.DataFrame({
        "id": range(50),
        "updated_at": dates,
        "value": range(50),
    })


@pytest.fixture
def target_null_watermark():
    """Target DataFrame with all NULL watermarks."""
    return pd.DataFrame({
        "id": range(10),
        "updated_at": [None] * 10,
        "value": range(10),
    })


@pytest.fixture
def target_very_stale(base_date):
    """Target DataFrame 60 days behind source."""
    old_max = base_date - timedelta(days=60)
    dates = [old_max - timedelta(hours=i) for i in range(30)]
    return pd.DataFrame({
        "id": range(30),
        "updated_at": dates,
        "value": range(30),
    })


# ============================================================================
# Tests — staleness classification
# ============================================================================


class TestClassifyStaleness:
    """Test _classify_staleness with all 5 scenarios."""

    def test_up_to_date(self, base_date):
        """Target matches source → up_to_date."""
        result = _classify_staleness(
            target_max=base_date,
            source_max=base_date,
            pending_count=0,
            lookback_days=30,
        )
        assert result == "up_to_date"

    def test_stale(self, base_date):
        """Target behind source with pending rows → stale."""
        target_max = base_date - timedelta(days=2)
        result = _classify_staleness(
            target_max=target_max,
            source_max=base_date,
            pending_count=100,
            lookback_days=30,
        )
        assert result == "stale"

    def test_watermark_null(self, base_date):
        """Target watermark is None → watermark_null."""
        result = _classify_staleness(
            target_max=None,
            source_max=base_date,
            pending_count=50,
            lookback_days=30,
        )
        assert result == "watermark_null"

    def test_very_stale(self, base_date):
        """Target more than lookback_days behind → very_stale."""
        target_max = base_date - timedelta(days=60)
        result = _classify_staleness(
            target_max=target_max,
            source_max=base_date,
            pending_count=1000,
            lookback_days=30,
        )
        assert result == "very_stale"

    def test_filter_mismatch(self, base_date):
        """Target behind source but 0 pending → filter_mismatch."""
        target_max = base_date - timedelta(days=2)
        result = _classify_staleness(
            target_max=target_max,
            source_max=base_date,
            pending_count=0,
            lookback_days=30,
        )
        assert result == "filter_mismatch"

    def test_source_null(self, base_date):
        """Source has no data (None max) → up_to_date."""
        result = _classify_staleness(
            target_max=base_date,
            source_max=None,
            pending_count=0,
            lookback_days=30,
        )
        assert result == "up_to_date"


# ============================================================================
# Tests — gap detection
# ============================================================================


class TestGapDetection:
    """Test _detect_gaps with various scenarios."""

    def test_no_gaps(self):
        """Consecutive dates → no gaps."""
        dates = pd.date_range("2026-05-20", "2026-06-03", freq="D")
        daily = pd.Series([10] * len(dates), index=[d.date() for d in dates])
        gaps = _detect_gaps(daily, lookback_days=30)
        assert gaps == []

    def test_single_gap(self):
        """3-day gap in middle → detected."""
        dates = list(pd.date_range("2026-05-20", "2026-05-25", freq="D"))
        dates += list(pd.date_range("2026-05-29", "2026-06-03", freq="D"))
        daily = pd.Series([10] * len(dates), index=[d.date() for d in dates])
        gaps = _detect_gaps(daily, lookback_days=30)
        assert len(gaps) == 1
        assert gaps[0]["days"] == 3
        assert gaps[0]["start"] == "2026-05-26"
        assert gaps[0]["end"] == "2026-05-28"

    def test_multiple_gaps(self):
        """Multiple gaps → all detected."""
        # Days: 1,2,3, gap, 6,7, gap, 10,11
        dates = [
            pd.Timestamp("2026-06-01").date(),
            pd.Timestamp("2026-06-02").date(),
            pd.Timestamp("2026-06-03").date(),
            pd.Timestamp("2026-06-06").date(),
            pd.Timestamp("2026-06-07").date(),
            pd.Timestamp("2026-06-10").date(),
            pd.Timestamp("2026-06-11").date(),
        ]
        daily = pd.Series([5] * len(dates), index=dates)
        gaps = _detect_gaps(daily, lookback_days=30)
        assert len(gaps) == 2
        assert gaps[0]["days"] == 2  # June 4-5
        assert gaps[1]["days"] == 2  # June 8-9

    def test_empty_input(self):
        """Empty or single-day input → no gaps."""
        assert _detect_gaps(pd.Series(dtype="int64"), lookback_days=30) == []
        daily = pd.Series([10], index=[pd.Timestamp("2026-06-01").date()])
        assert _detect_gaps(daily, lookback_days=30) == []

    def test_none_input(self):
        """None input → no gaps."""
        assert _detect_gaps(None, lookback_days=30) == []


# ============================================================================
# Tests — cadence inference
# ============================================================================


class TestCadenceInference:
    """Test _infer_cadence with different patterns."""

    def test_daily_cadence(self):
        """Consecutive daily data → daily."""
        dates = pd.date_range("2026-05-01", "2026-06-01", freq="D")
        daily = pd.Series([10] * len(dates), index=[d.date() for d in dates])
        assert _infer_cadence(daily) == "daily"

    def test_weekly_cadence(self):
        """Weekly data → weekly."""
        dates = pd.date_range("2026-04-01", "2026-06-01", freq="7D")
        daily = pd.Series([10] * len(dates), index=[d.date() for d in dates])
        assert _infer_cadence(daily) == "weekly"

    def test_irregular_cadence(self):
        """Irregular spacing → irregular."""
        dates = [
            pd.Timestamp("2026-05-01").date(),
            pd.Timestamp("2026-05-05").date(),
            pd.Timestamp("2026-05-20").date(),
            pd.Timestamp("2026-06-01").date(),
        ]
        daily = pd.Series([10] * len(dates), index=dates)
        assert _infer_cadence(daily) == "irregular"

    def test_unknown_cadence(self):
        """Insufficient data → unknown."""
        assert _infer_cadence(None) == "unknown"
        assert _infer_cadence(pd.Series(dtype="int64")) == "unknown"


# ============================================================================
# Tests — full integration (watermark_debug_context)
# ============================================================================


class TestWatermarkDebugContext:
    """Integration tests for the main entry point."""

    def test_up_to_date_scenario(self, source_df, target_up_to_date):
        """Source and target aligned → up_to_date classification."""
        result = watermark_debug_context(
            source_df, target_up_to_date,
            watermark_col="updated_at",
            output_format="dict",
        )
        assert result["kind"] == "watermark"
        assert result["metrics"]["staleness"] == "up_to_date"
        assert result["metrics"]["pending_count"] == 0

    def test_stale_scenario(self, source_df, target_stale):
        """Target 2 days behind → stale classification with pending rows."""
        result = watermark_debug_context(
            source_df, target_stale,
            watermark_col="updated_at",
            output_format="dict",
        )
        assert result["kind"] == "watermark"
        assert result["metrics"]["staleness"] == "stale"
        assert result["metrics"]["pending_count"] > 0
        assert result["metrics"]["lag_hours"] is not None
        assert result["metrics"]["lag_hours"] > 0

    def test_null_watermark_scenario(self, source_df, target_null_watermark):
        """Target all NULLs → watermark_null classification."""
        result = watermark_debug_context(
            source_df, target_null_watermark,
            watermark_col="updated_at",
            output_format="dict",
        )
        assert result["metrics"]["staleness"] == "watermark_null"
        assert result["metrics"]["target_null_watermark_count"] == 10
        assert result["metrics"]["pending_count"] == 100  # All source rows pending

    def test_very_stale_scenario(self, source_df, target_very_stale):
        """Target 60 days behind → very_stale classification."""
        result = watermark_debug_context(
            source_df, target_very_stale,
            watermark_col="updated_at",
            output_format="dict",
        )
        assert result["metrics"]["staleness"] == "very_stale"
        assert result["metrics"]["lag_days"] > 30

    def test_different_watermark_columns(self, base_date):
        """Source and target with different watermark column names."""
        source = pd.DataFrame({
            "id": range(10),
            "modified_date": [base_date - timedelta(hours=i) for i in range(10)],
        })
        target = pd.DataFrame({
            "id": range(5),
            "load_timestamp": [base_date - timedelta(days=3, hours=i) for i in range(5)],
        })
        result = watermark_debug_context(
            source, target,
            source_watermark_col="modified_date",
            target_watermark_col="load_timestamp",
            output_format="dict",
        )
        assert result["kind"] == "watermark"
        assert result["metrics"]["staleness"] in ("stale", "very_stale")
        assert result["metrics"]["pending_count"] > 0

    def test_contract_shape(self, source_df, target_stale):
        """Verify output has all standard Anchor contract keys."""
        result = watermark_debug_context(
            source_df, target_stale,
            watermark_col="updated_at",
            output_format="dict",
        )
        required_keys = {"kind", "subject", "summary", "metrics", "findings",
                         "risks", "samples", "suggested_next_actions"}
        assert required_keys.issubset(set(result.keys()))
        assert isinstance(result["metrics"], dict)
        assert isinstance(result["findings"], list)
        assert isinstance(result["risks"], list)
        assert isinstance(result["samples"], dict)
        assert isinstance(result["suggested_next_actions"], list)

    def test_markdown_output(self, source_df, target_stale):
        """output_format='markdown' returns a string with expected sections."""
        result = watermark_debug_context(
            source_df, target_stale,
            watermark_col="updated_at",
            output_format="markdown",
        )
        assert isinstance(result, str)
        assert "Watermark Debug" in result
        assert "STALE" in result
        assert "Findings" in result
        assert "Suggested Next Actions" in result

    def test_subject_override(self, source_df, target_stale):
        """Custom subject is used in output."""
        result = watermark_debug_context(
            source_df, target_stale,
            watermark_col="updated_at",
            subject="orders_pipeline",
            output_format="dict",
        )
        assert result["subject"] == "orders_pipeline"

    def test_missing_source_raises(self, target_stale):
        """Missing source raises ValueError."""
        with pytest.raises(ValueError, match="source is required"):
            watermark_debug_context(target=target_stale, watermark_col="updated_at")

    def test_missing_target_raises(self, source_df):
        """Missing target raises ValueError."""
        with pytest.raises(ValueError, match="target is required"):
            watermark_debug_context(source_df, watermark_col="updated_at")

    def test_missing_watermark_col_raises(self, source_df, target_stale):
        """Missing watermark_col raises ValueError."""
        with pytest.raises(ValueError, match="watermark_col"):
            watermark_debug_context(source_df, target_stale)


# ============================================================================
# Tests — compute stats (Pandas path)
# ============================================================================


class TestComputeStatsPandas:
    """Test _compute_stats_pandas helper."""

    def test_basic_stats(self, base_date):
        """Computes max, null count, total correctly."""
        df = pd.DataFrame({
            "wm": [base_date - timedelta(hours=i) for i in range(10)] + [None],
        })
        stats = _compute_stats_pandas(df, "wm", lookback_days=30)
        assert stats["max_watermark"] == base_date
        assert stats["null_count"] == 1
        assert stats["total_count"] == 11

    def test_all_null(self):
        """All NULL watermarks → max is None."""
        df = pd.DataFrame({"wm": [None, None, None]})
        stats = _compute_stats_pandas(df, "wm", lookback_days=30)
        assert stats["max_watermark"] is None
        assert stats["null_count"] == 3
        assert stats["total_count"] == 3

    def test_daily_counts_produced(self, base_date):
        """Daily counts series is populated."""
        dates = [base_date - timedelta(days=i) for i in range(10)]
        df = pd.DataFrame({"wm": dates})
        stats = _compute_stats_pandas(df, "wm", lookback_days=30)
        assert len(stats["daily_counts"]) == 10


# ============================================================================
# Tests — pending count
# ============================================================================


class TestCountPendingPandas:
    """Test _count_pending_pandas helper."""

    def test_pending_with_target_max(self, base_date):
        """Counts rows newer than target_max."""
        source = pd.DataFrame({
            "wm": [base_date - timedelta(hours=i) for i in range(10)],
        })
        target_max = base_date - timedelta(hours=5)
        assert _count_pending_pandas(source, "wm", target_max) == 5

    def test_pending_with_null_target(self, base_date):
        """All rows pending when target_max is None."""
        source = pd.DataFrame({
            "wm": [base_date - timedelta(hours=i) for i in range(10)],
        })
        assert _count_pending_pandas(source, "wm", None) == 10

    def test_zero_pending(self, base_date):
        """No rows pending when target is ahead."""
        source = pd.DataFrame({
            "wm": [base_date - timedelta(days=5, hours=i) for i in range(10)],
        })
        target_max = base_date  # Target is ahead
        assert _count_pending_pandas(source, "wm", target_max) == 0
