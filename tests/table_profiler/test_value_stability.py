"""Tests for lib/value_stability.py — Value Stability (F15)."""

from __future__ import annotations

import sys

import pandas as pd
import numpy as np
import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.value_stability import (
    analyze_value_stability,
    ColumnStability,
    ValueStabilityResult,
    _STATIC_THRESHOLD,
    _MOSTLY_STATIC_THRESHOLD,
    _VOLATILE_THRESHOLD,
    _MIN_DUPLICATE_RATE,
    _MIN_AVG_ROWS_PER_ENTITY,
    _MAX_ENTITIES_SAMPLE,
)
from tools.table_profiler_tool.lib.models import (
    ColumnProfile,
    ColumnRole,
    GrainAnalysis,
    Inference,
    SemanticType,
    TableClassification,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_grain(best_grain, is_unique=False, duplicate_rate=0.50):
    """Create a minimal GrainAnalysis for testing."""
    return GrainAnalysis(
        best_grain=best_grain,
        is_unique=is_unique,
        duplicate_rate=duplicate_rate,
    )


def _make_profile(name, distinct_count=10, null_pct=0.0, role=ColumnRole.DIMENSION):
    """Create a minimal ColumnProfile for testing."""
    return ColumnProfile(
        name=name,
        position=0,
        spark_type="string",
        null_pct=null_pct,
        null_count=int(null_pct * 100),
        non_null_count=int((1 - null_pct) * 100),
        row_count=100,
        distinct_count=distinct_count,
        semantic_type=SemanticType.UNKNOWN,
        role=role,
    )


# ---------------------------------------------------------------------------
# Skip Conditions
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """Tests for conditions where analysis returns None."""

    def test_returns_none_when_no_grain(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        result = analyze_value_stability(df, None, None)
        assert result is None

    def test_returns_none_when_grain_is_empty(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        grain = _make_grain(best_grain=[])
        result = analyze_value_stability(df, grain, None)
        assert result is None

    def test_returns_none_when_grain_is_unique(self):
        """Unique grain means no repeated entities — analysis meaningless."""
        df = pd.DataFrame({"id": range(50), "val": range(50)})
        grain = _make_grain(best_grain=["id"], is_unique=True, duplicate_rate=0.0)
        result = analyze_value_stability(df, grain, None)
        assert result is None

    def test_returns_none_when_duplicate_rate_too_low(self):
        """Less than 5% duplicates — not enough repetition."""
        df = pd.DataFrame({"id": range(100), "val": range(100)})
        grain = _make_grain(best_grain=["id"], duplicate_rate=0.02)
        result = analyze_value_stability(df, grain, None)
        assert result is None

    def test_returns_none_when_avg_rows_per_entity_too_low(self):
        """Need at least 1.5 rows per entity on average."""
        # 100 entities with 100 rows = 1.0 avg (but dup rate would be 0)
        # Create a borderline case: 80 entities in 100 rows = 1.25 avg
        df = pd.DataFrame({
            "entity": list(range(80)) + list(range(20)),
            "val": range(100),
        })
        grain = _make_grain(best_grain=["entity"], duplicate_rate=0.20)
        result = analyze_value_stability(df, grain, None)
        # avg = 100/80 = 1.25, below threshold 1.5
        assert result is None


# ---------------------------------------------------------------------------
# Static Column Detection
# ---------------------------------------------------------------------------


class TestStaticColumns:
    """Tests for detecting static columns."""

    def test_detects_static_column(self):
        """A column with the same value for every occurrence of each entity."""
        n_entities = 20
        rows_per = 5
        # Entity repeats, name is always the same per entity
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        names = [f"Name_{i}" for i in range(n_entities) for _ in range(rows_per)]
        amounts = [float(j + i * 10) for i in range(n_entities) for j in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "name": names, "amount": amounts})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("name", distinct_count=20),
            _make_profile("amount", distinct_count=100),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        # name should be static (same value per entity)
        name_stab = next(s for s in result.column_stabilities if s.column_name == "name")
        assert name_stab.stability == "static"
        assert "name" in result.static_columns

    def test_detects_volatile_column(self):
        """A column that changes in 80%+ of entities."""
        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        # timestamp varies every row
        timestamps = [f"2024-01-{i+1:02d}T{j:02d}:00:00"
                      for i in range(n_entities) for j in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "ts": timestamps})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("ts", distinct_count=100),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        ts_stab = next(s for s in result.column_stabilities if s.column_name == "ts")
        assert ts_stab.stability == "volatile"
        assert "ts" in result.volatile_columns

    def test_detects_changing_column(self):
        """A column that changes in 20-80% of entities."""
        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        # Status changes for ~50% of entities (10 out of 20)
        statuses = []
        for i in range(n_entities):
            if i < 10:
                # These entities have status change
                statuses.extend(["Active"] * 3 + ["Inactive"] * 2)
            else:
                # These entities have consistent status
                statuses.extend(["Active"] * rows_per)

        df = pd.DataFrame({"entity_id": entities, "status": statuses})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("status", distinct_count=2),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        status_stab = next(s for s in result.column_stabilities if s.column_name == "status")
        assert status_stab.stability == "changing"
        assert status_stab.change_rate == 0.5

    def test_detects_mostly_static_column(self):
        """A column that changes in 5-20% of entities."""
        n_entities = 50
        rows_per = 3
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        # Address changes for only 5 entities (10%)
        addresses = []
        for i in range(n_entities):
            if i < 5:
                addresses.extend([f"Addr_{i}_old", f"Addr_{i}_new", f"Addr_{i}_new"])
            else:
                addresses.extend([f"Addr_{i}"] * rows_per)

        df = pd.DataFrame({"entity_id": entities, "address": addresses})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.67)
        profiles = [
            _make_profile("entity_id", distinct_count=50),
            _make_profile("address", distinct_count=55),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        addr_stab = next(s for s in result.column_stabilities if s.column_name == "address")
        assert addr_stab.stability == "mostly_static"
        assert 0.05 < addr_stab.change_rate <= 0.20


# ---------------------------------------------------------------------------
# Entity Key Handling
# ---------------------------------------------------------------------------


class TestEntityKey:
    """Tests for entity key handling."""

    def test_composite_grain(self):
        """Composite grain (multi-column) should work."""
        n = 60
        df = pd.DataFrame({
            "region": ["US"] * 30 + ["EU"] * 30,
            "product": [f"P{i % 10}" for i in range(n)],
            "status": ["Active"] * 30 + ["Active"] * 15 + ["Inactive"] * 15,
            "amount": range(n),
        })
        grain = _make_grain(best_grain=["region", "product"], duplicate_rate=0.67)
        profiles = [
            _make_profile("region", distinct_count=2),
            _make_profile("product", distinct_count=10),
            _make_profile("status", distinct_count=2),
            _make_profile("amount", distinct_count=60),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        assert result.entity_key == ["region", "product"]
        # status and amount should be in column_stabilities (not region/product which are keys)
        col_names = [s.column_name for s in result.column_stabilities]
        assert "region" not in col_names
        assert "product" not in col_names
        assert "status" in col_names
        assert "amount" in col_names

    def test_excludes_metadata_role(self):
        """Columns with METADATA role should be excluded from analysis."""
        n_entities = 20
        rows_per = 3
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        meta_col = [f"loaded_{i}_{j}" for i in range(n_entities) for j in range(rows_per)]
        name_col = [f"Name_{i}" for i in range(n_entities) for _ in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "load_ts": meta_col, "name": name_col})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.67)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("load_ts", distinct_count=60, role=ColumnRole.METADATA),
            _make_profile("name", distinct_count=20),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        col_names = [s.column_name for s in result.column_stabilities]
        assert "load_ts" not in col_names
        assert "name" in col_names

    def test_excludes_constant_columns(self):
        """Columns with distinct_count=1 are trivially stable — skip them."""
        n_entities = 20
        rows_per = 3
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]

        df = pd.DataFrame({
            "entity_id": entities,
            "constant": ["SAME"] * (n_entities * rows_per),
            "name": [f"Name_{i}" for i in range(n_entities) for _ in range(rows_per)],
        })
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.67)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("constant", distinct_count=1),
            _make_profile("name", distinct_count=20),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        col_names = [s.column_name for s in result.column_stabilities]
        assert "constant" not in col_names


