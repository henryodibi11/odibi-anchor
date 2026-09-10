"""Unit tests for format consistency detection module."""

from __future__ import annotations

import pandas as pd
import pytest

from tools.table_profiler_tool.lib.format_checker import detect_format_issues, _check_inconsistent_code_format
from tools.table_profiler_tool.lib.models import FormatIssue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_date_df() -> pd.DataFrame:
    """Column with ISO and US date formats mixed."""
    vals = (
        ["2026-01-15"] * 50      # %Y-%m-%d
        + ["01/16/2026"] * 30    # %m/%d/%Y
        + ["01/17/26"] * 20      # %m/%d/%y
    )
    return pd.DataFrame({"event_date": vals})


@pytest.fixture
def single_date_format_df() -> pd.DataFrame:
    """Column with a single consistent date format."""
    return pd.DataFrame({"created": ["2026-01-" + f"{d:02d}" for d in range(1, 31)]})


@pytest.fixture
def mixed_case_df() -> pd.DataFrame:
    """Low-cardinality column with same values in different cases."""
    vals = ["Active"] * 40 + ["ACTIVE"] * 30 + ["active"] * 20 + ["Withdrawn"] * 10
    return pd.DataFrame({"status": vals})


@pytest.fixture
def consistent_case_df() -> pd.DataFrame:
    """Column already normalised to a single case."""
    return pd.DataFrame({"status": ["Active"] * 60 + ["Withdrawn"] * 40})


@pytest.fixture
def mixed_bool_df() -> pd.DataFrame:
    """Column with Y/N and Yes/No mixed."""
    vals = ["Y"] * 45 + ["N"] * 45 + ["Yes"] * 5 + ["No"] * 5
    return pd.DataFrame({"flag": vals})


@pytest.fixture
def consistent_bool_df() -> pd.DataFrame:
    """Column using a single boolean encoding."""
    return pd.DataFrame({"active": ["Y"] * 60 + ["N"] * 40})


@pytest.fixture
def mixed_code_df() -> pd.DataFrame:
    """Structured codes using both dash and underscore separators."""
    vals = ["PRJ-001"] * 60 + ["PRJ_002"] * 40
    return pd.DataFrame({"project_code": vals})


@pytest.fixture
def consistent_code_df() -> pd.DataFrame:
    """Structured codes using only dash separators."""
    return pd.DataFrame({"code": [f"PRJ-{i:03d}" for i in range(100)]})


@pytest.fixture
def units_df() -> pd.DataFrame:
    """Column with embedded unit suffixes."""
    vals = ["100.5MW"] * 50 + ["200 kW"] * 30 + ["150MW"] * 20
    return pd.DataFrame({"capacity": vals})


@pytest.fixture
def clean_numeric_df() -> pd.DataFrame:
    """Plain numeric strings — no false positives expected."""
    return pd.DataFrame({"amount": [str(i * 10) for i in range(1, 101)]})


@pytest.fixture
def comma_num_df() -> pd.DataFrame:
    """Numeric strings with comma thousands separators."""
    vals = ["1,234"] * 40 + ["2,567.89"] * 40 + ["999"] * 20
    return pd.DataFrame({"revenue": vals})


@pytest.fixture
def currency_df() -> pd.DataFrame:
    """Currency values — should NOT be flagged as numeric_with_units."""
    return pd.DataFrame({"price": ["$100.00", "$200.50", "$300.75"] * 34})


@pytest.fixture
def date_string_col_df() -> pd.DataFrame:
    """Column storing datetime strings — must NOT produce inconsistent_code_format.

    Mirrors real-world columns like Air_Permit / GHG_Permit in queue_ercot
    that store datetime strings such as '2024-02-09 00:00:00'.  These values
    match the code-separator regex (alphanumeric + '-' + alphanumeric) AND have
    mixed separators ('-' between date parts, ' ' between date and time), which
    previously triggered a false-positive inconsistent_code_format error.
    """
    vals = (
        ["2024-02-09 00:00:00"] * 40
        + ["2025-01-15 12:30:00"] * 30
        + ["2023-11-01 00:00:00"] * 30
    )
    return pd.DataFrame({"air_permit": vals})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMixedDateFormat:
    """mixed_date_format: flags columns with 2+ date formats."""

    def test_mixed_dates_flagged(self, mixed_date_df: pd.DataFrame) -> None:
        issues = detect_format_issues(mixed_date_df)
        types = [i.issue_type for i in issues]
        assert "mixed_date_format" in types

    def test_issue_type_correct(self, mixed_date_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(mixed_date_df)
                  if i.issue_type == "mixed_date_format"]
        assert len(issues) >= 1
        assert issues[0].column == "event_date"

    def test_severity_is_error(self, mixed_date_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(mixed_date_df)
                  if i.issue_type == "mixed_date_format"]
        assert issues[0].severity == "error"

    def test_single_format_not_flagged(self, single_date_format_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(single_date_format_df)
                  if i.issue_type == "mixed_date_format"]
        assert issues == []


