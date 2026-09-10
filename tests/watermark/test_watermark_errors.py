"""Error-path tests for watermark_debug_context.

Covers:
- None source input
- None target input
- Missing watermark_col (no source or target watermark col)
- Wrong type for source (int, list)
- Wrong type for target (dict)
- Empty DataFrame for source
- DataFrame with only null watermark column
- Invalid output_format
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from tools.watermark_tool.watermark_impl import watermark_debug_context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def source_df():
    """Minimal source DataFrame with a date watermark column."""
    return pd.DataFrame({
        "id": [1, 2, 3],
        "event_date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
        "value": [10.0, 20.0, 30.0],
    })


@pytest.fixture
def target_df():
    """Minimal target DataFrame lagging behind source."""
    return pd.DataFrame({
        "id": [1, 2],
        "event_date": pd.to_datetime(["2024-01-01", "2024-01-02"]),
        "value": [10.0, 20.0],
    })


# ---------------------------------------------------------------------------
# TestWatermarkNoneInputs
# ---------------------------------------------------------------------------

class TestWatermarkNoneInputs:
    def test_none_source_raises(self, target_df):
        """None source must raise ValueError."""
        with pytest.raises(ValueError, match="source is required"):
            watermark_debug_context(
                source=None,
                target=target_df,
                watermark_col="event_date",
                output_format="dict",
            )

    def test_none_target_raises(self, source_df):
        """None target must raise ValueError."""
        with pytest.raises(ValueError, match="target is required"):
            watermark_debug_context(
                source=source_df,
                target=None,
                watermark_col="event_date",
                output_format="dict",
            )

    def test_none_source_and_target_raises(self):
        """Both source and target None must raise ValueError (source checked first)."""
        with pytest.raises(ValueError, match="source is required"):
            watermark_debug_context(
                source=None,
                target=None,
                watermark_col="event_date",
                output_format="dict",
            )


# ---------------------------------------------------------------------------
# TestWatermarkMissingWatermarkCol
# ---------------------------------------------------------------------------

class TestWatermarkMissingWatermarkCol:
    def test_no_watermark_col_raises(self, source_df, target_df):
        """Missing watermark_col must raise ValueError."""
        with pytest.raises(ValueError, match="watermark_col"):
            watermark_debug_context(
                source=source_df,
                target=target_df,
                watermark_col=None,
                output_format="dict",
            )

    def test_no_watermark_col_at_all_raises(self, source_df, target_df):
        """No watermark_col, no source_watermark_col, no target_watermark_col must raise."""
        with pytest.raises(ValueError, match="watermark_col"):
            watermark_debug_context(
                source=source_df,
                target=target_df,
                # All watermark col args omitted
                output_format="dict",
            )


# ---------------------------------------------------------------------------
# TestWatermarkWrongTypes
# ---------------------------------------------------------------------------

class TestWatermarkWrongTypes:
    def test_int_source_raises(self, target_df):
        """Integer source must raise TypeError."""
        with pytest.raises(TypeError, match="must be a table name string or DataFrame"):
            watermark_debug_context(
                source=42,
                target=target_df,
                watermark_col="event_date",
                output_format="dict",
            )

    def test_list_source_raises(self, target_df):
        """List source must raise TypeError."""
        with pytest.raises(TypeError, match="must be a table name string or DataFrame"):
            watermark_debug_context(
                source=["2024-01-01", "2024-01-02"],
                target=target_df,
                watermark_col="event_date",
                output_format="dict",
            )

    def test_dict_target_raises(self, source_df):
        """Dict target must raise TypeError."""
        with pytest.raises(TypeError, match="must be a table name string or DataFrame"):
            watermark_debug_context(
                source=source_df,
                target={"event_date": ["2024-01-01"]},
                watermark_col="event_date",
                output_format="dict",
            )


# ---------------------------------------------------------------------------
# TestWatermarkEdgeCaseDataFrames
# ---------------------------------------------------------------------------

class TestWatermarkEdgeCaseDataFrames:
    def test_empty_source_dataframe_returns_context(self, target_df):
        """Empty source DataFrame (0 rows) must return a valid context dict."""
        empty_src = pd.DataFrame({
            "id": pd.Series([], dtype="int64"),
            "event_date": pd.Series([], dtype="datetime64[ns]"),
        })
        result = watermark_debug_context(
            source=empty_src,
            target=target_df,
            watermark_col="event_date",
            output_format="dict",
        )
        assert isinstance(result, dict)
        assert result["kind"] == "watermark"

    def test_all_null_watermark_col_returns_context(self, target_df):
        """Source with all-null watermark column must return a valid context."""
        null_src = pd.DataFrame({
            "id": [1, 2, 3],
            "event_date": [pd.NaT, pd.NaT, pd.NaT],
        })
        result = watermark_debug_context(
            source=null_src,
            target=target_df,
            watermark_col="event_date",
            output_format="dict",
        )
        assert isinstance(result, dict)
        assert result["kind"] == "watermark"


# ---------------------------------------------------------------------------
# TestWatermarkOutputContract
# ---------------------------------------------------------------------------

class TestWatermarkOutputContract:
    def test_invalid_output_format_raises(self, source_df, target_df):
        """Invalid output_format must raise ValueError."""
        with pytest.raises(ValueError, match="output_format"):
            watermark_debug_context(
                source=source_df,
                target=target_df,
                watermark_col="event_date",
                output_format="xml",
            )

    def test_valid_call_has_required_keys(self, source_df, target_df):
        """Valid call returns a dict with all required contract keys."""
        result = watermark_debug_context(
            source=source_df,
            target=target_df,
            watermark_col="event_date",
            output_format="dict",
        )
        assert isinstance(result, dict)
        for key in ("kind", "subject", "summary", "metrics", "findings"):
            assert key in result, f"Missing required key: {key}"
        assert result["kind"] == "watermark"
