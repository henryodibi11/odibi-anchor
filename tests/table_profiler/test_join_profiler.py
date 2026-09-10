"""Tests for lib/join_profiler.py — Join Readiness (F13)."""

from __future__ import annotations

import sys

import pandas as pd
import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.join_profiler import (
    _check_format_compatibility_samples,
    _determine_cardinality,
    profile_joins,
)
from tools.table_profiler_tool.lib.models import JoinProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _source_df() -> pd.DataFrame:
    """Source fact table with customer_id FK."""
    return pd.DataFrame({
        "order_id": list(range(100)),
        "customer_id": [f"CUST-{i % 20:03d}" for i in range(100)],
        "amount": [float(i * 10) for i in range(100)],
    })


def _dim_customers() -> pd.DataFrame:
    """Dimension table — 25 customers, so 20 overlap + 5 no match from source."""
    return pd.DataFrame({
        "id": [f"CUST-{i:03d}" for i in range(25)],
        "name": [f"Customer {i}" for i in range(25)],
    })


def _dim_products() -> pd.DataFrame:
    """Dimension table with all product codes matching."""
    return pd.DataFrame({
        "code": [f"PROD-{i:03d}" for i in range(50)],
        "description": [f"Product {i}" for i in range(50)],
    })


# ---------------------------------------------------------------------------
# Skip / edge case tests
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """Tests where profile_joins returns empty or handles gracefully."""

    def test_empty_reference_tables(self):
        df = _source_df()
        result = profile_joins(df, {})
        assert result == []

    def test_none_reference_tables(self):
        df = _source_df()
        result = profile_joins(df, {})
        assert result == []

    def test_missing_source_column(self):
        df = _source_df()
        ref = {"nonexistent_col": (_dim_customers(), "id", "dim_customers")}
        result = profile_joins(df, ref)
        assert result == []

    def test_missing_target_column(self):
        df = _source_df()
        ref = {"customer_id": (_dim_customers(), "nonexistent", "dim_customers")}
        result = profile_joins(df, ref)
        assert result == []

    def test_invalid_target_spec(self):
        df = _source_df()
        ref = {"customer_id": 12345}  # Invalid type
        result = profile_joins(df, ref)
        assert result == []


# ---------------------------------------------------------------------------
# Overlap / orphan tests
# ---------------------------------------------------------------------------


class TestOverlapAnalysis:
    """Tests for overlap and orphan detection."""

    def test_full_overlap(self):
        """When all source values exist in target → 100% overlap."""
        source = pd.DataFrame({"key": ["A", "B", "C"]})
        target = pd.DataFrame({"id": ["A", "B", "C", "D", "E"]})
        ref = {"key": (target, "id", "dim_table")}

        result = profile_joins(source, ref)
        assert len(result) == 1
        jp = result[0]
        assert jp.overlap_pct == 1.0
        assert jp.orphan_count == 0
        assert jp.orphan_pct == 0.0
        assert jp.safe_join_type == "INNER"

    def test_partial_overlap(self):
        """Some source values not in target → orphans exist."""
        source = pd.DataFrame({"key": ["A", "B", "C", "X", "Y"]})
        target = pd.DataFrame({"id": ["A", "B", "C"]})
        ref = {"key": (target, "id", "dim_table")}

        result = profile_joins(source, ref)
        assert len(result) == 1
        jp = result[0]
        assert jp.overlap_pct == 0.6  # 3 of 5
        assert jp.orphan_count == 2
        assert jp.orphan_pct == 0.4
        assert jp.safe_join_type == "LEFT"

    def test_no_overlap(self):
        """Zero overlap between source and target."""
        source = pd.DataFrame({"key": ["X", "Y", "Z"]})
        target = pd.DataFrame({"id": ["A", "B", "C"]})
        ref = {"key": (target, "id", "dim_table")}

        result = profile_joins(source, ref)
        assert len(result) == 1
        jp = result[0]
        assert jp.overlap_pct == 0.0
        assert jp.orphan_count == 3
        assert jp.safe_join_type == "LEFT"

    def test_realistic_scenario(self):
        """Source has 20 distinct customer_ids, dim has 25 → full overlap."""
        source = _source_df()
        dim = _dim_customers()
        ref = {"customer_id": (dim, "id", "dim_customers")}

        result = profile_joins(source, ref)
        assert len(result) == 1
        jp = result[0]
        # Source has CUST-000 to CUST-019, dim has CUST-000 to CUST-024
        assert jp.overlap_pct == 1.0
        assert jp.orphan_count == 0
        assert jp.safe_join_type == "INNER"


