"""Tests for explain_row tool.

Covers:
- Basic single-upstream trace
- Multi-upstream with first-match priority
- Composite keys
- All 5 match types (exact, transformed, coerced, null_filled, not_found)
- Edge cases: null-to-null, row not found, invalid inputs
- Markdown rendering
- Contract compliance
"""
from __future__ import annotations

import sys
import os

import pandas as pd
import pytest

# Ensure the tool module is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"
))

from explain_row_tool.explain_row_impl import (
    explain_row_context,
    render_explain_row_report,
    _classify_value_match,
    _extract_row,
    _is_null,
    _normalize_for_coercion,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def output_df():
    """Output DataFrame with mixed sources."""
    return pd.DataFrame({
        "project_id": ["PROJ-001", "PROJ-002", "PROJ-003"],
        "project_name": ["Solar Alpha", "Wind Beta", "Hydro Gamma"],
        "capacity_mw": [150, 200, 9999],
        "market": ["PJM", "ERCOT", "MISO"],
        "status": ["Active", "Pending", "Active"],
        "_row_hash": ["abc", "def", "ghi"],
    })


@pytest.fixture
def source_a():
    """Upstream source A — main project data."""
    return pd.DataFrame({
        "project_id": ["PROJ-001", "PROJ-002", "PROJ-003"],
        "project_name": ["Solar Alpha", "Wind Beta", "Hydro Gamma"],
        "market": ["PJM", "ERCOT", "MISO"],
        "status": [None, "Pending", None],
    })


@pytest.fixture
def source_b():
    """Upstream source B — capacity data."""
    return pd.DataFrame({
        "project_id": ["PROJ-001", "PROJ-002", "PROJ-003"],
        "capacity_mw": [150, 200, 300],
    })


@pytest.fixture
def source_c():
    """Upstream source C — alternate capacity with different value."""
    return pd.DataFrame({
        "project_id": ["PROJ-003"],
        "capacity_mw": [300],
    })


# ============================================================================
# Tests: _classify_value_match
# ============================================================================


class TestClassifyValueMatch:
    """Unit tests for value classification."""

    def test_exact_match_same_type(self):
        assert _classify_value_match(42, 42) == "exact"

    def test_exact_match_strings(self):
        assert _classify_value_match("hello", "hello") == "exact"

    def test_exact_match_both_null(self):
        assert _classify_value_match(None, None) == "exact"

    def test_null_filled(self):
        assert _classify_value_match("Active", None) == "null_filled"

    def test_transformed_value_differs(self):
        assert _classify_value_match(9999, 300) == "transformed"

    def test_transformed_output_null_upstream_has_value(self):
        assert _classify_value_match(None, "something") == "transformed"

    def test_coerced_type_difference(self):
        assert _classify_value_match("42", 42) == "coerced"

    def test_coerced_whitespace(self):
        assert _classify_value_match("hello", "  hello  ") == "coerced"

    def test_coerced_case(self):
        assert _classify_value_match("Hello", "HELLO") == "coerced"

    def test_coerced_whitespace_and_case(self):
        assert _classify_value_match("active", "  ACTIVE  ") == "coerced"

    def test_transformed_truly_different(self):
        assert _classify_value_match("apples", "oranges") == "transformed"


# ============================================================================
# Tests: _is_null and _normalize_for_coercion
# ============================================================================


class TestHelpers:
    """Unit tests for helper functions."""

    def test_is_null_none(self):
        assert _is_null(None) is True

    def test_is_null_nan(self):
        import math
        assert _is_null(float("nan")) is True

    def test_is_null_value(self):
        assert _is_null(42) is False

    def test_is_null_empty_string(self):
        assert _is_null("") is False

    def test_normalize_none(self):
        assert _normalize_for_coercion(None) is None

    def test_normalize_strips_and_lowers(self):
        assert _normalize_for_coercion("  Hello World  ") == "hello world"

    def test_normalize_int(self):
        assert _normalize_for_coercion(42) == "42"


# ============================================================================
# Tests: _extract_row
# ============================================================================


class TestExtractRow:
    """Unit tests for row extraction."""

    def test_extract_existing_row(self, output_df):
        row = _extract_row(output_df, ["project_id"], {"project_id": "PROJ-001"}, "pandas")
        assert row is not None
        assert row["project_id"] == "PROJ-001"
        assert row["capacity_mw"] == 150

    def test_extract_missing_row(self, output_df):
        row = _extract_row(output_df, ["project_id"], {"project_id": "NOPE"}, "pandas")
        assert row is None

    def test_extract_missing_key_column(self, output_df):
        row = _extract_row(output_df, ["nonexistent_col"], {"nonexistent_col": "X"}, "pandas")
        assert row is None

    def test_extract_composite_key(self):
        df = pd.DataFrame({
            "a": [1, 1, 2],
            "b": ["x", "y", "x"],
            "val": [10, 20, 30],
        })
        row = _extract_row(df, ["a", "b"], {"a": 1, "b": "y"}, "pandas")
        assert row is not None
        assert row["val"] == 20


# ============================================================================
# Tests: explain_row_context — basic
# ============================================================================


class TestExplainRowBasic:
    """Integration tests for explain_row_context."""

    def test_basic_single_upstream(self, output_df, source_a):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a},
        )
        assert result["kind"] == "explain_row"
        assert "PROJ-001" in result["subject"]
        assert result["metrics"]["output_columns"] == 6
        assert result["metrics"]["columns_traced"] >= 3  # at least project_name, market, status

    def test_multi_upstream(self, output_df, source_a, source_b):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a, "source_b": source_b},
        )
        assert result["metrics"]["upstream_sources_matched"] == 2

        # capacity_mw should come from source_b
        lineage = result["column_lineage"]
        cap_col = next(c for c in lineage if c["column"] == "capacity_mw")
        assert cap_col["origin"] == "source_b"
        assert cap_col["match_type"] == "exact"
        assert cap_col["output_value"] == 150

    def test_transformed_value_detected(self, output_df, source_b):
        # PROJ-003 has capacity_mw=9999 in output, 300 in source_b
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-003"},
            upstream={"source_b": source_b},
        )
        lineage = result["column_lineage"]
        cap_col = next(c for c in lineage if c["column"] == "capacity_mw")
        assert cap_col["match_type"] == "transformed"
        assert cap_col["upstream_value"] == 300
        assert cap_col["output_value"] == 9999

    def test_null_filled_detected(self, output_df, source_a):
        # PROJ-001: status="Active" in output, None in source_a
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a},
        )
        lineage = result["column_lineage"]
        status_col = next(c for c in lineage if c["column"] == "status")
        assert status_col["match_type"] == "null_filled"
        assert status_col["origin"] == "source_a"

    def test_not_found_for_computed_column(self, output_df, source_a, source_b):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a, "source_b": source_b},
        )
        lineage = result["column_lineage"]
        hash_col = next(c for c in lineage if c["column"] == "_row_hash")
        assert hash_col["match_type"] == "not_found"
        assert hash_col["origin"] is None


