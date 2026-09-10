"""Tests for lib/string_length.py — String Length Analysis (F12)."""

from __future__ import annotations

import sys

import pandas as pd
import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.string_length import (
    _BOUNDARY_TOLERANCE,
    _OUTLIER_MIN_MAX_LENGTH,
    _OUTLIER_RATIO_THRESHOLD,
    _TRUNCATION_BOUNDARIES,
    detect_string_length_issues,
)
from tools.table_profiler_tool.lib.models import ColumnProfile, ColumnRole, FormatIssue, SemanticType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_profile(
    name: str = "col",
    spark_type: str = "string",
    min_length: int | None = None,
    max_length: int | None = None,
    avg_length: float | None = None,
    distinct_count: int = 10,
    is_constant: bool = False,
    non_null_count: int = 100,
    role: ColumnRole = ColumnRole.DIMENSION,
) -> ColumnProfile:
    """Build a minimal ColumnProfile for string length tests."""
    return ColumnProfile(
        name=name,
        position=0,
        spark_type=spark_type,
        role=role,
        min_length=min_length,
        max_length=max_length,
        avg_length=avg_length,
        distinct_count=distinct_count,
        is_constant=is_constant,
        non_null_count=non_null_count,
        row_count=non_null_count,
    )


# ---------------------------------------------------------------------------
# Skip / no-op tests
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """Tests where detect_string_length_issues returns empty list."""

    def test_empty_profiles(self):
        result = detect_string_length_issues([])
        assert result == []

    def test_non_string_columns_skipped(self):
        profiles = [
            _make_profile("amount", spark_type="double", max_length=10, avg_length=5.0),
            _make_profile("id", spark_type="integer", max_length=5, avg_length=3.0),
        ]
        result = detect_string_length_issues(profiles)
        assert result == []

    def test_null_lengths_skipped(self):
        profiles = [_make_profile("notes", max_length=None, avg_length=None)]
        result = detect_string_length_issues(profiles)
        assert result == []


# ---------------------------------------------------------------------------
# Truncation risk tests
# ---------------------------------------------------------------------------


class TestTruncationRisk:
    """Tests for VARCHAR boundary truncation detection."""

    def test_detects_255_boundary(self):
        """max_length=255 with avg much lower → truncation risk."""
        profiles = [_make_profile("description", max_length=255, avg_length=50.0)]
        result = detect_string_length_issues(profiles)
        trunc = [i for i in result if i.issue_type == "truncation_risk"]
        assert len(trunc) == 1
        assert trunc[0].column == "description"
        assert trunc[0].severity == "warning"
        assert "255" in trunc[0].description
        assert trunc[0].metadata["boundary"] == 255

    def test_detects_256_boundary(self):
        """max_length=256 → truncation risk."""
        profiles = [_make_profile("notes", max_length=256, avg_length=30.0)]
        result = detect_string_length_issues(profiles)
        trunc = [i for i in result if i.issue_type == "truncation_risk"]
        assert len(trunc) == 1
        assert trunc[0].metadata["boundary"] == 256

    def test_detects_4000_boundary(self):
        """max_length=4000 → SQL Server NVARCHAR(MAX) boundary."""
        profiles = [_make_profile("body", max_length=4000, avg_length=200.0)]
        result = detect_string_length_issues(profiles)
        trunc = [i for i in result if i.issue_type == "truncation_risk"]
        assert len(trunc) == 1
        assert trunc[0].metadata["boundary"] == 4000

    def test_no_truncation_when_avg_close_to_max(self):
        """max_length=255 but avg=200 → natural length, not truncation."""
        profiles = [_make_profile("title", max_length=255, avg_length=200.0)]
        result = detect_string_length_issues(profiles)
        trunc = [i for i in result if i.issue_type == "truncation_risk"]
        assert len(trunc) == 0

    def test_no_truncation_for_non_boundary_length(self):
        """max_length=150 (not a boundary) → no truncation risk."""
        profiles = [_make_profile("name", max_length=150, avg_length=20.0)]
        result = detect_string_length_issues(profiles)
        trunc = [i for i in result if i.issue_type == "truncation_risk"]
        assert len(trunc) == 0


# ---------------------------------------------------------------------------
# Length outlier tests
# ---------------------------------------------------------------------------


class TestLengthOutlier:
    """Tests for extreme length variance detection."""

    def test_detects_extreme_ratio(self):
        """max=5000 vs avg=30 → 166x ratio → outlier."""
        profiles = [_make_profile("content", max_length=5000, avg_length=30.0)]
        result = detect_string_length_issues(profiles)
        outliers = [i for i in result if i.issue_type == "length_outlier"]
        assert len(outliers) == 1
        assert outliers[0].column == "content"
        assert outliers[0].severity == "info"
        assert outliers[0].metadata["ratio"] > _OUTLIER_RATIO_THRESHOLD

    def test_no_outlier_when_ratio_normal(self):
        """max=100 vs avg=50 → 2x ratio → normal."""
        profiles = [_make_profile("name", max_length=100, avg_length=50.0)]
        result = detect_string_length_issues(profiles)
        outliers = [i for i in result if i.issue_type == "length_outlier"]
        assert len(outliers) == 0

    def test_no_outlier_when_max_too_short(self):
        """max=30 → below _OUTLIER_MIN_MAX_LENGTH threshold."""
        profiles = [_make_profile("code", max_length=30, avg_length=2.0)]
        result = detect_string_length_issues(profiles)
        outliers = [i for i in result if i.issue_type == "length_outlier"]
        assert len(outliers) == 0

    def test_no_outlier_when_avg_zero(self):
        """avg_length=0 → skip to avoid division by zero."""
        profiles = [_make_profile("empty_col", max_length=100, avg_length=0.0)]
        result = detect_string_length_issues(profiles)
        outliers = [i for i in result if i.issue_type == "length_outlier"]
        assert len(outliers) == 0


