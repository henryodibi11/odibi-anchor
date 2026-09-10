"""Unit tests for cleanliness audit module."""

from __future__ import annotations

import pandas as pd
import pytest

from tools.table_profiler_tool.lib.cleanliness import audit_cleanliness
from tools.table_profiler_tool.lib.models import FormatIssue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def leading_space_df() -> pd.DataFrame:
    """Column with consistent leading spaces on most values."""
    vals = [" Alice"] * 60 + [" Bob"] * 30 + ["Carol"] * 10
    return pd.DataFrame({"name": vals})


@pytest.fixture
def trailing_space_df() -> pd.DataFrame:
    """Column with trailing spaces on most values."""
    vals = ["Alice "] * 60 + ["Bob "] * 30 + ["Carol"] * 10
    return pd.DataFrame({"name": vals})


@pytest.fixture
def null_like_df() -> pd.DataFrame:
    """Column with N/A, TBD, None sentinels stored as strings."""
    vals = ["N/A"] * 40 + ["TBD"] * 30 + ["Active"] * 30
    return pd.DataFrame({"status": vals})


@pytest.fixture
def null_like_case_insensitive_df() -> pd.DataFrame:
    """Null-like values in different cases."""
    vals = ["n/a"] * 30 + ["N/A"] * 30 + ["NULL"] * 20 + ["Valid"] * 20
    return pd.DataFrame({"status": vals})


@pytest.fixture
def empty_string_df() -> pd.DataFrame:
    """Column with empty and whitespace-only values."""
    vals = [""] * 40 + ["   "] * 30 + ["Active"] * 30
    return pd.DataFrame({"status": vals})


@pytest.fixture
def invisible_char_df() -> pd.DataFrame:
    """Column containing zero-width spaces."""
    # U+200B embedded in otherwise normal-looking strings
    vals = ["PRJ​001"] * 50 + ["PRJ001"] * 50
    return pd.DataFrame({"code": vals})


@pytest.fixture
def bom_char_df() -> pd.DataFrame:
    """Column containing BOM characters."""
    vals = ["﻿Value"] * 60 + ["NormalValue"] * 40
    return pd.DataFrame({"field": vals})


@pytest.fixture
def nbsp_df() -> pd.DataFrame:
    """Column containing non-breaking spaces."""
    vals = ["New York"] * 55 + ["Los Angeles"] * 45
    return pd.DataFrame({"city": vals})


@pytest.fixture
def clean_df() -> pd.DataFrame:
    """Clean string column — no issues expected."""
    return pd.DataFrame({"city": ["New York", "Chicago", "Houston"] * 33 + ["New York"]})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLeadingSpaces:
    """leading_spaces: flags values with leading whitespace."""

    def test_leading_spaces_flagged(self, leading_space_df: pd.DataFrame) -> None:
        issues = audit_cleanliness(leading_space_df)
        types = [i.issue_type for i in issues]
        assert "leading_spaces" in types

    def test_leading_spaces_flagged_on_string_dtype(self) -> None:
        """Regression: pandas StringDtype columns (future.infer_string / pandas
        3.0 default / explicit 'string') must be audited too — _string_columns
        previously matched only object dtype and silently skipped them."""
        vals = [" Alice"] * 60 + [" Bob"] * 30 + ["Carol"] * 10
        df = pd.DataFrame({"name": pd.array(vals, dtype="string")})
        assert df["name"].dtype == "string"
        types = [i.issue_type for i in audit_cleanliness(df)]
        assert "leading_spaces" in types

    def test_column_correct(self, leading_space_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(leading_space_df)
                  if i.issue_type == "leading_spaces"]
        assert issues[0].column == "name"

    def test_severity_warning(self, leading_space_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(leading_space_df)
                  if i.issue_type == "leading_spaces"]
        assert issues[0].severity == "warning"


class TestTrailingSpaces:
    """trailing_spaces: flags values with trailing whitespace."""

    def test_trailing_spaces_flagged(self, trailing_space_df: pd.DataFrame) -> None:
        issues = audit_cleanliness(trailing_space_df)
        types = [i.issue_type for i in issues]
        assert "trailing_spaces" in types

    def test_column_correct(self, trailing_space_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(trailing_space_df)
                  if i.issue_type == "trailing_spaces"]
        assert issues[0].column == "name"