# ============================================================================
# Tests: explain_row_context — composite keys
# ============================================================================


class TestExplainRowCompositeKeys:
    """Tests for composite key support."""

    def test_composite_key_lookup(self):
        output_df = pd.DataFrame({
            "region": ["US", "US", "EU"],
            "product": ["A", "B", "A"],
            "revenue": [100, 200, 150],
            "margin": [0.2, 0.3, 0.25],
        })
        upstream_df = pd.DataFrame({
            "region": ["US", "US", "EU"],
            "product": ["A", "B", "A"],
            "revenue": [100, 200, 150],
        })
        result = explain_row_context(
            output_df,
            keys=["region", "product"],
            values={"region": "US", "product": "B"},
            upstream={"sales": upstream_df},
        )
        lineage = result["column_lineage"]
        rev_col = next(c for c in lineage if c["column"] == "revenue")
        assert rev_col["match_type"] == "exact"
        assert rev_col["output_value"] == 200


# ============================================================================
# Tests: explain_row_context — error handling
# ============================================================================


class TestExplainRowErrors:
    """Tests for error handling."""

    def test_row_not_found_raises(self, output_df, source_a):
        with pytest.raises(ValueError, match="Row not found"):
            explain_row_context(
                output_df,
                keys=["project_id"],
                values={"project_id": "NONEXISTENT"},
                upstream={"source_a": source_a},
            )

    def test_missing_keys_raises(self, output_df, source_a):
        with pytest.raises(ValueError, match="keys is required"):
            explain_row_context(
                output_df,
                keys=None,
                values={"project_id": "PROJ-001"},
                upstream={"source_a": source_a},
            )

    def test_missing_values_raises(self, output_df, source_a):
        with pytest.raises(ValueError, match="values is required"):
            explain_row_context(
                output_df,
                keys=["project_id"],
                values=None,
                upstream={"source_a": source_a},
            )

    def test_missing_upstream_raises(self, output_df):
        with pytest.raises(ValueError, match="upstream is required"):
            explain_row_context(
                output_df,
                keys=["project_id"],
                values={"project_id": "PROJ-001"},
                upstream=None,
            )

    def test_missing_output_df_raises(self, source_a):
        with pytest.raises(ValueError, match="output_df is required"):
            explain_row_context(
                None,
                keys=["project_id"],
                values={"project_id": "PROJ-001"},
                upstream={"source_a": source_a},
            )

    def test_invalid_output_format_raises(self, output_df, source_a):
        with pytest.raises(ValueError, match="output_format"):
            explain_row_context(
                output_df,
                keys=["project_id"],
                values={"project_id": "PROJ-001"},
                upstream={"source_a": source_a},
                output_format="xml",
            )

    def test_values_missing_key_raises(self, output_df, source_a):
        with pytest.raises(ValueError, match="values dict is missing keys"):
            explain_row_context(
                output_df,
                keys=["project_id", "market"],
                values={"project_id": "PROJ-001"},  # missing "market"
                upstream={"source_a": source_a},
            )