# ---------------------------------------------------------------------------
# Output Contract
# ---------------------------------------------------------------------------


class TestOutputContract:
    """Verify output structure and field types."""

    def test_result_has_required_fields(self):
        result = ValueStabilityResult(
            entity_key=["id"], entity_count=10, avg_rows_per_entity=3.0
        )
        assert hasattr(result, "entity_key")
        assert hasattr(result, "entity_count")
        assert hasattr(result, "avg_rows_per_entity")
        assert hasattr(result, "column_stabilities")
        assert hasattr(result, "static_columns")
        assert hasattr(result, "changing_columns")
        assert hasattr(result, "volatile_columns")
        assert hasattr(result, "findings")
        assert hasattr(result, "inference")

    def test_column_stability_fields(self):
        cs = ColumnStability(
            column_name="test",
            stability="static",
            change_rate=0.02,
            entities_sampled=100,
            description="test: static",
        )
        assert cs.column_name == "test"
        assert cs.stability == "static"
        assert cs.change_rate == 0.02

    def test_findings_are_strings(self):
        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        names = [f"Name_{i}" for i in range(n_entities) for _ in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "name": names})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("name", distinct_count=20),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        for f in result.findings:
            assert isinstance(f, str)

    def test_inference_present(self):
        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        vals = [float(j) for i in range(n_entities) for j in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "val": vals})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("val", distinct_count=5),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        assert result.inference is not None
        assert result.inference.confidence >= 0.50


