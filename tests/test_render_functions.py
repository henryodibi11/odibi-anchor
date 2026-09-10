"""Tests for render functions across all three newly-enabled tools.

Covers:
- render_error_trace_report (debugging)
- render_duplicate_key_report (validation)
- render_dataset_profile_report (profiling)

Each render function is tested for:
- Correct markdown output structure
- Required key validation (ValueError on missing keys)
- output_format="markdown" dispatch from parent function
- output_format="invalid" raises ValueError
- Backward compatibility (default output_format="dict" returns dict)
"""

import pytest
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from odibi_anchor.debugging import error_trace_context, render_error_trace_report
from odibi_anchor.validation import duplicate_key_context, render_duplicate_key_report


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def error_ctx():
    """Generate a valid error_trace_context dict."""
    df = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    try:
        df["missing_col"]
    except Exception as exc:
        return error_trace_context(exc, df=df, subject="test.table")


@pytest.fixture
def duplicate_ctx():
    """Generate a valid duplicate_key_context dict (with duplicates)."""
    df = pd.DataFrame({"id": [1, 2, 2, 3, 3, 3], "val": [10, 20, 21, 30, 31, 32]})
    return duplicate_key_context(df, keys=["id"])


@pytest.fixture
def duplicate_ctx_clean():
    """Generate a duplicate_key_context dict with no duplicates."""
    df = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    return duplicate_key_context(df, keys=["id"])


# ===========================================================================
# render_error_trace_report
# ===========================================================================

class TestRenderErrorTraceReport:
    """Tests for render_error_trace_report."""

    def test_returns_string(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert isinstance(result, str)

    def test_contains_header(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert "# Error Trace: test.table" in result

    def test_contains_error_section(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert "## Error" in result
        assert "KeyError" in result

    def test_contains_metrics_table(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert "## Metrics" in result
        assert "| Metric | Value |" in result

    def test_contains_findings(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert "## Findings" in result

    def test_contains_risks(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert "## Risks" in result

    def test_contains_dataframe_context(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert "## DataFrame Context" in result

    def test_contains_trace(self, error_ctx):
        result = render_error_trace_report(error_ctx)
        assert "## Relevant Trace" in result

    def test_missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_error_trace_report({"kind": "error_trace_context"})

    def test_empty_dict_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_error_trace_report({})

    def test_output_format_markdown_dispatch(self):
        df = pd.DataFrame({"x": [1, 2]})
        try:
            df["nope"]
        except Exception as exc:
            result = error_trace_context(exc, df=df, output_format="markdown")
        assert isinstance(result, str)
        assert "# Error Trace:" in result

    def test_output_format_dict_default(self):
        df = pd.DataFrame({"x": [1, 2]})
        try:
            df["nope"]
        except Exception as exc:
            result = error_trace_context(exc, df=df)
        assert isinstance(result, dict)
        assert result["kind"] == "error_trace_context"

    def test_output_format_invalid_raises(self):
        df = pd.DataFrame({"x": [1, 2]})
        try:
            df["nope"]
        except Exception as exc:
            with pytest.raises(ValueError, match="output_format"):
                error_trace_context(exc, output_format="xml")

    def test_without_dataframe(self):
        try:
            1 / 0
        except Exception as exc:
            ctx = error_trace_context(exc, subject="division_error")
        result = render_error_trace_report(ctx)
        assert isinstance(result, str)
        assert "division_error" in result
        # Should NOT have DataFrame Context section
        assert "## DataFrame Context" not in result


# ===========================================================================
# render_duplicate_key_report
# ===========================================================================

class TestRenderDuplicateKeyReport:
    """Tests for render_duplicate_key_report."""

    def test_returns_string(self, duplicate_ctx):
        result = render_duplicate_key_report(duplicate_ctx)
        assert isinstance(result, str)

    def test_contains_header_with_subject(self, duplicate_ctx):
        result = render_duplicate_key_report(duplicate_ctx)
        assert "# Duplicate Key Check:" in result

    def test_duplicates_found_banner(self, duplicate_ctx):
        result = render_duplicate_key_report(duplicate_ctx)
        assert "DUPLICATES FOUND" in result

    def test_unique_banner(self, duplicate_ctx_clean):
        result = render_duplicate_key_report(duplicate_ctx_clean)
        assert "UNIQUE" in result

    def test_contains_metrics_table(self, duplicate_ctx):
        result = render_duplicate_key_report(duplicate_ctx)
        assert "## Metrics" in result
        assert "Total rows" in result
        assert "Key columns" in result
        assert "Duplicate key groups" in result

    def test_contains_top_duplicate_keys(self, duplicate_ctx):
        result = render_duplicate_key_report(duplicate_ctx)
        assert "## Top Duplicate Keys" in result

    def test_no_samples_when_clean(self, duplicate_ctx_clean):
        result = render_duplicate_key_report(duplicate_ctx_clean)
        assert "## Top Duplicate Keys" not in result

    def test_missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_duplicate_key_report({"kind": "duplicate_key_context"})

    def test_empty_dict_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_duplicate_key_report({})

    def test_output_format_markdown_dispatch(self):
        df = pd.DataFrame({"id": [1, 1, 2], "v": [10, 11, 20]})
        result = duplicate_key_context(df, keys=["id"], output_format="markdown")
        assert isinstance(result, str)
        assert "# Duplicate Key Check:" in result

    def test_output_format_dict_default(self):
        df = pd.DataFrame({"id": [1, 1, 2], "v": [10, 11, 20]})
        result = duplicate_key_context(df, keys=["id"])
        assert isinstance(result, dict)
        assert result["kind"] == "duplicate_key_context"

    def test_output_format_invalid_raises(self):
        df = pd.DataFrame({"id": [1, 2]})
        with pytest.raises(ValueError, match="output_format"):
            duplicate_key_context(df, keys=["id"], output_format="yaml")

    def test_null_keys_warning(self):
        df = pd.DataFrame({"id": [1, None, None, 2], "v": [10, 20, 30, 40]})
        ctx = duplicate_key_context(df, keys=["id"])
        result = render_duplicate_key_report(ctx)
        # Should render without error regardless of null status
        assert isinstance(result, str)

    def test_multi_column_key(self):
        df = pd.DataFrame({
            "a": [1, 1, 1, 2],
            "b": ["x", "x", "y", "z"],
            "val": [10, 11, 12, 13],
        })
        result = duplicate_key_context(df, keys=["a", "b"], output_format="markdown")
        assert isinstance(result, str)
        assert "`a`" in result
        assert "`b`" in result

# NOTE: render_dataset_profile_report was removed when dataset_profile_context
# was consolidated into the table_profiler tool (anchor("profile_table")).