# ============================================================================
# Tests: Contract compliance
# ============================================================================


class TestContractCompliance:
    """Tests that output matches Anchor contract."""

    def test_standard_keys_present(self, output_df, source_a):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a},
        )
        assert "kind" in result
        assert "subject" in result
        assert "summary" in result
        assert "metrics" in result
        assert "findings" in result
        assert "risks" in result
        assert "samples" in result
        assert "suggested_next_actions" in result
        assert "column_lineage" in result
        assert "join_paths" in result

    def test_metrics_keys(self, output_df, source_a):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a},
        )
        m = result["metrics"]
        assert "output_columns" in m
        assert "columns_traced" in m
        assert "columns_untraced" in m
        assert "upstream_sources_matched" in m
        assert "exact_matches" in m
        assert "transformed_values" in m
        assert "coerced_values" in m
        assert "null_filled_values" in m

    def test_column_lineage_structure(self, output_df, source_a):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a},
        )
        for entry in result["column_lineage"]:
            assert "column" in entry
            assert "output_value" in entry
            assert "origin" in entry
            assert "upstream_value" in entry
            assert "match_type" in entry
            assert "join_key" in entry
            assert entry["match_type"] in {"exact", "transformed", "coerced", "null_filled", "not_found"}

    def test_samples_structure(self, output_df, source_a):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a},
        )
        assert "output_row" in result["samples"]
        assert "upstream_rows" in result["samples"]
        assert isinstance(result["samples"]["output_row"], dict)
        assert isinstance(result["samples"]["upstream_rows"], dict)


# ============================================================================
# Tests: Markdown rendering
# ============================================================================


class TestMarkdownRendering:
    """Tests for markdown output."""

    def test_markdown_output_format(self, output_df, source_a, source_b):
        result = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a, "source_b": source_b},
            output_format="markdown",
        )
        assert isinstance(result, str)
        assert "# Explain Row:" in result
        assert "**Summary:**" in result
        assert "## Metrics" in result
        assert "## Column Lineage" in result

    def test_render_function_directly(self, output_df, source_a):
        ctx = explain_row_context(
            output_df,
            keys=["project_id"],
            values={"project_id": "PROJ-001"},
            upstream={"source_a": source_a},
            output_format="dict",
        )
        rendered = render_explain_row_report(ctx)
        assert isinstance(rendered, str)
        assert "Explain Row" in rendered


# ============================================================================
# Tests: Coercion detection
# ============================================================================


