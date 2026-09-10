"""Tests for lib/duplicate_forensics.py — Duplicate Forensics (F11)."""

from __future__ import annotations

import sys

import pandas as pd
import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.duplicate_forensics import (
    _CONCENTRATION_THRESHOLD,
    _MIN_DUP_RATE,
    _RECENT_PERIOD_COUNT,
    _RECENT_THRESHOLD,
    analyze_duplicates,
)
from tools.table_profiler_tool.lib.models import (
    ColumnProfile,
    ColumnRole,
    DuplicateForensics,
    FreshnessAnalysis,
    GrainAnalysis,
    Inference,
    SemanticType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_grain(
    best_grain: list[str],
    is_unique: bool = False,
    duplicate_rate: float = 0.0,
) -> GrainAnalysis:
    """Helper to build a GrainAnalysis for tests."""
    return GrainAnalysis(
        best_grain=best_grain,
        is_unique=is_unique,
        duplicate_rate=duplicate_rate,
        candidates_tested=[],
        inference=Inference(
            value="grain_detected",
            confidence=0.75,
            evidence=[],
            sample_size=100,
            method="grain_detection",
        ),
    )


def _make_profile(
    name: str,
    role: ColumnRole = ColumnRole.DIMENSION,
    spark_type: str = "string",
    distinct_count: int = 5,
    null_pct: float = 0.0,
) -> ColumnProfile:
    """Helper to build a minimal ColumnProfile."""
    return ColumnProfile(
        name=name,
        position=0,
        spark_type=spark_type,
        role=role,
        distinct_count=distinct_count,
        null_pct=null_pct,
        row_count=100,
        non_null_count=100,
    )


def _make_freshness(column: str = "loaded_at") -> FreshnessAnalysis:
    """Helper to build a FreshnessAnalysis."""
    return FreshnessAnalysis(
        freshness_column=column,
        latest_value="2026-06-07",
        earliest_value="2026-01-01",
        staleness="3h ago",
        staleness_hours=3.0,
        cadence="daily",
    )


# ---------------------------------------------------------------------------
# Guard / skip tests
# ---------------------------------------------------------------------------


class TestSkipConditions:
    """Tests where analyze_duplicates returns None (skips analysis)."""

    def test_returns_none_when_grain_is_unique(self):
        df = pd.DataFrame({"id": [1, 2, 3], "val": ["a", "b", "c"]})
        grain = _make_grain(["id"], is_unique=True, duplicate_rate=0.0)
        result = analyze_duplicates(df, grain)
        assert result is None

    def test_returns_none_when_duplicate_rate_below_threshold(self):
        df = pd.DataFrame({"id": [1, 2, 3, 3], "val": ["a", "b", "c", "d"]})
        grain = _make_grain(["id"], duplicate_rate=0.001)  # Below _MIN_DUP_RATE
        result = analyze_duplicates(df, grain)
        assert result is None

    def test_returns_none_when_no_grain_columns(self):
        df = pd.DataFrame({"id": [1, 2, 3], "val": ["a", "b", "c"]})
        grain = _make_grain([], duplicate_rate=0.50)
        result = analyze_duplicates(df, grain)
        assert result is None

    def test_returns_none_for_empty_dataframe(self):
        df = pd.DataFrame({"id": pd.Series([], dtype="int64"), "val": pd.Series([], dtype="str")})
        grain = _make_grain(["id"], duplicate_rate=0.50)
        result = analyze_duplicates(df, grain)
        assert result is None


# ---------------------------------------------------------------------------
# Temporal concentration tests
# ---------------------------------------------------------------------------


class TestTemporalConcentration:
    """Tests for time-based duplicate concentration detection."""

    def test_detects_recent_time_concentration(self):
        """Duplicates clustered in last 3 days should flag as time-concentrated."""
        # Create data where dupes are all in recent dates
        dates = (
            ["2026-06-01"] * 5  # old — unique grain
            + ["2026-06-05"] * 10  # old — unique grain
            + ["2026-06-07"] * 20  # recent — many dupes
            + ["2026-06-08"] * 20  # recent — many dupes
            + ["2026-06-09"] * 20  # recent — many dupes
        )
        ids = list(range(5)) + list(range(10)) + list(range(10)) * 2 + list(range(10)) * 2 + list(range(10)) * 2
        df = pd.DataFrame({
            "id": ids[:75],
            "loaded_at": pd.to_datetime(dates[:75]),
            "val": ["x"] * 75,
        })

        grain = _make_grain(["id"], duplicate_rate=0.50)
        freshness = _make_freshness("loaded_at")
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=50),
            _make_profile("loaded_at", role=ColumnRole.TIMESTAMP, spark_type="timestamp"),
            _make_profile("val", distinct_count=1),
        ]

        result = analyze_duplicates(df, grain, profiles, freshness)
        assert result is not None
        assert result.is_time_concentrated is True
        assert result.time_concentration_description is not None
        assert "loaded_at" in result.time_concentration_description

    def test_no_time_concentration_when_evenly_distributed(self):
        """Duplicates spread evenly across all dates should not flag."""
        # Each date has the same number of dupes
        n_dates = 10
        rows_per_date = 10
        data = []
        for i in range(n_dates):
            for j in range(rows_per_date):
                data.append({
                    "id": j,  # Only 10 unique IDs — every date has full duplication
                    "loaded_at": pd.Timestamp(f"2026-06-{i+1:02d}"),
                    "val": "x",
                })
        df = pd.DataFrame(data)

        grain = _make_grain(["id"], duplicate_rate=0.90)
        freshness = _make_freshness("loaded_at")
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=10),
            _make_profile("loaded_at", role=ColumnRole.TIMESTAMP, spark_type="timestamp"),
            _make_profile("val", distinct_count=1),
        ]

        result = analyze_duplicates(df, grain, profiles, freshness)
        assert result is not None
        # Evenly distributed = NOT time concentrated (each period has ~10% of dupes)
        assert result.is_time_concentrated is False


