"""Tests for odibi_anchor.tables.compare_ops — cross_check and detect_deletes."""

import pytest
import pandas as pd

from odibi_anchor.tables.compare_ops import (
    cross_check,
    detect_deletes,
    ALL_CHECKS,
    _check_row_count,
    _check_schema,
    _check_columns,
    _detect_deletes_pandas,
)


# ─── cross_check ────────────────────────────────────────────────────────────


class TestCrossCheckRowCount:
    """Tests for the row_count check."""

    def test_row_count__matching_counts(self):
        source = pd.DataFrame({"a": [1, 2, 3]})
        target = pd.DataFrame({"a": [4, 5, 6]})
        result = cross_check(source, target, checks=["row_count"])
        assert result["row_count"]["passed"] is True
        assert "source=3" in result["row_count"]["details"]

    def test_row_count__mismatched_counts(self):
        source = pd.DataFrame({"a": [1, 2, 3]})
        target = pd.DataFrame({"a": [4, 5]})
        result = cross_check(source, target, checks=["row_count"])
        assert result["row_count"]["passed"] is False
        assert "diff=" in result["row_count"]["details"]

    def test_row_count__empty_frames(self):
        source = pd.DataFrame({"a": []})
        target = pd.DataFrame({"a": []})
        result = cross_check(source, target, checks=["row_count"])
        assert result["row_count"]["passed"] is True


class TestCrossCheckSchema:
    """Tests for the schema check."""

    def test_schema__identical_schemas(self):
        source = pd.DataFrame({"a": [1], "b": ["x"]})
        target = pd.DataFrame({"a": [2], "b": ["y"]})
        result = cross_check(source, target, checks=["schema"])
        assert result["schema"]["passed"] is True
        assert "match" in result["schema"]["details"].lower()

    def test_schema__missing_column_in_target(self):
        source = pd.DataFrame({"a": [1], "b": ["x"]})
        target = pd.DataFrame({"a": [2]})
        result = cross_check(source, target, checks=["schema"])
        assert result["schema"]["passed"] is False
        assert "missing_in_target" in result["schema"]["details"]

    def test_schema__extra_column_in_target(self):
        source = pd.DataFrame({"a": [1]})
        target = pd.DataFrame({"a": [2], "b": ["x"]})
        result = cross_check(source, target, checks=["schema"])
        assert result["schema"]["passed"] is False
        assert "extra_in_target" in result["schema"]["details"]

    def test_schema__type_mismatch(self):
        source = pd.DataFrame({"a": [1]})
        target = pd.DataFrame({"a": ["1"]})
        result = cross_check(source, target, checks=["schema"])
        assert result["schema"]["passed"] is False
        assert "type_mismatches" in result["schema"]["details"]


class TestCrossCheckColumns:
    """Tests for the columns check."""

    def test_columns__matching_names(self):
        source = pd.DataFrame({"x": [1], "y": [2]})
        target = pd.DataFrame({"x": [3], "y": [4]})
        result = cross_check(source, target, checks=["columns"])
        assert result["columns"]["passed"] is True

    def test_columns__missing_column(self):
        source = pd.DataFrame({"x": [1], "y": [2]})
        target = pd.DataFrame({"x": [3]})
        result = cross_check(source, target, checks=["columns"])
        assert result["columns"]["passed"] is False
        assert "missing_in_target" in result["columns"]["details"]

    def test_columns__extra_column(self):
        source = pd.DataFrame({"x": [1]})
        target = pd.DataFrame({"x": [3], "z": [4]})
        result = cross_check(source, target, checks=["columns"])
        assert result["columns"]["passed"] is False
        assert "extra_in_target" in result["columns"]["details"]


class TestCrossCheckGeneral:
    """Tests for general cross_check behavior."""

    def test_default_runs_all_checks(self):
        source = pd.DataFrame({"a": [1, 2]})
        target = pd.DataFrame({"a": [3, 4]})
        result = cross_check(source, target)
        assert set(result.keys()) == set(ALL_CHECKS)

    def test_unknown_check_skipped(self):
        source = pd.DataFrame({"a": [1]})
        target = pd.DataFrame({"a": [2]})
        result = cross_check(source, target, checks=["row_count", "nonexistent_check"])
        assert "row_count" in result
        assert "nonexistent_check" not in result

    def test_all_pass__returns_all_passed(self):
        source = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
        target = pd.DataFrame({"a": [3, 4], "b": ["z", "w"]})
        result = cross_check(source, target)
        assert all(r["passed"] for r in result.values())

    def test_engine_auto_detect_pandas(self):
        """Non-Spark DataFrames should auto-use pandas engine."""
        source = pd.DataFrame({"a": [1]})
        target = pd.DataFrame({"a": [2]})
        # Should not raise even when engine="spark" specified — auto-detects pandas
        result = cross_check(source, target, engine="spark")
        assert "row_count" in result


# ─── detect_deletes ──────────────────────────────────────────────────────────


