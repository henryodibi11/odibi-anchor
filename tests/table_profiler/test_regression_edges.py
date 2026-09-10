"""Regression tests for audit-identified edge cases."""
import sys
sys.dont_write_bytecode = True

import json
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, "/Workspace/Users/user@example.com/tools/table_profiler")
from tools.table_profiler_tool.lib.profiler import profile_table
from tools.table_profiler_tool.lib.contract import serialize_profile
from tools.table_profiler_tool.lib.case_file import case_file
from tools.table_profiler_tool.lib.models import TableProfile, ColumnProfile, JoinProfile


class TestSerializationSafety:
    """Full-pipeline serialization safety — no JSON exceptions allowed."""

    def test_basic_profile_is_json_serializable(self):
        """Profile serialization must produce valid JSON on a simple table."""
        df = pd.DataFrame({
            "id": range(100),
            "name": [f"user_{i}" for i in range(100)],
            "amount": np.random.uniform(0, 1000, 100),
            "created": pd.date_range("2024-01-01", periods=100),
        })
        profile = profile_table(df, "regression_test")
        serialized = serialize_profile(profile)
        # Must be fully JSON-serializable
        json_str = json.dumps(serialized)
        assert len(json_str) > 100  # sanity check

    def test_join_profile_serialization(self):
        """serialize_profile must not crash when joins are populated."""
        profile = TableProfile(
            subject="test_joins",
            row_count=1000,
            column_count=1,
            columns=[
                ColumnProfile(name="user_id", position=0, spark_type="string"),
            ],
            joins=[
                JoinProfile(
                    source_column="user_id",
                    target_table="dim_users",
                    target_column="id",
                    cardinality="many-to-one",
                    overlap_pct=0.95,
                    orphan_count=50,
                    orphan_pct=0.05,
                    format_compatible=True,
                    safe_join_type="LEFT",
                ),
            ],
        )
        result = serialize_profile(profile)
        assert "joins" in result["samples"]
        assert result["samples"]["joins"][0]["safe_join_type"] == "LEFT"
        json.dumps(result)  # Must not raise

    def test_quick_level_profile_serializable(self):
        """Quick-level profiles (grain=None) must serialize without error."""
        df = pd.DataFrame({"x": [1, 2, 3]})
        profile = profile_table(df, "quick_test", level="quick")
        assert profile.grain is None
        result = serialize_profile(profile)
        json.dumps(result)  # Must not raise


class TestCaseFileEdgeModes:
    """Test under-covered case_file filter modes."""

    def test_pattern_filter(self):
        df = pd.DataFrame({"code": ["ABC-123", "DEF-456", "GHI-789", "XX-1", "ABC-999"]})
        result = case_file(df, column="code", filter="pattern:AAA-999")
        # ABC-123, DEF-456, GHI-789, ABC-999 all match AAA-999 pattern
        assert result["metrics"]["matched_rows"] >= 3

    def test_format_issues_filter(self):
        df = pd.DataFrame({"code": ["ABC", "ABC", "ABC", "abc", "123"]})
        result = case_file(df, column="code", filter="format_issues")
        # "abc" and "123" don't match dominant pattern "AAA"
        assert result["metrics"]["matched_rows"] >= 1

    def test_no_filter_column_only_with_nulls(self):
        df = pd.DataFrame({"x": [1, None, 3, None, 5]})
        result = case_file(df, column="x")
        assert result["metrics"]["filter_applied"] == "quality_issues"
        assert result["metrics"]["matched_rows"] == 2

    def test_no_filter_column_only_no_nulls(self):
        df = pd.DataFrame({"x": [1, 2, 3]})
        result = case_file(df, column="x")
        assert result["metrics"]["filter_applied"] == "all_rows"

    def test_multi_key_row_ids(self):
        df = pd.DataFrame({
            "a": [1, 1, 2, 2],
            "b": ["x", "y", "x", "y"],
            "val": [10, 20, 30, 40],
        })
        result = case_file(df, row_ids=[(1, "y"), (2, "x")], key_columns=["a", "b"])
        assert result["metrics"]["matched_rows"] == 2
