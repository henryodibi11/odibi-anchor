"""Tests for delta_diff tool.

Uses mocked SparkSession to test version resolution, history enrichment,
and output contract without requiring actual Delta tables.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the tool module is importable
_TOOL_DIR = Path(__file__).resolve().parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

# Ensure odibi_anchor src is importable
_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from delta_diff_impl import (
    _get_history_entry,
    _resolve_version,
    delta_diff_context,
    render_delta_diff_report,
)


# ============================================================================
# Fixtures
# ============================================================================


def _make_history(versions: int = 5) -> list[dict]:
    """Generate fake Delta history entries (newest first)."""
    entries = []
    for v in range(versions - 1, -1, -1):
        entries.append({
            "version": v,
            "timestamp": datetime(2026, 6, 1, 10, v * 10),
            "operation": "MERGE" if v > 0 else "CREATE TABLE",
            "userName": f"user{v}@company.com",
            "operationMetrics": {"numTargetRowsUpdated": str(v * 100)},
        })
    # Newest first
    entries.sort(key=lambda x: x["version"], reverse=True)
    return entries


def _make_mock_spark(old_data: list[dict], new_data: list[dict], history_entries: list[dict]):
    """Create a mock SparkSession that returns controlled DataFrames and history."""
    import pandas as pd
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.appName("test_delta_diff").getOrCreate()

    old_df = spark.createDataFrame(pd.DataFrame(old_data))
    new_df = spark.createDataFrame(pd.DataFrame(new_data))

    # Build history DataFrame
    hist_df = spark.createDataFrame(pd.DataFrame(history_entries))

    return spark, old_df, new_df, hist_df


# ============================================================================
# Unit tests: _resolve_version
# ============================================================================


class TestResolveVersion:
    """Tests for version resolution logic."""

    def test_none_with_versions_ago(self):
        history = _make_history(5)
        result = _resolve_version(None, history, current_version=4, versions_ago=2)
        assert result == 2

    def test_none_defaults_to_current(self):
        history = _make_history(5)
        result = _resolve_version(None, history, current_version=4, versions_ago=None)
        assert result == 4

    def test_int_passthrough(self):
        history = _make_history(5)
        result = _resolve_version(3, history, current_version=4)
        assert result == 3

    def test_timestamp_string_iso(self):
        history = _make_history(5)
        # Version 3 has timestamp 2026-06-01 10:30
        result = _resolve_version("2026-06-01T10:35:00", history, current_version=4)
        assert result == 3  # Latest version at or before 10:35

    def test_timestamp_string_date_only(self):
        history = _make_history(5)
        # All versions are on 2026-06-01, date-only parses to midnight
        # which is before all entries
        with pytest.raises(ValueError, match="No version found"):
            _resolve_version("2026-05-31", history, current_version=4)

    def test_versions_ago_exceeds_history(self):
        history = _make_history(3)
        with pytest.raises(ValueError, match="exceeds available history"):
            _resolve_version(None, history, current_version=2, versions_ago=5)

    def test_invalid_timestamp_raises(self):
        history = _make_history(5)
        with pytest.raises(ValueError, match="Cannot parse timestamp"):
            _resolve_version("not-a-date", history, current_version=4)

    def test_invalid_type_raises(self):
        history = _make_history(5)
        with pytest.raises(TypeError):
            _resolve_version([1, 2], history, current_version=4)


# ============================================================================
# Unit tests: _get_history_entry
# ============================================================================


class TestGetHistoryEntry:
    """Tests for history entry lookup."""

    def test_found(self):
        history = _make_history(5)
        entry = _get_history_entry(history, 3)
        assert entry is not None
        assert entry["version"] == 3
        assert entry["operation"] == "MERGE"

    def test_not_found(self):
        history = _make_history(5)
        entry = _get_history_entry(history, 99)
        assert entry is None


# ============================================================================
# Integration tests: delta_diff_context (with real Spark, mocked Delta)
# ============================================================================


@pytest.fixture(scope="module")
def spark_session():
    """Shared SparkSession for integration tests.

    On Databricks notebook: uses the active session.
    In subprocess (pytest CLI): skips if Spark Connect fails.
    Locally: uses default getOrCreate().
    """
    try:
        from pyspark.sql import SparkSession

        # Try active session first (works when run in notebook context)
        existing = SparkSession.getActiveSession()
        if existing is not None:
            yield existing
            return

        # Try getOrCreate (works locally, may fail in Databricks subprocess)
        try:
            spark = (
                SparkSession.builder
                .appName("test_delta_diff")
                .config("spark.sql.shuffle.partitions", "2")
                .getOrCreate()
            )
            yield spark
        except Exception as exc:
            pytest.skip(f"Cannot create SparkSession in this environment: {exc}")
    except ImportError:
        pytest.skip("PySpark not available")


class TestDeltaDiffContext:
    """Integration tests for delta_diff_context with mocked version reading."""

    def test_basic_diff_with_adds_removes_changes(self, spark_session):
        """Test basic diff detecting added, removed, and changed rows."""
        import pandas as pd

        old_data = [
            {"id": 1, "name": "Alice", "status": "active"},
            {"id": 2, "name": "Bob", "status": "active"},
            {"id": 3, "name": "Charlie", "status": "active"},
        ]
        new_data = [
            {"id": 1, "name": "Alice", "status": "active"},    # unchanged
            {"id": 2, "name": "Bob", "status": "inactive"},    # changed
            {"id": 4, "name": "Diana", "status": "active"},    # added
        ]
        # id=3 removed, id=4 added, id=2 changed

        old_df = spark_session.createDataFrame(pd.DataFrame(old_data))
        new_df = spark_session.createDataFrame(pd.DataFrame(new_data))

        history = _make_history(5)

        with (
            patch("delta_diff_impl._get_spark_session", return_value=spark_session),
            patch("delta_diff_impl._get_delta_history", return_value=history),
            patch("delta_diff_impl._read_delta_version") as mock_read,
        ):
            mock_read.side_effect = lambda spark, table, version: (
                old_df if version == 3 else new_df
            )

            result = delta_diff_context(
                table="catalog.schema.test_table",
                keys=["id"],
                old_version=3,
                new_version=4,
                output_format="dict",
            )

        # Verify contract structure
        assert result["kind"] == "delta_diff"
        assert result["subject"] == "catalog.schema.test_table"
        assert isinstance(result["summary"], str)
        assert isinstance(result["metrics"], dict)
        assert isinstance(result["findings"], list)
        assert isinstance(result["risks"], list)
        assert isinstance(result["samples"], dict)
        assert isinstance(result["suggested_next_actions"], list)

        # Verify metrics
        m = result["metrics"]
        assert m["added_count"] == 1
        assert m["removed_count"] == 1
        assert m["changed_count"] == 1
        assert m["unchanged_count"] == 1
        assert m["old_version"] == 3
        assert m["new_version"] == 4
        assert "status" in m["changed_column_counts"]
        assert m["changed_column_counts"]["status"]["value_changed"] == 1

    def test_versions_ago_default(self, spark_session):
        """Test that versions_ago=1 resolves correctly."""
        import pandas as pd

        data = [{"id": 1, "val": "x"}]
        df = spark_session.createDataFrame(pd.DataFrame(data))
        history = _make_history(5)

        with (
            patch("delta_diff_impl._get_spark_session", return_value=spark_session),
            patch("delta_diff_impl._get_delta_history", return_value=history),
            patch("delta_diff_impl._read_delta_version", return_value=df),
        ):
            result = delta_diff_context(
                table="catalog.schema.t",
                keys=["id"],
                output_format="dict",
            )

        # Default: versions_ago=1, so old=3, new=4
        assert result["metrics"]["old_version"] == 3
        assert result["metrics"]["new_version"] == 4

    def test_null_aware_breakdown(self, spark_session):
        """Test NULL-aware change classification."""
        import pandas as pd

        old_data = [
            {"id": 1, "score": None},      # null -> value
            {"id": 2, "score": 85.0},       # value -> null
            {"id": 3, "score": 70.0},       # value -> value
            {"id": 4, "score": 90.0},       # unchanged
        ]
        new_data = [
            {"id": 1, "score": 95.0},       # null -> value
            {"id": 2, "score": None},       # value -> null
            {"id": 3, "score": 80.0},       # value -> value
            {"id": 4, "score": 90.0},       # unchanged
        ]

        old_df = spark_session.createDataFrame(pd.DataFrame(old_data))
        new_df = spark_session.createDataFrame(pd.DataFrame(new_data))
        history = _make_history(5)

        with (
            patch("delta_diff_impl._get_spark_session", return_value=spark_session),
            patch("delta_diff_impl._get_delta_history", return_value=history),
            patch("delta_diff_impl._read_delta_version") as mock_read,
        ):
            mock_read.side_effect = lambda spark, table, version: (
                old_df if version == 3 else new_df
            )

            result = delta_diff_context(
                table="cat.sch.t",
                keys=["id"],
                old_version=3,
                new_version=4,
                output_format="dict",
            )

        counts = result["metrics"]["changed_column_counts"]["score"]
        assert counts["null_to_value"] == 1
        assert counts["value_to_null"] == 1
        assert counts["value_changed"] == 1

    def test_schema_evolution(self, spark_session):
        """Test handling of schema changes between versions."""
        import pandas as pd

        old_data = [{"id": 1, "name": "Alice", "old_col": "x"}]
        new_data = [{"id": 1, "name": "Alice", "new_col": "y"}]

        old_df = spark_session.createDataFrame(pd.DataFrame(old_data))
        new_df = spark_session.createDataFrame(pd.DataFrame(new_data))
        history = _make_history(5)

        with (
            patch("delta_diff_impl._get_spark_session", return_value=spark_session),
            patch("delta_diff_impl._get_delta_history", return_value=history),
            patch("delta_diff_impl._read_delta_version") as mock_read,
        ):
            mock_read.side_effect = lambda spark, table, version: (
                old_df if version == 3 else new_df
            )

            result = delta_diff_context(
                table="cat.sch.t",
                keys=["id"],
                old_version=3,
                new_version=4,
                output_format="dict",
            )

        assert result["metrics"]["schema_changed"] is True
        assert "new_col" in result["metrics"]["added_cols"]
        assert "old_col" in result["metrics"]["removed_cols"]

    def test_markdown_output(self, spark_session):
        """Test markdown rendering."""
        import pandas as pd

        old_data = [{"id": 1, "val": "a"}]
        new_data = [{"id": 1, "val": "b"}, {"id": 2, "val": "c"}]

        old_df = spark_session.createDataFrame(pd.DataFrame(old_data))
        new_df = spark_session.createDataFrame(pd.DataFrame(new_data))
        history = _make_history(5)

        with (
            patch("delta_diff_impl._get_spark_session", return_value=spark_session),
            patch("delta_diff_impl._get_delta_history", return_value=history),
            patch("delta_diff_impl._read_delta_version") as mock_read,
        ):
            mock_read.side_effect = lambda spark, table, version: (
                old_df if version == 3 else new_df
            )

            result = delta_diff_context(
                table="cat.sch.t",
                keys=["id"],
                old_version=3,
                new_version=4,
                output_format="markdown",
            )

        assert isinstance(result, str)
        assert "Version Info" in result
        assert "Changed Columns" in result or "Findings" in result

    def test_positional_args(self, spark_session):
        """Test that table and keys can be passed positionally."""
        import pandas as pd

        data = [{"id": 1, "val": "x"}]
        df = spark_session.createDataFrame(pd.DataFrame(data))
        history = _make_history(5)

        with (
            patch("delta_diff_impl._get_spark_session", return_value=spark_session),
            patch("delta_diff_impl._get_delta_history", return_value=history),
            patch("delta_diff_impl._read_delta_version", return_value=df),
        ):
            result = delta_diff_context(
                "cat.sch.t", ["id"],
                old_version=3, new_version=4,
                output_format="dict",
            )

        assert result["kind"] == "delta_diff"
        assert result["metrics"]["table"] == "cat.sch.t"


# ============================================================================
# Validation tests
# ============================================================================


class TestValidation:
    """Tests for input validation."""

    def test_missing_table_raises(self):
        with pytest.raises(ValueError, match="table is required"):
            delta_diff_context(keys=["id"])

    def test_missing_keys_raises(self):
        with pytest.raises(ValueError, match="keys is required"):
            delta_diff_context(table="cat.sch.t")

    def test_invalid_output_format_raises(self):
        with pytest.raises(ValueError, match="output_format"):
            delta_diff_context(
                table="cat.sch.t", keys=["id"], output_format="csv"
            )

    def test_old_version_gte_new_raises(self, spark_session):
        """old_version >= new_version should raise."""
        history = _make_history(5)

        with (
            patch("delta_diff_impl._get_spark_session", return_value=spark_session),
            patch("delta_diff_impl._get_delta_history", return_value=history),
        ):
            with pytest.raises(ValueError, match="must be less than"):
                delta_diff_context(
                    table="cat.sch.t", keys=["id"],
                    old_version=4, new_version=3,
                )


# ============================================================================
# Renderer tests
# ============================================================================


class TestRenderer:
    """Tests for render_delta_diff_report."""

    def test_render_basic_contract(self):
        ctx = {
            "kind": "delta_diff",
            "subject": "cat.sch.t",
            "summary": "Version 3\u21924: 1 added, 0 removed, 0 changed, 5 unchanged",
            "metrics": {
                "table": "cat.sch.t",
                "old_version": 3,
                "new_version": 4,
                "old_timestamp": "2026-06-01 10:30:00",
                "new_timestamp": "2026-06-01 10:40:00",
                "old_operation": "MERGE",
                "new_operation": "MERGE",
                "new_user": "user@co.com",
                "old_row_count": 100,
                "new_row_count": 101,
                "added_count": 1,
                "removed_count": 0,
                "changed_count": 0,
                "unchanged_count": 100,
                "changed_column_counts": {},
                "schema_changed": False,
            },
            "findings": ["1 rows added (new keys not in previous version)"],
            "risks": [],
            "samples": {"added_rows": [{"id": 101, "name": "NewUser"}]},
            "suggested_next_actions": ["Profile new rows"],
        }

        report = render_delta_diff_report(ctx)
        assert isinstance(report, str)
        assert "Version Info" in report
        assert "v3" in report
        assert "v4" in report
        assert "MERGE" in report
        assert "user@co.com" in report
        assert "Added Rows" in report