# ---------------------------------------------------------------------------
# Uniform length tests
# ---------------------------------------------------------------------------


class TestUniformLength:
    """Tests for fixed-width column detection."""

    def test_detects_uniform_length(self):
        """min==max==10 with multiple distinct values → fixed-width code."""
        profiles = [_make_profile("project_code", min_length=10, max_length=10, avg_length=10.0, distinct_count=50)]
        result = detect_string_length_issues(profiles)
        uniform = [i for i in result if i.issue_type == "uniform_length"]
        assert len(uniform) == 1
        assert uniform[0].column == "project_code"
        assert uniform[0].severity == "info"
        assert "10 characters" in uniform[0].description

    def test_no_uniform_when_lengths_differ(self):
        """min=5, max=20 → not uniform."""
        profiles = [_make_profile("name", min_length=5, max_length=20, avg_length=12.0)]
        result = detect_string_length_issues(profiles)
        uniform = [i for i in result if i.issue_type == "uniform_length"]
        assert len(uniform) == 0

    def test_no_uniform_for_single_char(self):
        """min==max==1 → too short to be interesting (flags like Y/N)."""
        profiles = [_make_profile("flag_col", min_length=1, max_length=1, avg_length=1.0)]
        result = detect_string_length_issues(profiles)
        uniform = [i for i in result if i.issue_type == "uniform_length"]
        assert len(uniform) == 0

    def test_no_uniform_for_two_char(self):
        """min==max==2 → too short (state codes, already typed semantically)."""
        profiles = [_make_profile("st", min_length=2, max_length=2, avg_length=2.0)]
        result = detect_string_length_issues(profiles)
        uniform = [i for i in result if i.issue_type == "uniform_length"]
        assert len(uniform) == 0

    def test_no_uniform_for_constant_column(self):
        """min==max but is_constant → already flagged elsewhere."""
        profiles = [_make_profile("const_col", min_length=5, max_length=5, avg_length=5.0, is_constant=True)]
        result = detect_string_length_issues(profiles)
        uniform = [i for i in result if i.issue_type == "uniform_length"]
        assert len(uniform) == 0


# ---------------------------------------------------------------------------
# Output contract tests
# ---------------------------------------------------------------------------


class TestOutputContract:
    """Tests that output conforms to FormatIssue contract."""

    def test_all_issues_are_format_issue_instances(self):
        profiles = [
            _make_profile("desc", max_length=255, avg_length=40.0),
            _make_profile("body", max_length=5000, avg_length=30.0),
            _make_profile("code", min_length=8, max_length=8, avg_length=8.0, distinct_count=20),
        ]
        result = detect_string_length_issues(profiles)
        assert len(result) >= 2  # At least truncation + outlier
        for issue in result:
            assert isinstance(issue, FormatIssue)
            assert issue.issue_type in ("truncation_risk", "length_outlier", "uniform_length")
            assert issue.severity in ("error", "warning", "info")
            assert issue.column != ""
            assert issue.description != ""

    def test_metadata_contains_expected_keys(self):
        profiles = [_make_profile("notes", max_length=255, avg_length=30.0)]
        result = detect_string_length_issues(profiles)
        trunc = [i for i in result if i.issue_type == "truncation_risk"]
        assert len(trunc) == 1
        meta = trunc[0].metadata
        assert "boundary" in meta
        assert "max_length" in meta
        assert "avg_length" in meta


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestProfilerIntegration:
    """Test that string length issues flow through profile_table."""

    def test_profile_table_includes_string_length_issues(self):
        """profile_table should detect truncation risk on a 255-max column."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        # Create data with one column that hits 255 char limit
        df = pd.DataFrame({
            "id": list(range(20)),
            "description": ["short"] * 19 + ["x" * 255],
        })

        tp = profile_table(df, subject="test.trunc_table")
        # Format issues should include truncation_risk from string_length
        trunc_issues = [
            i for i in tp.format_issues if i.issue_type == "truncation_risk"
        ]
        assert len(trunc_issues) == 1
        assert trunc_issues[0].column == "description"

    def test_profile_table_no_issues_for_normal_strings(self):
        """Normal string lengths should not produce string_length issues."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        df = pd.DataFrame({
            "id": list(range(20)),
            "name": [f"name_{i}" for i in range(20)],
        })

        tp = profile_table(df, subject="test.normal_table")
        sl_issues = [
            i for i in tp.format_issues
            if i.issue_type in ("truncation_risk", "length_outlier", "uniform_length")
        ]
        assert len(sl_issues) == 0


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


class TestRendering:
    """Test that the renderer shows length info."""

    def test_md_column_table_has_length_column(self):
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_profile_md

        tp = TableProfile(
            subject="test.table",
            row_count=100,
            column_count=2,
            columns=[
                _make_profile("name", min_length=3, max_length=50, avg_length=12.0),
                _make_profile("code", min_length=8, max_length=8, avg_length=8.0),
            ],
        )
        output = render_table_profile_md(tp)
        assert "Length" in output
        assert "3–50" in output
        assert "8–8" in output

    def test_md_column_table_dash_for_non_string(self):
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_profile_md

        tp = TableProfile(
            subject="test.table",
            row_count=100,
            column_count=1,
            columns=[
                _make_profile("amount", spark_type="double", min_length=None, max_length=None),
            ],
        )
        output = render_table_profile_md(tp)
        # Non-string should show "—" in Length column
        assert "—" in output