class TestCoercionDetection:
    """Tests for coerced value detection."""

    def test_whitespace_coercion(self):
        output_df = pd.DataFrame({
            "id": [1],
            "name": ["hello"],
        })
        upstream_df = pd.DataFrame({
            "id": [1],
            "name": ["  hello  "],
        })
        result = explain_row_context(
            output_df,
            keys=["id"],
            values={"id": 1},
            upstream={"src": upstream_df},
        )
        lineage = result["column_lineage"]
        name_col = next(c for c in lineage if c["column"] == "name")
        assert name_col["match_type"] == "coerced"

    def test_case_coercion(self):
        output_df = pd.DataFrame({
            "id": [1],
            "status": ["active"],
        })
        upstream_df = pd.DataFrame({
            "id": [1],
            "status": ["ACTIVE"],
        })
        result = explain_row_context(
            output_df,
            keys=["id"],
            values={"id": 1},
            upstream={"src": upstream_df},
        )
        lineage = result["column_lineage"]
        status_col = next(c for c in lineage if c["column"] == "status")
        assert status_col["match_type"] == "coerced"


# ============================================================================
# Tests: First-match priority
# ============================================================================


class TestFirstMatchPriority:
    """Tests for first-match-wins behavior across upstream sources."""

    def test_first_upstream_wins(self):
        output_df = pd.DataFrame({"id": [1], "value": [100]})
        src_a = pd.DataFrame({"id": [1], "value": [100]})
        src_b = pd.DataFrame({"id": [1], "value": [100]})

        result = explain_row_context(
            output_df,
            keys=["id"],
            values={"id": 1},
            upstream={"src_a": src_a, "src_b": src_b},
        )
        lineage = result["column_lineage"]
        val_col = next(c for c in lineage if c["column"] == "value")
        # First match wins — src_a should be origin
        assert val_col["origin"] == "src_a"
        # But all_sources should list both
        assert len(val_col["all_sources"]) == 2



# ============================================================================
# Tests: Better origin available finding
# ============================================================================


class TestBetterOriginFinding:
    """Tests for the 'better origin available' enhancement."""

    def test_finding_surfaces_when_later_upstream_has_exact(self):
        """When first-match says 'transformed' but a later upstream has exact, finding appears."""
        output_df = pd.DataFrame({"id": [1], "value": [500]})
        src_a = pd.DataFrame({"id": [1], "value": [250]})  # transformed (250≠500)
        src_b = pd.DataFrame({"id": [1], "value": [500]})  # exact match!

        result = explain_row_context(
            output_df,
            keys=["id"],
            values={"id": 1},
            upstream={"src_a": src_a, "src_b": src_b},
        )
        # Primary origin should be src_a (first match wins)
        lineage = result["column_lineage"]
        val_col = next(c for c in lineage if c["column"] == "value")
        assert val_col["origin"] == "src_a"
        assert val_col["match_type"] == "transformed"

        # Finding should mention the better origin
        origin_findings = [f for f in result["findings"] if "consider upstream ordering" in f]
        assert len(origin_findings) == 1
        assert "src_a" in origin_findings[0]
        assert "src_b" in origin_findings[0]
        assert "exact match" in origin_findings[0]

    def test_no_finding_when_first_match_is_exact(self):
        """No spurious finding when first match is already exact."""
        output_df = pd.DataFrame({"id": [1], "value": [100]})
        src_a = pd.DataFrame({"id": [1], "value": [100]})  # exact!
        src_b = pd.DataFrame({"id": [1], "value": [100]})  # also exact

        result = explain_row_context(
            output_df,
            keys=["id"],
            values={"id": 1},
            upstream={"src_a": src_a, "src_b": src_b},
        )
        origin_findings = [f for f in result["findings"] if "consider upstream ordering" in f]
        assert len(origin_findings) == 0

    def test_no_finding_when_no_exact_alternative_exists(self):
        """No finding when all upstreams show transformed (no better option)."""
        output_df = pd.DataFrame({"id": [1], "value": [500]})
        src_a = pd.DataFrame({"id": [1], "value": [250]})  # transformed
        src_b = pd.DataFrame({"id": [1], "value": [300]})  # also transformed

        result = explain_row_context(
            output_df,
            keys=["id"],
            values={"id": 1},
            upstream={"src_a": src_a, "src_b": src_b},
        )
        origin_findings = [f for f in result["findings"] if "consider upstream ordering" in f]
        assert len(origin_findings) == 0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