# ---------------------------------------------------------------------------
# Cardinality tests
# ---------------------------------------------------------------------------


class TestCardinality:
    """Tests for cardinality detection."""

    def test_one_to_one(self):
        """Source unique, target unique → 1:1."""
        assert _determine_cardinality(1.0, True, True) == "1:1"

    def test_many_to_one(self):
        """Source has duplicates, target unique → many:1."""
        assert _determine_cardinality(0.5, False, True) == "many:1"

    def test_one_to_many(self):
        """Source unique, target has duplicates → 1:many."""
        assert _determine_cardinality(1.0, True, False) == "1:many"

    def test_many_to_many(self):
        """Both sides have duplicates → many:many."""
        assert _determine_cardinality(0.5, False, False) == "many:many"

    def test_fact_to_dim_is_many_to_one(self):
        """Typical fact→dim pattern: fact has dupes on FK, dim is unique."""
        source = _source_df()  # 100 rows, 20 distinct customer_ids
        dim = _dim_customers()  # 25 rows, 25 distinct ids
        ref = {"customer_id": (dim, "id", "dim_customers")}

        result = profile_joins(source, ref)
        assert len(result) == 1
        assert result[0].cardinality == "many:1"


# ---------------------------------------------------------------------------
# Format compatibility tests
# ---------------------------------------------------------------------------


class TestFormatCompatibility:
    """Tests for format mismatch detection."""

    def test_compatible_formats(self):
        """Same format on both sides → compatible."""
        source_sample = ["ABC-001", "ABC-002", "ABC-003"]
        target_sample = ["ABC-001", "ABC-004", "ABC-005"]
        compat, desc = _check_format_compatibility_samples(source_sample, target_sample)
        assert compat is True
        assert desc is None

    def test_case_mismatch(self):
        """Source upper, target lower → mismatch."""
        source_sample = ["ABC", "DEF", "GHI"]
        target_sample = ["abc", "def", "ghi"]
        compat, desc = _check_format_compatibility_samples(source_sample, target_sample)
        assert compat is False
        assert "case mismatch" in desc

    def test_whitespace_mismatch(self):
        """One side has trailing spaces → mismatch."""
        source_sample = ["ABC ", "DEF ", "GHI "]
        target_sample = ["ABC", "DEF", "GHI"]
        compat, desc = _check_format_compatibility_samples(source_sample, target_sample)
        assert compat is False
        assert "whitespace" in desc

    def test_dash_underscore_mismatch(self):
        """Source has dashes, target has underscores."""
        source_sample = ["US-EAST-1", "US-WEST-2", "EU-WEST-1"]
        target_sample = ["US_EAST_1", "US_WEST_2", "EU_WEST_1"]
        compat, desc = _check_format_compatibility_samples(source_sample, target_sample)
        assert compat is False
        assert "dashes" in desc or "underscores" in desc


# ---------------------------------------------------------------------------
# Multiple joins test
# ---------------------------------------------------------------------------


