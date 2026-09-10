"""Tests for the reconcile workflow (Phase 2 COMPOSE).

Covers:
- DataFrame mode: old_df + new_df + keys → diff + schema_diff
- Delta mode: table + keys → delta_diff (mocked)
- Adaptive skipping: partition_check, watermark, schema_diff
- Error handling: missing keys, no input
- Output contract shape
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

# Ensure both src/ and project root are importable
_PROJECT_ROOT = _Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from odibi_anchor._dispatcher._workflows import (
    _reconcile_workflow,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def old_df():
    """Previous version of a table."""
    return pd.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "name": ["alice", "bob", "carol", "dave"],
            "amount": [100.0, 200.0, 300.0, 400.0],
        }
    )


@pytest.fixture
def new_df():
    """Current version with changes: id=2 modified, id=4 removed, id=5 added."""
    return pd.DataFrame(
        {
            "id": [1, 2, 3, 5],
            "name": ["alice", "BOB", "carol", "eve"],
            "amount": [100.0, 250.0, 300.0, 500.0],
        }
    )


@pytest.fixture
def schema_drift_df():
    """New version with an extra column (schema drift)."""
    return pd.DataFrame(
        {
            "id": [1, 2, 3],
            "name": ["alice", "bob", "carol"],
            "amount": [100.0, 200.0, 300.0],
            "region": ["US", "EU", "US"],
        }
    )


# ---------------------------------------------------------------------------
# DataFrame Mode Tests
# ---------------------------------------------------------------------------


class TestReconcileDataFrameMode:
    """Tests using two DataFrames as input."""

    def test_basic_reconcile(self, old_df, new_df):
        """Basic reconcile returns valid contract with diff results."""
        result = _reconcile_workflow(old_df, new_df, keys=["id"], output_format="dict")
        assert result["kind"] == "workflow_reconcile"
        assert result["subject"] == "dataframe_comparison"
        tool_names = [s["tool"] for s in result["steps"]]
        assert "diff" in tool_names
        # partition_check skipped (no table)
        partition_steps = [s for s in result["steps"] if s["tool"] == "partition_check"]
        assert partition_steps[0]["status"] == "skipped"

    def test_added_removed_changed_metrics(self, old_df, new_df):
        """Extra metrics should capture row-level changes."""
        result = _reconcile_workflow(old_df, new_df, keys=["id"], output_format="dict")
        m = result["metrics"]
        assert m["added_rows"] == 1  # id=5
        assert m["removed_rows"] == 1  # id=4
        assert m["changed_rows"] == 1  # id=2

    def test_schema_drift_triggers_schema_diff(self, old_df, schema_drift_df):
        """When columns differ, schema_diff should run (not skip)."""
        result = _reconcile_workflow(
            old_df, schema_drift_df, keys=["id"], output_format="dict"
        )
        schema_steps = [s for s in result["steps"] if s["tool"] == "schema_diff"]
        assert schema_steps[0]["status"] == "ok"
        assert result["metrics"]["schema_changed"] is True

    def test_no_schema_drift_skips_schema_diff(self, old_df, new_df):
        """When columns are identical, schema_diff should be skipped."""
        result = _reconcile_workflow(old_df, new_df, keys=["id"], output_format="dict")
        schema_steps = [s for s in result["steps"] if s["tool"] == "schema_diff"]
        assert schema_steps[0]["status"] == "skipped"

    def test_watermark_skipped_without_params(self, old_df, new_df):
        """Watermark step should be skipped when no source/watermark_col."""
        result = _reconcile_workflow(old_df, new_df, keys=["id"], output_format="dict")
        wm_steps = [s for s in result["steps"] if s["tool"] == "watermark"]
        assert wm_steps[0]["status"] == "skipped"

    def test_custom_subject(self, old_df, new_df):
        """Subject parameter should override the default."""
        result = _reconcile_workflow(
            old_df, new_df, keys=["id"], subject="orders_v1_vs_v2", output_format="dict"
        )
        assert result["subject"] == "orders_v1_vs_v2"

    def test_markdown_output(self, old_df, new_df):
        """Markdown output format should return a string."""
        result = _reconcile_workflow(old_df, new_df, keys=["id"], output_format="markdown")
        assert isinstance(result, str)
        assert "workflow_reconcile" in result
        assert "diff" in result


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestReconcileErrors:
    """Tests for input validation and error handling."""

    def test_no_input_returns_error(self):
        """No arguments should return a usage error contract."""
        result = _reconcile_workflow(output_format="dict")
        assert result["kind"] == "workflow_reconcile"
        assert "error" in result["subject"]

    def test_missing_keys_returns_error(self, old_df, new_df):
        """Missing keys parameter should return a clear error."""
        result = _reconcile_workflow(old_df, new_df, output_format="dict")
        assert result["kind"] == "workflow_reconcile"
        assert "keys" in result["summary"].lower()

    def test_single_df_returns_error(self, old_df):
        """Single DataFrame without table name should error."""
        result = _reconcile_workflow(old_df, output_format="dict")
        assert result["kind"] == "workflow_reconcile"
        assert "error" in result["subject"]


# ---------------------------------------------------------------------------
# Delta Mode Tests (mocked)
# ---------------------------------------------------------------------------


class TestReconcileDeltaMode:
    """Tests for Delta table version comparison (mocked Spark)."""

    def test_delta_mode_calls_delta_diff(self):
        """When table is provided, should use delta_diff_context."""
        mock_result = {
            "kind": "delta_diff",
            "subject": "test_table",
            "summary": "v1→v2: 5 added, 2 removed",
            "metrics": {
                "added_count": 5,
                "removed_count": 2,
                "changed_count": 3,
                "unchanged_count": 90,
                "schema_changed": False,
            },
            "findings": ["5 rows added"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
        }
        with patch(
            "tools.delta_diff_tool.delta_diff_impl.delta_diff_context",
            return_value=mock_result,
        ):
            result = _reconcile_workflow(
                table="catalog.schema.test_table",
                keys=["id"],
                output_format="dict",
            )
        assert result["kind"] == "workflow_reconcile"
        assert result["metrics"]["added_rows"] == 5
        assert result["metrics"]["removed_rows"] == 2
        tool_names = [s["tool"] for s in result["steps"]]
        assert "delta_diff" in tool_names

    def test_delta_mode_partition_check_attempted(self):
        """In Delta mode, partition_check should be attempted (not skipped)."""
        mock_diff = {
            "kind": "delta_diff",
            "subject": "t",
            "summary": "ok",
            "metrics": {"added_count": 0, "removed_count": 0, "changed_count": 0, "unchanged_count": 10, "schema_changed": False},
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
        }
        with patch(
            "tools.delta_diff_tool.delta_diff_impl.delta_diff_context",
            return_value=mock_diff,
        ), patch(
            "tools.partition_check_tool.partition_check_impl.partition_check_context",
            side_effect=RuntimeError("no spark"),
        ):
            result = _reconcile_workflow(
                table="catalog.schema.t", keys=["id"], output_format="dict"
            )
        partition_steps = [s for s in result["steps"] if s["tool"] == "partition_check"]
        # Should have attempted (error or ok), NOT skipped
        assert partition_steps[0]["status"] == "error"


# ---------------------------------------------------------------------------
# Contract Shape Tests
# ---------------------------------------------------------------------------


class TestReconcileContract:
    """Verify output contract matches Anchor standard."""

    def test_required_keys(self, old_df, new_df):
        """All standard contract keys must be present."""
        result = _reconcile_workflow(old_df, new_df, keys=["id"], output_format="dict")
        required = {"kind", "subject", "summary", "metrics", "findings", "risks", "samples", "suggested_next_actions"}
        assert required.issubset(set(result.keys()))
        # Workflow-specific keys
        assert "steps" in result
        assert "skipped" in result