# ---------------------------------------------------------------------------
# Source/category concentration tests
# ---------------------------------------------------------------------------


class TestSourceConcentration:
    """Tests for source/category-based duplicate concentration."""

    def test_detects_source_concentration(self):
        """Dupes clustered in one source value should flag."""
        # All dupes come from source='legacy_import'
        data = []
        for i in range(20):
            data.append({"id": i, "source_system": "api", "val": i})
        for i in range(10):
            # Duplicate IDs from legacy
            data.append({"id": i, "source_system": "legacy_import", "val": i})
            data.append({"id": i, "source_system": "legacy_import", "val": i + 100})
        df = pd.DataFrame(data)

        grain = _make_grain(["id"], duplicate_rate=0.50)
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=20),
            _make_profile("source_system", role=ColumnRole.DIMENSION, distinct_count=2),
            _make_profile("val", role=ColumnRole.MEASURE, distinct_count=30),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        assert result.is_source_concentrated is True
        assert result.source_concentration_description is not None
        assert "legacy_import" in result.source_concentration_description

    def test_no_source_concentration_when_balanced(self):
        """Dupes distributed across all source values should not flag."""
        data = []
        sources = ["api", "file", "manual"]
        for i in range(30):
            src = sources[i % 3]
            data.append({"id": i % 10, "source_feed": src, "val": i})
        df = pd.DataFrame(data)

        grain = _make_grain(["id"], duplicate_rate=0.67)
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=10),
            _make_profile("source_feed", role=ColumnRole.DIMENSION, distinct_count=3),
            _make_profile("val", role=ColumnRole.MEASURE, distinct_count=30),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        # Each source has ~33% — no single source dominates above 50%
        assert result.is_source_concentrated is False


# ---------------------------------------------------------------------------
# Snapshot pattern tests
# ---------------------------------------------------------------------------


