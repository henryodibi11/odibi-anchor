"""Tests for lib/case_file.py — row-level investigation tool."""

import sys

sys.dont_write_bytecode = True

import numpy as np
import pandas as pd
import pytest

from tools.table_profiler_tool.lib.case_file import case_file
from tools.table_profiler_tool.lib.models import GrainAnalysis, TableProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_df():
    """DataFrame with various quality issues for testing filters."""
    return pd.DataFrame({
        "order_id": list(range(1, 21)),
        "customer_id": [
            "C001", "C002", "C003", None, "C005",
            "", "N/A", "C008", "C009", "C010",
            "C001", "C002", "C013", None, "C015",
            "null", "C017", "C018", "C019", "C020",
        ],
        "amount": [
            10, 20, 30, 40, 50,
            60, 70, 80, 90, 100,
            10, 20, 130, 140, 150,
            160, 170, 180, 190, 9999,  # 9999 is an outlier
        ],
        "status": [
            "ACTIVE", "ACTIVE", "ACTIVE", "CANCELLED", "ACTIVE",
            "CANCELLED", "CANCELLED", "ACTIVE", "ACTIVE", "ACTIVE",
            "ACTIVE", "ACTIVE", "ACTIVE", "CANCELLED", "ACTIVE",
            "CANCELLED", "ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE",
        ],
        "source": [
            "web", "web", "api", "legacy", "web",
            "legacy", "legacy", "web", "api", "web",
            "web", "web", "api", "legacy", "web",
            "legacy", "api", "web", "web", "api",
        ],
    })


@pytest.fixture
def duplicate_df():
    """DataFrame with duplicate keys."""
    return pd.DataFrame({
        "id": [1, 2, 3, 3, 4, 4, 4, 5],
        "name": ["A", "B", "C", "C", "D", "D", "D", "E"],
        "value": [10, 20, 30, 31, 40, 41, 42, 50],
    })


@pytest.fixture
def co_occurrence_df():
    """DataFrame where null rows strongly co-occur with specific values."""
    n = 100
    data = {
        "customer_id": [None if i < 20 else f"C{i:03d}" for i in range(n)],
        "status": ["CANCELLED" if i < 18 else "ACTIVE" for i in range(n)],  # 90% co-occurrence
        "source": ["legacy" if i < 16 else "web" for i in range(n)],  # 80% co-occurrence
        "amount": list(range(n)),
    }
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCaseFileFilterNulls:
    """Test filter='nulls'."""

    def test_nulls_basic(self, sample_df):
        result = case_file(sample_df, column="customer_id", filter="nulls")
        m = result["metrics"]
        assert m["matched_rows"] == 2  # Two None values
        assert m["filter_applied"] == "nulls"
        assert m["target_column"] == "customer_id"
        # Verify sample rows contain the null rows
        rows = result["samples"]["rows"]
        assert len(rows) == 2
        assert all(r["customer_id"] is None for r in rows)


class TestCaseFileFilterNullLike:
    """Test filter='null_like'."""

    def test_null_like_catches_sentinels(self, sample_df):
        result = case_file(sample_df, column="customer_id", filter="null_like")
        m = result["metrics"]
        # Should catch: 2 None + 1 empty string + 1 'N/A' + 1 'null' = 5
        assert m["matched_rows"] == 5
        assert m["filter_applied"] == "null_like"


class TestCaseFileFilterOutliers:
    """Test filter='outliers'."""

    def test_outliers_detected(self, sample_df):
        result = case_file(sample_df, column="amount", filter="outliers")
        m = result["metrics"]
        # 9999 is clearly an outlier
        assert m["matched_rows"] >= 1
        rows = result["samples"]["rows"]
        # At least one row with the outlier value
        assert any(r["amount"] == 9999 for r in rows)


class TestCaseFileFilterDuplicates:
    """Test filter='duplicates'."""

    def test_duplicates_with_key_columns(self, duplicate_df):
        result = case_file(
            duplicate_df, column="id", filter="duplicates",
            key_columns=["id"],
        )
        m = result["metrics"]
        # Duplicates: id=3 (2 rows) + id=4 (3 rows) = 5 rows
        assert m["matched_rows"] == 5

    def test_duplicates_with_profile_grain(self, duplicate_df):
        """When key_columns not provided, use profile.grain."""
        grain = GrainAnalysis(best_grain=["id"], is_unique=False)
        prof = TableProfile(subject="test", row_count=8, column_count=3, grain=grain)
        result = case_file(
            duplicate_df, filter="duplicates", profile=prof,
        )
        assert result["metrics"]["matched_rows"] == 5


