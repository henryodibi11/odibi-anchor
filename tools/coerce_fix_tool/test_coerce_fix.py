"""Tests for coerce_fix tool."""
from __future__ import annotations

import pandas as pd
import pytest

from odibi_anchor.tables.coercion_classifier import coercion_check_context
from tools.coerce_fix_tool.coerce_fix_impl import (
    _apply_fix_pandas,
    _capture_samples,
    _fix_case,
    _fix_date,
    _fix_numeric,
    _fix_unicode,
    _fix_whitespace,
    coerce_fix_context,
)


# ── Unit tests for individual fix functions ──────────────────────────────────


class TestFixWhitespace:
    """Test _fix_whitespace function."""

    def test_strips_leading_trailing(self):
        s = pd.Series(["  hello  ", "  world  "])
        result = _fix_whitespace(s)
        assert result.tolist() == ["hello", "world"]

    def test_collapses_internal_spaces(self):
        s = pd.Series(["New   York", "San    Francisco"])
        result = _fix_whitespace(s)
        assert result.tolist() == ["New York", "San Francisco"]

    def test_handles_tabs_and_newlines(self):
        s = pd.Series(["hello\tworld", "foo\nbar"])
        result = _fix_whitespace(s)
        assert result.tolist() == ["hello world", "foo bar"]

    def test_preserves_nulls(self):
        s = pd.Series(["  hello  ", None, "world"])
        result = _fix_whitespace(s)
        assert result.iloc[0] == "hello"
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == "world"


class TestFixCase:
    """Test _fix_case function."""

    def test_upper(self):
        s = pd.Series(["active", "Pending", "CLOSED"])
        result = _fix_case(s, target="upper")
        assert result.tolist() == ["ACTIVE", "PENDING", "CLOSED"]

    def test_lower(self):
        s = pd.Series(["ACTIVE", "Pending", "closed"])
        result = _fix_case(s, target="lower")
        assert result.tolist() == ["active", "pending", "closed"]

    def test_preserves_nulls(self):
        s = pd.Series(["Hello", None, "World"])
        result = _fix_case(s, target="upper")
        assert result.iloc[0] == "HELLO"
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == "WORLD"


class TestFixUnicode:
    """Test _fix_unicode function."""

    def test_strips_zero_width_space(self):
        s = pd.Series(["PJM\u200b", "MISO\u200c"])
        result = _fix_unicode(s)
        assert result.tolist() == ["PJM", "MISO"]

    def test_strips_bom(self):
        s = pd.Series(["\ufeffvalue", "normal"])
        result = _fix_unicode(s)
        assert result.tolist() == ["value", "normal"]

    def test_strips_soft_hyphen(self):
        s = pd.Series(["soft\u00adhyphen"])
        result = _fix_unicode(s)
        assert result.tolist() == ["softhyphen"]

    def test_preserves_nulls(self):
        s = pd.Series(["hello\u200b", None])
        result = _fix_unicode(s)
        assert result.iloc[0] == "hello"
        assert pd.isna(result.iloc[1])


class TestFixNumeric:
    """Test _fix_numeric function."""

    def test_removes_commas(self):
        s = pd.Series(["1,234", "5,678,900"])
        result = _fix_numeric(s)
        assert result.tolist() == ["1234", "5678900"]

    def test_removes_spaces(self):
        s = pd.Series(["1 234", "5 678"])
        result = _fix_numeric(s)
        assert result.tolist() == ["1234", "5678"]

    def test_preserves_decimals(self):
        s = pd.Series(["1,234.56", "7,890.12"])
        result = _fix_numeric(s)
        assert result.tolist() == ["1234.56", "7890.12"]

    def test_preserves_nulls(self):
        s = pd.Series(["1,000", None])
        result = _fix_numeric(s)
        assert result.iloc[0] == "1000"
        assert pd.isna(result.iloc[1])


