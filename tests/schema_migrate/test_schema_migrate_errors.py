"""Error-path tests for schema_migrate_context.

Covers:
- None schema_diff_ctx input
- Wrong type (str instead of dict)
- Missing target_table
- Corrupt schema_diff_ctx (missing expected keys)
- Empty schema_diff_ctx dict
- DataFrame passed instead of dict
- Invalid output_format
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from tools.schema_migrate_tool.schema_migrate_impl import schema_migrate_context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_schema_diff_ctx():
    """Minimal valid schema_diff_ctx with all expected keys."""
    return {
        "kind": "schema_diff",
        "subject": "test_table",
        "summary": "1 column added",
        "metrics": {"columns_added": 1},
        "added": [{"column": "new_col", "type": "string"}],
        "removed": [],
        "type_changed": [],
        "likely_renames": [],
        "findings": [],
        "risks": [],
        "samples": {},
        "suggested_next_actions": [],
    }


# ---------------------------------------------------------------------------
# TestSchemaMigrateNoneAndMissingInputs
# ---------------------------------------------------------------------------

class TestSchemaMigrateNoneAndMissingInputs:
    def test_none_schema_diff_ctx_raises(self):
        """None schema_diff_ctx must raise ValueError."""
        with pytest.raises(ValueError, match="schema_diff_ctx is required"):
            schema_migrate_context(
                schema_diff_ctx=None,
                target_table="cat.sch.tbl",
                output_format="dict",
            )

    def test_missing_target_table_raises(self, minimal_schema_diff_ctx):
        """None target_table must raise ValueError."""
        with pytest.raises(ValueError, match="target_table is required"):
            schema_migrate_context(
                schema_diff_ctx=minimal_schema_diff_ctx,
                target_table=None,
                output_format="dict",
            )

    def test_empty_string_target_table_raises(self, minimal_schema_diff_ctx):
        """Blank target_table must raise ValueError."""
        with pytest.raises(ValueError, match="target_table is required"):
            schema_migrate_context(
                schema_diff_ctx=minimal_schema_diff_ctx,
                target_table="   ",
                output_format="dict",
            )


# ---------------------------------------------------------------------------
# TestSchemaMigrateWrongTypes
# ---------------------------------------------------------------------------

class TestSchemaMigrateWrongTypes:
    def test_string_schema_diff_ctx_raises(self):
        """Passing a string instead of dict must raise ValueError."""
        with pytest.raises(ValueError, match="must be a dict"):
            schema_migrate_context(
                schema_diff_ctx="not_a_dict",
                target_table="cat.sch.tbl",
                output_format="dict",
            )

    def test_dataframe_schema_diff_ctx_raises(self):
        """Passing a DataFrame instead of dict must raise ValueError."""
        df = pd.DataFrame({"col": [1, 2]})
        with pytest.raises(ValueError, match="must be a dict"):
            schema_migrate_context(
                schema_diff_ctx=df,
                target_table="cat.sch.tbl",
                output_format="dict",
            )

    def test_list_schema_diff_ctx_raises(self):
        """Passing a list instead of dict must raise ValueError."""
        with pytest.raises(ValueError, match="must be a dict"):
            schema_migrate_context(
                schema_diff_ctx=["added_col"],
                target_table="cat.sch.tbl",
                output_format="dict",
            )


# ---------------------------------------------------------------------------
# TestSchemaMigrateCorruptInput
# ---------------------------------------------------------------------------

class TestSchemaMigrateCorruptInput:
    def test_empty_dict_returns_error_context(self):
        """Empty dict (missing all keys) triggers the try/except and returns error context."""
        result = schema_migrate_context(
            schema_diff_ctx={},
            target_table="cat.sch.tbl",
            output_format="dict",
        )
        assert isinstance(result, dict)
        assert result["kind"] == "schema_migrate"
        # Empty dict → no columns added/removed/changed → no crash, valid empty plan
        # OR returns error context — either is acceptable
        assert "summary" in result

    def test_corrupt_type_changed_returns_error_context(self):
        """Corrupt type_changed entries (missing column/dtype keys) return error context."""
        corrupt_ctx = {
            "added": [],
            "removed": [],
            "type_changed": [{"not_the_expected_key": 999}],
            "likely_renames": [],
        }
        result = schema_migrate_context(
            schema_diff_ctx=corrupt_ctx,
            target_table="cat.sch.tbl",
            output_format="dict",
        )
        assert result["kind"] == "schema_migrate"
        assert "ERROR" in result["summary"]
        assert len(result["risks"]) > 0

    def test_corrupt_added_cols_returns_error_context(self):
        """Corrupt added entries (non-dict items) return error context."""
        corrupt_ctx = {
            "added": ["not_a_dict", 42],
            "removed": [],
            "type_changed": [],
            "likely_renames": [],
        }
        result = schema_migrate_context(
            schema_diff_ctx=corrupt_ctx,
            target_table="cat.sch.tbl",
            output_format="dict",
        )
        assert result["kind"] == "schema_migrate"
        assert "ERROR" in result["summary"]
        assert len(result["risks"]) > 0


# ---------------------------------------------------------------------------
# TestSchemaMigrateOutputContract
# ---------------------------------------------------------------------------

class TestSchemaMigrateOutputContract:
    def test_invalid_output_format_raises(self, minimal_schema_diff_ctx):
        """Invalid output_format must raise ValueError."""
        with pytest.raises(ValueError, match="output_format"):
            schema_migrate_context(
                schema_diff_ctx=minimal_schema_diff_ctx,
                target_table="cat.sch.tbl",
                output_format="html",
            )

    def test_valid_empty_plan_has_required_keys(self):
        """Empty schema_diff (no changes) returns a valid contract dict."""
        empty_diff = {
            "added": [],
            "removed": [],
            "type_changed": [],
            "likely_renames": [],
        }
        result = schema_migrate_context(
            schema_diff_ctx=empty_diff,
            target_table="cat.sch.tbl",
            output_format="dict",
        )
        assert isinstance(result, dict)
        for key in ("kind", "subject", "summary", "metrics", "findings"):
            assert key in result, f"Missing key: {key}"
        assert result["kind"] == "schema_migrate"
