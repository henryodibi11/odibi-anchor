"""Comprehensive tests for diff_ops.py internals and integration.

Tests cover:
- NULL-aware categorization (null_to_value, value_to_null, value_changed)
- render_diff_report() rendering
- Percentage metrics (added_key_pct, removed_key_pct, changed_key_pct)
- Internal helper functions
- Edge cases and output contract verification
- Performance bounds
"""

import json
from decimal import Decimal
from datetime import date, datetime

import pandas as pd
import pytest

from odibi_anchor.tables.diff_ops import (
    diff_tables_by_key,
    render_diff_report,
    _validate_engine_arg,
    _validate_sample_limit,
    _validate_column_name_list,
    _duplicate_values,
    _json_safe_value,
    _column_change_masks_pandas,
    _changed_column_counts_categorized,
    _resolve_compare_columns,
    _validate_no_duplicate_dataframe_columns,
    _null_key_summary_pandas,
    _duplicate_key_summary_pandas,
    _dtype_changes_pandas,
    _normalize_dtype_changes,
    _columns_context,
    _schema_findings,
    _blocking_diff_risks,
    _non_blocking_diff_risks,
    _change_findings,
    _empty_diff_samples,
    _records_to_json_safe,
    _blocked_summary,
    _ok_summary,
    _false_mask_like,
    _combine_boolean_masks,
    _sample_key_records_pandas,
)


# =============================================================================
# Section 1: NULL-aware categorization tests
# =============================================================================