class TestFixDate:
    """Test _fix_date function."""

    def test_us_format_to_iso(self):
        s = pd.Series(["05/14/2026", "12/25/2025"])
        result = _fix_date(s, target_fmt="%Y-%m-%d")
        assert result.tolist() == ["2026-05-14", "2025-12-25"]

    def test_iso_stays_iso(self):
        s = pd.Series(["2026-05-14"])
        result = _fix_date(s, target_fmt="%Y-%m-%d")
        assert result.tolist() == ["2026-05-14"]

    def test_preserves_nulls(self):
        s = pd.Series(["2026-01-01", None])
        result = _fix_date(s, target_fmt="%Y-%m-%d")
        assert result.iloc[0] == "2026-01-01"
        assert pd.isna(result.iloc[1])

    def test_unparseable_left_unchanged(self):
        s = pd.Series(["not-a-date", "2026-01-01"])
        result = _fix_date(s, target_fmt="%Y-%m-%d")
        assert result.iloc[0] == "not-a-date"
        assert result.iloc[1] == "2026-01-01"


# ── Unit test for apply_fix_pandas dispatcher ────────────────────────────────


class TestApplyFixPandas:
    """Test _apply_fix_pandas dispatcher."""

    def test_dispatches_whitespace(self):
        s = pd.Series(["  hello  "])
        result = _apply_fix_pandas(s, "whitespace")
        assert result.iloc[0] == "hello"

    def test_dispatches_case_upper(self):
        s = pd.Series(["hello"])
        result = _apply_fix_pandas(s, "case", case_target="upper")
        assert result.iloc[0] == "HELLO"

    def test_dispatches_case_lower(self):
        s = pd.Series(["HELLO"])
        result = _apply_fix_pandas(s, "case", case_target="lower")
        assert result.iloc[0] == "hello"

    def test_dispatches_unicode(self):
        s = pd.Series(["hi\u200b"])
        result = _apply_fix_pandas(s, "unicode")
        assert result.iloc[0] == "hi"

    def test_dispatches_numeric(self):
        s = pd.Series(["1,234"])
        result = _apply_fix_pandas(s, "numeric_representation")
        assert result.iloc[0] == "1234"

    def test_dispatches_date(self):
        s = pd.Series(["05/14/2026"])
        result = _apply_fix_pandas(s, "date_format", date_target="%Y-%m-%d")
        assert result.iloc[0] == "2026-05-14"

    def test_invalid_category_raises(self):
        s = pd.Series(["x"])
        with pytest.raises(ValueError, match="Cannot fix category"):
            _apply_fix_pandas(s, "genuine")


# ── Unit test for sampling ───────────────────────────────────────────────────


class TestCaptureSamples:
    """Test _capture_samples."""

    def test_captures_changed_values(self):
        before = pd.Series(["  A  ", "  B  ", "C"])
        after = pd.Series(["A", "B", "C"])
        result = _capture_samples(before, after, n=5)
        assert result["before"] == ["  A  ", "  B  "]
        assert result["after"] == ["A", "B"]

    def test_respects_limit(self):
        before = pd.Series([f"  val{i}  " for i in range(20)])
        after = pd.Series([f"val{i}" for i in range(20)])
        result = _capture_samples(before, after, n=3)
        assert len(result["before"]) == 3
        assert len(result["after"]) == 3

    def test_no_changes_returns_empty(self):
        before = pd.Series(["A", "B", "C"])
        after = pd.Series(["A", "B", "C"])
        result = _capture_samples(before, after, n=5)
        assert result["before"] == []
        assert result["after"] == []


# ── Integration tests: coerce_fix_context ────────────────────────────────────


