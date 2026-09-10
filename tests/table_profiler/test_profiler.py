"""Unit tests for profiler orchestrator."""

from __future__ import annotations

import pandas as pd
import pytest
import random

from tools.table_profiler_tool.lib.profiler import profile_table
from tools.table_profiler_tool.lib.models import (
    ColumnRole,
    FreshnessAnalysis,
    GrainAnalysis,
    OutlierProfile,
    ProfilingLevel,
    TableClassification,
    TableProfile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_df() -> pd.DataFrame:
    """Minimal clean DataFrame with unique id, name, amount."""
    return pd.DataFrame({
        "id": range(1, 101),
        "name": [f"Person_{i}" for i in range(100)],
        "amount": [float(i * 10) for i in range(100)],
    })


@pytest.fixture
def email_df() -> pd.DataFrame:
    """DataFrame with email column for semantic type detection."""
    return pd.DataFrame({
        "user_key": range(100),
        "email": [f"user{i}@example.com" for i in range(100)],
    })


@pytest.fixture
def temporal_df() -> pd.DataFrame:
    """DataFrame with datetime column for freshness detection."""
    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    return pd.DataFrame({
        "event_key": range(100),
        "created_at": dates,
        "value": range(100),
    })


@pytest.fixture
def leading_space_df() -> pd.DataFrame:
    """Column with leading spaces to trigger cleanliness issues."""
    vals = [" Alice"] * 60 + [" Bob"] * 30 + ["Carol"] * 10
    return pd.DataFrame({"label": vals})


@pytest.fixture
def null_heavy_df() -> pd.DataFrame:
    """DataFrame with >20% nulls to trigger quality deduction."""
    return pd.DataFrame({
        "data_key": range(100),
        "sparse": [None] * 80 + ["val"] * 20,
    })


@pytest.fixture
def null_forensics_df() -> pd.DataFrame:
    """DataFrame with a strong co-null pattern for null-forensics findings."""
    legacy_values = ["legacy"] * 60
    modern_values = ["modern"] * 40
    source_system = legacy_values + modern_values
    migrated_code = ([None] * 57) + ([f"LEG-{i:03d}" for i in range(3)]) + [f"MOD-{i:03d}" for i in range(40)]
    order_id = list(range(100))
    return pd.DataFrame(
        {
            "order_id": order_id,
            "source_system": source_system,
            "migrated_code": migrated_code,
        }
    )


@pytest.fixture
def numeric_outlier_df() -> pd.DataFrame:
    """DataFrame with a deterministic numeric outlier cluster."""
    baseline = [10.0] * 50 + [12.0] * 25 + [14.0] * 20
    extreme = [250.0] * 5
    amount = baseline + extreme
    return pd.DataFrame(
        {
            "metric_id": range(100),
            "amount": amount,
        }
    )


# ---------------------------------------------------------------------------
# TestProfileTableBasic
# ---------------------------------------------------------------------------


class TestProfileTableBasic:
    """Smoke tests: return type, identity fields."""

    def test_returns_tableprofile(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "test.table")
        assert isinstance(result, TableProfile)

    def test_subject_set(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "my.subject")
        assert result.subject == "my.subject"

    def test_row_count(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert result.row_count == 100

    def test_column_count(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert result.column_count == 3

    def test_profiling_duration_non_negative(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert result.profiling_duration_ms >= 0

    def test_profiled_at_iso_format(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert result.profiled_at is not None
        assert "T" in result.profiled_at
        assert "+" in result.profiled_at or "Z" in result.profiled_at or result.profiled_at.endswith("+00:00")

    def test_profiling_level_stored(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t", level="quick")
        assert result.profiling_level == "quick"


# ---------------------------------------------------------------------------
# TestProfileTableColumns
# ---------------------------------------------------------------------------


class TestProfileTableColumns:
    """Column enrichment: stats, semantic type, roles."""

    def test_columns_populated(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert len(result.columns) == 3

    def test_column_names_present(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        names = {c.name for c in result.columns}
        assert {"id", "name", "amount"} == names

    def test_semantic_type_email_detected(self, email_df: pd.DataFrame) -> None:
        """Email column should receive SemanticType.EMAIL."""
        result = profile_table(email_df, "t")
        email_col = next((c for c in result.columns if c.name == "email"), None)
        assert email_col is not None
        assert email_col.semantic_type.value == "email"


# ---------------------------------------------------------------------------
# TestProfileTableGrain
# ---------------------------------------------------------------------------


class TestProfileTableGrain:
    """Grain detection integration."""

    def test_grain_is_grainanalysis(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert isinstance(result.grain, GrainAnalysis)

    def test_unique_id_column_grain(self, basic_df: pd.DataFrame) -> None:
        """basic_df has a unique 'id' column — grain should be unique."""
        result = profile_table(basic_df, "t")
        assert result.grain.is_unique is True

    def test_quick_level_no_grain(self, basic_df: pd.DataFrame) -> None:
        """Quick level skips grain detection — grain is None (not-evaluated)."""
        result = profile_table(basic_df, "t", level="quick")
        # Quick level: grain=None distinguishes not-evaluated from evaluated+no-grain-found
        assert result.grain is None


# ---------------------------------------------------------------------------
# TestProfileTableFreshness
# ---------------------------------------------------------------------------


class TestProfileTableFreshness:
    """Freshness detection integration."""

    def test_freshness_on_date_column(self, temporal_df: pd.DataFrame) -> None:
        result = profile_table(temporal_df, "t")
        assert result.freshness is not None
        assert isinstance(result.freshness, FreshnessAnalysis)

    def test_freshness_none_no_dates(self) -> None:
        df = pd.DataFrame({"x": range(100), "y": range(100)})
        result = profile_table(df, "t")
        assert result.freshness is None


# ---------------------------------------------------------------------------
# TestProfileTableClassification
# ---------------------------------------------------------------------------


class TestProfileTableClassification:
    """Table classification integration."""

    def test_classification_is_enum(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert isinstance(result.classification, TableClassification)

    def test_quick_level_skips_classification(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t", level="quick")
        assert result.classification == TableClassification.UNKNOWN
        assert result.classification_confidence == 0.0


# ---------------------------------------------------------------------------
# TestProfileTableQuality
# ---------------------------------------------------------------------------


class TestProfileTableQuality:
    """Quality score, findings, issue attachment."""

    def test_quality_score_in_range(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert 0.0 <= result.overall_quality_score <= 1.0

    def test_quality_summary_valid(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert result.quality_summary in ("excellent", "good", "fair", "poor")

    def test_findings_non_empty(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert isinstance(result.findings, list)
        assert len(result.findings) >= 1

    def test_cleanliness_issues_in_format_issues(self, leading_space_df: pd.DataFrame) -> None:
        result = profile_table(leading_space_df, "t")
        issue_types = {i.issue_type for i in result.format_issues}
        assert "leading_spaces" in issue_types

    def test_format_issue_attached_to_column(self, leading_space_df: pd.DataFrame) -> None:
        result = profile_table(leading_space_df, "t")
        label_col = next((c for c in result.columns if c.name == "label"), None)
        assert label_col is not None
        assert label_col.has_leading_spaces is True

    def test_quality_deduction_high_nulls(self, null_heavy_df: pd.DataFrame) -> None:
        result = profile_table(null_heavy_df, "t")
        # High null rate + no grain -> quality should be below 1.0
        assert result.overall_quality_score < 1.0

    def test_deep_level_same_as_standard(self, basic_df: pd.DataFrame) -> None:
        standard = profile_table(basic_df, "t", level="standard")
        deep = profile_table(basic_df, "t", level="deep")
        # Both run all steps, so grain and freshness should be populated the same way
        assert standard.grain.is_unique == deep.grain.is_unique

    def test_outliers_populated_for_numeric_extremes(self, numeric_outlier_df: pd.DataFrame) -> None:
        result = profile_table(numeric_outlier_df, "t")

        assert len(result.outliers) == 1
        assert isinstance(result.outliers[0], OutlierProfile)
        assert result.outliers[0].column == "amount"
        assert result.outliers[0].outlier_count == 5
        assert result.outliers[0].outlier_pct == 0.05

    def test_outlier_finding_added_to_profile_summary(self, numeric_outlier_df: pd.DataFrame) -> None:
        result = profile_table(numeric_outlier_df, "t")

        assert any("outlier context:" in finding.lower() for finding in result.findings)
        assert any("amount" in finding.lower() for finding in result.findings)

    @staticmethod
    def _make_stale_df() -> pd.DataFrame:
        """100-row table whose latest timestamp is 60 days ago.

        Produces staleness_hours ~1440 (> 48h threshold), reliably triggering
        _build_freshness_risks and _build_freshness_actions.
        Uses timezone-aware datetimes to avoid tz-naive detection issues.
        """
        from datetime import datetime, timedelta, timezone
        end_date = datetime.now(timezone.utc) - timedelta(days=60)
        dates    = [end_date - timedelta(days=i) for i in range(99, -1, -1)]
        return pd.DataFrame({
            "event_id":  range(100),
            "created_at": dates,
            "value":     range(100),
        })

    def test_freshness_stale_adds_risk(self) -> None:
        """Stale table (60 days old) produces a risk entry mentioning 'stale'.

        Uses _make_stale_df (staleness_hours ~1440, well above the 48h threshold).
        _build_freshness_risks surfaces '[created_at] stale — last load Xd ago' into risks.
        """
        df = self._make_stale_df()
        result = profile_table(df, "t")

        stale_risks = [r for r in result.risks if "stale" in r.lower()]
        assert len(stale_risks) >= 1, (
            f"Expected stale risk entry. risks={result.risks}"
        )

    def test_freshness_stale_adds_action(self) -> None:
        """Stale table produces a suggested action mentioning 'freshness' or 'stale'.

        _build_freshness_actions emits:
        'Investigate data freshness: \'created_at\' last updated Xd ago — check if pipeline is broken'
        """
        df = self._make_stale_df()
        result = profile_table(df, "t")

        fresh_actions = [
            a for a in result.suggested_actions
            if "freshness" in a.lower() or "stale" in a.lower()
        ]
        assert len(fresh_actions) >= 1, (
            f"Expected freshness/stale action. suggested_actions={result.suggested_actions}"
        )

    @staticmethod
    def _make_partial_overlap_df() -> pd.DataFrame:
        """40% duplicate rate on event_id, no temporal or source column.

        Produces verdict='partial_overlap' from analyze_duplicates because:
        - duplicate_rate=0.40 (>30% threshold)
        - no temporal column → time_concentrated=False
        - no low-cardinality categorical column → source_concentrated=False
        - no periodic partition pattern → is_snapshot=False
        """
        return pd.DataFrame({
            "event_id": [1, 1, 1, 1, 2, 2, 3, 4, 5, 6],
            "amount":   [100.0, 100.0, 100.0, 100.0,
                         200.0, 200.0, 300.0, 400.0, 500.0, 600.0],
        })

    def test_dedup_risk_added_for_partial_overlap(self) -> None:
        """Partial-overlap duplicate verdict produces a risk entry mentioning 'duplicate'.

        Uses _make_partial_overlap_df (40% dup rate on event_id) which reliably
        produces verdict='partial_overlap'. _build_dedup_risks then surfaces
        '[event_id] 40% duplicate rate — cause unclear' into risks.
        """
        df = self._make_partial_overlap_df()
        result = profile_table(df, "t")

        dup_risks = [r for r in result.risks if "duplicate" in r.lower()]
        assert len(dup_risks) >= 1, (
            f"Expected duplicate risk entry. risks={result.risks}"
        )

    def test_dedup_action_recommends_dedup(self) -> None:
        """Partial-overlap verdict produces a 'Deduplicate' suggested action.

        _build_dedup_actions emits 'Deduplicate [event_id] before downstream consumption'
        which is distinct from the grain-level 'Investigate duplicates' action already
        emitted by _build_profile_actions.
        """
        df = self._make_partial_overlap_df()
        result = profile_table(df, "t")

        dedup_actions = [a for a in result.suggested_actions if "dedup" in a.lower()]
        assert len(dedup_actions) >= 1, (
            f"Expected 'dedup' suggested action. suggested_actions={result.suggested_actions}"
        )

    def test_outlier_added_to_risks(
        self,
        numeric_outlier_df: pd.DataFrame,
    ) -> None:
        """Outlier detection produces a risk entry mentioning 'outlier'.

        Uses the numeric_outlier_df fixture (5 extreme values at 250.0 in 100 rows)
        which reliably triggers _detect_numeric_outliers → OutlierProfile.
        _build_outlier_risks then surfaces '[amount] 5 outlier(s) (5.0%)...' into risks.
        """
        result = profile_table(numeric_outlier_df, "t")

        outlier_risks = [r for r in result.risks if "outlier" in r.lower()]
        assert len(outlier_risks) >= 1, (
            f"Expected outlier risk entry. risks={result.risks}"
        )

    def test_outlier_action_recommends_investigation(
        self,
        numeric_outlier_df: pd.DataFrame,
    ) -> None:
        """Outlier detection produces a 'Investigate or cap' suggested action.

        _build_outlier_actions emits one action per OutlierProfile:
        'Investigate or cap outliers in \'amount\' before aggregating (5 value(s), 5.0%)'
        """
        result = profile_table(numeric_outlier_df, "t")

        outlier_actions = [a for a in result.suggested_actions if "outlier" in a.lower()]
        assert len(outlier_actions) >= 1, (
            f"Expected outlier suggested action. suggested_actions={result.suggested_actions}"
        )

    def test_null_forensics_finding_added_for_co_null_pattern(
        self,
        null_forensics_df: pd.DataFrame,
    ) -> None:
        result = profile_table(null_forensics_df, "t")

        migrated_code = next((c for c in result.columns if c.name == "migrated_code"), None)
        assert migrated_code is not None
        assert "source_system" in migrated_code.correlated_nulls
        assert any("null forensics:" in finding.lower() for finding in result.findings)
        assert any("source_system='legacy'" in finding.lower() for finding in result.findings)


# ---------------------------------------------------------------------------
# TestProfileTableOutputContract
# ---------------------------------------------------------------------------


class TestProfileTableOutputContract:
    """Verify all required fields are present on the returned TableProfile."""

    def test_required_fields(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        required = [
            "subject", "row_count", "column_count", "columns",
            "grain", "freshness", "format_issues", "outliers",
            "overall_quality_score", "quality_summary",
            "profiling_level", "profiling_duration_ms", "profiled_at",
            "findings", "degraded_features", "degradation_reasons",
            "classification", "classification_confidence",
        ]
        for field in required:
            assert hasattr(result, field), f"Missing field: {field}"

    def test_degraded_features_list(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert isinstance(result.degraded_features, list)

    def test_degradation_reasons_dict(self, basic_df: pd.DataFrame) -> None:
        result = profile_table(basic_df, "t")
        assert isinstance(result.degradation_reasons, dict)

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

sys.path.insert(0, ".")
from tools.table_profiler_tool.lib.profiler import _detect_periodic_partition


# ---------------------------------------------------------------------------
# Minimal ColumnProfile stub for testing _detect_periodic_partition in isolation
# ---------------------------------------------------------------------------

@dataclass
class _CP:
    """Minimal stub matching the fields _detect_periodic_partition accesses."""
    name: str
    spark_type: str
    min_value: Any = None
    max_value: Any = None
    distinct_count: int | None = None


def _make_cp(name, spark_type="long", mn=None, mx=None, dc=None):
    return _CP(name=name, spark_type=spark_type, min_value=mn, max_value=mx, distinct_count=dc)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDetectPeriodicPartition:
    """Tests for _detect_periodic_partition() value-range finding."""

    def test_year_and_month_fires(self):
        """Integer year col (2020-2026) + month col (1-12) produces a finding."""
        profiles = [
            _make_cp("id",             spark_type="long",   mn=1,    mx=50000),
            _make_cp("snapshot_year",  spark_type="long",   mn=2020, mx=2026, dc=7),
            _make_cp("snapshot_month", spark_type="long",   mn=1,    mx=12,   dc=12),
            _make_cp("amount",         spark_type="double", mn=0.0,  mx=9999.0),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None, "Expected a finding for year+month"
        assert "year_col=snapshot_year" in result
        assert "month_col=snapshot_month" in result

    def test_year_and_quarter_fires(self):
        """Year col + quarter col (1-4) also produces a finding."""
        profiles = [
            _make_cp("report_year",    spark_type="integer", mn=2018, mx=2025, dc=8),
            _make_cp("fiscal_quarter", spark_type="integer", mn=1,    mx=4,    dc=4),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None, "Expected a finding for year+quarter"
        assert "year_col=report_year" in result
        assert "quarter_col=fiscal_quarter" in result

    def test_year_alone_no_finding(self):
        """Year col without a period col produces no finding."""
        profiles = [
            _make_cp("snap_year", spark_type="long", mn=2020, mx=2024, dc=5),
            _make_cp("amount",    spark_type="long", mn=0,    mx=9999),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is None, f"Year-alone should not produce a finding, got: {result}"

    def test_none_min_max_handled_gracefully(self):
        """None min_value/max_value must not raise — column is skipped."""
        profiles = [
            _make_cp("snapshot_year",  spark_type="long", mn=None, mx=None),
            _make_cp("snapshot_month", spark_type="long", mn=None, mx=None),
        ]
        result = _detect_periodic_partition(profiles)  # must not raise
        assert result is None

    def test_empty_profiles_no_finding(self):
        """Empty profile list returns None without error."""
        assert _detect_periodic_partition([]) is None

    def test_non_integer_columns_ignored(self):
        """String / double columns are not mistaken for year or month."""
        profiles = [
            _make_cp("year_str",  spark_type="string", mn="2020", mx="2026"),
            _make_cp("month_flt", spark_type="double", mn=1.0,   mx=12.0, dc=12),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is None

    def test_finding_mentions_incremental_snapshots(self):
        """Finding text must guide the consumer to interpret the evidence."""
        profiles = [
            _make_cp("as_of_year",  spark_type="long", mn=2021, mx=2025, dc=5),
            _make_cp("as_of_month", spark_type="long", mn=1,    mx=12,   dc=12),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        assert "incremental snapshot" in result.lower()


class TestPeriodicPartitionNameTiebreaker:
    """Tests for name-preference tiebreaker in _detect_periodic_partition."""

    def test_prefers_snapshot_month_over_arbitrary_column(self):
        """When two columns match month range, prefer the one named 'snapshot_month'."""
        profiles = [
            _make_cp("snapshot_year",  spark_type="long", mn=2025, mx=2026, dc=2),
            # This comes FIRST but has a non-temporal name
            _make_cp("__of_Requests_on_Proposed_POI", spark_type="long", mn=1, mx=12, dc=12),
            # This comes SECOND but has the temporal hint
            _make_cp("snapshot_month", spark_type="long", mn=1, mx=12, dc=3),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        assert "month_col=snapshot_month" in result, f"Expected snapshot_month, got: {result}"

    def test_prefers_year_named_column_over_arbitrary(self):
        """When two columns match year range, prefer the one with 'year' in name."""
        profiles = [
            _make_cp("record_count", spark_type="long", mn=2020, mx=2026, dc=7),
            _make_cp("report_year",  spark_type="long", mn=2020, mx=2026, dc=7),
            _make_cp("snap_month",   spark_type="long", mn=1,    mx=12,   dc=12),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        assert "year_col=report_year" in result, f"Expected report_year, got: {result}"

    def test_prefers_quarter_named_column(self):
        """When two columns match quarter range, prefer the one with 'qtr' in name."""
        profiles = [
            _make_cp("fiscal_year",   spark_type="long", mn=2022, mx=2025, dc=4),
            _make_cp("priority_level", spark_type="long", mn=1, mx=4, dc=4),
            _make_cp("fiscal_qtr",    spark_type="long", mn=1, mx=4, dc=4),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        assert "quarter_col=fiscal_qtr" in result, f"Expected fiscal_qtr, got: {result}"

    def test_single_candidate_wins_without_hint(self):
        """If only one candidate exists, it wins even without a temporal name."""
        profiles = [
            _make_cp("snapshot_year", spark_type="long", mn=2020, mx=2026, dc=7),
            _make_cp("__of_Requests_on_Proposed_POI", spark_type="long", mn=1, mx=12, dc=12),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        # Only one month candidate — it wins regardless of name
        assert "month_col=__of_Requests_on_Proposed_POI" in result

    def test_fallback_to_first_when_no_hints_match(self):
        """If no candidate has a temporal hint, first one wins."""
        profiles = [
            _make_cp("snap_yr", spark_type="long", mn=2020, mx=2026, dc=7),
            _make_cp("count_a", spark_type="long", mn=1, mx=12, dc=12),
            _make_cp("count_b", spark_type="long", mn=1, mx=12, dc=12),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        # snap_yr contains "yr" hint → wins year. Neither count_a nor count_b has month hint → first wins
        assert "year_col=snap_yr" in result
        assert "month_col=count_a" in result


    def test_fy_hint_prefers_fiscal_year(self):
        """'fy' hint matches column named 'fy_2025'."""
        profiles = [
            _make_cp("record_count", spark_type="long", mn=2020, mx=2026, dc=7),
            _make_cp("fy_2025",      spark_type="long", mn=2020, mx=2026, dc=7),
            _make_cp("snap_month",   spark_type="long", mn=1,    mx=12,   dc=12),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        assert "year_col=fy_2025" in result, f"Expected fy_2025, got: {result}"

    def test_mth_hint_prefers_report_mth(self):
        """'mth' hint matches column named 'report_mth'."""
        profiles = [
            _make_cp("snapshot_year", spark_type="long", mn=2020, mx=2026, dc=7),
            _make_cp("priority_level", spark_type="long", mn=1, mx=12, dc=12),
            _make_cp("report_mth",    spark_type="long", mn=1, mx=12, dc=12),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        assert "month_col=report_mth" in result, f"Expected report_mth, got: {result}"

    def test_fq_hint_prefers_fiscal_quarter(self):
        """'fq' hint matches column named 'fq_num'."""
        profiles = [
            _make_cp("fiscal_year", spark_type="long", mn=2022, mx=2025, dc=4),
            _make_cp("priority",    spark_type="long", mn=1, mx=4, dc=4),
            _make_cp("fq_num",      spark_type="long", mn=1, mx=4, dc=4),
        ]
        result = _detect_periodic_partition(profiles)
        assert result is not None
        assert "quarter_col=fq_num" in result, f"Expected fq_num, got: {result}"


# ---------------------------------------------------------------------------
# TestProfileTableJoinReadiness
# ---------------------------------------------------------------------------


class TestProfileTableJoinReadiness:
    """Join-readiness surfacing into TableProfile.findings, .risks, .suggested_actions.

    Covers: healthy join, orphan risk, fan-out cardinality risk, and no-join baseline.
    reference_tables format for pandas: {"source_col": (target_df, "target_col")}
    """

    # ── fixtures ──────────────────────────────────────────────────────────────

    @staticmethod
    def _make_source() -> pd.DataFrame:
        """Source table with customer_id 1-20, all unique."""
        return pd.DataFrame({
            "order_id": range(1, 21),
            "customer_id": range(1, 21),
        })

    @staticmethod
    def _make_full_dim() -> pd.DataFrame:
        """Customer dimension with cust_id 1-20 (100% overlap with source)."""
        return pd.DataFrame({"cust_id": range(1, 21)})

    @staticmethod
    def _make_partial_dim() -> pd.DataFrame:
        """Customer dimension with cust_id 1-15 only (5 orphans in source)."""
        return pd.DataFrame({"cust_id": range(1, 16)})

    # ── healthy join ──────────────────────────────────────────────────────────

    def test_healthy_join_produces_finding(self) -> None:
        """Full-overlap join adds a join-ready finding to TableProfile.findings."""
        source_df = self._make_source()
        dim_df = self._make_full_dim()
        result = profile_table(
            source_df,
            "test.orders",
            reference_tables={"customer_id": (dim_df, "cust_id")},
        )

        assert len(result.joins) == 1
        assert any("join ready" in f.lower() for f in result.findings), (
            f"Expected join-ready finding in findings: {result.findings}"
        )

    def test_healthy_join_no_orphan_risk(self) -> None:
        """Full-overlap join adds no orphan risk entry."""
        source_df = self._make_source()
        dim_df = self._make_full_dim()
        result = profile_table(
            source_df,
            "test.orders",
            reference_tables={"customer_id": (dim_df, "cust_id")},
        )

        orphan_risks = [r for r in result.risks if "orphan" in r.lower()]
        assert orphan_risks == [], f"Unexpected orphan risks: {orphan_risks}"

    def test_healthy_join_no_fan_out_risk(self) -> None:
        """1:1 cardinality join adds no fan-out risk entry."""
        source_df = self._make_source()
        dim_df = self._make_full_dim()
        result = profile_table(
            source_df,
            "test.orders",
            reference_tables={"customer_id": (dim_df, "cust_id")},
        )

        fan_out_risks = [r for r in result.risks if "fan-out" in r.lower()]
        assert fan_out_risks == [], f"Unexpected fan-out risks: {fan_out_risks}"

    # ── orphan risk ───────────────────────────────────────────────────────────

    def test_orphan_risk_added_for_partial_overlap(self) -> None:
        """Orphan rows in the source produce a risk entry mentioning 'orphan'."""
        source_df = pd.DataFrame({
            "order_id": range(1, 21),
            # customer_id 99-103 have no match in partial_dim → 5 orphans
            "customer_id": list(range(1, 16)) + [99, 100, 101, 102, 103],
        })
        dim_df = self._make_partial_dim()
        result = profile_table(
            source_df,
            "test.orders",
            reference_tables={"customer_id": (dim_df, "cust_id")},
        )

        orphan_risks = [r for r in result.risks if "orphan" in r.lower()]
        assert len(orphan_risks) >= 1, (
            f"Expected at least one orphan risk. risks={result.risks}"
        )

    def test_orphan_action_recommends_left_join(self) -> None:
        """Orphan rows trigger a 'LEFT JOIN' suggested action."""
        source_df = pd.DataFrame({
            "order_id": range(1, 21),
            "customer_id": list(range(1, 16)) + [99, 100, 101, 102, 103],
        })
        dim_df = self._make_partial_dim()
        result = profile_table(
            source_df,
            "test.orders",
            reference_tables={"customer_id": (dim_df, "cust_id")},
        )

        left_join_actions = [a for a in result.suggested_actions if "left join" in a.lower()]
        assert len(left_join_actions) >= 1, (
            f"Expected LEFT JOIN action. suggested_actions={result.suggested_actions}"
        )

    # ── fan-out cardinality risk ──────────────────────────────────────────────

    def test_many_to_many_cardinality_adds_fan_out_risk(self) -> None:
        """Many-to-many cardinality on target adds a fan-out risk entry."""
        # source: tag_id has duplicates → source NOT unique
        source_df = pd.DataFrame({
            "row_id": range(1, 6),
            "tag_id": [10, 20, 10, 30, 20],
        })
        # target: also has duplicates on tag_id → target NOT unique → many:many
        tag_dim = pd.DataFrame({"tag_id": [10, 10, 20, 20, 30]})
        result = profile_table(
            source_df,
            "test.tagged_rows",
            reference_tables={"tag_id": (tag_dim, "tag_id")},
        )

        fan_out_risks = [r for r in result.risks if "fan-out" in r.lower()]
        assert len(fan_out_risks) >= 1, (
            f"Expected fan-out risk for many:many. risks={result.risks}"
        )

    def test_many_to_many_action_recommends_uniqueness_validation(self) -> None:
        """Fan-out cardinality triggers a uniqueness-validation suggested action."""
        source_df = pd.DataFrame({
            "row_id": range(1, 6),
            "tag_id": [10, 20, 10, 30, 20],
        })
        tag_dim = pd.DataFrame({"tag_id": [10, 10, 20, 20, 30]})
        result = profile_table(
            source_df,
            "test.tagged_rows",
            reference_tables={"tag_id": (tag_dim, "tag_id")},
        )

        uniqueness_actions = [
            a for a in result.suggested_actions if "uniqueness" in a.lower() or "unique" in a.lower()
        ]
        assert len(uniqueness_actions) >= 1, (
            f"Expected uniqueness validation action. suggested_actions={result.suggested_actions}"
        )

    # ── no-reference-tables baseline ─────────────────────────────────────────

    def test_no_reference_tables_no_join_findings(self) -> None:
        """When no reference_tables are provided, no join findings appear."""
        source_df = pd.DataFrame({
            "order_id": range(1, 11),
            "amount": [float(i) for i in range(1, 11)],
        })
        result = profile_table(source_df, "test.orders")

        assert len(result.joins) == 0
        join_findings = [f for f in result.findings if "join ready" in f.lower()]
        assert join_findings == [], f"Unexpected join findings: {join_findings}"

    def test_no_reference_tables_no_orphan_risk(self) -> None:
        """When no reference_tables are provided, no orphan risks appear."""
        source_df = pd.DataFrame({
            "order_id": range(1, 11),
            "amount": [float(i) for i in range(1, 11)],
        })
        result = profile_table(source_df, "test.orders")

        orphan_risks = [r for r in result.risks if "orphan" in r.lower()]
        assert orphan_risks == [], f"Unexpected orphan risks: {orphan_risks}"

    # ── format incompatibility risk ───────────────────────────────────────────

    def test_format_mismatch_adds_risk(self) -> None:
        """Case mismatch between source and target keys adds a format-incompatibility risk entry.

        Source key values are UPPERCASE; target key values are lowercase.
        _check_format_compatibility_samples detects case_mismatch: source=upper, target=lower
        and sets format_compatible=False on the resulting JoinProfile.
        """
        # UPPERCASE source codes vs lowercase target codes — guaranteed case mismatch
        source_df = pd.DataFrame({
            "order_id": range(1, 6),
            "code": ["ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON"],
        })
        dim_df = pd.DataFrame({
            "ref_code": ["alpha", "beta", "gamma", "delta", "epsilon"],
        })
        result = profile_table(
            source_df,
            "test.orders",
            reference_tables={"code": (dim_df, "ref_code", "dim_codes")},
        )

        format_risks = [r for r in result.risks if "format incompatibility" in r.lower()]
        assert len(format_risks) >= 1, (
            f"Expected format-incompatibility risk. risks={result.risks}"
        )

    def test_format_mismatch_action_recommends_normalise(self) -> None:
        """Case mismatch triggers a 'Normalise formats' suggested action."""
        source_df = pd.DataFrame({
            "order_id": range(1, 6),
            "code": ["ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON"],
        })
        dim_df = pd.DataFrame({
            "ref_code": ["alpha", "beta", "gamma", "delta", "epsilon"],
        })
        result = profile_table(
            source_df,
            "test.orders",
            reference_tables={"code": (dim_df, "ref_code", "dim_codes")},
        )

        normalise_actions = [a for a in result.suggested_actions if "normalise" in a.lower()]
        assert len(normalise_actions) >= 1, (
            f"Expected normalise-formats action. suggested_actions={result.suggested_actions}"
        )

class TestRoleClassificationRunnerUps:
    """Tests that infer_column_role populates runner_ups with competing-evidence alternatives.

    Phase 4 adds a secondary scoring pass after the winner is determined.
    Any alternative role scoring >= _RUNNER_UP_THRESHOLD (0.60) is surfaced as
    a CompetingHypothesis entry in Inference.runner_ups.
    """

    @staticmethod
    def _make_low_cardinality_amount_df() -> pd.DataFrame:
        """Integer 'amount' column with only 4 distinct values cycling over 100 rows.

        Produces role=MEASURE (name matches measure pattern + numeric + not unique).
        Also triggers _score_partition because:
        - _is_integer_type("int64") = True
        - _is_partition_like_numeric: distinct_count=4 <= 20, distinct_pct=0.04 <= 0.10,
          value_range=30 (10-40) <= 200
        Expected runner_up: PARTITION (confidence=0.82).
        """
        return pd.DataFrame({
            "row_id": range(100),
            "amount": [10, 20, 30, 40] * 25,
        })

    @staticmethod
    def _make_low_cardinality_branch_id_df() -> pd.DataFrame:
        """Integer 'branch_id' column with 5 distinct values cycling over 100 rows.

        Produces role=FOREIGN_KEY (name has _id suffix, non-unique, distinct_pct=0.05).
        Also triggers _score_partition because:
        - _is_integer_type("int64") = True
        - _is_partition_like_numeric: distinct_count=5 <= 20, distinct_pct=0.05 <= 0.10,
          value_range=4 (1-5) <= 200
        Expected runner_up: PARTITION (confidence=0.82).
        """
        return pd.DataFrame({
            "row_id": range(100),
            "branch_id": [1, 2, 3, 4, 5] * 20,
        })

    def test_measure_has_partition_runner_up(self) -> None:
        """Low-cardinality integer MEASURE column surfaces PARTITION as a runner_up.

        'amount' column (integer, 4 distinct values) wins as MEASURE(0.90) because
        of the name pattern signal. The secondary scoring pass also fires
        _score_partition → PARTITION(0.82) appears as a CompetingHypothesis in runner_ups.
        """
        df = self._make_low_cardinality_amount_df()
        result = profile_table(df, "t")
        amount_col = next(
            (p for p in result.columns if p.name == "amount"), None
        )
        assert amount_col is not None, "Column 'amount' not found in profile"
        assert amount_col.role_inference is not None, "role_inference is None"
        amount_inf = amount_col.role_inference

        assert amount_inf.value == ColumnRole.MEASURE, (
            f"Expected MEASURE winner, got {amount_inf.value}"
        )
        partition_rus = [r for r in amount_inf.runner_ups if r.value == ColumnRole.PARTITION]
        assert len(partition_rus) >= 1, (
            f"Expected PARTITION runner_up. runner_ups={amount_inf.runner_ups}"
        )

    def test_foreign_key_has_partition_runner_up(self) -> None:
        """Low-cardinality integer FK column surfaces PARTITION as a runner_up.

        'branch_id' column (integer, 5 distinct values) wins as FOREIGN_KEY(0.82)
        because of the _id suffix + non-unique signals. The secondary scoring pass
        fires _score_partition → PARTITION(0.82) appears in runner_ups.
        """
        df = self._make_low_cardinality_branch_id_df()
        result = profile_table(df, "t")
        bid_col = next(
            (p for p in result.columns if p.name == "branch_id"), None
        )
        assert bid_col is not None, "Column 'branch_id' not found in profile"
        assert bid_col.role_inference is not None, "role_inference is None"
        bid_inf = bid_col.role_inference

        assert bid_inf.value == ColumnRole.FOREIGN_KEY, (
            f"Expected FOREIGN_KEY winner, got {bid_inf.value}"
        )
        partition_rus = [r for r in bid_inf.runner_ups if r.value == ColumnRole.PARTITION]
        assert len(partition_rus) >= 1, (
            f"Expected PARTITION runner_up. runner_ups={bid_inf.runner_ups}"
        )

class TestTableClassificationRunnerUps:
    """Tests that classify_table populates runner_ups with competing-evidence alternatives.

    Phase 5 adds a secondary scoring pass after the winner is determined.
    Any alternative table type scoring >= _RUNNER_UP_THRESHOLD (0.60) is
    surfaced as a CompetingHypothesis entry in Inference.runner_ups.

    Fixtures use 1100 rows to bypass the LOOKUP short-circuit
    (_LOOKUP_MAX_ROWS=1000 AND col_count<=5).
    """

    @staticmethod
    def _make_fact_with_timestamp_df() -> pd.DataFrame:
        """FACT table with a timestamp column providing SNAPSHOT competing evidence.

        1100 rows: snapshot_date (TIMESTAMP, low cardinality), site_id (FK),
        capacity_mw (MEASURE, non-unique repeating values).

        Expected winner: FACT (measure_count=1, fk_count=1 → confidence=0.80).
        Expected runner_up: SNAPSHOT (temporal column present → 0.72).
        """
        from datetime import datetime, timedelta
        dates = [(datetime(2025, 1, 1) + timedelta(days=i // 50)) for i in range(1100)]
        return pd.DataFrame({
            "snapshot_date": dates,
            "site_id":       [i % 10 for i in range(1100)],
            "capacity_mw":   [float(i % 5) * 50 for i in range(1100)],
        })

    @staticmethod
    def _make_fact_with_dimensions_df() -> pd.DataFrame:
        """FACT table with multiple DIMENSION columns providing DIMENSION competing evidence.

        1100 rows: order_id (SURROGATE_KEY), customer_id (FK), status/category (DIMENSION×2),
        total_amount (MEASURE, non-unique repeating values).

        Expected winner: FACT (measure_count=1, fk_count=1 → confidence=0.80).
        Expected runner_up: DIMENSION (key_count=1, dim_count=2 → 0.75).
        """
        return pd.DataFrame({
            "order_id":    range(1100),
            "customer_id": [i % 50 for i in range(1100)],
            "status":      (["open", "closed", "pending"] * 366 + ["open", "open"]),
            "category":    (["A", "B", "C", "D"] * 275),
            "total_amount": [100.0, 200.0, 300.0, 400.0, 500.0] * 220,
        })

    def test_fact_has_snapshot_runner_up(self) -> None:
        """FACT winner with temporal column surfaces SNAPSHOT as a runner_up.

        _score_snapshot fires when any column has TIMESTAMP role or temporal
        spark type — confidence=0.72. The FACT winner (0.80) is preserved;
        SNAPSHOT appears as a competing interpretation in runner_ups.
        """
        df = self._make_fact_with_timestamp_df()
        result = profile_table(df, "t")
        ci = result.classification_inference
        assert ci is not None, "classification_inference is None"

        assert ci.value == TableClassification.FACT, (
            f"Expected FACT winner, got {ci.value}"
        )
        snap_rus = [r for r in ci.runner_ups if r.value == TableClassification.SNAPSHOT]
        assert len(snap_rus) >= 1, (
            f"Expected SNAPSHOT runner_up. runner_ups={ci.runner_ups}"
        )

    def test_fact_has_dimension_runner_up(self) -> None:
        """FACT winner with multiple dimension columns surfaces DIMENSION as a runner_up.

        _score_dimension fires when key_count>=1 AND dim_count>=1 — confidence=0.75.
        Importantly the scorer does NOT filter on measure_count==0, so it fires
        even for FACT tables with strong dimension evidence.
        """
        df = self._make_fact_with_dimensions_df()
        result = profile_table(df, "t")
        ci = result.classification_inference
        assert ci is not None, "classification_inference is None"

        assert ci.value == TableClassification.FACT, (
            f"Expected FACT winner, got {ci.value}"
        )
        dim_rus = [r for r in ci.runner_ups if r.value == TableClassification.DIMENSION]
        assert len(dim_rus) >= 1, (
            f"Expected DIMENSION runner_up. runner_ups={ci.runner_ups}"
        )

class TestSampleReliabilityCalibration:
    """Tests that both classifiers apply a confidence penalty for small samples.

    Phase 6 adds small-sample confidence calibration to both role inference
    and table classification:
    - Below _MIN_RELIABLE_SAMPLE (30 rows), confidence is scaled linearly by
      (sample_size / 30), floored at _SAMPLE_PENALTY_FLOOR (0.50).
    - At or above 30 rows, confidence is unchanged.
    - Inference.sample_size is now populated on table classification Inference.

    Table fixtures use 6 columns to bypass the LOOKUP short-circuit
    (_LOOKUP_MAX_ROWS=1000 AND col_count<=5: 6 cols forces the LOOKUP check False).
    """

    @staticmethod
    def _make_small_fk_df() -> pd.DataFrame:
        """15-row DataFrame with a FK column.

        Expected role=FOREIGN_KEY (baseline confidence=0.82).
        After penalty: max(0.50, 0.82 * 15/30) = max(0.50, 0.41) = 0.50.
        """
        return pd.DataFrame({
            "row_id":    range(15),
            "branch_id": [1, 2, 3, 4, 5] * 3,
        })

    @staticmethod
    def _make_large_fk_df() -> pd.DataFrame:
        """100-row DataFrame with the same FK column layout.

        100 rows >= _MIN_RELIABLE_SAMPLE (30), so no penalty applied.
        Expected role=FOREIGN_KEY, confidence=0.82 unchanged.
        """
        return pd.DataFrame({
            "row_id":    range(100),
            "branch_id": [1, 2, 3, 4, 5] * 20,
        })

    @staticmethod
    def _make_small_fact_df() -> pd.DataFrame:
        """20-row, 6-column FACT DataFrame (bypasses LOOKUP via col_count=6 > 5).

        Expected classification=FACT (baseline confidence=0.80).
        After penalty: max(0.50, 0.80 * 20/30) = max(0.50, 0.533) ≈ 0.533.
        sample_size is set to 20.
        """
        return pd.DataFrame({
            "order_id":    range(20),
            "customer_id": [i % 4 for i in range(20)],
            "status":      ["open", "closed"] * 10,
            "category":    ["A", "B", "C", "D"] * 5,
            "subcategory": ["X", "Y", "Z"] * 6 + ["X", "Y"],
            "total_amount": [100.0, 200.0, 300.0, 400.0] * 5,
        })

    @staticmethod
    def _make_large_fact_df() -> pd.DataFrame:
        """1100-row, 6-column FACT DataFrame (>1000 rows also bypasses LOOKUP).

        Expected classification=FACT, confidence=0.80 unchanged.
        sample_size is set to 1100.
        """
        return pd.DataFrame({
            "order_id":    range(1100),
            "customer_id": [i % 50 for i in range(1100)],
            "status":      (["open", "closed", "pending"] * 366 + ["open", "open"]),
            "category":    (["A", "B", "C", "D"] * 275),
            "subcategory": (["X", "Y", "Z", "W"] * 275),
            "total_amount": [100.0, 200.0, 300.0, 400.0, 500.0] * 220,
        })

    # ── Role inference ────────────────────────────────────────────────────────

    def test_role_inference_sample_size_populated(self) -> None:
        """role_inference.sample_size equals the DataFrame row count.

        _classify_winner sets sample_size=profile.row_count on every Inference.
        Verifying with a 100-row fixture so no penalty is applied.
        """
        df = self._make_large_fk_df()
        result = profile_table(df, "t")
        col = next((p for p in result.columns if p.name == "branch_id"), None)
        assert col is not None, "Column 'branch_id' not found"
        assert col.role_inference is not None, "role_inference is None"
        assert col.role_inference.sample_size == 100, (
            f"Expected sample_size=100, got {col.role_inference.sample_size}"
        )

    def test_role_inference_confidence_penalized_for_small_sample(self) -> None:
        """Role confidence is penalised to the floor (0.50) for 15-row inputs.

        Baseline confidence for FOREIGN_KEY is 0.82.
        Penalty factor = 15/30 = 0.50 → penalised = max(0.50, 0.82*0.50) = 0.50.
        Large sample (100 rows) returns the full 0.82, confirming penalty is
        not always applied.
        """
        df_small = self._make_small_fk_df()
        df_big   = self._make_large_fk_df()

        col_small = next(
            (p for p in profile_table(df_small, "t").columns if p.name == "branch_id"), None
        )
        col_big = next(
            (p for p in profile_table(df_big, "t").columns if p.name == "branch_id"), None
        )
        assert col_small is not None and col_small.role_inference is not None
        assert col_big is not None and col_big.role_inference is not None

        assert col_small.role_inference.confidence == 0.50, (
            f"Expected penalised confidence=0.50, got {col_small.role_inference.confidence}"
        )
        assert col_big.role_inference.confidence == 0.82, (
            f"Expected unpenalised confidence=0.82, got {col_big.role_inference.confidence}"
        )

    # ── Table classification ──────────────────────────────────────────────────

    def test_table_classification_sample_size_populated(self) -> None:
        """classification_inference.sample_size equals the DataFrame row count.

        Prior to Phase 6, sample_size was always 0 on table classification Inference.
        """
        df = self._make_large_fact_df()
        result = profile_table(df, "t")
        ci = result.classification_inference
        assert ci is not None, "classification_inference is None"
        assert ci.sample_size == 1100, (
            f"Expected sample_size=1100, got {ci.sample_size}"
        )

    def test_table_classification_confidence_penalized_for_small_sample(self) -> None:
        """Table classification confidence is penalised for 20-row inputs.

        Baseline confidence for FACT is 0.80.
        Penalty factor = 20/30 = 0.667 → penalised = max(0.50, 0.80*0.667) ≈ 0.533.
        Large sample (1100 rows) returns the full 0.80.
        """
        df_small = self._make_small_fact_df()
        df_big   = self._make_large_fact_df()

        ci_small = profile_table(df_small, "t").classification_inference
        ci_big   = profile_table(df_big, "t").classification_inference
        assert ci_small is not None, "classification_inference is None (small)"
        assert ci_big   is not None, "classification_inference is None (big)"

        assert ci_small.confidence < 0.80, (
            f"Expected penalised confidence < 0.80, got {ci_small.confidence}"
        )
        assert ci_small.confidence > 0.50, (
            f"Expected confidence > floor (0.50), got {ci_small.confidence}"
        )
        assert ci_big.confidence == 0.80, (
            f"Expected unpenalised confidence=0.80, got {ci_big.confidence}"
        )

class TestRunnerUpsInFindings:
    """End-to-end tests: runner_ups propagate into TableProfile.findings.

    Phases 4-6 populated runner_ups on role and table classification Inference
    objects. The existing _build_findings / _collect_ambiguity_findings wiring
    in profiler.py already formatted those runner_ups into findings strings —
    but no test asserted the full end-to-end path from runner_up → findings.

    These tests close that gap.

    Fixture: 1100 rows (bypasses LOOKUP short-circuit at row_count <= 1000)
    with snapshot_date/TIMESTAMP + site_id/FK + capacity_mw/MEASURE.
      - Table: FACT(0.80) with SNAPSHOT runner_up → "Classification alternatives:" in findings
      - Column: site_id FK with PARTITION runner_up → "role alternatives:" in findings
    """

    @staticmethod
    def _make_fact_with_competing_signals_df() -> pd.DataFrame:
        """1100-row DataFrame with FACT winner and meaningful runner_ups on both levels.

        Table runner_up: SNAPSHOT (temporal column present → 0.72).
        Column runner_up: PARTITION on site_id (low-cardinality integer → 0.82).
        """
        return pd.DataFrame({
            "snapshot_date": [
                (pd.Timestamp("2025-01-01") + pd.Timedelta(days=i // 50))
                for i in range(1100)
            ],
            "site_id":     [i % 10 for i in range(1100)],
            "capacity_mw": [float(i % 5) * 50 for i in range(1100)],
        })

    def test_classification_runner_up_appears_in_findings(self) -> None:
        """Table classification runner_ups produce a 'Classification alternatives' finding.

        _build_findings checks classification_inference.runner_ups and appends
        "Classification alternatives: <formatted list>" when any runner_up is present.
        Populated by Phase 5 (_score_snapshot → SNAPSHOT at 0.72 for FACT winner).
        """
        df = self._make_fact_with_competing_signals_df()
        result = profile_table(df, "t")

        has_alt = any(
            "classification alternatives" in f.lower()
            for f in result.findings
        )
        assert has_alt, (
            f"Expected 'Classification alternatives' in findings. "
            f"findings={result.findings}"
        )

    def test_column_role_runner_up_appears_in_findings(self) -> None:
        """Column role runner_ups produce a 'role alternatives' finding per column.

        _collect_ambiguity_findings (called from _build_findings) iterates column
        profiles and appends "Column <name> — role alternatives: ..." when
        role_inference.runner_ups is non-empty.
        Populated by Phase 4 (_score_partition → PARTITION at 0.82 for site_id FK).
        """
        df = self._make_fact_with_competing_signals_df()
        result = profile_table(df, "t")

        has_role_alt = any(
            "role alternatives" in f.lower()
            for f in result.findings
        )
        assert has_role_alt, (
            f"Expected 'role alternatives' in findings. "
            f"findings={result.findings}"
        )

class TestFreetextNamePattern:
    """Tests that annotation columns (notes, description, etc.) classify as FREETEXT.

    _FREETEXT_NAME_RE covers universal annotation column name conventions:
    notes, comments, description, remarks, memo, narrative, annotation,
    body, message, summary, details, text.

    A name-pattern match fires in section 9 of _classify_winner — after the
    avg_length>50 and semantic_type=FREETEXT gates — when the column is
    string-typed AND avg_length >= 8.  This prevents the DIMENSION fallback
    from claiming well-known annotation columns.

    Guard cases verified live (not tested here to keep suite fast):
    - "notes" col with avg_length < 8 (Y/N values) → DIMENSION (not FREETEXT)
    - "notes" col with numeric dtype → UNKNOWN (not FREETEXT)
    """

    @staticmethod
    def _make_notes_df() -> pd.DataFrame:
        """200-row DataFrame where 'notes' has typical annotation-column profile.

        avg_length=21 ('Scheduled maintenance'), null_pct=60%, distinct_count=1.
        Before the fix this classified as DIMENSION (low distinct_pct fallback).
        """
        N = 200
        notes_pattern = (["Scheduled maintenance"] * 2 + [None] * 3) * (N // 5)
        return pd.DataFrame({"notes": notes_pattern[:N]})

    @staticmethod
    def _make_description_df() -> pd.DataFrame:
        """200-row DataFrame where 'description' has a longer annotation string.

        Confirms the pattern set covers the broader 'description' convention,
        not just 'notes'.
        """
        N = 200
        return pd.DataFrame({"description": ["Solar plant in TX"] * N})

    def test_notes_column_classifies_as_freetext(self) -> None:
        """'notes' col with avg_length=21 → FREETEXT via _FREETEXT_NAME_RE.

        Previously fell through to DIMENSION because avg_length=21 < 50
        (old threshold) and distinct_pct=0.002 < 0.80 (cardinality gate).
        The name-pattern check fires first and returns FREETEXT(0.80).
        """
        df = self._make_notes_df()
        result = profile_table(df, "t")
        col = result.columns[0]
        assert col.role_inference is not None
        assert col.role_inference.value.value == "freetext", (
            f"Expected FREETEXT, got {col.role_inference.value.value}. "
            f"evidence={col.role_inference.evidence}"
        )
        assert any(
            "name_matches_freetext_pattern" in e
            for e in col.role_inference.evidence
        ), f"Expected name-pattern evidence. evidence={col.role_inference.evidence}"

    def test_description_column_classifies_as_freetext(self) -> None:
        """'description' col → FREETEXT — confirms broader pattern coverage.

        The _FREETEXT_NAME_RE covers 'description' as a standard annotation
        column name. This test ensures the fix is generalised, not just
        special-cased for 'notes'.
        """
        df = self._make_description_df()
        result = profile_table(df, "t")
        col = result.columns[0]
        assert col.role_inference is not None
        assert col.role_inference.value.value == "freetext", (
            f"Expected FREETEXT for 'description' col, "
            f"got {col.role_inference.value.value}."
        )

class TestRisksAndActionsStressTest:
    """Stress-tests for the risks and suggested_actions output paths.

    Exercises outlier detection and format-error detection jointly, verifying
    that profile_table surfaces both signal types correctly into result.risks
    and result.suggested_actions.

    Key constraint — IQR gotcha:
        The outlier detector returns None when IQR == 0.  A constant baseline
        (e.g. all-10.0) gives Q1 == Q3 == 10 → IQR = 0 → early return.
        Fixtures must use a VARIED baseline so IQR > 0 and extreme values
        clear the upper fence.  The pattern `[10.0]*50 + [12.0]*25 + [14.0]*20`
        is the established convention (also used by numeric_outlier_df).

    Fixture layout:
        _make_outlier_df       — turbine_id (SK) + capacity_mw (varied + 5 extremes)
        _make_format_error_df  — invoice_id (SK) + invoice_date (3 mixed formats) + amount_usd
        _make_combined_df      — invoice_id (SK) + invoice_date (mixed) + amount_usd (varied + extremes)
    """

    @staticmethod
    def _make_outlier_df() -> pd.DataFrame:
        """100-row DataFrame with 5 IQR-detectable numeric outliers.

        capacity_mw: [10.0]*50 + [12.0]*25 + [14.0]*20  (varied baseline, IQR > 0)
                   + [250.0]*5  (extremes — 17× the upper fence)
        outlier_pct = 5.0%  >  _OUTLIER_MIN_PCT (1%).
        """
        baseline = [10.0] * 50 + [12.0] * 25 + [14.0] * 20
        extreme  = [250.0] * 5
        return pd.DataFrame({
            "turbine_id":  list(range(100)),
            "capacity_mw": baseline + extreme,
        })

    @staticmethod
    def _make_format_error_df() -> pd.DataFrame:
        """100-row DataFrame with three mixed date formats in invoice_date.

        Format split: ISO 8601 (40%), US slash (40%), named-month (20%).
        Expected FormatIssue: column=invoice_date, issue_type=mixed_date_format, severity=error.
        """
        mixed_dates = (
            ["2026-01-15"] * 40 +   # %Y-%m-%d
            ["01/15/2026"] * 40 +   # %m/%d/%Y
            ["15-Jan-2026"] * 20    # %d-%b-%Y
        )
        return pd.DataFrame({
            "invoice_id":   list(range(100)),
            "invoice_date": mixed_dates,
            "amount_usd":   [100.0] * 100,
        })

    @staticmethod
    def _make_combined_df() -> pd.DataFrame:
        """100-row DataFrame that triggers BOTH outlier and format-error paths.

        invoice_date: three mixed date formats → format risk + action.
        amount_usd:   varied baseline + 5 extremes → outlier risk + action.

        Expected: len(result.risks) == 2, len(result.suggested_actions) == 2.
        """
        mixed_dates = (
            ["2026-01-15"] * 40 +
            ["01/15/2026"] * 40 +
            ["15-Jan-2026"] * 20
        )
        baseline = [10.0] * 50 + [12.0] * 25 + [14.0] * 20
        extreme  = [250.0] * 5
        return pd.DataFrame({
            "invoice_id":   list(range(100)),
            "invoice_date": mixed_dates,
            "amount_usd":   baseline + extreme,
        })

    # ── Outlier path ──────────────────────────────────────────────────────────

    def test_outlier_risk_appears_in_risks(self) -> None:
        """IQR-detected outliers produce an entry in result.risks.

        _build_outlier_risks surfaces OutlierProfile results as:
        "[<col>] <n> outlier(s) (<pct>%) outside IQR bounds — may skew aggregations"
        """
        result = profile_table(self._make_outlier_df(), "t")

        assert any(
            "outside iqr bounds" in r.lower() for r in result.risks
        ), f"Expected IQR-bounds risk. risks={result.risks}"

    def test_outlier_action_recommends_investigation(self) -> None:
        """IQR outliers produce a 'Investigate or cap' action in suggested_actions.

        _build_outlier_actions surfaces the recommended remediation step.
        """
        result = profile_table(self._make_outlier_df(), "t")

        assert any(
            "investigate or cap outliers" in a.lower()
            for a in result.suggested_actions
        ), f"Expected outlier-cap action. actions={result.suggested_actions}"

    # ── Format error path ─────────────────────────────────────────────────────

    def test_format_error_risk_appears_in_risks(self) -> None:
        """Mixed date formats produce a 'Multiple date formats detected' risk entry.

        _build_profile_risks surfaces format_issues of severity='error' via the
        format_checker pipeline.
        """
        result = profile_table(self._make_format_error_df(), "t")

        assert any(
            "multiple date formats" in r.lower() for r in result.risks
        ), f"Expected mixed-date-format risk. risks={result.risks}"

    # ── Combined path ─────────────────────────────────────────────────────────

    def test_combined_fixture_produces_two_risks_and_two_actions(self) -> None:
        """Both outlier and format-error signals fire together on the combined fixture.

        Verifies that the risk/action builders are additive — neither path silences
        the other.  Exact string content is verified by the individual tests above;
        this test asserts count only.
        """
        result = profile_table(self._make_combined_df(), "t")

        # After the DATE_STRING→TIMESTAMP fix, invoice_date is correctly classified
        # as TIMESTAMP, so freshness analysis now fires a staleness risk (+1 risk, +1 action).
        # Expected: format-error risk + outlier risk + freshness-stale risk = 3 total.
        assert len(result.risks) == 3, (
            f"Expected 3 risks (format + outlier + freshness-stale). risks={result.risks}"
        )
        assert len(result.suggested_actions) == 3, (
            f"Expected 3 actions (format + outlier + freshness). actions={result.suggested_actions}"
        )

class TestNearUniqueNaturalKey:
    """Tests for step 8b: near-unique CODE semantic columns → NATURAL_KEY.

    The fix targets business-assigned alphanumeric codes (e.g. "PJM-AG2-073",
    "MISO-J3087") that have semantic_type=CODE and a very high but not perfect
    distinct_pct.  Snapshot duplication or minor rekeying depresses distinct_pct
    slightly below 1.0 without changing the column's role as a natural key.

    Step 8b fires when:
        is_code_semantic AND string_type AND not is_unique
        AND distinct_pct >= _NEAR_UNIQUE_NK_THRESHOLD (0.90)
        AND null_pct < _NK_MAX_NULL_PCT (0.05)

    Fixtures use market-prefixed codes in the format "<MARKET>-<NNNN>" which
    trigger semantic_type=CODE via _CODE_RE.  Both fixtures have 1,000 rows to
    avoid the LOOKUP short-circuit (≤1,000 rows AND ≤5 cols).
    """

    @staticmethod
    def _make_near_unique_code_df() -> pd.DataFrame:
        """1,000-row DataFrame with a near-unique code column (distinct_pct = 0.950).

        950 unique market-prefixed codes + 50 duplicates drawn from the first 50.
        seed=0 gives distinct_pct=0.950, null_pct=0.0, semantic_type=CODE.
        Expected role: NATURAL_KEY (0.78), counter_signal 'not_strictly_unique'.
        """
        random.seed(0)
        markets = ["PJM", "MISO", "SPP", "ERCOT", "CAISO"]
        base  = [f"{random.choice(markets)}-{i:04d}" for i in range(950)]
        dupes = random.choices(base[:50], k=50)
        values = (base + dupes)[:1000]
        random.shuffle(values)
        return pd.DataFrame({
            "interconnection_ref": values,
            "capacity_mw":         [100.0] * 1000,
        })

    @staticmethod
    def _make_below_threshold_code_df() -> pd.DataFrame:
        """1,000-row DataFrame with a code column below the 0.90 threshold.

        800 unique codes + 200 duplicates drawn from the first 100.
        seed=1 gives distinct_pct=0.800, null_pct=0.0, semantic_type=CODE.
        Expected role: NOT NATURAL_KEY (falls through to UNKNOWN at step 8b threshold).
        """
        random.seed(1)
        markets = ["PJM", "MISO", "SPP", "ERCOT", "CAISO"]
        base  = [f"{random.choice(markets)}-{i:04d}" for i in range(800)]
        dupes = random.choices(base[:100], k=200)
        values = (base + dupes)[:1000]
        random.shuffle(values)
        return pd.DataFrame({
            "interconnection_ref": values,
            "capacity_mw":         [100.0] * 1000,
        })

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_near_unique_code_column_classifies_as_natural_key(self) -> None:
        """A near-unique CODE column (distinct_pct=0.950) → NATURAL_KEY(0.78).

        Verifies that step 8b fires and populates the correct role, confidence,
        and 'not_strictly_unique' counter_signal on the Inference object.
        """
        result = profile_table(self._make_near_unique_code_df(), "t")
        col    = next(c for c in result.columns if c.name == "interconnection_ref")
        ri     = col.role_inference
        assert ri is not None, "role_inference must not be None"

        assert col.role.value == "natural_key", (
            f"Expected natural_key, got {col.role.value!r}. "
            f"distinct_pct={col.distinct_pct:.3f}  evidence={ri.evidence}"
        )
        assert ri.confidence == pytest.approx(0.78, abs=0.01), (
            f"Expected confidence≈0.78, got {ri.confidence:.2f}"
        )
        assert "not_strictly_unique" in ri.counter_signals, (
            f"Expected 'not_strictly_unique' in counter_signals. "
            f"Got: {ri.counter_signals}"
        )

    # ── Boundary ─────────────────────────────────────────────────────────────

    def test_code_below_threshold_does_not_classify_as_natural_key(self) -> None:
        """A CODE column with distinct_pct=0.800 (below 0.90) → NOT NATURAL_KEY.

        The 0.90 threshold prevents near-FK-like columns with ~20% duplicates
        from being promoted to natural key.  They should fall through to UNKNOWN
        rather than receiving an inflated role assignment.
        """
        result = profile_table(self._make_below_threshold_code_df(), "t")
        col    = next(c for c in result.columns if c.name == "interconnection_ref")

        assert col.role.value != "natural_key", (
            f"Expected role != natural_key for "
            f"distinct_pct={col.distinct_pct:.3f} (below 0.90 threshold). "
            f"Got: {col.role.value!r}"
        )

class TestConstantIntegerPartition:
    """Tests for the _is_partition_like_numeric fix: constant integers in bounded ranges.

    Background
    ----------
    _is_partition_like_numeric originally returned False for distinct_count < 2,
    which caused constant snapshot columns (snapshot_year=2026, snapshot_month=5)
    to fall through to MEASURE(0.72) instead of PARTITION(0.82).

    The fix allows distinct_count==1 when the constant value is in a
    partition-typical range:
        - Small ordinals: 1 ≤ v ≤ 53  (month, quarter, week, day-of-week, etc.)
        - Year range:     1990 ≤ v ≤ 2100

    This fix is purely behavioral — no column name matching is used inside
    _is_partition_like_numeric.  Name-based classification (via _PARTITION_NAME_RE)
    only applies to the separate step-7 path in _classify_winner, which is also
    tested indirectly here but is not the target of this class.

    Fixture design
    --------------
    All fixtures use 1,000 rows and 2 columns.  The column under test is paired
    with a dummy `capacity_mw` float column to give profile_table something to
    work with.  The column names (snapshot_year, snapshot_month, load_batch) are
    illustrative but the assertions target the behavioral path
    (evidence contains 'bounded_value_range_suggests_partition').
    """

    @staticmethod
    def _make_constant_year_df() -> pd.DataFrame:
        """1,000 rows with a constant integer year column (value=2026).

        distinct_count=1, min=max=2026 → inside 1990-2100 year range.
        Expected: PARTITION(0.82) via _is_partition_like_numeric.
        """
        return pd.DataFrame({
            "snapshot_year": [2026] * 1000,
            "capacity_mw":   [100.0] * 1000,
        })

    @staticmethod
    def _make_constant_month_df() -> pd.DataFrame:
        """1,000 rows with a constant integer month column (value=5).

        distinct_count=1, min=max=5 → inside 1-53 small-ordinal range.
        Expected: PARTITION(0.82) via _is_partition_like_numeric.
        """
        return pd.DataFrame({
            "snapshot_month": [5] * 1000,
            "capacity_mw":    [100.0] * 1000,
        })

    @staticmethod
    def _make_out_of_range_df() -> pd.DataFrame:
        """1,000 rows with a constant integer outside both partition ranges (value=500).

        500 > 53 and 500 < 1990 — not a year, not a small ordinal.
        Expected: NOT PARTITION (falls through to MEASURE via generic numeric path).
        """
        return pd.DataFrame({
            "load_batch":  [500] * 1000,
            "capacity_mw": [100.0] * 1000,
        })

    # ── Constant year ─────────────────────────────────────────────────────────

    def test_constant_year_integer_classifies_as_partition(self) -> None:
        """A constant snapshot_year=2026 column → PARTITION(0.82).

        Verifies that _is_partition_like_numeric now allows distinct_count==1
        when the value is in the year range 1990-2100.  The evidence list must
        contain 'bounded_value_range_suggests_partition' confirming the
        behavioral path fired (not a name-pattern path).
        """
        result = profile_table(self._make_constant_year_df(), "t")
        col    = next(c for c in result.columns if c.name == "snapshot_year")
        ri     = col.role_inference
        assert ri is not None

        assert col.role.value == "partition", (
            f"Expected partition, got {col.role.value!r}. evidence={ri.evidence}"
        )
        assert ri.confidence == pytest.approx(0.82, abs=0.01)
        assert "bounded_value_range_suggests_partition" in ri.evidence, (
            f"Expected behavioral evidence. Got: {ri.evidence}"
        )

    # ── Constant small ordinal ────────────────────────────────────────────────

    def test_constant_small_ordinal_integer_classifies_as_partition(self) -> None:
        """A constant snapshot_month=5 column → PARTITION(0.82).

        Verifies that distinct_count==1 with a value in the small-ordinal range
        (1-53) is recognised as a partition column (covers month, quarter, week,
        day-of-week, etc.).
        """
        result = profile_table(self._make_constant_month_df(), "t")
        col    = next(c for c in result.columns if c.name == "snapshot_month")
        ri     = col.role_inference
        assert ri is not None

        assert col.role.value == "partition", (
            f"Expected partition, got {col.role.value!r}. evidence={ri.evidence}"
        )
        assert "bounded_value_range_suggests_partition" in ri.evidence

    # ── Out-of-range boundary ─────────────────────────────────────────────────

    def test_constant_out_of_range_integer_stays_measure(self) -> None:
        """A constant load_batch=500 column → NOT PARTITION.

        500 is above the small-ordinal ceiling (53) and below the year floor
        (1990), so _is_partition_like_numeric returns False and the column falls
        through to the generic MEASURE path.  This test guards the boundary —
        the fix must not promote arbitrary constant integers to PARTITION.
        """
        result = profile_table(self._make_out_of_range_df(), "t")
        col    = next(c for c in result.columns if c.name == "load_batch")

        assert col.role.value != "partition", (
            f"Expected role != partition for out-of-range constant (value=500). "
            f"Got: {col.role.value!r}"
        )

class TestDateStringTimestamp:
    """Tests for the _TIMESTAMP_NAME_RE regex fix and DATE_STRING semantic fallback.

    Two root causes were fixed in lib/role_classifier.py:

    Bug 1 — Broken regex word-boundary tokens
    ------------------------------------------
    _TIMESTAMP_NAME_RE had tokens like '_date', '_time', '_at', '_ts', '_dt'
    inside the alternation group, but the outer ``(?:^|_)`` separator already
    consumes the preceding underscore.  The effective requirement was therefore
    ``__date`` (double underscore), which never matches real column names like
    ``queue_date``, ``start_date``, or ``in_service_date``.

    Fix: tokens changed to ``date``, ``time``, ``at``, ``ts``, ``dt`` — the
    outer separator now correctly handles the word boundary.

    Bug 2 — Missing DATE_STRING semantic-only fallback (step 2b)
    -------------------------------------------------------------
    String-typed date columns with non-conventional names (e.g. "expiry") had
    no path to TIMESTAMP even when semantic_type=DATE_STRING was correctly
    detected.  A new step-2b path returns TIMESTAMP(0.85) for any DATE_STRING
    column that falls through the name-based path.

    Fixture design
    --------------
    All date fixtures use 1,000 rows of mixed ISO + US slash dates, which is
    the same pattern seen in the dogfood table (queue_positions).  Mixed dates
    ensure semantic_type=DATE_STRING is detected by the semantic typer.
    A non-temporal column (batch_count) guards against false positives.
    """

    # ── Shared fixture data ────────────────────────────────────────────────────
    _DATES = (["2023-06-15"] * 400 + ["06/15/2023"] * 400 + ["2022-01-01"] * 200)

    @classmethod
    def _make_date_suffix_df(cls) -> pd.DataFrame:
        """1,000 rows: queue_date column with mixed date strings.

        Column name ends in '_date' → now matches _TIMESTAMP_NAME_RE after fix.
        Combined with semantic_type=DATE_STRING → TIMESTAMP(0.90) via name+semantic path.
        """
        return pd.DataFrame({
            "queue_date":  cls._DATES,
            "capacity_mw": [100.0] * 1000,
        })

    @classmethod
    def _make_no_name_match_df(cls) -> pd.DataFrame:
        """1,000 rows: 'expiry' column with mixed date strings.

        Column name does NOT match any temporal pattern in _TIMESTAMP_NAME_RE.
        semantic_type=DATE_STRING alone → TIMESTAMP(0.85) via step-2b fallback.
        """
        return pd.DataFrame({
            "expiry":      cls._DATES,
            "capacity_mw": [100.0] * 1000,
        })

    @classmethod
    def _make_non_temporal_df(cls) -> pd.DataFrame:
        """1,000 rows: numeric batch_count column — not temporal, not DATE_STRING.

        Guards the fix against false positives.  Expected: NOT TIMESTAMP.
        """
        return pd.DataFrame({
            "batch_count": list(range(1, 6)) * 200,   # 5 distinct integers
            "capacity_mw": [100.0] * 1000,
        })

    # ── Tests ─────────────────────────────────────────────────────────────────

    def test_date_suffix_column_classifies_as_timestamp(self) -> None:
        """A '_date'-suffixed string column with date values → TIMESTAMP(0.90).

        Verifies that fixing the _TIMESTAMP_NAME_RE regex (removing the spurious
        leading '_' from the '_date' token) allows 'queue_date' to match the
        temporal name pattern.  Combined with semantic_type=DATE_STRING, the
        name+semantic path in step 2 fires at confidence 0.90.
        """
        result = profile_table(self._make_date_suffix_df(), "t")
        col    = next(c for c in result.columns if c.name == "queue_date")
        ri     = col.role_inference
        assert ri is not None

        assert col.role.value == "timestamp", (
            f"Expected timestamp, got {col.role.value!r}. "
            f"semantic={col.semantic_type.value}  evidence={ri.evidence}"
        )
        assert ri.confidence == pytest.approx(0.90, abs=0.01), (
            f"Expected confidence≈0.90 (name+semantic path), got {ri.confidence:.2f}"
        )
        assert "name_matches_timestamp_pattern" in ri.evidence, (
            f"Expected name evidence in {ri.evidence}"
        )

    def test_date_string_semantic_no_name_match_classifies_as_timestamp(self) -> None:
        """A DATE_STRING column with a non-temporal name → TIMESTAMP(0.85).

        Verifies step-2b: when semantic_type=DATE_STRING but the column name does
        not match _TIMESTAMP_NAME_RE (e.g. 'expiry'), the semantic-only fallback
        path returns TIMESTAMP with slightly lower confidence (0.85 vs 0.90).
        """
        result = profile_table(self._make_no_name_match_df(), "t")
        col    = next(c for c in result.columns if c.name == "expiry")
        ri     = col.role_inference
        assert ri is not None

        assert col.role.value == "timestamp", (
            f"Expected timestamp for DATE_STRING column 'expiry', "
            f"got {col.role.value!r}. evidence={ri.evidence}"
        )
        assert ri.confidence == pytest.approx(0.85, abs=0.01), (
            f"Expected confidence≈0.85 (semantic-only path), got {ri.confidence:.2f}"
        )
        assert "semantic_type=date_string" in ri.evidence

    def test_non_temporal_column_not_classified_as_timestamp(self) -> None:
        """A numeric column with no temporal signals → NOT TIMESTAMP.

        Guards the fix against false positives: batch_count has no temporal name,
        no DATE_STRING semantic type, and no temporal spark dtype.  It must not
        be promoted to TIMESTAMP by either fix.
        """
        result = profile_table(self._make_non_temporal_df(), "t")
        col    = next(c for c in result.columns if c.name == "batch_count")

        assert col.role.value != "timestamp", (
            f"batch_count must not be TIMESTAMP. Got {col.role.value!r}."
        )



def test_invalid_level_raises():
    """profile_table should raise ValueError for unknown levels."""
    df = pd.DataFrame({"x": [1, 2, 3]})
    with pytest.raises(ValueError, match="Invalid profiling level"):
        profile_table(df, "test", level="turbo")


def test_quick_level_skips_grain():
    """Quick profiling should set grain=None (not a default GrainAnalysis)."""
    df = pd.DataFrame({"x": [1, 2, 3]})
    profile = profile_table(df, "test", level="quick")
    assert profile.grain is None


def test_materialization_threshold_respects_column_count():
    """Wide tables should not be materialized even if row count is low."""
    from tools.table_profiler_tool.lib.profiler import _PANDAS_MATERIALIZE_THRESHOLD, _PANDAS_MATERIALIZE_MAX_COLUMNS
    assert _PANDAS_MATERIALIZE_MAX_COLUMNS <= 200  # sanity check
    assert _PANDAS_MATERIALIZE_THRESHOLD <= 1_000_000