class TestNullCategorization:
    """Test the null_to_value / value_to_null / value_changed breakdown."""

    def test_null_to_value__single_column(self):
        old = pd.DataFrame({"id": [1, 2], "status": [None, "active"]})
        new = pd.DataFrame({"id": [1, 2], "status": ["new", "active"]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        cc = ctx["metrics"]["changed_column_counts"]
        status_cc = next(c for c in cc if c["column"] == "status")
        assert status_cc["null_to_value"] == 1
        assert status_cc["value_to_null"] == 0
        assert status_cc["value_changed"] == 0
        assert status_cc["changed_key_count"] == 1

    def test_value_to_null__single_column(self):
        old = pd.DataFrame({"id": [1, 2], "status": ["active", "retired"]})
        new = pd.DataFrame({"id": [1, 2], "status": ["active", None]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        cc = ctx["metrics"]["changed_column_counts"]
        status_cc = next(c for c in cc if c["column"] == "status")
        assert status_cc["value_to_null"] == 1
        assert status_cc["null_to_value"] == 0
        assert status_cc["value_changed"] == 0

    def test_value_changed__both_non_null(self):
        old = pd.DataFrame({"id": [1], "amount": [100.0]})
        new = pd.DataFrame({"id": [1], "amount": [200.0]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        cc = ctx["metrics"]["changed_column_counts"]
        amount_cc = next(c for c in cc if c["column"] == "amount")
        assert amount_cc["value_changed"] == 1
        assert amount_cc["null_to_value"] == 0
        assert amount_cc["value_to_null"] == 0

    def test_mixed_null_transitions__multi_row(self):
        old = pd.DataFrame({"id": [1, 2, 3, 4], "val": [None, "x", "y", None]})
        new = pd.DataFrame({"id": [1, 2, 3, 4], "val": ["a", None, "z", None]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        cc = ctx["metrics"]["changed_column_counts"]
        val_cc = next(c for c in cc if c["column"] == "val")
        assert val_cc["null_to_value"] == 1   # id=1: None→"a"
        assert val_cc["value_to_null"] == 1   # id=2: "x"→None
        assert val_cc["value_changed"] == 1   # id=3: "y"→"z"
        # id=4: None→None is NOT a change
        assert val_cc["changed_key_count"] == 3

    def test_both_null__not_counted_as_change(self):
        old = pd.DataFrame({"id": [1, 2], "val": [None, None]})
        new = pd.DataFrame({"id": [1, 2], "val": [None, None]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        assert ctx["metrics"]["changed_key_count"] == 0
        assert ctx["metrics"]["changed_column_counts"] == []

    def test_multi_column_null_categorization(self):
        old = pd.DataFrame({"id": [1], "a": [None], "b": ["old"]})
        new = pd.DataFrame({"id": [1], "a": ["new"], "b": [None]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        cc = ctx["metrics"]["changed_column_counts"]
        a_cc = next(c for c in cc if c["column"] == "a")
        b_cc = next(c for c in cc if c["column"] == "b")
        assert a_cc["null_to_value"] == 1
        assert b_cc["value_to_null"] == 1

    def test_null_categorization__all_null_column_unchanged(self):
        old = pd.DataFrame({"id": [1, 2, 3], "val": [None, None, None]})
        new = pd.DataFrame({"id": [1, 2, 3], "val": [None, None, None]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        assert ctx["metrics"]["changed_key_count"] == 0


# =============================================================================
# Section 2: Percentage metrics tests
# =============================================================================


class TestPercentageMetrics:
    """Test added_key_pct, removed_key_pct, changed_key_pct."""

    def test_pct_metrics__half_added(self):
        old = pd.DataFrame({"id": [1, 2], "v": [10, 20]})
        new = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        assert ctx["metrics"]["added_key_pct"] == 0.5  # 1 added / 2 old keys
        assert ctx["metrics"]["removed_key_pct"] == 0.0
        assert ctx["metrics"]["changed_key_pct"] == 0.0

    def test_pct_metrics__all_removed(self):
        old = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
        new = pd.DataFrame({"id": [4], "v": [40]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        assert ctx["metrics"]["removed_key_pct"] == 1.0  # 3 removed / 3 old
        assert ctx["metrics"]["added_key_pct"] == pytest.approx(1/3, rel=1e-3)

    def test_pct_metrics__empty_old_no_division_error(self):
        old = pd.DataFrame(columns=["id", "v"])
        new = pd.DataFrame({"id": [1, 2], "v": [10, 20]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        # With old_key_count=0, denominator becomes 1 to avoid ZeroDivisionError
        assert ctx["metrics"]["added_key_pct"] == 2.0

    def test_pct_metrics__changed(self):
        old = pd.DataFrame({"id": [1, 2, 3, 4], "v": [10, 20, 30, 40]})
        new = pd.DataFrame({"id": [1, 2, 3, 4], "v": [10, 99, 99, 40]})

        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        assert ctx["metrics"]["changed_key_pct"] == 0.5  # 2 changed / 4 old keys


# =============================================================================
# Section 3: render_diff_report() tests
# =============================================================================


class TestRenderDiffReport:
    """Test the markdown rendering of diff contexts."""

    def test_render__ok_status(self):
        old = pd.DataFrame({"id": [1, 2], "v": [10, 20]})
        new = pd.DataFrame({"id": [1, 2], "v": [10, 25]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        report = render_diff_report(ctx)

        assert "# Diff:" in report
        assert "Status:" in report
        assert "OK" in report.upper()
        assert "## Metrics" in report

    def test_render__blocked_status(self):
        old = pd.DataFrame({"id": [1, 1], "v": [10, 20]})
        new = pd.DataFrame({"id": [1], "v": [10]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        report = render_diff_report(ctx)

        assert "BLOCKED" in report.upper()
        assert "Risks" in report or "risks" in report.lower()

    def test_render__changed_columns_section(self):
        old = pd.DataFrame({"id": [1], "a": [1], "b": ["x"]})
        new = pd.DataFrame({"id": [1], "a": [2], "b": ["y"]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        report = render_diff_report(ctx)

        assert "Changed Columns" in report
        assert "null→value" in report or "null\u2192value" in report or "null" in report.lower()

    def test_render__no_samples_when_disabled(self):
        old = pd.DataFrame({"id": [1], "v": [10]})
        new = pd.DataFrame({"id": [1], "v": [20]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        report = render_diff_report(ctx, show_samples=False)

        assert "Changed Row Evidence" not in report

    def test_render__missing_keys_raises(self):
        with pytest.raises(ValueError, match="missing keys"):
            render_diff_report({"kind": "x"})

    def test_render__output_format_markdown(self):
        old = pd.DataFrame({"id": [1], "v": [10]})
        new = pd.DataFrame({"id": [1], "v": [20]})

        result = diff_tables_by_key(old, new, keys=["id"], engine="pandas", output_format="markdown")

        assert isinstance(result, str)
        assert "# Diff:" in result

    def test_render__returns_string(self):
        old = pd.DataFrame({"id": [1], "v": [10]})
        new = pd.DataFrame({"id": [1], "v": [20]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        report = render_diff_report(ctx)

        assert isinstance(report, str)
        assert len(report) > 0


# =============================================================================
# Section 4: Internal helper tests
# =============================================================================


class TestValidationHelpers:
    """Test internal validation functions."""

    def test_validate_engine_arg__valid(self):
        _validate_engine_arg("auto")
        _validate_engine_arg("pandas")
        _validate_engine_arg("spark")

    def test_validate_engine_arg__invalid(self):
        with pytest.raises(ValueError, match="Unsupported engine"):
            _validate_engine_arg("duckdb")

    def test_validate_sample_limit__valid(self):
        _validate_sample_limit(0)
        _validate_sample_limit(100)

    def test_validate_sample_limit__negative(self):
        with pytest.raises(ValueError, match="non-negative"):
            _validate_sample_limit(-1)

    def test_validate_sample_limit__bool_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            _validate_sample_limit(True)

    def test_validate_column_name_list__valid(self):
        result = _validate_column_name_list("keys", ["a", "b"], allow_empty=False)
        assert result == ["a", "b"]

    def test_validate_column_name_list__empty_not_allowed(self):
        with pytest.raises(ValueError, match="at least one"):
            _validate_column_name_list("keys", [], allow_empty=False)

    def test_validate_column_name_list__empty_allowed(self):
        result = _validate_column_name_list("cols", [], allow_empty=True)
        assert result == []

    def test_validate_column_name_list__non_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            _validate_column_name_list("keys", "single", allow_empty=False)

    def test_validate_column_name_list__duplicates(self):
        with pytest.raises(ValueError, match="duplicate"):
            _validate_column_name_list("keys", ["a", "a"], allow_empty=False)

    def test_validate_column_name_list__non_string(self):
        with pytest.raises(ValueError, match="string column names"):
            _validate_column_name_list("keys", [1, 2], allow_empty=False)

    def test_duplicate_values__none(self):
        assert _duplicate_values(["a", "b", "c"]) == []

    def test_duplicate_values__some(self):
        assert _duplicate_values(["a", "b", "a", "c", "b"]) == ["a", "b"]

    def test_duplicate_values__empty(self):
        assert _duplicate_values([]) == []


class TestJsonSafeValue:
    """Test _json_safe_value conversion."""

    def test_json_safe__none(self):
        assert _json_safe_value(None) is None

    def test_json_safe__string(self):
        assert _json_safe_value("hello") == "hello"

    def test_json_safe__int(self):
        assert _json_safe_value(42) == 42

    def test_json_safe__float(self):
        assert _json_safe_value(3.14) == 3.14

    def test_json_safe__bool(self):
        assert _json_safe_value(True) is True

    def test_json_safe__decimal(self):
        assert _json_safe_value(Decimal("10.5")) == "10.5"

    def test_json_safe__datetime(self):
        result = _json_safe_value(datetime(2026, 1, 15, 10, 30))
        assert result == "2026-01-15T10:30:00"

    def test_json_safe__date(self):
        result = _json_safe_value(date(2026, 1, 15))
        assert result == "2026-01-15"

    def test_json_safe__pandas_nat(self):
        result = _json_safe_value(pd.NaT)
        # NaT has .isoformat() returning 'NaT' string — code path hits datetime before pd.isna
        assert result == "NaT"

    def test_json_safe__numpy_int(self):
        import numpy as np
        result = _json_safe_value(np.int64(42))
        assert result == 42

    def test_json_safe__dict(self):
        result = _json_safe_value({"a": Decimal("1.5")})
        assert result == {"a": "1.5"}

    def test_json_safe__list(self):
        result = _json_safe_value([1, Decimal("2.5"), None])
        assert result == [1, "2.5", None]

    def test_json_safe__unknown_type_to_str(self):
        class Custom:
            def __str__(self):
                return "custom_repr"
        assert _json_safe_value(Custom()) == "custom_repr"


class TestResolveCompareColumns:
    """Test _resolve_compare_columns logic."""

    def test_resolve__auto_selects_common_non_key(self):
        result = _resolve_compare_columns(
            old_columns=["id", "a", "b"],
            new_columns=["id", "a", "c"],
            keys=["id"],
            compare_columns=None,
        )
        assert result == ["a"]  # only common non-key column

    def test_resolve__explicit_columns(self):
        result = _resolve_compare_columns(
            old_columns=["id", "a", "b"],
            new_columns=["id", "a", "b"],
            keys=["id"],
            compare_columns=["a"],
        )
        assert result == ["a"]

    def test_resolve__missing_key_raises(self):
        with pytest.raises(ValueError, match="missing required key"):
            _resolve_compare_columns(
                old_columns=["id", "a"],
                new_columns=["other", "a"],
                keys=["id"],
                compare_columns=None,
            )

    def test_resolve__key_in_compare_raises(self):
        with pytest.raises(ValueError, match="cannot include key"):
            _resolve_compare_columns(
                old_columns=["id", "a"],
                new_columns=["id", "a"],
                keys=["id"],
                compare_columns=["id"],
            )

    def test_resolve__missing_compare_column_raises(self):
        with pytest.raises(ValueError, match="missing required compare"):
            _resolve_compare_columns(
                old_columns=["id", "a"],
                new_columns=["id", "b"],
                keys=["id"],
                compare_columns=["a"],
            )


class TestDuplicateKeyDetection:
    """Test _duplicate_key_summary_pandas."""

    def test_no_duplicates(self):
        df = pd.DataFrame({"id": [1, 2, 3]})
        result = _duplicate_key_summary_pandas(df, ["id"], 10)
        assert result["duplicate_key_count"] == 0
        assert result["duplicate_row_count"] == 0
        assert result["samples"] == []

    def test_with_duplicates(self):
        df = pd.DataFrame({"id": [1, 1, 2, 3, 3, 3]})
        result = _duplicate_key_summary_pandas(df, ["id"], 10)
        assert result["duplicate_key_count"] == 2  # id=1, id=3
        assert result["duplicate_row_count"] == 5  # 2+3

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["id"])
        result = _duplicate_key_summary_pandas(df, ["id"], 10)
        assert result["duplicate_key_count"] == 0


class TestNullKeySummary:
    """Test _null_key_summary_pandas."""

    def test_no_nulls(self):
        df = pd.DataFrame({"id": [1, 2, 3]})
        result = _null_key_summary_pandas(df, ["id"], 10)
        assert result["null_key_row_count"] == 0
        assert result["samples"] == []

    def test_with_null_key(self):
        df = pd.DataFrame({"id": [1, None, 3]})
        result = _null_key_summary_pandas(df, ["id"], 10)
        assert result["null_key_row_count"] == 1
        assert len(result["samples"]) == 1

    def test_composite_key_null(self):
        df = pd.DataFrame({"a": [1, 2, None], "b": ["x", None, "z"]})
        result = _null_key_summary_pandas(df, ["a", "b"], 10)
        assert result["null_key_row_count"] == 2  # rows 1 and 2

    def test_empty_dataframe(self):
        df = pd.DataFrame(columns=["id"])
        result = _null_key_summary_pandas(df, ["id"], 10)
        assert result["null_key_row_count"] == 0


class TestDtypeChanges:
    """Test _dtype_changes_pandas."""

    def test_no_changes(self):
        old = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
        new = pd.DataFrame({"a": [3, 4], "b": ["p", "q"]})
        result = _dtype_changes_pandas(old, new, ["a", "b"], require_observed_values=False)
        assert result == []

    def test_int_to_float(self):
        old = pd.DataFrame({"a": pd.Series([1, 2], dtype="int64")})
        new = pd.DataFrame({"a": pd.Series([1.0, 2.0], dtype="float64")})
        result = _dtype_changes_pandas(old, new, ["a"], require_observed_values=False)
        assert len(result) == 1
        assert result[0]["column"] == "a"
        assert "int" in result[0]["old_dtype"]
        assert "float" in result[0]["new_dtype"]

    def test_require_observed_skips_all_null(self):
        old = pd.DataFrame({"a": pd.Series([None, None], dtype="object")})
        new = pd.DataFrame({"a": pd.Series([1, 2], dtype="int64")})
        result = _dtype_changes_pandas(old, new, ["a"], require_observed_values=True)
        # old has no non-null values, so change is not reported
        assert result == []


class TestColumnChangeMasks:
    """Test _column_change_masks_pandas."""

    def test_basic_change_detection(self):
        joined = pd.DataFrame({
            "id": [1, 2],
            "val__old": [10, 20],
            "val__new": [10, 25],
            "_merge": ["both", "both"],
        })
        common_mask = pd.Series([True, True])
        masks = _column_change_masks_pandas(joined, common_mask, ["val"])
        assert "val" in masks
        assert masks["val"].tolist() == [False, True]

    def test_null_aware_change_detection(self):
        joined = pd.DataFrame({
            "id": [1, 2, 3],
            "val__old": [None, "x", None],
            "val__new": ["a", None, None],
            "_merge": ["both", "both", "both"],
        })
        common_mask = pd.Series([True, True, True])
        masks = _column_change_masks_pandas(joined, common_mask, ["val"])
        # row 0: None→"a" is a change, row 1: "x"→None is a change, row 2: None→None is NOT
        assert masks["val"].tolist() == [True, True, False]


class TestChangedColumnCountsCategorized:
    """Test _changed_column_counts_categorized."""

    def test_categorization_counts(self):
        joined = pd.DataFrame({
            "a__old": [None, "v", "old", None],
            "a__new": ["new", None, "new", None],
        })
        masks = {"a": pd.Series([True, True, True, False])}

        result = _changed_column_counts_categorized(masks, joined)

        assert len(result) == 1
        assert result[0]["column"] == "a"
        assert result[0]["null_to_value"] == 1
        assert result[0]["value_to_null"] == 1
        assert result[0]["value_changed"] == 1
        assert result[0]["changed_key_count"] == 3

    def test_no_changes_returns_empty(self):
        joined = pd.DataFrame({"a__old": [1, 2], "a__new": [1, 2]})
        masks = {"a": pd.Series([False, False])}

        result = _changed_column_counts_categorized(masks, joined)
        assert result == []

    def test_sorted_by_count_descending(self):
        joined = pd.DataFrame({
            "a__old": [1, 2, 3],
            "a__new": [9, 9, 9],
            "b__old": [1, 2, 3],
            "b__new": [1, 9, 3],
        })
        masks = {
            "a": pd.Series([True, True, True]),
            "b": pd.Series([False, True, False]),
        }

        result = _changed_column_counts_categorized(masks, joined)
        assert result[0]["column"] == "a"  # 3 changes
        assert result[1]["column"] == "b"  # 1 change


# =============================================================================
# Section 5: Output contract tests
# =============================================================================


class TestOutputContract:
    """Verify the output dict has all standard keys."""

    REQUIRED_KEYS = {
        "kind", "subject", "engine", "status", "summary",
        "metrics", "columns", "findings", "risks", "samples",
        "suggested_next_actions",
    }

    def test_contract__ok_status(self):
        old = pd.DataFrame({"id": [1], "v": [10]})
        new = pd.DataFrame({"id": [1], "v": [20]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert self.REQUIRED_KEYS <= set(ctx.keys())
        assert ctx["kind"] == "diff_tables_by_key"
        assert ctx["engine"] == "pandas"
        assert ctx["status"] == "ok"

    def test_contract__blocked_status(self):
        old = pd.DataFrame({"id": [1, 1], "v": [10, 20]})
        new = pd.DataFrame({"id": [1], "v": [10]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert self.REQUIRED_KEYS <= set(ctx.keys())
        assert ctx["status"] == "blocked"

    def test_contract__metrics_all_integers(self):
        old = pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]})
        new = pd.DataFrame({"id": [2, 3, 4], "v": [20, 35, 40]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        m = ctx["metrics"]
        for key in ["old_row_count", "new_row_count", "added_key_count",
                    "removed_key_count", "changed_key_count", "unchanged_key_count"]:
            assert isinstance(m[key], int), f"{key} should be int, got {type(m[key])}"

    def test_contract__json_serializable(self):
        old = pd.DataFrame({"id": [1], "ts": [pd.Timestamp("2026-01-01")], "amt": [Decimal("10.5")]})
        new = pd.DataFrame({"id": [1], "ts": [pd.Timestamp("2026-01-02")], "amt": [Decimal("11.0")]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        # Must not raise
        serialized = json.dumps(ctx)
        assert len(serialized) > 0

    def test_contract__samples_keys_present(self):
        old = pd.DataFrame({"id": [1, 2], "v": [10, 20]})
        new = pd.DataFrame({"id": [2, 3], "v": [25, 30]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        expected_sample_keys = {"added_keys", "removed_keys", "added_rows", "removed_rows", "changed_rows"}
        assert expected_sample_keys <= set(ctx["samples"].keys())

    def test_contract__changed_column_counts_structure(self):
        old = pd.DataFrame({"id": [1], "a": [1], "b": ["x"]})
        new = pd.DataFrame({"id": [1], "a": [2], "b": ["y"]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")

        for cc in ctx["metrics"]["changed_column_counts"]:
            assert "column" in cc
            assert "changed_key_count" in cc
            assert "null_to_value" in cc
            assert "value_to_null" in cc
            assert "value_changed" in cc
            assert isinstance(cc["changed_key_count"], int)


# =============================================================================
# Section 6: Edge cases
# =============================================================================


class TestEdgeCases:
    """Edge cases for diff_tables_by_key."""

    def test_both_empty__zero_counts(self):
        old = pd.DataFrame(columns=["id", "v"])
        new = pd.DataFrame(columns=["id", "v"])
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert ctx["metrics"]["old_row_count"] == 0
        assert ctx["metrics"]["new_row_count"] == 0
        assert ctx["metrics"]["added_key_count"] == 0
        assert ctx["metrics"]["removed_key_count"] == 0
        assert ctx["metrics"]["changed_key_count"] == 0

    def test_single_row__no_change(self):
        old = pd.DataFrame({"id": [1], "v": [10]})
        new = pd.DataFrame({"id": [1], "v": [10]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert ctx["metrics"]["unchanged_key_count"] == 1
        assert ctx["metrics"]["has_changes"] is False

    def test_single_row__with_change(self):
        old = pd.DataFrame({"id": [1], "v": [10]})
        new = pd.DataFrame({"id": [1], "v": [99]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert ctx["metrics"]["changed_key_count"] == 1
        assert ctx["metrics"]["has_changes"] is True

    def test_key_only_dataframes(self):
        old = pd.DataFrame({"id": [1, 2, 3]})
        new = pd.DataFrame({"id": [2, 3, 4]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert ctx["metrics"]["compared_column_count"] == 0
        assert ctx["metrics"]["added_key_count"] == 1
        assert ctx["metrics"]["removed_key_count"] == 1

    def test_no_common_keys(self):
        old = pd.DataFrame({"id": [1, 2], "v": [10, 20]})
        new = pd.DataFrame({"id": [3, 4], "v": [30, 40]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert ctx["metrics"]["common_key_count"] == 0
        assert ctx["metrics"]["added_key_count"] == 2
        assert ctx["metrics"]["removed_key_count"] == 2
        assert "no_common_keys" in {r["type"] for r in ctx["risks"]}

    def test_changed_cell_count(self):
        old = pd.DataFrame({"id": [1, 2], "a": [1, 2], "b": ["x", "y"]})
        new = pd.DataFrame({"id": [1, 2], "a": [9, 2], "b": ["x", "z"]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        # id=1 changes a, id=2 changes b = 2 cells
        assert ctx["metrics"]["changed_cell_count"] == 2

    def test_merge_expr_present_on_ok(self):
        old = pd.DataFrame({"id": [1], "v": [10]})
        new = pd.DataFrame({"id": [1], "v": [20]})
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        assert "merge_expr" in ctx
        assert "MERGE INTO" in ctx["merge_expr"]


# =============================================================================
# Section 7: Misc internal helpers
# =============================================================================


class TestMiscHelpers:
    """Test remaining internal helpers."""

    def test_empty_diff_samples(self):
        result = _empty_diff_samples()
        assert set(result.keys()) == {
            "old_duplicate_keys", "new_duplicate_keys",
            "old_null_key_rows", "new_null_key_rows",
            "added_keys", "removed_keys",
            "added_rows", "removed_rows", "changed_rows",
        }
        assert all(v == [] for v in result.values())

    def test_records_to_json_safe(self):
        records = [{"id": 1, "val": Decimal("3.14")}]
        result = _records_to_json_safe(records)
        assert result == [{"id": 1, "val": "3.14"}]

    def test_validate_no_duplicate_columns__ok(self):
        _validate_no_duplicate_dataframe_columns("df", ["a", "b", "c"])

    def test_validate_no_duplicate_columns__raises(self):
        with pytest.raises(ValueError, match="duplicate column"):
            _validate_no_duplicate_dataframe_columns("df", ["a", "b", "a"])

    def test_normalize_dtype_changes(self):
        changes = [{"column": "a", "old_dtype": "int64", "new_dtype": "float64"}]
        result = _normalize_dtype_changes(changes, is_key=True, is_compared=False)
        assert result[0]["column"] == "a"
        assert result[0]["is_key"] is True
        assert result[0]["is_compared"] is False

    def test_columns_context(self):
        result = _columns_context(
            keys=["id"],
            compare_cols=["a", "b"],
            old_only_columns=["x"],
            new_only_columns=["y"],
            common_columns=["id", "a", "b"],
            type_changed_columns=[],
        )
        assert result["keys"] == ["id"]
        assert result["compared"] == ["a", "b"]
        assert result["old_only_columns"] == ["x"]
        assert result["new_only_columns"] == ["y"]

    def test_false_mask_like(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        mask = _false_mask_like(df)
        assert mask.tolist() == [False, False, False]

    def test_combine_boolean_masks(self):
        idx = pd.RangeIndex(3)
        masks = {
            "a": pd.Series([True, False, False], index=idx),
            "b": pd.Series([False, True, False], index=idx),
        }
        result = _combine_boolean_masks(masks, index=idx, default=False)
        assert result.tolist() == [True, True, False]

    def test_sample_key_records__capped(self):
        df = pd.DataFrame({"id": range(10)})
        result = _sample_key_records_pandas(df, ["id"], 3)
        assert len(result) == 3

    def test_sample_key_records__empty(self):
        df = pd.DataFrame(columns=["id"])
        result = _sample_key_records_pandas(df, ["id"], 10)
        assert result == []

    def test_sample_key_records__zero_limit(self):
        df = pd.DataFrame({"id": [1, 2, 3]})
        result = _sample_key_records_pandas(df, ["id"], 0)
        assert result == []

    def test_blocked_summary(self):
        risks = [{"type": "duplicate_keys"}, {"type": "null_keys"}]
        result = _blocked_summary("my_table", risks)
        assert "my_table" in result
        assert "duplicate_keys" in result

    def test_ok_summary(self):
        result = _ok_summary("table", ["id"], 10, 12, 2, 0, 1)
        assert "table" in result


# =============================================================================
# Section 8: Performance
# =============================================================================


class TestPerformance:
    """Verify acceptable performance bounds."""

    def test_performance__10k_rows_under_5s(self):
        import time
        n = 10_000
        old = pd.DataFrame({"id": range(n), "val": range(n)})
        new = pd.DataFrame({"id": range(n), "val": [x + 1 if x % 10 == 0 else x for x in range(n)]})

        start = time.time()
        ctx = diff_tables_by_key(old, new, keys=["id"], engine="pandas")
        elapsed = time.time() - start

        assert elapsed < 5.0
        assert ctx["metrics"]["changed_key_count"] == 1000  # every 10th row
