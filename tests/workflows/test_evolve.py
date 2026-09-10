"""Tests for the evolve workflow (Phase 3 COMPOSE — final workflow).

Covers:
- Error handling for missing df, target_schema/coerce_ctx
- Adaptive schema_diff: runs with target_schema, skipped with coerce_ctx
- Adaptive schema_migrate: runs only when type changes detected
- Adaptive coerce_fix: runs only when coercion issues present
- Adaptive validate: runs only when changes made + rules provided
- Output contract shape
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from odibi_anchor._dispatcher._workflows import _evolve_workflow


@pytest.fixture
def df():
    return pd.DataFrame({"id": [1, 2], "amount": ["100", "200"], "name": ["alice", "BOB"]})


@pytest.fixture
def target_df():
    return pd.DataFrame({"id": [1], "amount": [100.0], "name": ["ALICE"]})


@pytest.fixture
def coerce_ctx_fixture():
    return {
        "kind": "coercion_check",
        "subject": "orders",
        "summary": "2 coercion issues",
        "metrics": {"columns_with_issues": 2, "total_issues": 4},
        "findings": ["amount: string→float mismatch", "name: case mismatch"],
        "risks": [],
        "samples": {},
    }


class TestEvolveWorkflowErrors:
    def test_missing_df(self):
        result = _evolve_workflow(
            target_schema=pd.DataFrame({"id": [1]}),
            output_format="dict",
        )
        assert result["kind"] == "workflow_evolve"
        assert result["subject"] == "error"
        assert "No DataFrame" in result["summary"]

    def test_missing_target_and_coerce(self, df):
        result = _evolve_workflow(df, output_format="dict")
        assert "Either target_schema or coerce_ctx" in result["summary"]


class TestEvolveWorkflowAdaptive:
    def test_coerce_ctx_skips_schema_diff(self, df, coerce_ctx_fixture):
        """When coerce_ctx is provided, schema_diff should be skipped."""
        coerce_fix_ctx = {
            "kind": "coerce_fix",
            "subject": "orders",
            "summary": "fixed 2 columns",
            "metrics": {"columns_fixed": 2, "rows_affected": 2},
            "findings": [],
            "risks": [],
            "samples": {},
        }

        with patch(
            "tools.coerce_fix_tool.coerce_fix_impl.coerce_fix_context",
            return_value=coerce_fix_ctx,
        ):
            result = _evolve_workflow(
                df, coerce_ctx=coerce_ctx_fixture, output_format="dict",
            )

        sd_steps = [s for s in result["steps"] if s["tool"] == "schema_diff"]
        assert sd_steps[0]["status"] == "skipped"
        assert "coerce_ctx provided" in sd_steps[0]["reason"]

    def test_no_type_changes_skips_migrate(self, df, target_df):
        """When schema_diff finds no type changes, schema_migrate should be skipped."""
        schema_diff_result = {
            "kind": "schema_diff",
            "subject": "dataframe",
            "summary": "schemas match",
            "metrics": {"type_changed_column_count": 0, "new_only_column_count": 0, "old_only_column_count": 0},
            "findings": [],
            "risks": [],
            "samples": {},
        }

        with patch(
            "odibi_anchor.tables.schema_diff_context.schema_diff_context",
            return_value=schema_diff_result,
        ):
            result = _evolve_workflow(
                df, target_schema=target_df, output_format="dict",
            )

        migrate_steps = [s for s in result["steps"] if s["tool"] == "schema_migrate"]
        assert migrate_steps[0]["status"] == "skipped"
        assert "no type changes" in migrate_steps[0]["reason"]

    def test_type_changes_triggers_migrate(self, df, target_df):
        """When schema_diff finds type changes, schema_migrate should run."""
        schema_diff_result = {
            "kind": "schema_diff",
            "subject": "dataframe",
            "summary": "1 type change",
            "metrics": {"type_changed_column_count": 1, "new_only_column_count": 0, "old_only_column_count": 0},
            "findings": ["amount: object → float64"],
            "risks": [],
            "samples": {},
        }
        migrate_result = {
            "kind": "schema_migrate",
            "subject": "dataframe",
            "summary": "1 column migrated",
            "metrics": {"changes_applied": 1},
            "findings": [],
            "risks": [],
            "samples": {},
        }

        with patch(
            "odibi_anchor.tables.schema_diff_context.schema_diff_context",
            return_value=schema_diff_result,
        ), patch(
            "tools.schema_migrate_tool.schema_migrate_impl.schema_migrate_context",
            return_value=migrate_result,
        ):
            result = _evolve_workflow(
                df, target_schema=target_df, output_format="dict",
            )

        migrate_steps = [s for s in result["steps"] if s["tool"] == "schema_migrate"]
        assert migrate_steps[0]["status"] == "ok"

    def test_no_changes_skips_validate(self, df, target_df):
        """When no changes are made and no rules, validate is skipped."""
        schema_diff_result = {
            "kind": "schema_diff",
            "subject": "dataframe",
            "summary": "schemas match",
            "metrics": {"type_changed_column_count": 0, "new_only_column_count": 0, "old_only_column_count": 0},
            "findings": [],
            "risks": [],
            "samples": {},
        }

        with patch(
            "odibi_anchor.tables.schema_diff_context.schema_diff_context",
            return_value=schema_diff_result,
        ):
            result = _evolve_workflow(
                df, target_schema=target_df, output_format="dict",
            )

        validate_steps = [s for s in result["steps"] if s["tool"] == "validate"]
        assert validate_steps[0]["status"] == "skipped"
        assert "no changes applied" in validate_steps[0]["reason"]

    def test_coerce_fix_no_issues_skips_validate(self, df, coerce_ctx_fixture):
        """When coerce_fix reports no columns fixed, validate is skipped."""
        coerce_fix_ctx = {
            "kind": "coerce_fix",
            "subject": "orders",
            "summary": "no fixes needed",
            "metrics": {"columns_fixed": 0, "rows_affected": 0},
            "findings": [],
            "risks": [],
            "samples": {},
        }

        with patch(
            "tools.coerce_fix_tool.coerce_fix_impl.coerce_fix_context",
            return_value=coerce_fix_ctx,
        ):
            result = _evolve_workflow(
                df, coerce_ctx=coerce_ctx_fixture, output_format="dict",
            )

        validate_steps = [s for s in result["steps"] if s["tool"] == "validate"]
        assert validate_steps[0]["status"] == "skipped"


class TestEvolveWorkflowContract:
    def test_output_keys(self, df, target_df):
        """Verify output contract has all required keys."""
        schema_diff_result = {
            "kind": "schema_diff",
            "subject": "dataframe",
            "summary": "no diff",
            "metrics": {"type_changed_column_count": 0, "new_only_column_count": 0, "old_only_column_count": 0},
            "findings": [],
            "risks": [],
            "samples": {},
        }

        with patch(
            "odibi_anchor.tables.schema_diff_context.schema_diff_context",
            return_value=schema_diff_result,
        ):
            result = _evolve_workflow(
                df, target_schema=target_df, output_format="dict",
            )

        assert result["kind"] == "workflow_evolve"
        assert "steps" in result
        assert "skipped" in result
        assert "metrics" in result
        assert "findings" in result
        assert result["metrics"]["has_type_changes"] is False
        assert result["metrics"]["dry_run"] is True

    def test_markdown_output(self, df, target_df):
        schema_diff_result = {
            "kind": "schema_diff",
            "subject": "dataframe",
            "summary": "no diff",
            "metrics": {"type_changed_column_count": 0, "new_only_column_count": 0, "old_only_column_count": 0},
            "findings": [],
            "risks": [],
            "samples": {},
        }

        with patch(
            "odibi_anchor.tables.schema_diff_context.schema_diff_context",
            return_value=schema_diff_result,
        ):
            result = _evolve_workflow(
                df, target_schema=target_df, output_format="markdown",
            )

        assert isinstance(result, str)
        assert "workflow_evolve" in result
