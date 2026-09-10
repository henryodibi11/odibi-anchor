"""Comprehensive tests for _validation_checks.py.

Tests cover:
- _evaluate_rule_pandas: all rule types with pass and fail cases
- _validate_pandas: batch evaluation, output contract, edge cases
- Schema mismatch handling
- Sample collection
"""

import pandas as pd
import numpy as np
import pytest

from odibi_anchor.validation._validation_checks import (
    _evaluate_rule_pandas,
    _validate_pandas,
)


# =============================================================================
# Section 1: _evaluate_rule_pandas — rule type tests
# =============================================================================


class TestEvaluateRuleNotNull:
    """Test not_null rule type."""

    def test_not_null__all_present_passes(self):
        df = pd.DataFrame({"name": ["Alice", "Bob", "Charlie"]})
        rule = {"type": "not_null", "columns": ["name"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_not_null__with_nulls_fails(self):
        df = pd.DataFrame({"name": ["Alice", None, "Charlie"]})
        rule = {"type": "not_null", "columns": ["name"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [True, False, True]

    def test_not_null__multi_column(self):
        df = pd.DataFrame({"a": [1, None, 3], "b": ["x", "y", None]})
        rule = {"type": "not_null", "columns": ["a", "b"]}
        mask = _evaluate_rule_pandas(df, rule)
        # Row 0: both present, Row 1: a is null, Row 2: b is null
        assert mask.tolist() == [True, False, False]

    def test_not_null__missing_column_passes_all(self):
        df = pd.DataFrame({"name": ["Alice", "Bob"]})
        rule = {"type": "not_null", "columns": ["nonexistent"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_not_null__empty_columns_passes_all(self):
        df = pd.DataFrame({"name": ["Alice"]})
        rule = {"type": "not_null", "columns": []}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()


class TestEvaluateRuleUnique:
    """Test unique rule type."""

    def test_unique__all_distinct_passes(self):
        df = pd.DataFrame({"id": [1, 2, 3]})
        rule = {"type": "unique", "columns": ["id"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_unique__with_duplicates_fails(self):
        df = pd.DataFrame({"id": [1, 2, 2, 3]})
        rule = {"type": "unique", "columns": ["id"]}
        mask = _evaluate_rule_pandas(df, rule)
        # Both duplicate rows should fail (keep=False)
        assert mask.tolist() == [True, False, False, True]

    def test_unique__composite_key(self):
        df = pd.DataFrame({"a": [1, 1, 2], "b": ["x", "y", "x"]})
        rule = {"type": "unique", "columns": ["a", "b"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()  # All combinations unique

    def test_unique__composite_key_with_dupe(self):
        df = pd.DataFrame({"a": [1, 1, 2], "b": ["x", "x", "y"]})
        rule = {"type": "unique", "columns": ["a", "b"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [False, False, True]

    def test_unique__missing_column_passes_all(self):
        df = pd.DataFrame({"id": [1, 1]})
        rule = {"type": "unique", "columns": ["nonexistent"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()


class TestEvaluateRuleAcceptedValues:
    """Test accepted_values rule type."""

    def test_accepted_values__all_in_set_passes(self):
        df = pd.DataFrame({"status": ["active", "retired", "active"]})
        rule = {"type": "accepted_values", "column": "status", "values": ["active", "retired", "new"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_accepted_values__invalid_value_fails(self):
        df = pd.DataFrame({"status": ["active", "unknown", "retired"]})
        rule = {"type": "accepted_values", "column": "status", "values": ["active", "retired"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [True, False, True]

    def test_accepted_values__missing_column_passes_all(self):
        df = pd.DataFrame({"other": [1, 2]})
        rule = {"type": "accepted_values", "column": "status", "values": ["a"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_accepted_values__empty_values_fails_all(self):
        df = pd.DataFrame({"status": ["active"]})
        rule = {"type": "accepted_values", "column": "status", "values": []}
        mask = _evaluate_rule_pandas(df, rule)
        assert not mask.any()


class TestEvaluateRuleRange:
    """Test range rule type."""

    def test_range__within_bounds_passes(self):
        df = pd.DataFrame({"age": [18, 25, 65]})
        rule = {"type": "range", "column": "age", "min": 0, "max": 120}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_range__below_min_fails(self):
        df = pd.DataFrame({"age": [-1, 18, 65]})
        rule = {"type": "range", "column": "age", "min": 0, "max": 120}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [False, True, True]

    def test_range__above_max_fails(self):
        df = pd.DataFrame({"age": [18, 121, 65]})
        rule = {"type": "range", "column": "age", "min": 0, "max": 120}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [True, False, True]

    def test_range__min_only(self):
        df = pd.DataFrame({"val": [-5, 0, 10]})
        rule = {"type": "range", "column": "val", "min": 0}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [False, True, True]

    def test_range__max_only(self):
        df = pd.DataFrame({"val": [5, 10, 15]})
        rule = {"type": "range", "column": "val", "max": 10}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [True, True, False]

    def test_range__missing_column_passes_all(self):
        df = pd.DataFrame({"other": [1, 2]})
        rule = {"type": "range", "column": "val", "min": 0, "max": 10}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_range__type_mismatch_passes_all(self):
        df = pd.DataFrame({"val": ["not", "a", "number"]})
        rule = {"type": "range", "column": "val", "min": 0, "max": 10}
        mask = _evaluate_rule_pandas(df, rule)
        # Type mismatch is handled gracefully — returns all True
        assert mask.all()


class TestEvaluateRuleRegex:
    """Test regex rule type."""

    def test_regex__matching_pattern_passes(self):
        df = pd.DataFrame({"email": ["a@b.com", "x@y.org"]})
        rule = {"type": "regex", "column": "email", "pattern": r".+@.+\..+"}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_regex__non_matching_fails(self):
        df = pd.DataFrame({"email": ["valid@test.com", "invalid", "also@ok.net"]})
        rule = {"type": "regex", "column": "email", "pattern": r".+@.+\..+"}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [True, False, True]

    def test_regex__null_values_pass(self):
        df = pd.DataFrame({"email": ["a@b.com", None, "c@d.org"]})
        rule = {"type": "regex", "column": "email", "pattern": r".+@.+\..+"}
        mask = _evaluate_rule_pandas(df, rule)
        # Nulls should pass (not considered violations)
        assert mask.all()

    def test_regex__missing_column_passes_all(self):
        df = pd.DataFrame({"other": ["abc"]})
        rule = {"type": "regex", "column": "email", "pattern": r".+"}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_regex__invalid_pattern_raises(self):
        """re.error propagates — only TypeError/ValueError are caught."""
        import re as re_module
        df = pd.DataFrame({"val": ["abc"]})
        rule = {"type": "regex", "column": "val", "pattern": r"[invalid"}
        with pytest.raises(re_module.error):
            _evaluate_rule_pandas(df, rule)


class TestEvaluateRuleCustomSql:
    """Test custom_sql rule type (pandas query)."""

    def test_custom_sql__passing_condition(self):
        df = pd.DataFrame({"amount": [10, 20, 30]})
        rule = {"type": "custom_sql", "condition": "amount > 0"}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_custom_sql__failing_condition(self):
        df = pd.DataFrame({"amount": [-5, 10, 20]})
        rule = {"type": "custom_sql", "condition": "amount > 0"}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [False, True, True]

    def test_custom_sql__invalid_expression_passes_all(self):
        df = pd.DataFrame({"amount": [10]})
        rule = {"type": "custom_sql", "condition": "INVALID SYNTAX !!!"}
        mask = _evaluate_rule_pandas(df, rule)
        # Graceful error handling
        assert mask.all()

    def test_custom_sql__complex_condition(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [10, 5, 8]})
        rule = {"type": "custom_sql", "condition": "a < b"}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()


class TestEvaluateRuleReferentialIntegrity:
    """Test referential_integrity rule type."""

    def test_referential_integrity__all_in_ref_passes(self):
        df = pd.DataFrame({"status_id": [1, 2, 3]})
        rule = {"type": "referential_integrity", "column": "status_id", "reference_values": [1, 2, 3, 4]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_referential_integrity__orphan_fails(self):
        df = pd.DataFrame({"status_id": [1, 99, 3]})
        rule = {"type": "referential_integrity", "column": "status_id", "reference_values": [1, 2, 3]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [True, False, True]

    def test_referential_integrity__nulls_pass(self):
        df = pd.DataFrame({"fk": [1, None, 3]})
        rule = {"type": "referential_integrity", "column": "fk", "reference_values": [1, 3]}
        mask = _evaluate_rule_pandas(df, rule)
        # Nulls are acceptable (nullable foreign key)
        assert mask.all()

    def test_referential_integrity__empty_ref_passes_all(self):
        df = pd.DataFrame({"fk": [1, 2]})
        rule = {"type": "referential_integrity", "column": "fk", "reference_values": []}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_referential_integrity__missing_column_passes_all(self):
        df = pd.DataFrame({"other": [1, 2]})
        rule = {"type": "referential_integrity", "column": "fk", "reference_values": [1, 2]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()


class TestEvaluateRuleUnknown:
    """Test unknown rule types default to pass."""

    def test_unknown_rule_type__passes_all(self):
        df = pd.DataFrame({"x": [1, 2, 3]})
        rule = {"type": "totally_unknown_rule"}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()


# =============================================================================
# Section 2: _validate_pandas — integration tests
# =============================================================================


class TestValidatePandas:
    """Test the full _validate_pandas orchestration."""

    def test_validate__all_rules_pass(self):
        df = pd.DataFrame({"id": [1, 2, 3], "name": ["A", "B", "C"]})
        rules = [
            {"type": "not_null", "columns": ["id", "name"]},
            {"type": "unique", "columns": ["id"]},
        ]
        ctx = _validate_pandas(df, rules, subject="test_table", severity_map=None, sample_failures=5)

        assert ctx["kind"] == "validation_summary_context"
        assert ctx["metrics"]["rules_passed"] == 2
        assert ctx["metrics"]["rules_failed"] == 0
        assert ctx["metrics"]["is_promotion_safe"] is True

    def test_validate__rule_failure_detected(self):
        df = pd.DataFrame({"id": [1, 1, 3], "name": ["A", None, "C"]})
        rules = [
            {"type": "not_null", "columns": ["name"]},
            {"type": "unique", "columns": ["id"]},
        ]
        ctx = _validate_pandas(df, rules, subject="test_table", severity_map=None, sample_failures=5)

        assert ctx["metrics"]["rules_failed"] == 2
        assert ctx["metrics"]["rules_passed"] == 0

    def test_validate__sample_failures_collected(self):
        df = pd.DataFrame({"val": [1, -1, 2, -2, 3]})
        rules = [{"type": "range", "column": "val", "min": 0}]
        ctx = _validate_pandas(df, rules, subject="test", severity_map=None, sample_failures=2)

        failed_rule = ctx["rules"][0]
        assert not failed_rule["passed"]
        assert failed_rule["failed_count"] == 2
        assert len(failed_rule["sample_failures"]) <= 2

    def test_validate__empty_dataframe_all_pass(self):
        df = pd.DataFrame(columns=["id", "name"])
        rules = [
            {"type": "not_null", "columns": ["id"]},
            {"type": "unique", "columns": ["id"]},
        ]
        ctx = _validate_pandas(df, rules, subject="empty", severity_map=None, sample_failures=5)

        assert ctx["metrics"]["rules_passed"] == 2
        assert ctx["metrics"]["total_rows"] == 0

    def test_validate__no_rules(self):
        df = pd.DataFrame({"id": [1, 2]})
        ctx = _validate_pandas(df, [], subject="test", severity_map=None, sample_failures=5)

        assert ctx["metrics"]["rules_passed"] == 0
        assert ctx["metrics"]["rules_failed"] == 0
        assert ctx["metrics"]["total_rows"] == 2

    def test_validate__severity_map_applied(self):
        df = pd.DataFrame({"id": [1, 1]})
        rules = [{"type": "unique", "columns": ["id"]}]
        severity_map = {"unique_id": "blocker"}
        ctx = _validate_pandas(df, rules, subject="test", severity_map=severity_map, sample_failures=5)

        # The rule should fail
        failed_rule = ctx["rules"][0]
        assert not failed_rule["passed"]

    def test_validate__mixed_pass_fail(self):
        df = pd.DataFrame({"id": [1, 2, 3], "val": [10, -5, 20]})
        rules = [
            {"type": "not_null", "columns": ["id"]},  # passes
            {"type": "range", "column": "val", "min": 0},  # fails (val=-5)
        ]
        ctx = _validate_pandas(df, rules, subject="test", severity_map=None, sample_failures=5)

        assert ctx["metrics"]["rules_passed"] == 1
        assert ctx["metrics"]["rules_failed"] == 1

    def test_validate__output_contract_keys(self):
        df = pd.DataFrame({"id": [1]})
        rules = [{"type": "not_null", "columns": ["id"]}]
        ctx = _validate_pandas(df, rules, subject="test", severity_map=None, sample_failures=5)

        required_keys = {"kind", "subject", "summary", "metrics", "findings", "risks", "suggested_next_actions"}
        assert required_keys <= set(ctx.keys())

    def test_validate__metrics_contract(self):
        df = pd.DataFrame({"id": [1, 2], "name": ["A", None]})
        rules = [{"type": "not_null", "columns": ["name"]}]
        ctx = _validate_pandas(df, rules, subject="test", severity_map=None, sample_failures=5)

        m = ctx["metrics"]
        assert "total_rows" in m
        assert "rules_passed" in m
        assert "rules_failed" in m
        assert "is_promotion_safe" in m
        assert "overall_pass_rate" in m
        assert isinstance(m["total_rows"], int)
        assert isinstance(m["overall_pass_rate"], float)
        # Rule results live at top level ctx["rules"]
        assert "rules" in ctx
        assert isinstance(ctx["rules"], list)

    def test_validate__rule_result_structure(self):
        df = pd.DataFrame({"id": [1, None, 3]})
        rules = [{"type": "not_null", "columns": ["id"]}]
        ctx = _validate_pandas(df, rules, subject="test", severity_map=None, sample_failures=5)

        rr = ctx["rules"][0]
        assert "rule_id" in rr
        assert "rule_type" in rr
        assert "passed" in rr
        assert "failed_count" in rr
        assert "failed_rate" in rr
        assert "sample_failures" in rr
        assert rr["rule_type"] == "not_null"
        assert rr["failed_count"] == 1

    def test_validate__schema_mismatch_handled(self):
        """Rule referencing wrong type should be error, not crash."""
        df = pd.DataFrame({"name": ["Alice", "Bob"]})
        # range rule on a string column — schema mismatch
        rules = [{"type": "range", "column": "name", "min": 0, "max": 100}]
        ctx = _validate_pandas(df, rules, subject="test", severity_map=None, sample_failures=5)

        # Should complete without crash — range on string returns all-True (graceful)
        assert ctx["kind"] == "validation_summary_context"


# =============================================================================
# Section 3: Edge cases for _evaluate_rule_pandas
# =============================================================================


class TestEvaluateRuleEdgeCases:
    """Edge cases for individual rule evaluation."""

    def test_single_row_dataframe__not_null(self):
        df = pd.DataFrame({"a": [None]})
        rule = {"type": "not_null", "columns": ["a"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.tolist() == [False]

    def test_single_row_dataframe__unique_always_passes(self):
        df = pd.DataFrame({"id": [1]})
        rule = {"type": "unique", "columns": ["id"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()

    def test_empty_dataframe__all_rules_produce_empty_series(self):
        df = pd.DataFrame(columns=["id", "val"])
        for rule in [
            {"type": "not_null", "columns": ["id"]},
            {"type": "unique", "columns": ["id"]},
            {"type": "accepted_values", "column": "val", "values": [1]},
            {"type": "range", "column": "val", "min": 0},
            {"type": "regex", "column": "val", "pattern": r".*"},
            {"type": "custom_sql", "condition": "val > 0"},
            {"type": "referential_integrity", "column": "val", "reference_values": [1]},
        ]:
            mask = _evaluate_rule_pandas(df, rule)
            assert len(mask) == 0

    def test_large_dataframe__not_null_performance(self):
        import time
        n = 100_000
        df = pd.DataFrame({"id": range(n), "val": [None if i % 100 == 0 else i for i in range(n)]})
        rule = {"type": "not_null", "columns": ["val"]}

        start = time.time()
        mask = _evaluate_rule_pandas(df, rule)
        elapsed = time.time() - start

        assert elapsed < 2.0
        assert (~mask).sum() == 1000  # every 100th is null

    def test_accepted_values__with_nan_handling(self):
        df = pd.DataFrame({"status": ["active", np.nan, "retired"]})
        rule = {"type": "accepted_values", "column": "status", "values": ["active", "retired"]}
        mask = _evaluate_rule_pandas(df, rule)
        # NaN is not in the accepted values list
        assert mask.tolist() == [True, False, True]

    def test_range__boundary_values(self):
        df = pd.DataFrame({"val": [0, 10]})
        rule = {"type": "range", "column": "val", "min": 0, "max": 10}
        mask = _evaluate_rule_pandas(df, rule)
        # Boundaries are inclusive (>= min, <= max)
        assert mask.all()

    def test_not_null__with_empty_string(self):
        """Empty strings are not null."""
        df = pd.DataFrame({"name": ["", "Alice", ""]})
        rule = {"type": "not_null", "columns": ["name"]}
        mask = _evaluate_rule_pandas(df, rule)
        assert mask.all()  # Empty string is not null
