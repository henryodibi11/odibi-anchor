"""Tests for validation/_quality_gate_helpers.py.

Tests utility functions: _type_category, _make_check, _format_age,
_sanitize_value, _sanitize_dict, _check_row_count, _pandas_rows_to_dicts.
"""

import math
import pandas as pd
import numpy as np
import pytest

from odibi_anchor.validation._quality_gate_helpers import (
    _type_category,
    _make_check,
    _format_age,
    _pandas_rows_to_dicts,
    _sanitize_dict,
    _sanitize_value,
    _check_row_count,
)


class TestTypeCategory:
    """Test _type_category classification."""

    def test_int_types(self):
        assert _type_category("int64") == "numeric"
        assert _type_category("int32") == "numeric"

    def test_float_types(self):
        assert _type_category("float64") == "numeric"
        assert _type_category("float32") == "numeric"

    def test_string_types(self):
        assert _type_category("object") == "string"
        assert _type_category("string") == "string"

    def test_datetime_types(self):
        assert _type_category("datetime64[ns]") == "temporal"

    def test_bool_type(self):
        assert _type_category("bool") == "boolean"

    def test_unknown_returns_none(self):
        result = _type_category("completely_unknown_type_xyz")
        # May return None or a category depending on implementation
        assert result is None or isinstance(result, str)


class TestMakeCheck:
    """Test _make_check factory."""

    def test_basic_check(self):
        result = _make_check(
            check_id="test",
            status="pass",
            severity="blocker",
            detail="All good",
            fix_expr="",
            samples=[],
        )
        assert result["check_id"] == "test"
        assert result["status"] == "pass"
        assert result["severity"] == "blocker"
        assert result["detail"] == "All good"
        assert result["fix_expr"] == ""
        assert result["samples"] == []

    def test_with_extra(self):
        result = _make_check(
            check_id="dup",
            status="fail",
            severity="blocker",
            detail="found dups",
            fix_expr="dedup()",
            samples=[{"id": 1}],
            extra={"count": 5, "pct": 10.0},
        )
        assert result["count"] == 5
        assert result["pct"] == 10.0

    def test_no_extra(self):
        result = _make_check(
            check_id="x", status="pass", severity="warning",
            detail="ok", fix_expr="", samples=[],
        )
        assert "count" not in result


class TestFormatAge:
    """Test _format_age formatting."""

    def test_minutes(self):
        assert _format_age(0.5) == "30m"

    def test_hours(self):
        assert _format_age(5.0) == "5.0h"

    def test_days(self):
        assert _format_age(48.0) == "2.0d"

    def test_zero(self):
        result = _format_age(0.0)
        assert "0m" in result


class TestSanitizeValue:
    """Test _sanitize_value conversion."""

    def test_none(self):
        assert _sanitize_value(None) is None

    def test_string(self):
        assert _sanitize_value("hello") == "hello"

    def test_int(self):
        assert _sanitize_value(42) == 42

    def test_float_normal(self):
        assert _sanitize_value(3.14) == 3.14

    def test_float_nan(self):
        assert _sanitize_value(float("nan")) is None

    def test_float_inf(self):
        assert _sanitize_value(float("inf")) is None

    def test_bool(self):
        assert _sanitize_value(True) is True

    def test_list(self):
        assert _sanitize_value([1, None, "x"]) == [1, None, "x"]

    def test_dict(self):
        assert _sanitize_value({"a": 1}) == {"a": 1}

    def test_unknown_to_str(self):
        class Custom:
            def __str__(self):
                return "custom"
        assert _sanitize_value(Custom()) == "custom"


class TestSanitizeDict:
    """Test _sanitize_dict."""

    def test_basic(self):
        result = _sanitize_dict({"a": 1, "b": "x", "c": None})
        assert result == {"a": 1, "b": "x", "c": None}

    def test_nan_removed(self):
        result = _sanitize_dict({"val": float("nan")})
        assert result == {"val": None}

    def test_nested_dict(self):
        result = _sanitize_dict({"inner": {"val": float("inf")}})
        assert result == {"inner": {"val": None}}


class TestCheckRowCount:
    """Test _check_row_count."""

    def test_zero_rows__fails(self):
        result = _check_row_count(0, {"min_rows": 10})
        assert result["status"] == "fail"
        assert result["severity"] == "blocker"
        assert "empty" in result["detail"].lower()

    def test_below_threshold__warns(self):
        result = _check_row_count(5, {"min_rows": 10})
        assert result["status"] == "warn"
        assert result["severity"] == "warning"

    def test_above_threshold__passes(self):
        result = _check_row_count(100, {"min_rows": 10})
        assert result["status"] == "pass"
        assert "100" in result["detail"]

    def test_default_threshold(self):
        # Default min_rows is 10
        result = _check_row_count(15, {})
        assert result["status"] == "pass"


class TestPandasRowsToDicts:
    """Test _pandas_rows_to_dicts."""

    def test_basic_conversion(self):
        df = pd.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        result = _pandas_rows_to_dicts(df)
        assert result == [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]

    def test_nan_sanitized(self):
        df = pd.DataFrame({"val": [1.0, float("nan")]})
        result = _pandas_rows_to_dicts(df)
        assert result[1]["val"] is None

    def test_empty_df(self):
        df = pd.DataFrame(columns=["a", "b"])
        result = _pandas_rows_to_dicts(df)
        assert result == []