class TestDetectDeletesPandas:
    """Tests for detect_deletes with pandas engine."""

    def test_no_deletes__all_keys_present(self):
        current = pd.DataFrame({"id": [1, 2, 3], "val": ["a", "b", "c"]})
        previous = pd.DataFrame({"id": [1, 2, 3], "val": ["x", "y", "z"]})
        result = detect_deletes(None, current, previous, keys=["id"])
        assert len(result) == 3
        assert result["_is_deleted"].all() == False

    def test_one_delete__key_missing_in_current(self):
        current = pd.DataFrame({"id": [1, 2], "val": ["a", "b"]})
        previous = pd.DataFrame({"id": [1, 2, 3], "val": ["x", "y", "z"]})
        result = detect_deletes(None, current, previous, keys=["id"], threshold=0.5)
        assert len(result) == 3
        deleted_rows = result[result["_is_deleted"] == True]
        assert len(deleted_rows) == 1
        assert deleted_rows.iloc[0]["id"] == 3

    def test_all_deletes__empty_current(self):
        current = pd.DataFrame({"id": pd.Series([], dtype=int), "val": pd.Series([], dtype=str)})
        previous = pd.DataFrame({"id": [1, 2], "val": ["x", "y"]})
        # threshold=1.0 allows 100% deletes
        result = detect_deletes(None, current, previous, keys=["id"], threshold=1.0)
        assert len(result) == 2
        assert result["_is_deleted"].all()

    def test_threshold_exceeded__raises_value_error(self):
        current = pd.DataFrame({"id": [1], "val": ["a"]})
        previous = pd.DataFrame({"id": [1, 2, 3, 4, 5], "val": list("xyzwv")})
        # 4/5 = 80% deletes, threshold 10%
        with pytest.raises(ValueError, match="exceeds threshold"):
            detect_deletes(None, current, previous, keys=["id"], threshold=0.1)

    def test_threshold_boundary__exact_threshold_does_not_raise(self):
        current = pd.DataFrame({"id": [1, 2, 3, 4, 5, 6, 7, 8, 9], "val": list("abcdefghi")})
        previous = pd.DataFrame({"id": list(range(1, 11)), "val": list("abcdefghij")})
        # 1/10 = 10%, threshold=0.1 → at boundary, should not raise (> not >=)
        result = detect_deletes(None, current, previous, keys=["id"], threshold=0.1)
        deleted = result[result["_is_deleted"] == True]
        assert len(deleted) == 1

    def test_custom_soft_delete_column(self):
        current = pd.DataFrame({"id": [1], "val": ["a"]})
        previous = pd.DataFrame({"id": [1, 2], "val": ["x", "y"]})
        result = detect_deletes(None, current, previous, keys=["id"], threshold=0.5, soft_delete_col="removed")
        assert "removed" in result.columns
        assert "_is_deleted" not in result.columns

    def test_composite_keys(self):
        current = pd.DataFrame({"k1": [1, 1], "k2": ["a", "b"], "val": [10, 20]})
        previous = pd.DataFrame({"k1": [1, 1, 1], "k2": ["a", "b", "c"], "val": [10, 20, 30]})
        result = detect_deletes(None, current, previous, keys=["k1", "k2"], threshold=0.5)
        deleted = result[result["_is_deleted"] == True]
        assert len(deleted) == 1
        assert deleted.iloc[0]["k2"] == "c"

    def test_empty_previous__no_deletes(self):
        current = pd.DataFrame({"id": [1, 2, 3], "val": ["a", "b", "c"]})
        previous = pd.DataFrame({"id": pd.Series([], dtype=int), "val": pd.Series([], dtype=str)})
        result = detect_deletes(None, current, previous, keys=["id"])
        assert len(result) == 3
        assert not result["_is_deleted"].any()

    def test_result_dtypes__soft_delete_is_bool(self):
        current = pd.DataFrame({"id": [1, 2], "val": ["a", "b"]})
        previous = pd.DataFrame({"id": [1, 2, 3], "val": ["x", "y", "z"]})
        result = detect_deletes(None, current, previous, keys=["id"], threshold=0.5)
        assert result["_is_deleted"].dtype == bool


# ─── Internal helpers ─────────────────────────────────────────────────────────


class TestInternalHelpers:
    """Direct tests on internal functions."""

    def test_check_row_count_pandas__equal(self):
        s = pd.DataFrame({"a": [1, 2]})
        t = pd.DataFrame({"a": [3, 4]})
        r = _check_row_count(s, t, "pandas")
        assert r["passed"] is True

    def test_check_schema_pandas__type_diff(self):
        s = pd.DataFrame({"x": [1]})
        t = pd.DataFrame({"x": [1.0]})
        r = _check_schema(s, t, "pandas")
        assert r["passed"] is False

    def test_check_columns__exact_set_comparison(self):
        s = pd.DataFrame({"a": [1], "b": [2], "c": [3]})
        t = pd.DataFrame({"c": [3], "b": [2], "a": [1]})
        r = _check_columns(s, t)
        assert r["passed"] is True  # order doesn't matter, set comparison