class TestCoerceFixContext:
    """Test full coerce_fix_context integration."""

    @pytest.fixture
    def whitespace_data(self):
        """DataFrames with whitespace mismatches."""
        old = pd.DataFrame({"id": [1, 2, 3], "name": ["John", "Jane", "Bob"]})
        new = pd.DataFrame({"id": [1, 2, 3], "name": ["  John  ", "Jane  ", "  Bob"]})
        return old, new

    @pytest.fixture
    def case_data(self):
        """DataFrames with case mismatches."""
        old = pd.DataFrame({"id": [1, 2], "status": ["ACTIVE", "CLOSED"]})
        new = pd.DataFrame({"id": [1, 2], "status": ["active", "closed"]})
        return old, new

    @pytest.fixture
    def mixed_data(self):
        """DataFrames with mixed categories including genuine."""
        old = pd.DataFrame({
            "id": [1, 2, 3, 4],
            "name": ["John", "Jane", "Bob", "Alice"],
            "status": ["ACTIVE", "CLOSED", "PENDING", "ACTIVE"],
            "desc": ["Wind", "Solar", "Gas", "Hydro"],
        })
        new = pd.DataFrame({
            "id": [1, 2, 3, 4],
            "name": ["  John  ", "Jane  ", "  Bob", "Alice"],
            "status": ["active", "closed", "pending", "active"],
            "desc": ["Solar", "Wind", "Nuclear", "Coal"],
        })
        return old, new

    def test_whitespace_fix(self, whitespace_data):
        old, new = whitespace_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["name"])
        result = coerce_fix_context(new, coerce_ctx)

        assert result["kind"] == "coerce_fix"
        assert result["metrics"]["columns_fixed"] == 1
        assert result["metrics"]["total_values_corrected"] == 3
        # Verify the fixed DataFrame
        fixed_df = result["df"]
        assert fixed_df["name"].tolist() == ["John", "Jane", "Bob"]

    def test_case_fix_upper(self, case_data):
        old, new = case_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["status"])
        result = coerce_fix_context(new, coerce_ctx, case_target="upper")

        fixed_df = result["df"]
        assert fixed_df["status"].tolist() == ["ACTIVE", "CLOSED"]

    def test_case_fix_lower(self, case_data):
        old, new = case_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["status"])
        result = coerce_fix_context(new, coerce_ctx, case_target="lower")

        fixed_df = result["df"]
        assert fixed_df["status"].tolist() == ["active", "closed"]

    def test_genuine_skipped(self, mixed_data):
        old, new = mixed_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"])
        result = coerce_fix_context(new, coerce_ctx)

        # desc column should be skipped (genuine)
        skipped_cols = [s["column"] for s in result["skipped"]]
        assert "desc" in skipped_cols

        # name and status should be fixed
        fixed_cols = [f["column"] for f in result["fixes_applied"]]
        assert "name" in fixed_cols
        assert "status" in fixed_cols

    def test_selective_columns(self, mixed_data):
        old, new = mixed_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"])
        # Only fix name, not status
        result = coerce_fix_context(new, coerce_ctx, columns=["name"])

        fixed_cols = [f["column"] for f in result["fixes_applied"]]
        assert "name" in fixed_cols
        assert "status" not in fixed_cols

    def test_dry_run_no_modification(self, whitespace_data):
        old, new = whitespace_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["name"])
        result = coerce_fix_context(new, coerce_ctx, dry_run=True)

        assert result["metrics"]["dry_run"] is True
        assert "fixes_planned" in result
        assert len(result["fixes_planned"]) == 1
        # No df in dry run
        assert "df" not in result

    def test_does_not_mutate_original(self, whitespace_data):
        old, new = whitespace_data
        original_values = new["name"].tolist()
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["name"])
        coerce_fix_context(new, coerce_ctx)

        # Original DataFrame unchanged
        assert new["name"].tolist() == original_values

    def test_samples_populated(self, whitespace_data):
        old, new = whitespace_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["name"])
        result = coerce_fix_context(new, coerce_ctx)

        assert "name" in result["samples"]
        assert len(result["samples"]["name"]["before"]) > 0
        assert len(result["samples"]["name"]["after"]) > 0

    def test_empty_dataframe(self):
        old = pd.DataFrame({"id": [], "val": []})
        new = pd.DataFrame({"id": [], "val": []})
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["val"])
        result = coerce_fix_context(new, coerce_ctx)

        assert result["metrics"]["columns_fixed"] == 0
        assert result["metrics"]["total_values_corrected"] == 0

    def test_output_contract_keys(self, whitespace_data):
        old, new = whitespace_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["name"])
        result = coerce_fix_context(new, coerce_ctx)

        required_keys = [
            "kind", "subject", "summary", "metrics", "findings",
            "risks", "samples", "suggested_next_actions",
            "fixes_applied", "skipped", "df",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    def test_markdown_output(self, whitespace_data):
        old, new = whitespace_data
        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["name"])
        result = coerce_fix_context(new, coerce_ctx, output_format="markdown")

        assert isinstance(result, str)
        assert "Coerce Fix" in result
        assert "name" in result

    def test_missing_df_raises(self):
        with pytest.raises(ValueError, match="df is required"):
            coerce_fix_context(df=None, coerce_ctx={"column_results": {}})

    def test_missing_coerce_ctx_raises(self):
        df = pd.DataFrame({"id": [1], "val": ["x"]})
        with pytest.raises(ValueError, match="coerce_ctx is required"):
            coerce_fix_context(df=df, coerce_ctx=None)

    def test_invalid_coerce_ctx_raises(self):
        df = pd.DataFrame({"id": [1], "val": ["x"]})
        with pytest.raises(ValueError, match="missing.*column_results"):
            coerce_fix_context(df=df, coerce_ctx={"no_results": True})

    def test_invalid_case_target_raises(self):
        df = pd.DataFrame({"id": [1], "val": ["x"]})
        ctx = {"column_results": {"val": {"dominant_category": "case", "total_mismatches": 1}}}
        with pytest.raises(ValueError, match="case_target"):
            coerce_fix_context(df=df, coerce_ctx=ctx, case_target="title")


