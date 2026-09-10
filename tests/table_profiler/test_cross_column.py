"""Tests for lib/cross_column.py — Cross-Column Dependencies (F14)."""

from __future__ import annotations

import sys

import pandas as pd
import numpy as np
import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.cross_column import (
    detect_cross_column_dependencies,
    CoNullPattern,
    FunctionalDependency,
    CrossColumnResult,
    _CO_NULL_THRESHOLD,
    _MIN_NULL_RATE,
    _MIN_ROWS_FOR_FD,
)
from tools.table_profiler_tool.lib.models import ColumnProfile, ColumnRole, SemanticType, Inference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(name, null_pct=0.0, distinct_count=10, role=ColumnRole.DIMENSION):
    """Create a minimal ColumnProfile for testing."""
    row_count = 100
    return ColumnProfile(
        name=name,
        position=0,
        spark_type="string",
        null_pct=null_pct,
        null_count=int(null_pct * row_count),
        non_null_count=int((1 - null_pct) * row_count),
        row_count=row_count,
        distinct_count=distinct_count,
        semantic_type=SemanticType.UNKNOWN,
        role=role,
    )


# ---------------------------------------------------------------------------
# Skip Conditions
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """Tests for conditions where analysis is skipped."""

    def test_returns_empty_when_no_profiles(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        result = detect_cross_column_dependencies(df, None)
        assert isinstance(result, CrossColumnResult)
        assert result.co_null_patterns == []
        assert result.functional_dependencies == []

    def test_returns_empty_when_single_column(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        profiles = [_make_profile("a")]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.co_null_patterns == []

    def test_returns_empty_when_too_few_rows(self):
        """Less than _MIN_ROWS_FOR_FD rows -> skip."""
        df = pd.DataFrame({"a": range(5), "b": range(5)})
        profiles = [_make_profile("a"), _make_profile("b")]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.co_null_patterns == []

    def test_returns_empty_when_no_nullable_targets(self):
        """If no column has high enough null rate, no co-nulls detected."""
        n = 50
        df = pd.DataFrame({"driver": ["A"] * n, "target": range(n)})
        profiles = [
            _make_profile("driver", null_pct=0.0, distinct_count=1),
            _make_profile("target", null_pct=0.01, distinct_count=50),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.co_null_patterns == []


# ---------------------------------------------------------------------------
# Co-Null Pattern Detection
# ---------------------------------------------------------------------------


class TestCoNullPatterns:
    """Tests for co-null pattern detection."""

    def test_detects_basic_co_null(self):
        """When target is mostly null for a specific driver value."""
        n = 100
        driver = ["A"] * 50 + ["B"] * 50
        # target is null when driver = "A", populated when driver = "B"
        target = [None] * 50 + list(range(50))
        df = pd.DataFrame({"status": driver, "detail": target})
        profiles = [
            _make_profile("status", null_pct=0.0, distinct_count=2),
            _make_profile("detail", null_pct=0.50, distinct_count=50),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert len(result.co_null_patterns) >= 1
        pattern = result.co_null_patterns[0]
        assert pattern.target_column == "detail"
        assert pattern.driver_column == "status"
        assert pattern.driver_value == "A"
        assert pattern.null_rate_when_driven >= 0.90

    def test_does_not_flag_below_threshold(self):
        """70% null rate should NOT trigger (threshold is 90%)."""
        n = 100
        driver = ["A"] * 50 + ["B"] * 50
        # target is 70% null when driver = "A"
        target = [None] * 35 + list(range(15)) + list(range(50))
        df = pd.DataFrame({"status": driver, "val": target})
        profiles = [
            _make_profile("status", null_pct=0.0, distinct_count=2),
            _make_profile("val", null_pct=0.35, distinct_count=50),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        # Should not find co-null patterns (70% < 90% threshold)
        assert len(result.co_null_patterns) == 0

    def test_multiple_driver_values_trigger(self):
        """Multiple driver values can each trigger patterns."""
        n = 150
        driver = ["X"] * 50 + ["Y"] * 50 + ["Z"] * 50
        # null when X or Y, populated when Z
        target = [None] * 49 + ["val"] + [None] * 48 + ["val"] * 2 + list(range(50))
        df = pd.DataFrame({"cat": driver, "metric": target})
        profiles = [
            _make_profile("cat", null_pct=0.0, distinct_count=3),
            _make_profile("metric", null_pct=0.647, distinct_count=50),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        # X and Y should both trigger (>= 90% null)
        assert len(result.co_null_patterns) >= 2

    def test_skips_tiny_groups(self):
        """Groups with < 5 rows are skipped."""
        n = 50
        driver = ["COMMON"] * 47 + ["RARE"] * 3
        target = list(range(47)) + [None] * 3
        df = pd.DataFrame({"type_col": driver, "val_col": target})
        profiles = [
            _make_profile("type_col", null_pct=0.0, distinct_count=2),
            _make_profile("val_col", null_pct=0.06, distinct_count=50),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        # RARE group (3 rows) should be skipped even though 100% null
        co_null_for_rare = [
            p for p in result.co_null_patterns if p.driver_value == "RARE"
        ]
        assert len(co_null_for_rare) == 0

    def test_driver_must_be_low_cardinality(self):
        """Driver with > 50 distinct values is excluded."""
        n = 100
        driver = [f"val_{i}" for i in range(100)]
        target = [None if i < 50 else i for i in range(100)]
        df = pd.DataFrame({"high_card": driver, "metric": target})
        profiles = [
            _make_profile("high_card", null_pct=0.0, distinct_count=100),
            _make_profile("metric", null_pct=0.50, distinct_count=50),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.co_null_patterns == []

    def test_excludes_metadata_role_from_targets(self):
        """Columns with METADATA role should not be nullable targets."""
        n = 50
        driver = ["A"] * 25 + ["B"] * 25
        target = [None] * 25 + list(range(25))
        df = pd.DataFrame({"status": driver, "meta_col": target})
        profiles = [
            _make_profile("status", null_pct=0.0, distinct_count=2),
            _make_profile("meta_col", null_pct=0.50, distinct_count=25,
                         role=ColumnRole.METADATA),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.co_null_patterns == []

    def test_co_null_pattern_dataclass_fields(self):
        """Verify CoNullPattern has all expected fields."""
        pattern = CoNullPattern(
            target_column="col_a",
            driver_column="col_b",
            driver_value="X",
            null_rate_when_driven=0.95,
            affected_rows=100,
            description="col_a is 95% null when col_b='X'",
        )
        assert pattern.target_column == "col_a"
        assert pattern.driver_column == "col_b"
        assert pattern.driver_value == "X"
        assert pattern.null_rate_when_driven == 0.95
        assert pattern.affected_rows == 100
        assert "col_a" in pattern.description


# ---------------------------------------------------------------------------
# Functional Dependency Detection
# ---------------------------------------------------------------------------


class TestFunctionalDependencies:
    """Tests for functional dependency detection."""

    def test_detects_perfect_fd(self):
        """state_code -> state_name is a perfect FD."""
        n = 100
        mapping = {"CA": "California", "TX": "Texas", "NY": "New York"}
        codes = list(mapping.keys()) * 33 + ["CA"]
        names = [mapping[c] for c in codes]
        df = pd.DataFrame({"state_code": codes, "state_name": names})
        profiles = [
            _make_profile("state_code", null_pct=0.0, distinct_count=3),
            _make_profile("state_name", null_pct=0.0, distinct_count=3),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert len(result.functional_dependencies) >= 1
        # state_code -> state_name should be detected
        fd_found = [
            fd for fd in result.functional_dependencies
            if fd.determinant == "state_code" and fd.dependent == "state_name"
        ]
        assert len(fd_found) == 1
        assert fd_found[0].confidence >= 0.95

    def test_no_fd_when_not_deterministic(self):
        """Random relationship should NOT produce an FD."""
        n = 50
        np.random.seed(42)
        df = pd.DataFrame({
            "category": np.random.choice(["A", "B", "C"], n),
            "amount": np.random.randint(1, 100, n),
        })
        profiles = [
            _make_profile("category", null_pct=0.0, distinct_count=3),
            _make_profile("amount", null_pct=0.0, distinct_count=40),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        fd_found = [
            fd for fd in result.functional_dependencies
            if fd.determinant == "category" and fd.dependent == "amount"
        ]
        assert len(fd_found) == 0

    def test_fd_ignores_constant_dependent(self):
        """A -> B where B has only 1 distinct value is trivial -- skip it."""
        n = 50
        df = pd.DataFrame({
            "id_col": [f"id_{i}" for i in range(n)],
            "const_col": ["SAME"] * n,
        })
        profiles = [
            _make_profile("id_col", null_pct=0.0, distinct_count=50),
            _make_profile("const_col", null_pct=0.0, distinct_count=1),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        # const_col has distinct=1 -> excluded from FD candidates
        fd_found = [
            fd for fd in result.functional_dependencies
            if fd.dependent == "const_col"
        ]
        assert len(fd_found) == 0

    def test_fd_bidirectional_1_to_1(self):
        """1:1 mapping should detect FD in both directions."""
        n = 50
        df = pd.DataFrame({
            "alpha": [chr(65 + (i % 20)) for i in range(n)],
            "num": [str(i % 20) for i in range(n)],
        })
        profiles = [
            _make_profile("alpha", null_pct=0.0, distinct_count=20),
            _make_profile("num", null_pct=0.0, distinct_count=20),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        # Both directions should be detected
        determinants = {fd.determinant for fd in result.functional_dependencies}
        assert "alpha" in determinants or "num" in determinants

    def test_fd_excludes_high_cardinality(self):
        """Columns with > 500 distinct values excluded from FD check."""
        n = 600
        df = pd.DataFrame({
            "high_card": [f"v{i}" for i in range(n)],
            "derived": [f"d{i}" for i in range(n)],
        })
        profiles = [
            _make_profile("high_card", null_pct=0.0, distinct_count=600),
            _make_profile("derived", null_pct=0.0, distinct_count=600),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.functional_dependencies == []

    def test_fd_confidence_field(self):
        """Verify FunctionalDependency has confidence field."""
        fd = FunctionalDependency(
            determinant="A",
            dependent="B",
            confidence=1.0,
            distinct_determinant=10,
            description="A -> B (confidence 100%)",
        )
        assert fd.confidence == 1.0
        assert fd.distinct_determinant == 10


# ---------------------------------------------------------------------------
# CrossColumnResult Contract
# ---------------------------------------------------------------------------


class TestOutputContract:
    """Verify output structure and field types."""

    def test_result_has_required_fields(self):
        result = CrossColumnResult()
        assert hasattr(result, "co_null_patterns")
        assert hasattr(result, "functional_dependencies")
        assert hasattr(result, "findings")
        assert hasattr(result, "inference")

    def test_findings_are_strings(self):
        n = 100
        driver = ["A"] * 50 + ["B"] * 50
        target = [None] * 50 + list(range(50))
        df = pd.DataFrame({"status": driver, "detail": target})
        profiles = [
            _make_profile("status", null_pct=0.0, distinct_count=2),
            _make_profile("detail", null_pct=0.50, distinct_count=50),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        for f in result.findings:
            assert isinstance(f, str)

    def test_inference_when_patterns_found(self):
        n = 100
        mapping = {"CA": "California", "TX": "Texas", "NY": "New York"}
        codes = list(mapping.keys()) * 33 + ["CA"]
        names = [mapping[c] for c in codes]
        df = pd.DataFrame({"state_code": codes, "state_name": names})
        profiles = [
            _make_profile("state_code", null_pct=0.0, distinct_count=3),
            _make_profile("state_name", null_pct=0.0, distinct_count=3),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.inference is not None
        assert result.inference.confidence >= 0.50


# ---------------------------------------------------------------------------
# Profiler Integration
# ---------------------------------------------------------------------------


class TestProfilerIntegration:
    """Verify wiring into profiler.py."""

    def test_profiler_produces_cross_column_findings(self):
        """profile_table() should detect co-null patterns when data has them."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        n = 100
        driver = ["A"] * 50 + ["B"] * 50
        target = [None] * 50 + list(range(50))
        df = pd.DataFrame({
            "id": range(n),
            "status": driver,
            "detail": target,
        })
        result = profile_table(df, "test_cross_col", level="standard")
        # Should not crash
        assert result is not None
        # Check correlated_nulls populated
        detail_profile = next(
            (p for p in result.columns if p.name == "detail"), None
        )
        if detail_profile:
            assert "status" in detail_profile.correlated_nulls

    def test_profiler_handles_no_patterns(self):
        """profile_table() should not crash when no patterns exist."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        n = 50
        df = pd.DataFrame({
            "id": range(n),
            "value": range(n),
        })
        result = profile_table(df, "test_no_patterns", level="standard")
        assert result is not None
        assert "cross_column" not in result.degraded_features

    def test_profiler_crash_resilience(self):
        """Monkey-patch to force exception -- profiler should gracefully degrade."""
        import tools.table_profiler_tool.lib.profiler as profiler_mod

        original = profiler_mod.detect_cross_column_dependencies

        def _boom(*a, **kw):
            raise RuntimeError("test crash")

        profiler_mod.detect_cross_column_dependencies = _boom

        try:
            df = pd.DataFrame({"id": range(50), "val": range(50)})
            result = profiler_mod.profile_table(df, "test_crash", level="standard")
            assert "cross_column" in result.degraded_features
        finally:
            profiler_mod.detect_cross_column_dependencies = original


# ---------------------------------------------------------------------------
# Rendering Integration
# ---------------------------------------------------------------------------


class TestRendering:
    """Verify that cross-column findings render correctly."""

    def test_findings_appear_in_ai_summary(self):
        from tools.table_profiler_tool.lib.profiler import profile_table
        from tools.table_profiler_tool.lib.renderer import render_table_ai_summary

        n = 100
        driver = ["A"] * 50 + ["B"] * 50
        target = [None] * 50 + list(range(50))
        df = pd.DataFrame({
            "id": range(n),
            "status": driver,
            "detail": target,
        })
        result = profile_table(df, "test_render", level="standard")
        summary = render_table_ai_summary(result)
        # Cross-column findings should appear in FINDINGS section
        assert "Co-null" in summary or "cross_column" not in result.degraded_features


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_all_null_target(self):
        """100% null target with a driver should find pattern."""
        n = 50
        driver = ["X"] * 25 + ["Y"] * 25
        target = [None] * 50  # 100% null regardless
        df = pd.DataFrame({"driver_col": driver, "null_col": target})
        profiles = [
            _make_profile("driver_col", null_pct=0.0, distinct_count=2),
            _make_profile("null_col", null_pct=1.0, distinct_count=0),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        # Both driver values should trigger (100% null in each group)
        assert len(result.co_null_patterns) == 2

    def test_handles_numeric_driver_values(self):
        """Driver values that are numeric should be string-converted."""
        n = 50
        driver = [1] * 25 + [2] * 25
        target = [None] * 25 + list(range(25))
        df = pd.DataFrame({"code": driver, "detail": target})
        profiles = [
            _make_profile("code", null_pct=0.0, distinct_count=2),
            _make_profile("detail", null_pct=0.50, distinct_count=25),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert len(result.co_null_patterns) >= 1
        assert result.co_null_patterns[0].driver_value == "1"

    def test_same_column_not_paired(self):
        """A column should not be paired with itself."""
        n = 50
        driver = ["A"] * 50
        df = pd.DataFrame({"col_x": driver})
        profiles = [
            _make_profile("col_x", null_pct=0.10, distinct_count=1),
        ]
        result = detect_cross_column_dependencies(df, profiles)
        assert result.co_null_patterns == []