class TestMixedCaseEnum:
    """mixed_case_enum: flags low-cardinality columns with case variants."""

    def test_mixed_case_flagged(self, mixed_case_df: pd.DataFrame) -> None:
        issues = detect_format_issues(mixed_case_df)
        types = [i.issue_type for i in issues]
        assert "mixed_case_enum" in types

    def test_column_name_correct(self, mixed_case_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(mixed_case_df)
                  if i.issue_type == "mixed_case_enum"]
        assert issues[0].column == "status"

    def test_severity_is_warning(self, mixed_case_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(mixed_case_df)
                  if i.issue_type == "mixed_case_enum"]
        assert issues[0].severity == "warning"

    def test_consistent_case_not_flagged(self, consistent_case_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(consistent_case_df)
                  if i.issue_type == "mixed_case_enum"]
        assert issues == []


class TestMixedBooleanEncoding:
    """mixed_boolean_encoding: flags columns using multiple boolean schemes."""

    def test_mixed_bool_flagged(self, mixed_bool_df: pd.DataFrame) -> None:
        issues = detect_format_issues(mixed_bool_df)
        types = [i.issue_type for i in issues]
        assert "mixed_boolean_encoding" in types

    def test_severity_is_warning(self, mixed_bool_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(mixed_bool_df)
                  if i.issue_type == "mixed_boolean_encoding"]
        assert issues[0].severity == "warning"

    def test_consistent_bool_not_flagged(self, consistent_bool_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(consistent_bool_df)
                  if i.issue_type == "mixed_boolean_encoding"]
        assert issues == []


class TestInconsistentCodeFormat:
    """inconsistent_code_format: flags codes with mixed separators."""

    def test_mixed_separator_flagged(self, mixed_code_df: pd.DataFrame) -> None:
        issues = detect_format_issues(mixed_code_df)
        types = [i.issue_type for i in issues]
        assert "inconsistent_code_format" in types

    def test_column_name_correct(self, mixed_code_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(mixed_code_df)
                  if i.issue_type == "inconsistent_code_format"]
        assert issues[0].column == "project_code"

    def test_consistent_code_not_flagged(self, consistent_code_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(consistent_code_df)
                  if i.issue_type == "inconsistent_code_format"]
        assert issues == []


class TestInconsistentCodeFormatDateGuard:
    """Regression: date-string columns must not produce inconsistent_code_format."""

    def test_datetime_strings_not_flagged(self, date_string_col_df: pd.DataFrame) -> None:
        """'2024-02-09 00:00:00' values must not trigger code-format check."""
        issues = [
            i for i in detect_format_issues(date_string_col_df)
            if i.issue_type == "inconsistent_code_format"
        ]
        assert issues == [], (
            f"Date-string column raised false-positive code_format issues: "
            f"{[i.examples for i in issues]}"
        )

    def test_iso_date_only_strings_not_flagged(self) -> None:
        """Plain ISO date strings (no time part) should also be safe."""
        df = pd.DataFrame({
            "permit_date": ["2024-02-09"] * 60 + ["2025-03-15"] * 40,
        })
        issues = [
            i for i in detect_format_issues(df)
            if i.issue_type == "inconsistent_code_format"
        ]
        assert issues == []

    def test_real_code_column_still_flagged(self, mixed_code_df: pd.DataFrame) -> None:
        """Guard must not suppress genuine code-format issues."""
        issues = [
            i for i in detect_format_issues(mixed_code_df)
            if i.issue_type == "inconsistent_code_format"
        ]
        assert len(issues) >= 1, "mixed_code_df should still be flagged after guard added"

    def test_natural_language_phrase_column_not_flagged(self) -> None:
        """Regression: 'Not Required' / 'See Note' columns must not be flagged.

        Mirrors the real-world queue_ercot Air_Permit column which stores mostly
        'Not Required' (97%) with a few date strings (3%).  Space is the dominant
        separator so the column is a phrase column, not a code column.
        """
        vals = ["Not Required"] * 970 + ["2024-02-09 00:00:00"] * 30
        df = pd.DataFrame({"air_permit": vals})
        issues = [
            i for i in detect_format_issues(df)
            if i.issue_type == "inconsistent_code_format"
        ]
        assert issues == [], (
            f"Phrase column raised false-positive code_format issues: "
            f"{[i.examples[:2] for i in issues]}"
        )


class TestNumericWithUnits:
    """numeric_with_units: flags numbers with embedded unit suffixes."""

    def test_units_flagged(self, units_df: pd.DataFrame) -> None:
        issues = detect_format_issues(units_df)
        types = [i.issue_type for i in issues]
        assert "numeric_with_units" in types

    def test_column_name_correct(self, units_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(units_df)
                  if i.issue_type == "numeric_with_units"]
        assert issues[0].column == "capacity"

    def test_severity_is_error(self, units_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(units_df)
                  if i.issue_type == "numeric_with_units"]
        assert issues[0].severity == "error"

    def test_currency_not_flagged_as_units(self, currency_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(currency_df)
                  if i.issue_type == "numeric_with_units"]
        assert issues == []


