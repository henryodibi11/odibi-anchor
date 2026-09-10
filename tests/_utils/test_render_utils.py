"""Tests for odibi_anchor._utils.render_utils module."""

import pytest

from odibi_anchor._utils.render_utils import (
    render_header_lines,
    render_metrics_lines,
    render_bullet_section,
    render_numbered_section,
    render_table,
    format_metric,
    format_status_badge,
)


# ---------------------------------------------------------------------------
# render_header_lines
# ---------------------------------------------------------------------------


class TestRenderHeaderLines:
    """Tests for render_header_lines()."""

    def test_produces_title_and_summary(self):
        ctx = {"subject": "my_table", "summary": "All good."}
        lines = render_header_lines(ctx, "Test Focus")
        assert lines[0] == "# Test Focus: my_table"
        assert lines[1] == ""
        assert lines[2] == "**Summary:** All good."
        assert lines[3] == ""

    def test_returns_four_lines(self):
        ctx = {"subject": "x", "summary": "y"}
        assert len(render_header_lines(ctx, "Prefix")) == 4

    def test_special_characters_in_subject(self):
        ctx = {"subject": "catalog.schema.table", "summary": "fine"}
        lines = render_header_lines(ctx, "Profile")
        assert "catalog.schema.table" in lines[0]


# ---------------------------------------------------------------------------
# render_metrics_lines
# ---------------------------------------------------------------------------


class TestRenderMetricsLines:
    """Tests for render_metrics_lines()."""

    def test_produces_table_header(self):
        lines = render_metrics_lines({"rows": 100})
        assert lines[0] == "## Metrics"
        assert lines[1] == ""
        assert lines[2] == "| Metric | Value |"
        assert lines[3] == "| --- | --- |"

    def test_includes_all_metrics(self):
        metrics = {"a": 1, "b": "hello", "c": True}
        lines = render_metrics_lines(metrics)
        data_lines = [l for l in lines if l.startswith("| ") and "---" not in l and "Metric" not in l]
        assert len(data_lines) == 3
        assert "| a | 1 |" in data_lines

    def test_exclude_removes_keys(self):
        metrics = {"keep": 1, "skip": 2}
        lines = render_metrics_lines(metrics, exclude={"skip"})
        text = "\n".join(lines)
        assert "keep" in text
        assert "skip" not in text

    def test_custom_header(self):
        lines = render_metrics_lines({"x": 1}, header="## Stats")
        assert lines[0] == "## Stats"

    def test_empty_metrics(self):
        lines = render_metrics_lines({})
        # Should still produce header + table header rows
        assert len(lines) == 4


# ---------------------------------------------------------------------------
# render_bullet_section
# ---------------------------------------------------------------------------


class TestRenderBulletSection:
    """Tests for render_bullet_section()."""

    def test_empty_list_returns_empty(self):
        assert render_bullet_section([], "## Header") == []

    def test_produces_header_and_bullets(self):
        lines = render_bullet_section(["one", "two"], "## Findings")
        assert lines[0] == ""
        assert lines[1] == "## Findings"
        assert lines[2] == ""
        assert lines[3] == "- one"
        assert lines[4] == "- two"

    def test_custom_prefix(self):
        lines = render_bullet_section(["x"], "## H", prefix="* ")
        assert lines[3] == "* x"


# ---------------------------------------------------------------------------
# render_numbered_section
# ---------------------------------------------------------------------------


class TestRenderNumberedSection:
    """Tests for render_numbered_section()."""

    def test_empty_list_returns_empty(self):
        assert render_numbered_section([], "## Header") == []

    def test_produces_numbered_list(self):
        lines = render_numbered_section(["a", "b", "c"], "## Steps")
        assert lines[0] == ""
        assert lines[1] == "## Steps"
        assert lines[2] == ""
        assert lines[3] == "1. a"
        assert lines[4] == "2. b"
        assert lines[5] == "3. c"


# ---------------------------------------------------------------------------
# render_table
# ---------------------------------------------------------------------------


class TestRenderTable:
    """Tests for render_table()."""

    def test_empty_rows_returns_empty(self):
        assert render_table([], ["A", "B"]) == []

    def test_produces_header_separator_and_rows(self):
        lines = render_table([["v1", "v2"]], ["Col A", "Col B"])
        assert lines[0] == "| Col A | Col B |"
        assert lines[1] == "| --- | --- |"
        assert lines[2] == "| v1 | v2 |"

    def test_multiple_rows(self):
        rows = [["a", "b"], ["c", "d"]]
        lines = render_table(rows, ["X", "Y"])
        assert len(lines) == 4  # header + sep + 2 data rows


# ---------------------------------------------------------------------------
# format_metric
# ---------------------------------------------------------------------------


class TestFormatMetric:
    """Tests for format_metric()."""

    def test_int_with_commas(self):
        assert format_metric(1234567) == "1,234,567"

    def test_small_int(self):
        assert format_metric(5) == "5"

    def test_float_one_decimal(self):
        assert format_metric(3.14159) == "3.1"

    def test_bool_true(self):
        assert format_metric(True) == "✅"

    def test_bool_false(self):
        assert format_metric(False) == "❌"

    def test_string_passthrough(self):
        assert format_metric("hello") == "hello"

    def test_none_passthrough(self):
        assert format_metric(None) == "None"


# ---------------------------------------------------------------------------
# format_status_badge
# ---------------------------------------------------------------------------


class TestFormatStatusBadge:
    """Tests for format_status_badge()."""

    def test_passed(self):
        assert format_status_badge(True) == "✅ PASS"

    def test_failed(self):
        assert format_status_badge(False) == "❌ FAIL"