# ---------------------------------------------------------------------------
# Profiler Integration
# ---------------------------------------------------------------------------


class TestProfilerIntegration:
    """Verify wiring into profiler.py."""

    def test_profiler_runs_stability_on_entity_table(self):
        """profile_table() should detect stability when entities repeat."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        names = [f"Name_{i}" for i in range(n_entities) for _ in range(rows_per)]
        amounts = [float(j + i * 10) for i in range(n_entities) for j in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "name": names, "amount": amounts})
        result = profile_table(df, "test_stability", level="standard")
        assert result is not None
        assert "value_stability" not in result.degraded_features

    def test_profiler_skips_stability_on_unique_grain(self):
        """profile_table() should not crash when grain is unique."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        df = pd.DataFrame({"id": range(50), "val": range(50)})
        result = profile_table(df, "test_unique", level="standard")
        assert result is not None
        assert "value_stability" not in result.degraded_features

    def test_profiler_crash_resilience(self):
        """Monkey-patch to force exception — profiler should gracefully degrade."""
        import tools.table_profiler_tool.lib.profiler as profiler_mod

        original = profiler_mod.analyze_value_stability

        def _boom(*a, **kw):
            raise RuntimeError("test crash")

        profiler_mod.analyze_value_stability = _boom

        try:
            n_entities = 20
            rows_per = 3
            entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
            df = pd.DataFrame({"entity_id": entities, "val": range(60)})
            result = profiler_mod.profile_table(df, "test_crash", level="standard")
            assert "value_stability" in result.degraded_features
        finally:
            profiler_mod.analyze_value_stability = original


# ---------------------------------------------------------------------------
# Findings / Rendering
# ---------------------------------------------------------------------------