class TestNumericWithCommas:
    """numeric_with_commas: flags thousands-separator formatting."""

    def test_comma_nums_flagged(self, comma_num_df: pd.DataFrame) -> None:
        issues = detect_format_issues(comma_num_df)
        types = [i.issue_type for i in issues]
        assert "numeric_with_commas" in types

    def test_severity_is_warning(self, comma_num_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(comma_num_df)
                  if i.issue_type == "numeric_with_commas"]
        assert issues[0].severity == "warning"

    def test_plain_nums_not_flagged(self, clean_numeric_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(clean_numeric_df)
                  if i.issue_type == "numeric_with_commas"]
        assert issues == []


class TestCleanData:
    """No false positives on clean, well-typed DataFrames."""

    def test_empty_list_on_numeric_df(self) -> None:
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
        assert detect_format_issues(df) == []

    def test_empty_list_on_datetime_df(self) -> None:
        import pandas as pd
        df = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=10)})
        assert detect_format_issues(df) == []


class TestOutputContract:
    """FormatIssue fields are correctly populated."""

    def test_returns_list(self, mixed_date_df: pd.DataFrame) -> None:
        result = detect_format_issues(mixed_date_df)
        assert isinstance(result, list)

    def test_each_item_is_format_issue(self, mixed_date_df: pd.DataFrame) -> None:
        for item in detect_format_issues(mixed_date_df):
            assert isinstance(item, FormatIssue)

    def test_affected_count_positive(self, mixed_date_df: pd.DataFrame) -> None:
        for issue in detect_format_issues(mixed_date_df):
            assert issue.affected_count > 0

    def test_affected_pct_in_range(self, mixed_date_df: pd.DataFrame) -> None:
        for issue in detect_format_issues(mixed_date_df):
            assert 0.0 < issue.affected_pct <= 1.0

    def test_examples_populated(self, mixed_date_df: pd.DataFrame) -> None:
        issues = [i for i in detect_format_issues(mixed_date_df)
                  if i.issue_type == "mixed_date_format"]
        assert len(issues[0].examples) > 0

    def test_fix_suggestion_nonempty(self, units_df: pd.DataFrame) -> None:
        issues = detect_format_issues(units_df)
        for i in issues:
            assert len(i.fix_suggestion) > 0

class TestInconsistentCodeFormatPrefixGuard:
    """B1 corpus fix: prefix-overlap guard prevents false positives on cluster/cycle cols."""

    def test_disjoint_code_series_not_flagged(self):
        """ERCOT hyphen codes vs MISO space codes: disjoint prefixes must not produce errors."""
        sample = (
            ["ISA-3D2", "ISA-4D2", "ATXI-3", "ATXI-4"] * 20
            + ["Cycle 3", "Cycle 4", "Group A", "Group B"] * 5
        )
        df = pd.DataFrame({"cluster_cycle_code": sample})
        issues = detect_format_issues(df)
        errors = [
            i for i in issues
            if i.issue_type == "inconsistent_code_format"
            and i.severity == "error"
            and i.column == "cluster_cycle_code"
        ]
        assert errors == [], (
            f"Disjoint-prefix groups should not produce code_format errors: {errors}"
        )

    def test_same_series_inconsistent_separators_still_flagged(self):
        """Same prefix PRJ with both hyphen and space is a real inconsistency."""
        sample = (
            ["PRJ-001", "PRJ-002", "PRJ-003"] * 30
            + ["PRJ 004", "PRJ 005"] * 10
        )
        df = pd.DataFrame({"project_code": sample})
        issues = detect_format_issues(df)
        assert any(
            i.issue_type == "inconsistent_code_format" and i.column == "project_code"
            for i in issues
        ), "Same-prefix mixed separators should still be flagged"

    def test_partial_prefix_overlap_above_threshold_still_flagged(self):
        """When >=20pct of minority prefixes overlap with dominant, flag is retained."""
        sample = (
            ["ISA-3D2", "ISA-4D2", "ISA-5D2"] * 20
            + ["ISA Cycle", "ISA Group", "OTHER Cycle"] * 8
        )
        df = pd.DataFrame({"study_cycle": sample})
        issues = detect_format_issues(df)
        assert any(
            i.issue_type == "inconsistent_code_format" and i.column == "study_cycle"
            for i in issues
        ), "Partial overlap above threshold should still flag"

    def test_study_cycle_real_world_not_error(self):
        """Study_Cycle with real ERCOT+MISO corpus values must not produce an ERROR."""
        sample = (
            ["ISA-3D2", "ISA-4D2", "ATXI-3", "ISA-3"] * 25
            + ["Cycle 3", "Group A"] * 5
        )
        df = pd.DataFrame({"Study_Cycle": sample})
        issues = detect_format_issues(df)
        errors = [
            i for i in issues
            if i.issue_type == "inconsistent_code_format"
            and i.severity == "error"
            and i.column == "Study_Cycle"
        ]
        assert errors == [], (
            f"Study_Cycle with disjoint series should not produce errors: {errors}"
        )