class TestCaseFileFilterTopBottom:
    """Test filter='top:N' and 'bottom:N'."""

    def test_top_3(self, sample_df):
        result = case_file(sample_df, column="status", filter="top:1")
        # "ACTIVE" is most frequent
        rows = result["samples"]["rows"]
        assert all(r["status"] == "ACTIVE" for r in rows)

    def test_bottom_2(self, sample_df):
        result = case_file(sample_df, column="source", filter="bottom:1")
        m = result["metrics"]
        # "api" is least frequent (5 occurrences vs web=9, legacy=5)
        # Actually both api and legacy have similar counts
        assert m["matched_rows"] >= 1

    def test_top_n_numeric_returns_highest_values(self, sample_df):
        """top:N on numeric column returns exactly N rows with largest values."""
        result = case_file(sample_df, column="amount", filter="top:3")
        rows = result["samples"]["rows"]
        amounts = [r["amount"] for r in rows]
        assert len(rows) == 3
        assert max(amounts) == 9999
        assert all(a >= 180 for a in amounts)

    def test_bottom_n_numeric_returns_lowest_values(self, sample_df):
        """bottom:N on numeric column returns exactly N rows with smallest values."""
        result = case_file(sample_df, column="amount", filter="bottom:3")
        rows = result["samples"]["rows"]
        amounts = [r["amount"] for r in rows]
        assert len(rows) == 3
        assert all(a <= 20 for a in amounts)


class TestCaseFileFilterWhere:
    """Test filter='where:EXPR'."""

    def test_where_expression(self, sample_df):
        result = case_file(sample_df, filter="where:amount > 150")
        m = result["metrics"]
        # Values > 150: 160, 170, 180, 190, 9999 = 5 rows
        assert m["matched_rows"] == 5
        rows = result["samples"]["rows"]
        assert all(r["amount"] > 150 for r in rows)


class TestCaseFileCoOccurrence:
    """Test co-occurrence detection."""

    def test_co_occurrence_detected(self, co_occurrence_df):
        result = case_file(
            co_occurrence_df, column="customer_id", filter="nulls",
        )
        # 20 null rows, 18 have status=CANCELLED (90%)
        co = result["samples"].get("co_occurrences", [])
        assert len(co) > 0
        # Should find status=CANCELLED at >= 70%
        status_co = [c for c in co if c["column"] == "status"]
        assert len(status_co) == 1
        assert status_co[0]["value"] == "CANCELLED"
        assert status_co[0]["pct"] >= 0.70
        # Lift-aware: verify lift and baseline_pct keys
        assert "lift" in status_co[0]
        assert "baseline_pct" in status_co[0]
        assert status_co[0]["lift"] >= 2.0  # 90%/18% ≈ 5x

    def test_co_occurrence_in_findings(self, co_occurrence_df):
        result = case_file(
            co_occurrence_df, column="customer_id", filter="nulls",
        )
        # Should mention co-occurrence with lift info in findings
        co_findings = [f for f in result["findings"] if "Co-occurrence" in f]
        assert len(co_findings) > 0
        assert "lift=" in co_findings[0]
        assert "baseline" in co_findings[0]

    def test_co_occurrence_skips_table_constants(self):
        """Columns that are constant table-wide (mode_pct >= 90%) are ignored."""
        n = 200
        data = {
            "target": [None if i < 40 else f"V{i}" for i in range(n)],
            "constant_col": ["ALWAYS"] * n,  # 100% in full table — noise
            "signal_col": (
                ["ERCOT"] * 36  # 90% of null rows = ERCOT
                + ["MISO"] * 4  # remaining null rows
                + ["ERCOT"] * 40  # 25% of non-null rows
                + ["MISO"] * 60 + ["PJM"] * 60  # rest of non-nulls
            ),
        }
        df = pd.DataFrame(data)
        result = case_file(df, column="target", filter="nulls")
        co = result["samples"].get("co_occurrences", [])
        # constant_col should NOT appear (it's 100% everywhere)
        assert all(c["column"] != "constant_col" for c in co)
        # signal_col SHOULD appear (90% in flagged vs 38% baseline → lift ≈ 2.4x)
        signal = [c for c in co if c["column"] == "signal_col"]
        assert len(signal) == 1
        assert signal[0]["value"] == "ERCOT"
        assert signal[0]["lift"] >= 2.0

    def test_co_occurrence_requires_lift(self):
        """A column with high concentration but NO lift is correctly excluded."""
        n = 200
        # Column with 60% concentration uniformly distributed across all rows
        # (use repeating pattern so null rows and full table have same distribution)
        no_lift_vals = (["ACTIVE"] * 3 + ["INACTIVE"] * 2) * 40  # 60% everywhere
        data = {
            "target": [None if i < 40 else f"V{i}" for i in range(n)],
            "no_lift_col": no_lift_vals,  # 60% in flagged AND full → lift ≈ 1.0
        }
        df = pd.DataFrame(data)
        result = case_file(df, column="target", filter="nulls")
        co = result["samples"].get("co_occurrences", [])
        # no_lift_col has 60% concentration but lift ≈ 1.0 → should NOT appear
        assert len(co) == 0

    def test_co_occurrence_sorted_by_lift(self):
        """Results are sorted by lift descending (strongest signal first)."""
        n = 200
        data = {
            "target": [None if i < 40 else f"V{i}" for i in range(n)],
            # signal_a: 80% in flagged (32/40), 20% baseline → lift=4.0
            "signal_a": (
                ["X"] * 32 + ["Y"] * 8  # null rows
                + ["X"] * 8 + ["Y"] * 152  # non-null rows
            ),
            # signal_b: 60% in flagged (24/40), 15% baseline → lift=4.0
            # Actually let's make it clearly different lift
            # signal_b: 70% in flagged (28/40), 30% baseline → lift=2.33
            "signal_b": (
                ["A"] * 28 + ["B"] * 12  # null rows
                + ["A"] * 32 + ["B"] * 128  # non-null rows
            ),
        }
        df = pd.DataFrame(data)
        result = case_file(df, column="target", filter="nulls")
        co = result["samples"].get("co_occurrences", [])
        assert len(co) == 2
        # First should be highest lift
        assert co[0]["lift"] >= co[1]["lift"]
        assert co[0]["column"] == "signal_a"

    def test_co_occurrence_low_cardinality_checked_first(self):
        """Low-cardinality columns are checked before high-cardinality ones."""
        n = 200
        # Build a df with a high-cardinality col first in column order,
        # and a low-cardinality signal col after it
        data = {
            "target": [None if i < 40 else f"V{i}" for i in range(n)],
            "high_card": [f"ID_{i}" for i in range(n)],  # unique per row
            "low_card_signal": (
                ["SPECIAL"] * 36 + ["OTHER"] * 4  # 90% in flagged
                + ["SPECIAL"] * 20 + ["OTHER"] * 140  # 12.5% in non-null
            ),
        }
        df = pd.DataFrame(data)
        result = case_file(df, column="target", filter="nulls")
        co = result["samples"].get("co_occurrences", [])
        # low_card_signal should be found despite being after high_card in column order
        signal = [c for c in co if c["column"] == "low_card_signal"]
        assert len(signal) == 1
        assert signal[0]["value"] == "SPECIAL"


