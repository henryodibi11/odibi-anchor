"""Tests for the trace workflow (Phase 3 COMPOSE).

Covers:
- Error handling for missing output_df, keys, values, upstream
- explain_row always runs
- Adaptive pre_join: skipped with 1 upstream, runs with 2+
- Adaptive case_file: skipped when all matches are 1:1, runs on problematic upstreams
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

from odibi_anchor._dispatcher._workflows import _trace_workflow


@pytest.fixture
def output_df():
    return pd.DataFrame({"id": [42], "amount": [100.0], "region": ["US"]})


@pytest.fixture
def upstream_single():
    return {"source": pd.DataFrame({"id": [42, 43], "amount": [100.0, 200.0]})}


@pytest.fixture
def upstream_multi():
    return {
        "source": pd.DataFrame({"id": [42, 43], "amount": [100.0, 200.0]}),
        "dim": pd.DataFrame({"id": [42, 44], "region": ["US", "EU"]}),
    }


class TestTraceWorkflowErrors:
    def test_missing_output_df(self):
        result = _trace_workflow(
            keys=["id"], values={"id": 42}, upstream={"s": pd.DataFrame()},
            output_format="dict",
        )
        assert result["kind"] == "workflow_trace"
        assert result["subject"] == "error"
        assert "No output DataFrame" in result["summary"]

    def test_missing_keys(self, output_df, upstream_single):
        result = _trace_workflow(
            output_df, upstream=upstream_single, values={"id": 42},
            output_format="dict",
        )
        assert "keys parameter is required" in result["summary"]

    def test_missing_values(self, output_df, upstream_single):
        result = _trace_workflow(
            output_df, keys=["id"], upstream=upstream_single,
            output_format="dict",
        )
        assert "values parameter is required" in result["summary"]

    def test_missing_upstream(self, output_df):
        result = _trace_workflow(
            output_df, keys=["id"], values={"id": 42},
            output_format="dict",
        )
        assert "upstream dict is required" in result["summary"]


class TestTraceWorkflowAdaptive:
    def test_single_upstream_skips_pre_join(self, output_df, upstream_single):
        explain_ctx = {
            "kind": "explain_row",
            "subject": "id=42",
            "summary": "row traced",
            "metrics": {"columns_traced": 2, "columns_untraced": 0, "exact_matches": 2},
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
            "join_paths": [
                {"upstream": "source", "join_keys": ["id"], "match_count": 1}
            ],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
            return_value=explain_ctx,
        ):
            result = _trace_workflow(
                output_df,
                keys=["id"],
                values={"id": 42},
                upstream=upstream_single,
                output_format="dict",
            )

        pj_steps = [s for s in result["steps"] if s["tool"] == "pre_join"]
        assert pj_steps[0]["status"] == "skipped"
        assert "only 1 upstream" in pj_steps[0]["reason"]

    def test_multi_upstream_runs_pre_join(self, output_df, upstream_multi):
        explain_ctx = {
            "kind": "explain_row",
            "subject": "id=42",
            "summary": "row traced",
            "metrics": {"columns_traced": 3, "columns_untraced": 0, "exact_matches": 3},
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
            "join_paths": [
                {"upstream": "source", "join_keys": ["id"], "match_count": 1},
                {"upstream": "dim", "join_keys": ["id"], "match_count": 1},
            ],
        }
        pre_join_ctx = {
            "kind": "pre_join",
            "subject": "source↔dim",
            "summary": "join looks healthy",
            "metrics": {"overlap_pct": 0.5},
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
            return_value=explain_ctx,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._pre_join_context",
            return_value=pre_join_ctx,
        ):
            result = _trace_workflow(
                output_df,
                keys=["id"],
                values={"id": 42},
                upstream=upstream_multi,
                output_format="dict",
            )

        pj_steps = [s for s in result["steps"] if s["tool"] == "pre_join"]
        assert pj_steps[0]["status"] == "ok"

    def test_clean_matches_skip_case_file(self, output_df, upstream_single):
        explain_ctx = {
            "kind": "explain_row",
            "subject": "id=42",
            "summary": "row traced",
            "metrics": {"columns_traced": 2, "columns_untraced": 0, "exact_matches": 2},
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
            "join_paths": [
                {"upstream": "source", "join_keys": ["id"], "match_count": 1}
            ],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
            return_value=explain_ctx,
        ):
            result = _trace_workflow(
                output_df,
                keys=["id"],
                values={"id": 42},
                upstream=upstream_single,
                output_format="dict",
            )

        cf_steps = [s for s in result["steps"] if s["tool"] == "case_file"]
        assert cf_steps[0]["status"] == "skipped"
        assert "all upstreams matched cleanly" in cf_steps[0]["reason"]

    def test_missing_match_triggers_case_file(self, output_df, upstream_multi):
        explain_ctx = {
            "kind": "explain_row",
            "subject": "id=42",
            "summary": "row traced with missing",
            "metrics": {"columns_traced": 2, "columns_untraced": 1, "exact_matches": 1},
            "findings": ["dim: no match for id=42"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
            "join_paths": [
                {"upstream": "source", "join_keys": ["id"], "match_count": 1},
                {"upstream": "dim", "join_keys": ["id"], "match_count": 0},
            ],
        }
        pre_join_ctx = {
            "kind": "pre_join",
            "subject": "source↔dim",
            "summary": "join inspected",
            "metrics": {},
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
        }
        case_file_ctx = {
            "kind": "case_file",
            "subject": "dim.id",
            "summary": "investigated missing key",
            "metrics": {"row_count": 0},
            "findings": ["no rows match id=42 in dim"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
            return_value=explain_ctx,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._pre_join_context",
            return_value=pre_join_ctx,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._case_file_context",
            return_value=case_file_ctx,
        ) as mock_cf:
            result = _trace_workflow(
                output_df,
                keys=["id"],
                values={"id": 42},
                upstream=upstream_multi,
                output_format="dict",
            )

        assert mock_cf.called
        cf_steps = [s for s in result["steps"] if s["tool"] == "case_file" and s["status"] == "ok"]
        assert len(cf_steps) == 1
        assert result["metrics"]["problematic_upstreams"] == 1
        assert result["samples"]["problematic_upstreams"] == ["dim"]


class TestTraceWorkflowContract:
    def test_output_contract_keys(self, output_df, upstream_single):
        explain_ctx = {
            "kind": "explain_row",
            "subject": "id=42",
            "summary": "traced",
            "metrics": {"columns_traced": 2, "columns_untraced": 0, "exact_matches": 2},
            "findings": ["value from source"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": ["anchor(\'microscope\')"],
            "join_paths": [{"upstream": "source", "join_keys": ["id"], "match_count": 1}],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
            return_value=explain_ctx,
        ):
            result = _trace_workflow(
                output_df,
                keys=["id"],
                values={"id": 42},
                upstream=upstream_single,
                output_format="dict",
            )

        assert result["kind"] == "workflow_trace"
        assert "steps" in result
        assert "skipped" in result
        assert "metrics" in result
        assert "findings" in result
        assert result["metrics"]["columns_traced"] == 2
        assert result["metrics"]["upstream_count"] == 1
        assert result["samples"]["traced_values"] == {"id": 42}

    def test_markdown_output(self, output_df, upstream_single):
        explain_ctx = {
            "kind": "explain_row",
            "subject": "id=42",
            "summary": "traced",
            "metrics": {"columns_traced": 1, "columns_untraced": 0, "exact_matches": 1},
            "findings": [],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
            "join_paths": [{"upstream": "source", "join_keys": ["id"], "match_count": 1}],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._explain_row_context",
            return_value=explain_ctx,
        ):
            result = _trace_workflow(
                output_df,
                keys=["id"],
                values={"id": 42},
                upstream=upstream_single,
                output_format="markdown",
            )

        assert isinstance(result, str)
        assert "workflow_trace" in result
