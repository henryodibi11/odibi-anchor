"""Tests for NULL-aware change categorization in diff_ops."""

from __future__ import annotations

import pandas as pd
import pytest

from odibi_anchor.tables.diff_ops import diff_tables_by_key


class TestChangeCategorization:
    """Test NULL-aware sub-counts in changed_column_counts."""

    def test_null_to_value(self):
        """Old is NULL, new has a value."""
        old = pd.DataFrame({"id": [1, 2, 3], "val": [None, None, "a"]})
        new = pd.DataFrame({"id": [1, 2, 3], "val": ["x", "y", "a"]})
        ctx = diff_tables_by_key(old, new, keys=["id"])
        cc = ctx["metrics"]["changed_column_counts"]
        assert len(cc) == 1
        assert cc[0]["column"] == "val"
        assert cc[0]["null_to_value"] == 2
        assert cc[0]["value_to_null"] == 0
        assert cc[0]["value_changed"] == 0
        assert cc[0]["changed_key_count"] == 2

    def test_value_to_null(self):
        """Old has a value, new is NULL."""
        old = pd.DataFrame({"id": [1, 2, 3], "val": ["a", "b", "c"]})
        new = pd.DataFrame({"id": [1, 2, 3], "val": [None, None, "c"]})
        ctx = diff_tables_by_key(old, new, keys=["id"])
        cc = ctx["metrics"]["changed_column_counts"]
        assert len(cc) == 1
        assert cc[0]["null_to_value"] == 0
        assert cc[0]["value_to_null"] == 2
        assert cc[0]["value_changed"] == 0

    def test_value_changed(self):
        """Both have non-NULL values that differ."""
        old = pd.DataFrame({"id": [1, 2, 3], "val": ["a", "b", "c"]})
        new = pd.DataFrame({"id": [1, 2, 3], "val": ["x", "y", "c"]})
        ctx = diff_tables_by_key(old, new, keys=["id"])
        cc = ctx["metrics"]["changed_column_counts"]
        assert len(cc) == 1
        assert cc[0]["null_to_value"] == 0
        assert cc[0]["value_to_null"] == 0
        assert cc[0]["value_changed"] == 2

    def test_mixed_categorization(self):
        """Mix of all three categories."""
        old = pd.DataFrame({"id": [1, 2, 3, 4], "val": [None, "b", "c", "d"]})
        new = pd.DataFrame({"id": [1, 2, 3, 4], "val": ["x", None, "z", "d"]})
        ctx = diff_tables_by_key(old, new, keys=["id"])
        cc = ctx["metrics"]["changed_column_counts"]
        assert len(cc) == 1
        assert cc[0]["null_to_value"] == 1
        assert cc[0]["value_to_null"] == 1
        assert cc[0]["value_changed"] == 1
        assert cc[0]["changed_key_count"] == 3

    def test_invariant_sum_equals_total(self):
        """null_to_value + value_to_null + value_changed == changed_key_count."""
        old = pd.DataFrame({
            "id": range(10),
            "a": [None, "x", "y", None, "z", "w", None, "v", "u", "t"],
            "b": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"],
        })
        new = pd.DataFrame({
            "id": range(10),
            "a": ["A", None, "changed", "B", None, "w", "C", "v", None, "t"],
            "b": ["1", "2", "X", "4", "Y", "6", None, "8", "9", "10"],
        })
        ctx = diff_tables_by_key(old, new, keys=["id"])
        for cc in ctx["metrics"]["changed_column_counts"]:
            assert cc["null_to_value"] + cc["value_to_null"] + cc["value_changed"] == cc["changed_key_count"], \
                f"Invariant violated for column {cc['column']}"

    def test_no_changes_no_categories(self):
        """When no changes, changed_column_counts is empty."""
        old = pd.DataFrame({"id": [1, 2], "val": ["a", "b"]})
        new = pd.DataFrame({"id": [1, 2], "val": ["a", "b"]})
        ctx = diff_tables_by_key(old, new, keys=["id"])
        assert ctx["metrics"]["changed_column_counts"] == []

    def test_markdown_shows_subcounts(self):
        """Rendered markdown contains the sub-count columns."""
        old = pd.DataFrame({"id": [1], "val": [None]})
        new = pd.DataFrame({"id": [1], "val": ["x"]})
        ctx = diff_tables_by_key(old, new, keys=["id"], output_format="markdown")
        assert "null→value" in ctx
        assert "value→null" in ctx
        assert "value≠value" in ctx