class TestMultipleJoins:
    """Tests for profiling multiple join relationships at once."""

    def test_multiple_references(self):
        """Profile two joins in one call."""
        source = pd.DataFrame({
            "customer_id": ["C1", "C2", "C3"],
            "product_code": ["P1", "P2", "P3"],
            "amount": [100, 200, 300],
        })
        dim_cust = pd.DataFrame({"id": ["C1", "C2", "C3", "C4"]})
        dim_prod = pd.DataFrame({"code": ["P1", "P2", "P5"]})

        ref = {
            "customer_id": (dim_cust, "id", "dim_customers"),
            "product_code": (dim_prod, "code", "dim_products"),
        }

        result = profile_joins(source, ref)
        assert len(result) == 2

        cust_join = next(jp for jp in result if jp.source_column == "customer_id")
        prod_join = next(jp for jp in result if jp.source_column == "product_code")

        assert cust_join.overlap_pct == 1.0
        assert prod_join.overlap_pct == pytest.approx(2 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# Output contract tests
# ---------------------------------------------------------------------------


class TestOutputContract:
    """Tests that output matches JoinProfile contract."""

    def test_returns_join_profile_instances(self):
        source = pd.DataFrame({"key": ["A", "B", "C"]})
        target = pd.DataFrame({"id": ["A", "B", "D"]})
        ref = {"key": (target, "id", "dim_table")}

        result = profile_joins(source, ref)
        assert len(result) == 1
        jp = result[0]
        assert isinstance(jp, JoinProfile)
        assert jp.source_column == "key"
        assert jp.target_table == "dim_table"
        assert jp.target_column == "id"
        assert jp.cardinality in ("1:1", "1:many", "many:1", "many:many")
        assert 0.0 <= jp.overlap_pct <= 1.0
        assert jp.orphan_count >= 0
        assert 0.0 <= jp.orphan_pct <= 1.0
        assert isinstance(jp.format_compatible, bool)
        assert jp.safe_join_type in ("INNER", "LEFT")
        assert jp.inference is not None
        assert jp.inference.method == "join_profiling"


# ---------------------------------------------------------------------------
# Profiler integration tests
# ---------------------------------------------------------------------------


class TestProfilerIntegration:
    """Test that profile_table wires join profiling via opts."""

    def test_profile_table_with_reference_tables(self):
        from tools.table_profiler_tool.lib.profiler import profile_table

        source = pd.DataFrame({
            "order_id": list(range(20)),
            "customer_id": [f"C{i % 5}" for i in range(20)],
            "amount": [float(i) for i in range(20)],
        })
        dim = pd.DataFrame({"id": [f"C{i}" for i in range(5)]})

        tp = profile_table(
            source,
            subject="test.orders",
            reference_tables={"customer_id": (dim, "id", "dim_customers")},
        )
        assert len(tp.joins) == 1
        assert tp.joins[0].source_column == "customer_id"
        assert tp.joins[0].overlap_pct == 1.0

    def test_profile_table_without_reference_tables(self):
        from tools.table_profiler_tool.lib.profiler import profile_table

        source = pd.DataFrame({
            "id": list(range(20)),
            "val": [f"v{i}" for i in range(20)],
        })

        tp = profile_table(source, subject="test.simple")
        assert tp.joins == []


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


class TestRendering:
    """Test that renderers include join section when joins present."""

    def test_ai_summary_includes_joins(self):
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_ai_summary

        tp = TableProfile(
            subject="test.table",
            row_count=100,
            column_count=3,
            joins=[
                JoinProfile(
                    source_column="customer_id",
                    target_table="dim_customers",
                    target_column="id",
                    cardinality="many:1",
                    overlap_pct=0.987,
                    orphan_count=13,
                    orphan_pct=0.013,
                    format_compatible=True,
                    safe_join_type="LEFT",
                ),
            ],
        )
        output = render_table_ai_summary(tp)
        assert "JOINS" in output
        assert "customer_id" in output
        assert "dim_customers" in output
        assert "many:1" in output

    def test_md_report_includes_joins(self):
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_profile_md

        tp = TableProfile(
            subject="test.table",
            row_count=100,
            column_count=3,
            joins=[
                JoinProfile(
                    source_column="product_code",
                    target_table="dim_products",
                    target_column="code",
                    cardinality="many:1",
                    overlap_pct=1.0,
                    orphan_count=0,
                    orphan_pct=0.0,
                    format_compatible=True,
                    safe_join_type="INNER",
                ),
            ],
        )
        output = render_table_profile_md(tp)
        assert "## Join Readiness" in output
        assert "product_code" in output
        assert "INNER" in output

    def test_md_report_omits_joins_when_empty(self):
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_profile_md

        tp = TableProfile(subject="test.table", row_count=100, column_count=3)
        output = render_table_profile_md(tp)
        assert "Join Readiness" not in output
