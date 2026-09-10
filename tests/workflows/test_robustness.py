"""Tests for COMPOSE workflow robustness hardening.

Tests empty DataFrame guards, single-row warnings, and all-null column detection
across all 6 workflows (per COMPOSE_ROBUSTNESS_SPEC.md).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from odibi_anchor._dispatcher._workflows import (
    _onboard_workflow,
    _reconcile_workflow,
    _investigate_workflow,
    _debug_workflow,
    _trace_workflow,
    _evolve_workflow,
)


@pytest.fixture
def empty_df():
    return pd.DataFrame({"id": pd.Series(dtype="int64"), "amount": pd.Series(dtype="float64")})


@pytest.fixture
def single_row_df():
    return pd.DataFrame({"id": [1], "amount": [100.0], "status": ["Active"]})


@pytest.fixture
def all_null_df():
    return pd.DataFrame({"id": [1, 2, 3], "bad_col": [None, None, None], "ok_col": [1, 2, 3]})


@pytest.fixture
def normal_df():
    return pd.DataFrame({"id": [1, 2], "amount": [100.0, 200.0]})


class TestEmptyGuard:
    """Empty DataFrame guard returns early with clear message."""

    def test_onboard_empty(self, empty_df):
        result = _onboard_workflow(empty_df, output_format="dict")
        assert result["kind"] == "workflow_onboard"
        assert result["subject"] == "empty_input"
        assert "0 rows" in result["summary"]
        assert result["metrics"]["row_count"] == 0

    def test_reconcile_empty_old(self, empty_df, normal_df):
        result = _reconcile_workflow(empty_df, normal_df, keys=["id"], output_format="dict")
        assert result["kind"] == "workflow_reconcile"
        assert result["subject"] == "empty_input"
        assert "0 rows" in result["summary"]

    def test_reconcile_empty_new(self, normal_df, empty_df):
        result = _reconcile_workflow(normal_df, empty_df, keys=["id"], output_format="dict")
        assert result["kind"] == "workflow_reconcile"
        assert result["subject"] == "empty_input"
        assert "0 rows" in result["summary"]

    def test_investigate_empty(self, empty_df):
        result = _investigate_workflow(empty_df, subject="test", output_format="dict")
        assert result["kind"] == "workflow_investigate"
        assert result["subject"] == "empty_input"

    def test_trace_row_empty(self, empty_df, normal_df):
        result = _trace_workflow(
            output_df=empty_df,
            keys=["id"],
            values={"id": 1},
            upstream={"source": normal_df},
            output_format="dict",
        )
        assert result["kind"] == "workflow_trace_row"
        assert result["subject"] == "empty_input"

    def test_evolve_empty(self, empty_df, normal_df):
        result = _evolve_workflow(empty_df, target_schema=normal_df, output_format="dict")
        assert result["kind"] == "workflow_evolve"
        assert result["subject"] == "empty_input"

    def test_debug_handles_empty_natively(self, normal_df):
        """Debug does NOT guard — it handles empty results via diagnose_empty."""
        empty = pd.DataFrame({"id": pd.Series(dtype="int64")})
        diagnose_ctx = {
            "kind": "diagnose_empty",
            "subject": "result_df",
            "summary": "empty",
            "metrics": {"result_count": 0},
            "findings": [],
            "risks": [],
            "samples": {},
        }
        with patch(
            "tools.diagnose_empty_tool.diagnose_empty_impl.diagnose_empty_context",
            return_value=diagnose_ctx,
        ):
            result = _debug_workflow(
                result_df=empty,
                upstreams={"source": normal_df},
                keys=["id"],
                output_format="dict",
            )
        # Should NOT be "empty_input" — debug runs diagnose_empty instead
        assert result["subject"] != "empty_input"
        assert result["kind"] == "workflow_debug"


class TestSingleRowWarning:
    """Single-row DataFrames get a warning annotation in metrics."""

    def test_onboard_single_row(self, single_row_df):
        result = _onboard_workflow(single_row_df, subject="test", output_format="dict")
        assert result["kind"] == "workflow_onboard"
        assert result.get("metrics", {}).get("single_row") is True

    def test_investigate_single_row(self, single_row_df):
        result = _investigate_workflow(single_row_df, subject="test", output_format="dict")
        assert result["kind"] == "workflow_investigate"
        assert result.get("metrics", {}).get("single_row") is True

    def test_normal_df_no_warning(self, normal_df):
        result = _onboard_workflow(normal_df, subject="test", output_format="dict")
        assert result.get("metrics", {}).get("single_row") is None


class TestAllNullDetection:
    """All-null columns are surfaced in extra_metrics."""

    def test_onboard_all_null_columns(self, all_null_df):
        result = _onboard_workflow(all_null_df, subject="test", output_format="dict")
        assert result["kind"] == "workflow_onboard"
        assert "bad_col" in result.get("metrics", {}).get("all_null_columns", [])
        assert "ok_col" not in result.get("metrics", {}).get("all_null_columns", [])

    def test_investigate_all_null_columns(self, all_null_df):
        result = _investigate_workflow(all_null_df, subject="test", output_format="dict")
        assert result["kind"] == "workflow_investigate"
        assert "bad_col" in result.get("metrics", {}).get("all_null_columns", [])

    def test_no_nulls_no_key(self, normal_df):
        result = _onboard_workflow(normal_df, subject="test", output_format="dict")
        assert result.get("metrics", {}).get("all_null_columns") is None
