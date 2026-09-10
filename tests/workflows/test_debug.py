"""Tests for the debug workflow (Phase 4 COMPOSE).

Covers:
- Error handling for missing result_df and upstreams
- Adaptive pre_join execution when diagnose_empty finds problematic upstream pairs
- explain_row skipping on truly empty results
- explain_row execution when sparse results still have rows
- pre_merge skipping without a target and running with a target
- Output contract shape
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

from odibi_anchor._dispatcher._workflows import _debug_workflow


@pytest.fixture
def upstreams():
    return {
        "source": pd.DataFrame({"id": [1, 2, 3], "status": ["ACTIVE", "ACTIVE", "INACTIVE"]}),
        "dim": pd.DataFrame({"id": [1, 2, 4], "attr": ["a", "b", "d"]}),
    }


@pytest.fixture
def empty_result_df():
    return pd.DataFrame({"id": pd.Series(dtype="int64"), "status": pd.Series(dtype="object")})


@pytest.fixture
def sparse_result_df():
    return pd.DataFrame({"id": [1], "status": ["ACTIVE"]})


class TestDebugWorkflow:
    def test_missing_result_returns_error(self, upstreams):
        result = _debug_workflow(upstreams=upstreams, output_format="dict")
        assert result["kind"] == "workflow_debug"
        assert result["subject"] == "error"
        assert "No result DataFrame provided" in result["summary"]

    def test_missing_upstreams_returns_error(self, empty_result_df):
        result = _debug_workflow(empty_result_df, output_format="dict")
        assert result["kind"] == "workflow_debug"
        assert result["subject"] == "error"
        assert "upstreams dict is required" in result["summary"]

    def test_empty_result_runs_pre_join_and_skips_explain_row(self, empty_result_df, upstreams):
        diagnose_ctx = {
            "kind": "diagnose_empty",
            "subject": "result_df",
            "summary": "empty output diagnosed",
            "metrics": {
                "result_count": 0,
                "dropout_cause": "zero_key_overlap",
            },
            "findings": ["Zero key overlap between source↔dim"],
            "risks": [],
            "samples": {
                "key_overlaps": [
                    {
                        "pair": "source↔dim",
                        "common_keys": ["id"],
                        "overlap_count": 0,
                        "overlap_pct": 0.0,
                    }
                ]
            },
            "suggested_next_actions": [],
        }
        pre_join_calls: list[dict] = []

        def fake_pre_join(left_df, right_df, **kwargs):
            pre_join_calls.append(kwargs)
            return {
                "kind": "pre_join",
                "subject": f"{kwargs['left_subject']}↔{kwargs['right_subject']}",
                "summary": "join risk confirmed",
                "metrics": {"overlap_pct": 0.0},
                "findings": ["low overlap"],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._diagnose_empty_context",
            return_value=diagnose_ctx,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._pre_join_context",
            side_effect=fake_pre_join,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
        ) as mock_explain, patch(
            "odibi_anchor._dispatcher._tool_wrappers._pre_merge_context",
        ) as mock_pre_merge:
            result = _debug_workflow(
                empty_result_df,
                upstreams=upstreams,
                keys=["id"],
                filter_expr="status = 'Active'",
                subject="result_df",
                output_format="dict",
            )

        assert result["kind"] == "workflow_debug"
        assert result["metrics"]["problematic_pairs"] == 1
        assert pre_join_calls[0]["left_subject"] == "source"
        explain_steps = [s for s in result["steps"] if s["tool"] == "explain_row"]
        assert explain_steps[0]["status"] == "skipped"
        assert "truly empty" in explain_steps[0]["reason"]
        assert not mock_explain.called
        assert not mock_pre_merge.called

    def test_sparse_result_runs_explain_row_and_pre_merge(self, sparse_result_df, upstreams):
        diagnose_ctx = {
            "kind": "diagnose_empty",
            "subject": "result_df",
            "summary": "sparse output diagnosed",
            "metrics": {
                "result_count": 1,
                "dropout_cause": "filter_kills_all",
            },
            "findings": ["Filter removes most rows"],
            "risks": [],
            "samples": {
                "key_overlaps": [
                    {
                        "pair": "source↔dim",
                        "common_keys": ["id"],
                        "overlap_count": 2,
                        "overlap_pct": 0.5,
                    }
                ]
            },
            "suggested_next_actions": [],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._diagnose_empty_context",
            return_value=diagnose_ctx,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._pre_join_context",
            return_value={
                "kind": "pre_join",
                "subject": "source↔dim",
                "summary": "join inspected",
                "metrics": {"overlap_pct": 0.5},
                "findings": [],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
            return_value={
                "kind": "explain_row",
                "subject": "id=1",
                "summary": "row traced",
                "metrics": {"columns_traced": 2},
                "findings": ["value came from source"],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        ) as mock_explain, patch(
            "odibi_anchor._dispatcher._tool_wrappers._pre_merge_context",
            return_value={
                "kind": "pre_merge",
                "subject": "result_df",
                "summary": "merge inspected",
                "metrics": {"insert_count": 1, "update_count": 0},
                "findings": [],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        ) as mock_pre_merge:
            result = _debug_workflow(
                sparse_result_df,
                upstreams=upstreams,
                keys=["id"],
                target="catalog.schema.target_table",
                subject="result_df",
                output_format="dict",
            )

        explain_call = mock_explain.call_args.kwargs
        assert explain_call["values"] == {"id": 1}
        assert mock_pre_merge.called
        assert result["metrics"]["result_count"] == 1
        assert result["samples"]["representative_values"] == {"id": 1}
        tool_names = [s["tool"] for s in result["steps"]]
        assert tool_names[:4] == ["diagnose_empty", "pre_join", "explain_row", "pre_merge"]

    def test_markdown_output_returns_string(self, sparse_result_df, upstreams):
        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._diagnose_empty_context",
            return_value={
                "kind": "diagnose_empty",
                "subject": "result_df",
                "summary": "sparse output diagnosed",
                "metrics": {"result_count": 1, "dropout_cause": "unknown"},
                "findings": [],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        ):
            result = _debug_workflow(
                sparse_result_df,
                upstreams=upstreams,
                keys=["id"],
                output_format="markdown",
            )

        assert isinstance(result, str)
        assert "workflow_debug" in result