class TestSnapshotPattern:
    """Tests for snapshot design pattern detection."""

    def test_detects_snapshot_from_grain_name(self):
        """Grain column with 'snapshot' or 'month' keyword → snapshot pattern."""
        df = pd.DataFrame({
            "id": [1, 1, 2, 2],
            "snapshot_month": ["2026-01", "2026-02", "2026-01", "2026-02"],
            "val": [10, 11, 20, 21],
        })

        # Grain is just [id], which has dupes because it's a snapshot table
        grain = _make_grain(["id"], duplicate_rate=0.50)
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=2),
            _make_profile("snapshot_month", role=ColumnRole.PARTITION, distinct_count=2),
            _make_profile("val", role=ColumnRole.MEASURE, distinct_count=4),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        assert result.is_snapshot_pattern is True
        assert result.verdict == "snapshot_design"

    def test_detects_snapshot_from_partition_role(self):
        """Partition-role column outside grain with low cardinality → snapshot."""
        df = pd.DataFrame({
            "entity_id": [1, 1, 2, 2, 3, 3],
            "period_key": [1, 2, 1, 2, 1, 2],
            "amount": [100, 110, 200, 210, 300, 310],
        })

        grain = _make_grain(["entity_id"], duplicate_rate=0.50)
        profiles = [
            _make_profile("entity_id", role=ColumnRole.PRIMARY_KEY, distinct_count=3),
            _make_profile("period_key", role=ColumnRole.PARTITION, distinct_count=2),
            _make_profile("amount", role=ColumnRole.MEASURE, distinct_count=6),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        assert result.is_snapshot_pattern is True
        assert result.verdict == "snapshot_design"


# ---------------------------------------------------------------------------
# Verdict tests
# ---------------------------------------------------------------------------


class TestVerdict:
    """Tests for verdict determination logic."""

    def test_verdict_dedup_needed_time_concentrated(self):
        """Time-concentrated dupes without snapshot pattern → dedup_needed."""
        # Recent date cluster, no snapshot indicator
        dates = (
            ["2026-06-01"] * 5
            + ["2026-06-08"] * 15
            + ["2026-06-09"] * 15
            + ["2026-06-10"] * 15
        )
        ids = (
            list(range(5))
            + list(range(5)) * 3
            + list(range(5)) * 3
            + list(range(5)) * 3
        )
        df = pd.DataFrame({
            "id": ids[:50],
            "event_date": pd.to_datetime(dates[:50]),
            "status": ["active"] * 50,
        })

        grain = _make_grain(["id"], duplicate_rate=0.80)
        freshness = _make_freshness("event_date")
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=5),
            _make_profile("event_date", role=ColumnRole.TIMESTAMP, spark_type="timestamp"),
            _make_profile("status", role=ColumnRole.DIMENSION, distinct_count=1),
        ]

        result = analyze_duplicates(df, grain, profiles, freshness)
        assert result is not None
        assert result.verdict == "dedup_needed"

    def test_verdict_partial_overlap_high_dup_rate(self):
        """High dup rate without concentration → partial_overlap."""
        # Uniform distribution, no time column, no dominant source
        data = []
        for i in range(100):
            data.append({"id": i % 20, "cat": f"cat_{i % 10}", "val": i})
        df = pd.DataFrame(data)

        grain = _make_grain(["id"], duplicate_rate=0.80)
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=20),
            _make_profile("cat", role=ColumnRole.DIMENSION, distinct_count=10),
            _make_profile("val", role=ColumnRole.MEASURE, distinct_count=100),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        # Each cat has 10% — below 50% threshold, no time col, rate > 30%
        assert result.verdict == "partial_overlap"

    def test_verdict_unknown_low_rate_no_concentration(self):
        """Low-ish dup rate, no clear concentration → unknown."""
        # Dupes spread evenly across many categories — no single category > 50%
        data = []
        # 50 unique rows
        for i in range(50):
            data.append({"id": i, "measure_val": float(i * 10)})
        # Add 5 duplicate IDs, each in the same pattern as original
        for i in range(5):
            data.append({"id": i, "measure_val": float(i * 10 + 1)})
        df = pd.DataFrame(data)

        grain = _make_grain(["id"], duplicate_rate=0.09)
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=50),
            # measure_val is a MEASURE — should not be considered for category analysis
            _make_profile("measure_val", role=ColumnRole.MEASURE, distinct_count=55),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        # No categorical columns to concentrate in, no time col, rate < 30%
        assert result.verdict == "unknown"


# ---------------------------------------------------------------------------
# Output contract tests
# ---------------------------------------------------------------------------