class TestNullLikeStrings:
    """null_like_strings: flags sentinel strings stored as text."""

    def test_null_like_flagged(self, null_like_df: pd.DataFrame) -> None:
        issues = audit_cleanliness(null_like_df)
        types = [i.issue_type for i in issues]
        assert "null_like_strings" in types

    def test_case_insensitive_matching(self, null_like_case_insensitive_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(null_like_case_insensitive_df)
                  if i.issue_type == "null_like_strings"]
        assert len(issues) >= 1

    def test_severity_warning(self, null_like_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(null_like_df)
                  if i.issue_type == "null_like_strings"]
        assert issues[0].severity == "warning"

    def test_examples_populated(self, null_like_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(null_like_df)
                  if i.issue_type == "null_like_strings"]
        assert len(issues[0].examples) > 0


class TestEmptyStrings:
    """empty_strings: flags empty or whitespace-only values."""

    def test_empty_string_flagged(self, empty_string_df: pd.DataFrame) -> None:
        issues = audit_cleanliness(empty_string_df)
        types = [i.issue_type for i in issues]
        assert "empty_strings" in types

    def test_whitespace_only_flagged(self, empty_string_df: pd.DataFrame) -> None:
        # The whitespace-only values ("   ") are included as empty strings
        issues = [i for i in audit_cleanliness(empty_string_df)
                  if i.issue_type == "empty_strings"]
        assert issues[0].affected_count >= 40  # at least the empty-string rows

    def test_severity_info(self, empty_string_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(empty_string_df)
                  if i.issue_type == "empty_strings"]
        assert issues[0].severity == "info"


class TestInvisibleChars:
    """invisible_chars: flags embedded Unicode invisible characters."""

    def test_zero_width_space_flagged(self, invisible_char_df: pd.DataFrame) -> None:
        issues = audit_cleanliness(invisible_char_df)
        types = [i.issue_type for i in issues]
        assert "invisible_chars" in types

    def test_bom_flagged(self, bom_char_df: pd.DataFrame) -> None:
        issues = audit_cleanliness(bom_char_df)
        types = [i.issue_type for i in issues]
        assert "invisible_chars" in types

    def test_nbsp_flagged(self, nbsp_df: pd.DataFrame) -> None:
        issues = audit_cleanliness(nbsp_df)
        types = [i.issue_type for i in issues]
        assert "invisible_chars" in types

    def test_severity_error(self, invisible_char_df: pd.DataFrame) -> None:
        issues = [i for i in audit_cleanliness(invisible_char_df)
                  if i.issue_type == "invisible_chars"]
        assert issues[0].severity == "error"


class TestCleanData:
    """No false positives on clean DataFrames."""

    def test_clean_returns_empty(self, clean_df: pd.DataFrame) -> None:
        assert audit_cleanliness(clean_df) == []

    def test_numeric_df_returns_empty(self) -> None:
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
        assert audit_cleanliness(df) == []

    def test_datetime_df_returns_empty(self) -> None:
        df = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=10)})
        assert audit_cleanliness(df) == []


class TestOutputContract:
    """FormatIssue fields are correctly populated."""

    def test_returns_list(self, null_like_df: pd.DataFrame) -> None:
        assert isinstance(audit_cleanliness(null_like_df), list)

    def test_each_item_is_format_issue(self, null_like_df: pd.DataFrame) -> None:
        for item in audit_cleanliness(null_like_df):
            assert isinstance(item, FormatIssue)

    def test_affected_count_positive(self, null_like_df: pd.DataFrame) -> None:
        for issue in audit_cleanliness(null_like_df):
            assert issue.affected_count > 0

    def test_fix_suggestion_nonempty(self, leading_space_df: pd.DataFrame) -> None:
        for issue in audit_cleanliness(leading_space_df):
            assert len(issue.fix_suggestion) > 0
