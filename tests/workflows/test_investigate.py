"""Tests for the investigate workflow (Phase 3 COMPOSE).

Covers:
- Error handling for missing input
- Adaptive skipping of suggest_rules when explicit rules are provided
- Adaptive case_file selection for duplicates, nulls, and outliers
- coerce_check execution for pandas inputs with string normalization issues
- coerce_check skipping for non-pandas engines
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

@pytest.fixture(autouse=True)
def _reload_workflows():
    """Re-import _workflows to pick up a fresh module after any sys.modules flush."""
    import importlib
    import odibi_anchor._dispatcher._workflows as mod
    importlib.reload(mod)


def _get_investigate():
    """Return the current _investigate_workflow function from sys.modules."""
    from odibi_anchor._dispatcher._workflows import _investigate_workflow
    return _investigate_workflow


@pytest.fixture
def dirty_df():
    """DataFrame with duplicates, nulls, and string formatting issues."""
    return pd.DataFrame(
        {
            "id": [1, 2, 2, 4],
            "status": [" open ", "CLOSED", None, "open"],
            "amount": [10.0, 9999.0, 20.0, None],
        }
    )


@pytest.fixture
def fake_profile_ctx():
    """Minimal profile context used to drive adaptive logic."""
    return {
        "kind": "profile_table",
        "subject": "dirty_df",
        "summary": "profiled",
        "metrics": {"row_count": 4, "column_count": 3, "quality_score": 0.72},
        "column_profiles": {
            "status": {"null_pct": 0.25, "quality_flags": ["nulls"]},
            "amount": {"null_pct": 0.25, "quality_flags": ["outliers"]},
            "id": {"null_pct": 0.0, "quality_flags": []},
        },
        "findings": ["status has nulls"],
        "risks": [],
        "samples": {},
        "suggested_next_actions": [],
    }


@pytest.fixture
def fake_validation_ctx():
    """Validation result with uniqueness and null failures."""
    return {
        "kind": "validation_summary_context",
        "subject": "dirty_df",
        "summary": "2 failed rules",
        "metrics": {"rules_evaluated": 2, "rules_failed": 2},
        "rules": [
            {
                "rule_type": "unique",
                "columns": ["id"],
                "passed": False,
            },
            {
                "rule_type": "not_null",
                "columns": ["status"],
                "passed": False,
            },
        ],
        "findings": ["id is not unique", "status has nulls"],
        "risks": [],
        "samples": {},
        "suggested_next_actions": [],
    }


class TestInvestigateWorkflow:
    """Integration-style tests for adaptive investigate orchestration."""

    def test_no_input_returns_error(self):
        result = _get_investigate()(output_format="dict")
        assert result["kind"] == "workflow_investigate"
        assert result["subject"] == "error"
        assert "No table or DataFrame provided" in result["summary"]

    def test_explicit_rules_skip_suggest_rules(self, dirty_df, fake_profile_ctx, fake_validation_ctx):
        micro_by_column = {
            "status": {
                "kind": "microscope",
                "subject": "dirty_df.status",
                "summary": "status inspected",
                "metrics": {
                    "column_type_category": "string",
                    "null_count": 1,
                    "outlier_count": 0,
                    "whitespace_issues": 1,
                    "case_upper_pct": 0.5,
                    "case_lower_pct": 0.5,
                    "case_mixed_pct": 0.0,
                },
                "findings": ["format inconsistency detected"],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
            "amount": {
                "kind": "microscope",
                "subject": "dirty_df.amount",
                "summary": "amount inspected",
                "metrics": {
                    "column_type_category": "numeric",
                    "null_count": 1,
                    "outlier_count": 1,
                    "whitespace_issues": 0,
                },
                "findings": ["numeric outlier detected"],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        }
        case_calls: list[dict] = []

        def fake_case_file(df, **kwargs):
            case_calls.append(kwargs)
            return {
                "kind": "case_file",
                "subject": kwargs.get("subject", "case"),
                "summary": kwargs["filter"],
                "metrics": {},
                "findings": [kwargs["filter"]],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._profile_table_context",
            return_value=fake_profile_ctx,
        ), patch(
            "odibi_anchor.validation.validation_summary_context.validation_summary_context",
            return_value=fake_validation_ctx,
        ) as mock_validate, patch(
            "odibi_anchor._dispatcher._tool_wrappers._microscope_context",
            side_effect=lambda df, column, **kwargs: micro_by_column[column],
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._case_file_context",
            side_effect=fake_case_file,
        ), patch(
            "odibi_anchor.tables.coercion_classifier.coercion_check_context",
            return_value={
                "kind": "coerce_check",
                "subject": "dirty_df_coercion",
                "summary": "1 coercion candidate",
                "metrics": {},
                "findings": ["status changes after normalization"],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        ) as mock_coerce, patch(
            "odibi_anchor._dispatcher._workflows.detect_engine",
            return_value="pandas",
        ):
            result = _get_investigate()(
                dirty_df,
                subject="dirty_df",
                columns=["status", "amount"],
                rules=[{"rule_type": "unique", "columns": ["id"]}],
                output_format="dict",
            )

        assert result["kind"] == "workflow_investigate"
        suggest_steps = [s for s in result["steps"] if s["tool"] == "suggest_rules"]
        assert suggest_steps[0]["status"] == "skipped"
        assert suggest_steps[0]["reason"] == "explicit rules provided"
        assert mock_validate.called
        assert mock_coerce.called
        assert result["metrics"]["coercion_columns"] == 1

        filters_seen = {call["filter"] for call in case_calls}
        assert "duplicates" in filters_seen
        assert "nulls" in filters_seen
        assert "outliers" in filters_seen

    def test_non_pandas_engine_skips_coerce_check(self, dirty_df, fake_profile_ctx, fake_validation_ctx):
        status_micro = {
            "kind": "microscope",
            "subject": "dirty_df.status",
            "summary": "status inspected",
            "metrics": {
                "column_type_category": "string",
                "null_count": 1,
                "outlier_count": 0,
                "whitespace_issues": 2,
                "case_upper_pct": 0.5,
                "case_lower_pct": 0.5,
                "case_mixed_pct": 0.0,
            },
            "findings": ["pattern mismatch"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": [],
        }

        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._profile_table_context",
            return_value=fake_profile_ctx,
        ), patch(
            "tools.suggest_rules_tool.suggest_rules_impl.suggest_rules_context",
            return_value={"rules": [{"rule_type": "not_null", "columns": ["status"]}]},
        ), patch(
            "odibi_anchor.validation.validation_summary_context.validation_summary_context",
            return_value=fake_validation_ctx,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._microscope_context",
            return_value=status_micro,
        ), patch(
            "odibi_anchor._dispatcher._tool_wrappers._case_file_context",
            return_value={
                "kind": "case_file",
                "subject": "dirty_df.status.nulls",
                "summary": "nulls",
                "metrics": {},
                "findings": [],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        ), patch(
            "odibi_anchor.tables.coercion_classifier.coercion_check_context",
        ) as mock_coerce, patch(
            "odibi_anchor._dispatcher._workflows.detect_engine",
            return_value="spark",
        ):
            result = _get_investigate()(dirty_df, subject="dirty_df", output_format="dict")

        coerce_steps = [s for s in result["steps"] if s["tool"] == "coerce_check"]
        assert coerce_steps[-1]["status"] == "skipped"
        assert "pandas only" in coerce_steps[-1]["reason"]
        assert not mock_coerce.called

    def test_output_contract_has_required_keys(self, dirty_df, fake_profile_ctx, fake_validation_ctx):
        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._profile_table_context",
            return_value=fake_profile_ctx,
        ), patch(
            "tools.suggest_rules_tool.suggest_rules_impl.suggest_rules_context",
            return_value={"rules": []},
        ), patch(
            "odibi_anchor.validation.validation_summary_context.validation_summary_context",
            return_value=fake_validation_ctx,
        ):
            result = _get_investigate()(dirty_df, subject="dirty_df", output_format="dict")

        required = {
            "kind",
            "subject",
            "summary",
            "metrics",
            "findings",
            "risks",
            "samples",
            "suggested_next_actions",
            "steps",
            "skipped",
        }
        assert required.issubset(result.keys())
        assert result["metrics"]["row_count"] == 4

    def test_markdown_output(self, dirty_df, fake_profile_ctx):
        with patch(
            "odibi_anchor._dispatcher._tool_wrappers._profile_table_context",
            return_value=fake_profile_ctx,
        ), patch(
            "tools.suggest_rules_tool.suggest_rules_impl.suggest_rules_context",
            return_value={"rules": []},
        ):
            result = _get_investigate()(dirty_df, subject="dirty_df", output_format="markdown")

        assert isinstance(result, str)
        assert "workflow_investigate" in result
        assert "profile_table" in result