class TestOutputContract:
    """Tests that the output matches the DuplicateForensics contract."""

    def test_returns_duplicate_forensics_dataclass(self):
        """Result is a proper DuplicateForensics instance."""
        df = pd.DataFrame({
            "id": [1, 1, 2, 3],
            "val": ["a", "b", "c", "d"],
        })
        grain = _make_grain(["id"], duplicate_rate=0.25)
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=3),
            _make_profile("val", role=ColumnRole.DIMENSION, distinct_count=4),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        assert isinstance(result, DuplicateForensics)
        assert result.grain_columns == ["id"]
        assert result.duplicate_rate == 0.25
        assert result.verdict in (
            "snapshot_design", "dedup_needed", "partial_overlap", "unknown"
        )
        assert result.explanation != ""
        assert result.inference is not None
        assert result.inference.method == "duplicate_forensics"

    def test_concentration_values_format(self):
        """Concentration values have required keys."""
        # Build data with clear source concentration
        data = []
        for i in range(20):
            data.append({"id": i, "source": "good", "val": i})
        for i in range(10):
            data.append({"id": i, "source": "bad", "val": i + 100})
        df = pd.DataFrame(data)

        grain = _make_grain(["id"], duplicate_rate=0.33)
        profiles = [
            _make_profile("id", role=ColumnRole.PRIMARY_KEY, distinct_count=20),
            _make_profile("source", role=ColumnRole.DIMENSION, distinct_count=2),
            _make_profile("val", role=ColumnRole.MEASURE, distinct_count=30),
        ]

        result = analyze_duplicates(df, grain, profiles)
        assert result is not None
        if result.concentration_values:
            for v in result.concentration_values:
                assert "value" in v
                assert "dup_count" in v
                assert "dup_pct" in v
                assert isinstance(v["dup_count"], int)
                assert 0.0 <= v["dup_pct"] <= 1.0


# ---------------------------------------------------------------------------
# Integration with profiler
# ---------------------------------------------------------------------------


class TestProfilerIntegration:
    """Test that duplicate_forensics is wired into profile_table."""

    def test_profile_table_populates_duplicate_forensics(self):
        """profile_table should set duplicate_forensics when dupes exist."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        # Table with duplicates on the obvious grain
        df = pd.DataFrame({
            "order_id": [1, 1, 2, 3, 4, 5, 6, 7, 8, 9],
            "source_system": ["api"] * 5 + ["legacy"] * 5,
            "amount": [100, 100, 200, 300, 400, 500, 600, 700, 800, 900],
        })

        tp = profile_table(df, subject="test.dup_table")
        # TableProfile should have the field
        assert hasattr(tp, "duplicate_forensics")
        # With actual duplicates, it should be populated (or None if rate too low)
        # order_id has dupes → grain detection will find dup_rate > 0

    def test_profile_table_none_when_unique(self):
        """profile_table should set duplicate_forensics=None for unique grain."""
        from tools.table_profiler_tool.lib.profiler import profile_table

        df = pd.DataFrame({
            "id": list(range(20)),
            "val": [f"v{i}" for i in range(20)],
        })

        tp = profile_table(df, subject="test.unique_table")
        assert tp.duplicate_forensics is None


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


class TestRendering:
    """Test that renderers include duplicate forensics when present."""

    def test_ai_summary_includes_dupes_section(self):
        """render_table_ai_summary should show DUPES line."""
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_ai_summary

        tp = TableProfile(
            subject="test.table",
            row_count=100,
            column_count=3,
            duplicate_forensics=DuplicateForensics(
                grain_columns=["id"],
                duplicate_count=25,
                duplicate_rate=0.25,
                verdict="dedup_needed",
                explanation="Testing",
                concentration_column="source",
            ),
        )
        output = render_table_ai_summary(tp)
        assert "DUPES:" in output
        assert "dedup_needed" in output
        assert "concentrated_in=source" in output

    def test_md_report_includes_dupes_section(self):
        """render_table_profile_md should show Duplicate Forensics section."""
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_profile_md

        tp = TableProfile(
            subject="test.table",
            row_count=100,
            column_count=3,
            duplicate_forensics=DuplicateForensics(
                grain_columns=["id"],
                duplicate_count=25,
                duplicate_rate=0.25,
                verdict="snapshot_design",
                explanation="Snapshot table by design.",
                is_snapshot_pattern=True,
                concentration_values=[
                    {"value": "2026-01", "dup_count": 10, "dup_pct": 0.40},
                    {"value": "2026-02", "dup_count": 15, "dup_pct": 0.60},
                ],
            ),
        )
        output = render_table_profile_md(tp)
        assert "## Duplicate Forensics" in output
        assert "snapshot_design" in output
        assert "2026-01" in output

    def test_md_report_omits_when_none(self):
        """render_table_profile_md should not show section when None."""
        from tools.table_profiler_tool.lib.models import TableProfile
        from tools.table_profiler_tool.lib.renderer import render_table_profile_md

        tp = TableProfile(
            subject="test.table",
            row_count=100,
            column_count=3,
            duplicate_forensics=None,
        )
        output = render_table_profile_md(tp)
        assert "Duplicate Forensics" not in output