class TestCaseFileLimitAndContext:
    """Test limit parameter and context_columns."""

    def test_limit_parameter(self, sample_df):
        result = case_file(
            sample_df, column="customer_id", filter="null_like", limit=3,
        )
        rows = result["samples"]["rows"]
        assert len(rows) <= 3

    def test_context_columns(self, sample_df):
        result = case_file(
            sample_df, column="customer_id", filter="nulls",
            context_columns=["order_id", "customer_id"],
        )
        rows = result["samples"]["rows"]
        # Should only have order_id and customer_id columns
        for row in rows:
            assert set(row.keys()) == {"order_id", "customer_id"}


class TestCaseFileRowIds:
    """Test row_ids + key_columns."""

    def test_row_ids_lookup(self, sample_df):
        result = case_file(
            sample_df, row_ids=[1, 5, 10],
            key_columns=["order_id"],
        )
        m = result["metrics"]
        assert m["matched_rows"] == 3
        rows = result["samples"]["rows"]
        assert {r["order_id"] for r in rows} == {1, 5, 10}


class TestCaseFileOutputShape:
    """Test output contract compliance."""

    def test_output_shape(self, sample_df):
        result = case_file(sample_df, column="customer_id", filter="nulls")
        required_keys = {
            "kind", "subject", "summary", "metrics",
            "findings", "risks", "samples", "suggested_next_actions",
        }
        assert required_keys == set(result.keys())
        assert result["kind"] == "case_file"
        assert isinstance(result["metrics"], dict)
        assert isinstance(result["findings"], list)
        assert isinstance(result["risks"], list)
        assert isinstance(result["samples"], dict)
        assert isinstance(result["suggested_next_actions"], list)
        assert "rows" in result["samples"]

    def test_error_case_returns_valid_contract(self, sample_df):
        """Even errors return a valid Anchor contract."""
        result = case_file(
            sample_df, column="nonexistent", filter="nulls",
        )
        # Should return error result, not raise
        required_keys = {
            "kind", "subject", "summary", "metrics",
            "findings", "risks", "samples", "suggested_next_actions",
        }
        assert required_keys == set(result.keys())
        assert result["kind"] == "case_file"


def test_pandas_path_unchanged(sample_df):
    """Existing pandas path should still work identically after Spark-first refactor."""
    result = case_file(sample_df, column="customer_id", filter="nulls")
    assert result["metrics"]["matched_rows"] == 2  # 2 None values in fixture
    assert result["metrics"]["total_rows"] == 20
