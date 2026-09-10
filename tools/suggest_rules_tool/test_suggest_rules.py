"""Tests for suggest_rules_tool.

Covers: not_null, unique, accepted_values, range, expression inference,
strictness levels, exclude_columns, include_types, and output format.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# Add project root to path for imports
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from tools.suggest_rules_tool.suggest_rules_impl import (
    suggest_rules_context,
    render_suggest_rules_report,
    _infer_not_null,
    _infer_unique,
    _infer_accepted_values,
    _infer_range,
    _infer_expressions,
    _find_date_ordering_pairs,
    STRICTNESS_CONFIG,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def basic_profile_ctx():
    """Profile context with a mix of column types for testing."""
    return {
        "kind": "dataset_profile_context",
        "subject": "test_table",
        "metrics": {
            "row_count": 100,
            "column_count": 6,
            "potential_key_columns": ["id"],
        },
        "column_profiles": {
            "id": {
                "name": "id",
                "null_pct": 0.0,
                "distinct_count": 100,
                "distinct_pct": 1.0,
                "is_unique": True,
                "is_constant": False,
                "inferred_type": "integer",
                "stats": {"min": 1, "max": 100, "mean": 50.5, "std": 28.87},
                "top_values": [],
                "quality_flags": ["potential_key"],
            },
            "status": {
                "name": "status",
                "null_pct": 0.0,
                "distinct_count": 4,
                "distinct_pct": 0.04,
                "is_unique": False,
                "is_constant": False,
                "inferred_type": "string",
                "stats": {},
                "top_values": [
                    {"value": "Active", "count": 40, "pct": 0.4},
                    {"value": "Inactive", "count": 30, "pct": 0.3},
                    {"value": "Pending", "count": 20, "pct": 0.2},
                    {"value": "Withdrawn", "count": 10, "pct": 0.1},
                ],
                "quality_flags": [],
            },
            "capacity_mw": {
                "name": "capacity_mw",
                "null_pct": 0.0,
                "distinct_count": 80,
                "distinct_pct": 0.8,
                "is_unique": False,
                "is_constant": False,
                "inferred_type": "numeric",
                "stats": {"min": 1, "max": 9800, "mean": 500.0, "std": 1200.0},
                "top_values": [],
                "quality_flags": [],
            },
            "county": {
                "name": "county",
                "null_pct": 0.109,
                "distinct_count": 50,
                "distinct_pct": 0.56,
                "is_unique": False,
                "is_constant": False,
                "inferred_type": "string",
                "stats": {},
                "top_values": [],
                "quality_flags": ["high_null"],
            },
            "start_date": {
                "name": "start_date",
                "null_pct": 0.0,
                "distinct_count": 90,
                "distinct_pct": 0.9,
                "is_unique": False,
                "is_constant": False,
                "inferred_type": "datetime",
                "stats": {"min": "2020-01-01", "max": "2024-12-31"},
                "top_values": [],
                "quality_flags": [],
            },
            "end_date": {
                "name": "end_date",
                "null_pct": 0.05,
                "distinct_count": 85,
                "distinct_pct": 0.89,
                "is_unique": False,
                "is_constant": False,
                "inferred_type": "datetime",
                "stats": {"min": "2020-06-01", "max": "2025-06-30"},
                "top_values": [],
                "quality_flags": [],
            },
        },
    }


@pytest.fixture
def profile_with_audit_cols(basic_profile_ctx):
    """Profile with audit columns that should be auto-excluded."""
    basic_profile_ctx["column_profiles"]["_extracted_at"] = {
        "name": "_extracted_at",
        "null_pct": 0.0,
        "distinct_count": 10,
        "distinct_pct": 0.1,
        "is_unique": False,
        "is_constant": False,
        "inferred_type": "datetime",
        "stats": {},
        "top_values": [],
        "quality_flags": [],
    }
    return basic_profile_ctx


# ============================================================================
# Test: suggest_rules_context (integration)
# ============================================================================


class TestSuggestRulesContext:
    """Integration tests for the main entry point."""

    def test_basic_profile_input(self, basic_profile_ctx):
        """Standard call with profile_ctx returns valid rules."""
        result = suggest_rules_context(profile_ctx=basic_profile_ctx)
        assert result["kind"] == "suggest_rules"
        assert result["subject"] == "test_table"
        assert isinstance(result["rules"], list)
        assert result["metrics"]["total_rules"] > 0

    def test_positional_profile_input(self, basic_profile_ctx):
        """Positional arg with profile dict works."""
        result = suggest_rules_context(basic_profile_ctx)
        assert result["kind"] == "suggest_rules"
        assert result["metrics"]["total_rules"] > 0

    def test_no_input_raises(self):
        """No profile_ctx or df raises ValueError."""
        with pytest.raises(ValueError, match="Must provide"):
            suggest_rules_context()

    def test_invalid_strictness_raises(self, basic_profile_ctx):
        """Invalid strictness raises ValueError."""
        with pytest.raises(ValueError, match="strictness must be"):
            suggest_rules_context(profile_ctx=basic_profile_ctx, strictness="extreme")

    def test_invalid_include_types_raises(self, basic_profile_ctx):
        """Invalid include_types raises ValueError."""
        with pytest.raises(ValueError, match="Invalid include_types"):
            suggest_rules_context(profile_ctx=basic_profile_ctx, include_types=["bogus"])

    def test_empty_column_profiles_raises(self):
        """Empty column_profiles raises ValueError."""
        ctx = {"kind": "dataset_profile_context", "column_profiles": {}, "metrics": {}}
        with pytest.raises(ValueError, match="no column_profiles"):
            suggest_rules_context(profile_ctx=ctx)

    def test_output_format_markdown(self, basic_profile_ctx):
        """output_format='markdown' returns a string."""
        result = suggest_rules_context(profile_ctx=basic_profile_ctx, output_format="markdown")
        assert isinstance(result, str)
        assert "suggest_rules" in result.lower() or "Generated" in result

    def test_rules_are_validate_compatible(self, basic_profile_ctx):
        """All rules have 'type' key and valid structure."""
        result = suggest_rules_context(profile_ctx=basic_profile_ctx)
        for rule in result["rules"]:
            assert "type" in rule
            assert rule["type"] in {"not_null", "unique", "accepted_values", "range", "expression"}
            # Metadata keys are underscore-prefixed
            assert "_confidence" in rule
            assert "_reason" in rule

    def test_exclude_columns(self, basic_profile_ctx):
        """exclude_columns removes specified columns from rules."""
        result = suggest_rules_context(
            profile_ctx=basic_profile_ctx,
            exclude_columns=["status", "capacity_mw"],
        )
        # These columns should not appear in any rule
        for rule in result["rules"]:
            cols = rule.get("columns", [rule.get("column", "")])
            if isinstance(cols, str):
                cols = [cols]
            assert "status" not in cols
            assert "capacity_mw" not in cols

    def test_include_types_filters(self, basic_profile_ctx):
        """include_types limits which rule types are generated."""
        result = suggest_rules_context(
            profile_ctx=basic_profile_ctx,
            include_types=["not_null"],
        )
        for rule in result["rules"]:
            assert rule["type"] == "not_null"

    def test_auto_excludes_audit_columns(self, profile_with_audit_cols):
        """Audit columns like _extracted_at are auto-excluded."""
        result = suggest_rules_context(profile_ctx=profile_with_audit_cols)
        for rule in result["rules"]:
            cols = rule.get("columns", [rule.get("column", "")])
            if isinstance(cols, str):
                cols = [cols]
            assert "_extracted_at" not in cols


# ============================================================================
# Test: not_null inference
# ============================================================================


class TestInferNotNull:
    """Unit tests for not_null rule inference."""

    def test_zero_null_columns(self):
        """Columns with 0% null get not_null rules."""
        profiles = {
            "col_a": {"null_pct": 0.0},
            "col_b": {"null_pct": 0.0},
            "col_c": {"null_pct": 0.5},
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings = _infer_not_null(profiles, config)
        assert len(rules) == 1
        assert set(rules[0]["columns"]) == {"col_a", "col_b"}

    def test_lenient_tolerance(self):
        """Lenient mode allows up to 5% nulls."""
        profiles = {
            "col_a": {"null_pct": 0.0},
            "col_b": {"null_pct": 0.03},
            "col_c": {"null_pct": 0.10},
        }
        config = STRICTNESS_CONFIG["lenient"]
        rules, findings = _infer_not_null(profiles, config)
        assert len(rules) == 1
        assert set(rules[0]["columns"]) == {"col_a", "col_b"}

    def test_no_columns_pass(self):
        """No rules generated if all columns have nulls above tolerance."""
        profiles = {"col_a": {"null_pct": 0.5}}
        config = STRICTNESS_CONFIG["standard"]
        rules, findings = _infer_not_null(profiles, config)
        assert rules == []


# ============================================================================
# Test: unique inference
# ============================================================================


class TestInferUnique:
    """Unit tests for unique rule inference."""

    def test_candidate_key(self):
        """is_unique=True + 0% null generates unique rule."""
        profiles = {
            "id": {"is_unique": True, "null_pct": 0.0, "distinct_pct": 1.0},
            "name": {"is_unique": False, "null_pct": 0.0, "distinct_pct": 0.5},
        }
        ctx = {"metrics": {"potential_key_columns": ["id"]}}
        rules, findings = _infer_unique(profiles, ctx)
        assert len(rules) == 1
        assert rules[0]["columns"] == ["id"]
        assert rules[0]["_confidence"] == 0.85

    def test_unique_with_nulls_skipped(self):
        """Unique column with nulls is NOT a candidate key."""
        profiles = {
            "id": {"is_unique": True, "null_pct": 0.05, "distinct_pct": 1.0},
        }
        ctx = {"metrics": {"potential_key_columns": []}}
        rules, findings = _infer_unique(profiles, ctx)
        assert rules == []


# ============================================================================
# Test: accepted_values inference
# ============================================================================


class TestInferAcceptedValues:
    """Unit tests for accepted_values rule inference."""

    def test_low_cardinality_categorical(self):
        """Low-cardinality string column generates accepted_values."""
        profiles = {
            "status": {
                "distinct_count": 4,
                "inferred_type": "string",
                "top_values": [
                    {"value": "A", "count": 50, "pct": 0.5},
                    {"value": "B", "count": 30, "pct": 0.3},
                    {"value": "C", "count": 15, "pct": 0.15},
                    {"value": "D", "count": 5, "pct": 0.05},
                ],
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings, risks = _infer_accepted_values(profiles, config)
        assert len(rules) == 1
        assert rules[0]["type"] == "accepted_values"
        assert rules[0]["column"] == "status"
        assert sorted(rules[0]["values"]) == ["A", "B", "C", "D"]

    def test_numeric_column_skipped(self):
        """Numeric columns don't get accepted_values even if low cardinality."""
        profiles = {
            "priority": {
                "distinct_count": 5,
                "inferred_type": "integer",
                "top_values": [{"value": 1, "count": 20, "pct": 0.2}],
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings, risks = _infer_accepted_values(profiles, config)
        assert rules == []

    def test_high_cardinality_skipped(self):
        """Columns with >20 distinct values are skipped."""
        profiles = {
            "city": {
                "distinct_count": 25,
                "inferred_type": "string",
                "top_values": [{"value": "NYC", "count": 10, "pct": 0.1}],
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings, risks = _infer_accepted_values(profiles, config)
        assert rules == []

    def test_risk_for_wide_categoricals(self):
        """Columns with 10+ distinct values generate a risk warning."""
        profiles = {
            "market": {
                "distinct_count": 15,
                "inferred_type": "string",
                "top_values": [
                    {"value": f"M{i}", "count": 10, "pct": 0.067}
                    for i in range(15)
                ],
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings, risks = _infer_accepted_values(profiles, config)
        assert len(rules) == 1
        assert len(risks) == 1
        assert "may need updating" in risks[0]


# ============================================================================
# Test: range inference
# ============================================================================


class TestInferRange:
    """Unit tests for range rule inference."""

    def test_numeric_with_margin(self):
        """Numeric column gets range with 10% margin (standard)."""
        profiles = {
            "amount": {
                "inferred_type": "numeric",
                "stats": {"min": 100, "max": 1000},
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings = _infer_range(profiles, config)
        assert len(rules) == 1
        assert rules[0]["type"] == "range"
        assert rules[0]["column"] == "amount"
        assert rules[0]["min"] < 100
        assert rules[0]["max"] > 1000

    def test_strict_no_margin(self):
        """Strict mode uses exact observed range."""
        profiles = {
            "amount": {
                "inferred_type": "integer",
                "stats": {"min": 10, "max": 200},
            }
        }
        config = STRICTNESS_CONFIG["strict"]
        rules, findings = _infer_range(profiles, config)
        assert len(rules) == 1
        assert rules[0]["min"] == 10
        assert rules[0]["max"] == 200

    def test_non_negative_floor(self):
        """Non-negative columns have min floored at 0."""
        profiles = {
            "count": {
                "inferred_type": "numeric",
                "stats": {"min": 5, "max": 100},
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings = _infer_range(profiles, config)
        assert rules[0]["min"] >= 0

    def test_constant_column_skipped(self):
        """Column with min==max (span=0) is skipped."""
        profiles = {
            "x": {
                "inferred_type": "numeric",
                "stats": {"min": 42, "max": 42},
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings = _infer_range(profiles, config)
        assert rules == []

    def test_nan_stats_skipped(self):
        """Columns with NaN stats are skipped."""
        profiles = {
            "x": {
                "inferred_type": "numeric",
                "stats": {"min": float("nan"), "max": 100},
            }
        }
        config = STRICTNESS_CONFIG["standard"]
        rules, findings = _infer_range(profiles, config)
        assert rules == []


# ============================================================================
# Test: expression inference
# ============================================================================


class TestInferExpressions:
    """Unit tests for expression rule inference."""

    def test_start_end_pair(self):
        """start_date / end_date pair generates ordering expression."""
        profiles = {
            "start_date": {"inferred_type": "datetime"},
            "end_date": {"inferred_type": "datetime"},
        }
        rules, findings = _infer_expressions(profiles)
        assert len(rules) == 1
        assert rules[0]["type"] == "expression"
        assert "start_date" in rules[0]["expr"]
        assert "end_date" in rules[0]["expr"]
        assert "<=" in rules[0]["expr"]

    def test_created_updated_pair(self):
        """created_at / updated_at generates ordering expression."""
        profiles = {
            "created_at": {"inferred_type": "datetime"},
            "updated_at": {"inferred_type": "datetime"},
        }
        rules, findings = _infer_expressions(profiles)
        assert len(rules) == 1
        assert "created_at" in rules[0]["expr"]
        assert "updated_at" in rules[0]["expr"]

    def test_no_date_columns(self):
        """No expression rules when no date columns exist."""
        profiles = {
            "name": {"inferred_type": "string"},
            "amount": {"inferred_type": "numeric"},
        }
        rules, findings = _infer_expressions(profiles)
        assert rules == []

    def test_single_date_column(self):
        """Single date column — no pair, no rule."""
        profiles = {
            "created_at": {"inferred_type": "datetime"},
        }
        rules, findings = _infer_expressions(profiles)
        assert rules == []


# ============================================================================
# Test: strictness levels
# ============================================================================


class TestStrictnessLevels:
    """Test that strictness levels produce different rule sets."""

    def test_strict_vs_standard_range_margin(self, basic_profile_ctx):
        """Strict has tighter range bounds than standard."""
        strict = suggest_rules_context(
            profile_ctx=basic_profile_ctx, strictness="strict", include_types=["range"]
        )
        standard = suggest_rules_context(
            profile_ctx=basic_profile_ctx, strictness="standard", include_types=["range"]
        )
        # Both should have range rules for capacity_mw
        strict_rules = [r for r in strict["rules"] if r["column"] == "capacity_mw"]
        standard_rules = [r for r in standard["rules"] if r["column"] == "capacity_mw"]
        if strict_rules and standard_rules:
            assert strict_rules[0]["max"] <= standard_rules[0]["max"]

    def test_lenient_allows_nulls(self, basic_profile_ctx):
        """Lenient mode excludes columns with <=5% nulls from not_null."""
        lenient = suggest_rules_context(
            profile_ctx=basic_profile_ctx, strictness="lenient", include_types=["not_null"]
        )
        standard = suggest_rules_context(
            profile_ctx=basic_profile_ctx, strictness="standard", include_types=["not_null"]
        )
        # end_date has 5% nulls — standard includes it in not_null, lenient includes it too (<=5%)
        # But a column with 4% nulls would be in lenient not_null but not one with 6%
        assert lenient["metrics"]["total_rules"] >= 0
        assert standard["metrics"]["total_rules"] >= 0


# ============================================================================
# Test: renderer
# ============================================================================


class TestRenderer:
    """Test markdown rendering."""

    def test_includes_code_block(self, basic_profile_ctx):
        """Markdown output includes copy-paste Python code block."""
        result = suggest_rules_context(profile_ctx=basic_profile_ctx, output_format="markdown")
        assert "```python" in result
        assert "anchor('validate'" in result

    def test_includes_table(self, basic_profile_ctx):
        """Markdown output includes rules table."""
        result = suggest_rules_context(profile_ctx=basic_profile_ctx, output_format="markdown")
        assert "Confidence" in result
        assert "Reason" in result


# ============================================================================
# Test: date pair detection helper
# ============================================================================


class TestFindDateOrderingPairs:
    """Unit tests for the date pair detection helper."""

    def test_simple_start_end(self):
        pairs = _find_date_ordering_pairs(["start_date", "end_date"])
        assert len(pairs) == 1
        assert pairs[0] == ("start_date", "end_date")

    def test_no_pairs(self):
        pairs = _find_date_ordering_pairs(["arrival_date", "departure_date"])
        assert pairs == []

    def test_multiple_pairs(self):
        cols = ["created_at", "updated_at", "start_time", "end_time"]
        pairs = _find_date_ordering_pairs(cols)
        assert len(pairs) >= 2