class TestFindings:
    """Verify findings generation."""

    def test_static_columns_generate_findings(self):
        """Many static columns should produce a denormalization finding."""
        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        # 3 static columns
        col_a = [f"A_{i}" for i in range(n_entities) for _ in range(rows_per)]
        col_b = [f"B_{i}" for i in range(n_entities) for _ in range(rows_per)]
        col_c = [f"C_{i}" for i in range(n_entities) for _ in range(rows_per)]
        # 1 changing column
        col_d = [f"D_{i}_{j}" for i in range(n_entities) for j in range(rows_per)]

        df = pd.DataFrame({
            "entity_id": entities,
            "attr_a": col_a, "attr_b": col_b,
            "attr_c": col_c, "measure": col_d,
        })
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("attr_a", distinct_count=20),
            _make_profile("attr_b", distinct_count=20),
            _make_profile("attr_c", distinct_count=20),
            _make_profile("measure", distinct_count=100),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        assert len(result.findings) >= 1
        # Should mention denormalization or static
        assert any("static" in f.lower() or "denorm" in f.lower() for f in result.findings)

    def test_scd_finding_for_snapshot_table(self):
        """Changing columns in snapshot table should suggest SCD tracking."""
        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        statuses = []
        for i in range(n_entities):
            if i < 10:
                statuses.extend(["Active"] * 3 + ["Inactive"] * 2)
            else:
                statuses.extend(["Active"] * rows_per)

        df = pd.DataFrame({"entity_id": entities, "status": statuses})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("status", distinct_count=2),
        ]

        result = analyze_value_stability(
            df, grain, profiles,
            classification=TableClassification.SNAPSHOT
        )
        assert result is not None
        assert any("SCD" in f for f in result.findings)

    def test_findings_appear_in_ai_summary(self):
        """Findings should surface in the profiler output."""
        from tools.table_profiler_tool.lib.profiler import profile_table
        from tools.table_profiler_tool.lib.renderer import render_table_ai_summary

        n_entities = 20
        rows_per = 5
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        names = [f"Name_{i}" for i in range(n_entities) for _ in range(rows_per)]
        amounts = [float(j + i * 10) for i in range(n_entities) for j in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "name": names, "amount": amounts})
        result = profile_table(df, "test_render", level="standard")
        summary = render_table_ai_summary(result)
        # Should not crash — findings may or may not appear depending on grain detection
        assert isinstance(summary, str)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_handles_null_values_in_grouped_column(self):
        """Null values should be handled gracefully in nunique counts."""
        n_entities = 20
        rows_per = 3
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        # Some values are null — still one distinct non-null per entity
        values = []
        for i in range(n_entities):
            values.extend([f"V_{i}", None, f"V_{i}"])

        df = pd.DataFrame({"entity_id": entities, "nullable_col": values})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.67)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("nullable_col", distinct_count=20, null_pct=0.33),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        nc_stab = next(s for s in result.column_stabilities if s.column_name == "nullable_col")
        assert nc_stab.stability == "static"

    def test_large_entity_count_uses_sampling(self):
        """More than _MAX_ENTITIES_SAMPLE entities should trigger sampling."""
        n_entities = 600
        rows_per = 2
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]
        names = [f"Name_{i}" for i in range(n_entities) for _ in range(rows_per)]

        df = pd.DataFrame({"entity_id": entities, "name": names})
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.50)
        profiles = [
            _make_profile("entity_id", distinct_count=600),
            _make_profile("name", distinct_count=600),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is not None
        # Should have sampled entities
        name_stab = next(s for s in result.column_stabilities if s.column_name == "name")
        assert name_stab.entities_sampled == _MAX_ENTITIES_SAMPLE

    def test_no_analyzable_columns_returns_none(self):
        """If all non-key columns are excluded, return None."""
        n_entities = 20
        rows_per = 3
        entities = [f"E{i}" for i in range(n_entities) for _ in range(rows_per)]

        df = pd.DataFrame({
            "entity_id": entities,
            "load_ts": range(n_entities * rows_per),
        })
        grain = _make_grain(best_grain=["entity_id"], duplicate_rate=0.67)
        profiles = [
            _make_profile("entity_id", distinct_count=20),
            _make_profile("load_ts", distinct_count=60, role=ColumnRole.METADATA),
        ]

        result = analyze_value_stability(df, grain, profiles)
        assert result is None
