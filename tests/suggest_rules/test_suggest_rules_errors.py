"""Error-path tests for suggest_rules_context.

Covers:
- None input (no profile_ctx, no df)
- Wrong type for profile_ctx (str, list, int)
- profile_ctx with missing column_profiles key
- Corrupt column_profiles (non-dict values)
- Invalid include_types value
- Empty DataFrame input
- DataFrame with only null columns
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from tools.suggest_rules_tool.suggest_rules_impl import suggest_rules_context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_profile_ctx():
    """Minimal valid profile context with column_profiles."""
    return {
        "kind": "profile_table",
        "subject": "test_df",
        "summary": "profiled 2 cols",
        "metrics": {"row_count": 3, "column_count": 2},
        "column_profiles": {
            "id": {
                "dtype": "int64",
                "null_pct": 0.0,
                "distinct_count": 3,
                "distinct_pct": 1.0,
                "min": 1,
                "max": 3,
                "value_counts": {"1": 1, "2": 1, "3": 1},
                "quality_flags": [],
            },
            "status": {
                "dtype": "object",
                "null_pct": 0.0,
                "distinct_count": 2,
                "distinct_pct": 0.67,
                "value_counts": {"active": 2, "inactive": 1},
                "quality_flags": [],
            },
        },
        "findings": [],
        "risks": [],
        "samples": {},
        "suggested_next_actions": [],
    }


# ---------------------------------------------------------------------------
# TestSuggestRulesNoneAndMissingInputs
# ---------------------------------------------------------------------------

class TestSuggestRulesNoneAndMissingInputs:
    def test_no_input_raises(self):
        """No profile_ctx and no df must raise ValueError."""
        with pytest.raises(ValueError, match="Must provide profile_ctx"):
            suggest_rules_context(output_format="dict")

    def test_none_profile_ctx_none_df_raises(self):
        """Explicit None for both must raise ValueError."""
        with pytest.raises(ValueError, match="Must provide profile_ctx"):
            suggest_rules_context(
                profile_ctx=None, df=None, output_format="dict"
            )


# ---------------------------------------------------------------------------
# TestSuggestRulesWrongTypes
# ---------------------------------------------------------------------------

class TestSuggestRulesWrongTypes:
    def test_string_profile_ctx_raises(self):
        """Passing a string as profile_ctx must raise TypeError."""
        with pytest.raises(TypeError, match="must resolve to a dict"):
            suggest_rules_context(
                profile_ctx="not_a_dict", output_format="dict"
            )

    def test_list_profile_ctx_raises(self):
        """Passing a list as profile_ctx must raise TypeError."""
        with pytest.raises(TypeError, match="must resolve to a dict"):
            suggest_rules_context(
                profile_ctx=["item1", "item2"], output_format="dict"
            )

    def test_int_profile_ctx_raises(self):
        """Passing an int as profile_ctx must raise TypeError."""
        with pytest.raises(TypeError, match="must resolve to a dict"):
            suggest_rules_context(
                profile_ctx=42, output_format="dict"
            )


# ---------------------------------------------------------------------------
# TestSuggestRulesInvalidIncludeTypes
# ---------------------------------------------------------------------------

class TestSuggestRulesInvalidIncludeTypes:
    def test_invalid_include_types_raises(self, minimal_profile_ctx):
        """Unrecognised rule type in include_types must raise ValueError."""
        with pytest.raises(ValueError, match="Invalid include_types"):
            suggest_rules_context(
                profile_ctx=minimal_profile_ctx,
                include_types=["not_a_real_rule_type"],
                output_format="dict",
            )

    def test_partially_invalid_include_types_raises(self, minimal_profile_ctx):
        """Mix of valid and invalid include_types must still raise ValueError."""
        with pytest.raises(ValueError, match="Invalid include_types"):
            suggest_rules_context(
                profile_ctx=minimal_profile_ctx,
                include_types=["not_null", "TOTALLY_WRONG"],
                output_format="dict",
            )


# ---------------------------------------------------------------------------
# TestSuggestRulesCorruptProfileCtx
# ---------------------------------------------------------------------------

class TestSuggestRulesCorruptProfileCtx:
    def test_missing_column_profiles_raises_value_error(self):
        """profile_ctx without column_profiles raises ValueError by design.

        column_profiles is a required key; suggest_rules explicitly raises
        ValueError when it is absent (this is intentional, not a bug).
        """
        bad_ctx = {
            "kind": "profile_table",
            "subject": "test",
            # Deliberately missing 'column_profiles'
        }
        with pytest.raises(ValueError, match="column_profiles"):
            suggest_rules_context(
                profile_ctx=bad_ctx, output_format="dict"
            )

    def test_corrupt_column_profiles_returns_error_context(self):
        """column_profiles with non-dict values returns error context, not traceback."""
        bad_ctx = {
            "kind": "profile_table",
            "subject": "test",
            "column_profiles": {
                "id": "this_should_be_a_dict_not_a_string",
            },
        }
        result = suggest_rules_context(
            profile_ctx=bad_ctx, output_format="dict"
        )
        assert isinstance(result, dict)
        assert result["kind"] == "suggest_rules"


# ---------------------------------------------------------------------------
# TestSuggestRulesEdgeCaseDataFrames
# ---------------------------------------------------------------------------

class TestSuggestRulesEdgeCaseDataFrames:
    def test_empty_dataframe_returns_context(self):
        """Empty DataFrame (0 rows) must not crash — returns context (possibly empty rules)."""
        empty_df = pd.DataFrame({"id": pd.Series([], dtype="int64"),
                                  "name": pd.Series([], dtype="object")})
        result = suggest_rules_context(df=empty_df, output_format="dict")
        assert isinstance(result, dict)
        assert result["kind"] == "suggest_rules"
        assert "rules" in result or "summary" in result

    def test_all_null_columns_dataframe_returns_context(self):
        """DataFrame where all values are null must return a valid context."""
        null_df = pd.DataFrame({
            "id": [None, None, None],
            "status": [np.nan, np.nan, np.nan],
        })
        result = suggest_rules_context(df=null_df, output_format="dict")
        assert isinstance(result, dict)
        assert result["kind"] == "suggest_rules"
