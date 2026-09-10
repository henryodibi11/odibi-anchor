"""Tests for coercion classifier."""

from __future__ import annotations

import pandas as pd
import pytest

from odibi_anchor.tables.coercion_classifier import (
    _classify_pair,
    coercion_check_context,
)


class TestClassifyPair:
    """Test _classify_pair for all categories."""

    def test_identical(self):
        assert _classify_pair("hello", "hello") == "identical"

    def test_whitespace_leading_trailing(self):
        assert _classify_pair("  Active ", "Active") == "whitespace"

    def test_whitespace_internal(self):
        assert _classify_pair("New  York", "New York") == "whitespace"

    def test_case(self):
        assert _classify_pair("ACTIVE", "Active") == "case"

    def test_unicode_zero_width_space(self):
        assert _classify_pair("PJM\u200b", "PJM") == "unicode"

    def test_unicode_feff(self):
        assert _classify_pair("\ufeffvalue", "value") == "unicode"

    def test_numeric_float_suffix(self):
        assert _classify_pair("2026.0", "2026") == "numeric_representation"

    def test_numeric_negative(self):
        assert _classify_pair("-5.0", "-5") == "numeric_representation"

    def test_date_format(self):
        assert _classify_pair("05/14/2026", "2026-05-14") == "date_format"

    def test_genuine(self):
        assert _classify_pair("Wind", "Solar") == "genuine"

    def test_priority_whitespace_before_case(self):
        """Whitespace check happens before case check."""
        assert _classify_pair("  active  ", "active") == "whitespace"


class TestCoercionCheckContext:
    """Test full coercion_check_context function."""

    def test_basic_numeric_mismatch(self):
        old = pd.DataFrame({"id": [1, 2, 3], "year": ["2026.0", "2025.0", "2024.0"]})
        new = pd.DataFrame({"id": [1, 2, 3], "year": ["2026", "2025", "2024"]})
        ctx = coercion_check_context(old, new, keys=["id"], columns=["year"])
        assert ctx["kind"] == "coercion_check_context"
        cr = ctx["column_results"]["year"]
        assert cr["dominant_category"] == "numeric_representation"
        assert cr["total_mismatches"] == 3
        assert cr["confidence"] == 1.0

    def test_mixed_categories(self):
        old = pd.DataFrame({
            "id": [1, 2, 3, 4],
            "val": ["  hello  ", "WORLD", "2026.0", "abc"],
        })
        new = pd.DataFrame({
            "id": [1, 2, 3, 4],
            "val": ["hello", "world", "2026", "xyz"],
        })
        ctx = coercion_check_context(old, new, keys=["id"], columns=["val"])
        cats = ctx["column_results"]["val"]["categories"]
        assert cats.get("whitespace", 0) == 1
        assert cats.get("case", 0) == 1
        assert cats.get("numeric_representation", 0) == 1
        assert cats.get("genuine", 0) == 1

    def test_representation_pct(self):
        old = pd.DataFrame({"id": [1, 2, 3], "v": ["2026.0", "2025.0", "abc"]})
        new = pd.DataFrame({"id": [1, 2, 3], "v": ["2026", "2025", "xyz"]})
        ctx = coercion_check_context(old, new, keys=["id"], columns=["v"])
        # 2 representation, 1 genuine -> pct = 2/3
        assert ctx["metrics"]["representation_pct"] == pytest.approx(2/3, abs=0.01)

    def test_columns_none_checks_all(self):
        """When columns=None, checks all non-key common columns."""
        old = pd.DataFrame({"id": [1], "a": ["X"], "b": ["Y"]})
        new = pd.DataFrame({"id": [1], "a": ["x"], "b": ["y"]})
        ctx = coercion_check_context(old, new, keys=["id"])
        assert ctx["metrics"]["columns_checked"] == 2

    def test_no_mismatches(self):
        old = pd.DataFrame({"id": [1, 2], "val": ["a", "b"]})
        new = pd.DataFrame({"id": [1, 2], "val": ["a", "b"]})
        ctx = coercion_check_context(old, new, keys=["id"], columns=["val"])
        assert ctx["metrics"]["total_mismatches"] == 0

    def test_output_schema_keys(self):
        old = pd.DataFrame({"id": [1], "val": ["A"]})
        new = pd.DataFrame({"id": [1], "val": ["a"]})
        ctx = coercion_check_context(old, new, keys=["id"])
        for key in ["kind", "subject", "summary", "metrics", "column_results", "findings", "risks", "samples", "suggested_next_actions"]:
            assert key in ctx, f"Missing key: {key}"

    def test_markdown_output(self):
        old = pd.DataFrame({"id": [1], "val": ["2026.0"]})
        new = pd.DataFrame({"id": [1], "val": ["2026"]})
        result = coercion_check_context(old, new, keys=["id"], output_format="markdown")
        assert isinstance(result, str)
        assert "coercion" in result.lower() or "Coercion" in result

    def test_samples_capped(self):
        old = pd.DataFrame({"id": range(50), "v": ["2026.0"] * 50})
        new = pd.DataFrame({"id": range(50), "v": ["2026"] * 50})
        ctx = coercion_check_context(old, new, keys=["id"], columns=["v"], sample_limit=5)
        assert len(ctx["samples"]["v"]) <= 5


class TestCoercionCheckIntegration:
    """Integration tests with real queue automation data."""

    @pytest.fixture
    def idb_data(self):
        pytest.importorskip("openpyxl")
        import os
        fixtures = os.path.join(os.path.dirname(__file__), "fixtures")
        old_path = os.path.join(fixtures, "CRM_data_2026_05_19.xlsx")
        new_path = os.path.join(fixtures, "CRM_data_2026_05_29_GOLD.xlsx")
        if not os.path.exists(old_path) or not os.path.exists(new_path):
            pytest.skip("Fixture files not available")
        old = pd.read_excel(old_path, sheet_name="INTERCONNECTIONS DB")
        new = pd.read_excel(new_path, sheet_name="INTERCONNECTIONS DB")
        return old, new

    def test_requested_cod_year_no_coercion(self, idb_data):
        """Requested COD Year diffs are null transitions, not coercion."""
        old, new = idb_data
        ctx = coercion_check_context(
            old, new, keys=["Application ID"],
            columns=["Requested COD Year"],
        )
        cr = ctx["column_results"].get("Requested COD Year", {})
        assert cr["total_mismatches"] == 0

    def test_unicode_mismatches_detected(self, idb_data):
        """Real data contains unicode/invisible-char mismatches."""
        old, new = idb_data
        ctx = coercion_check_context(
            old, new, keys=["Application ID"],
        )
        cat_totals = ctx["metrics"]["category_totals"]
        assert cat_totals.get("unicode", 0) > 0