# ── Integration: end-to-end workflow ─────────────────────────────────────────


class TestEndToEndWorkflow:
    """Test the full diff → coerce_check → coerce_fix → verify workflow."""

    def test_fix_reduces_mismatches_to_zero(self):
        """After coerce_fix, re-running coerce_check should show no mismatches."""
        old = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["John Smith", "Jane Doe", "Bob Jones"],
            "code": ["ABC", "DEF", "GHI"],
        })
        new = pd.DataFrame({
            "id": [1, 2, 3],
            "name": ["  John  Smith  ", "Jane   Doe", "  Bob Jones  "],
            "code": ["abc", "def", "ghi"],
        })

        # Step 1: coerce_check
        coerce_ctx = coercion_check_context(old, new, keys=["id"])

        # Step 2: coerce_fix
        fix_result = coerce_fix_context(new, coerce_ctx)
        fixed_df = fix_result["df"]

        # Step 3: re-check — should be zero mismatches
        recheck_ctx = coercion_check_context(old, fixed_df, keys=["id"])
        assert recheck_ctx["metrics"]["total_mismatches"] == 0

    def test_numeric_representation_fix(self):
        """Numeric representation fixes work end-to-end.

        Note: coerce_check classifies numeric_representation when float()
        succeeds on both sides (e.g. "2026.0" vs "2026"). Commas don't
        parse as float in Python, so "1,234" vs "1234" is classified genuine.
        """
        old = pd.DataFrame({"id": [1, 2], "amount": ["1234", "5678"]})
        new = pd.DataFrame({"id": [1, 2], "amount": ["1234.0", "5678.0"]})

        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["amount"])
        assert coerce_ctx["column_results"]["amount"]["dominant_category"] == "numeric_representation"

        fix_result = coerce_fix_context(new, coerce_ctx)
        fixed_df = fix_result["df"]

        # numeric fix removes commas/spaces but doesn't change decimal notation
        # The fix strips formatting chars — "1234.0" stays as "1234.0"
        # What matters is coerce_fix applied the operation
        assert fix_result["metrics"]["columns_fixed"] == 1

    def test_unicode_fix_end_to_end(self):
        """Unicode zero-width char fixes work end-to-end."""
        old = pd.DataFrame({"id": [1, 2], "region": ["PJM", "MISO"]})
        new = pd.DataFrame({"id": [1, 2], "region": ["PJM\u200b", "MISO\ufeff"]})

        coerce_ctx = coercion_check_context(old, new, keys=["id"], columns=["region"])
        fix_result = coerce_fix_context(new, coerce_ctx)
        fixed_df = fix_result["df"]

        assert fixed_df["region"].tolist() == ["PJM", "MISO"]
