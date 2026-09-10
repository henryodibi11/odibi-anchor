"""Unit tests for table classification module."""

from __future__ import annotations

import pytest

from tools.table_profiler_tool.lib.table_classifier import classify_table
from tools.table_profiler_tool.lib.models import (
    ColumnProfile,
    ColumnRole,
    FreshnessAnalysis,
    GrainAnalysis,
    Inference,
    SemanticType,
    TableClassification,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(
    name: str,
    role: ColumnRole = ColumnRole.UNKNOWN,
    spark_type: str = "string",
    row_count: int = 5000,
    semantic_type: SemanticType = SemanticType.UNKNOWN,
) -> ColumnProfile:
    """Build a minimal ColumnProfile for classifier tests."""
    return ColumnProfile(
        name=name,
        position=0,
        spark_type=spark_type,
        role=role,
        semantic_type=semantic_type,
        row_count=row_count,
        non_null_count=row_count,
        null_count=0,
        null_pct=0.0,
        distinct_count=row_count,
        distinct_pct=1.0,
    )


def _make_grain(
    best_grain: list[str],
    sample_size: int = 5000,
    is_unique: bool = True,
) -> GrainAnalysis:
    """Build a minimal GrainAnalysis."""
    return GrainAnalysis(
        best_grain=best_grain,
        is_unique=is_unique,
        duplicate_rate=0.0,
        candidates_tested=[],
        inference=Inference(
            value="grain_detected",
            confidence=0.95,
            evidence=[],
            sample_size=sample_size,
            method="grain_detection",
        ),
    )


def _make_freshness(cadence: str) -> FreshnessAnalysis:
    """Build a minimal FreshnessAnalysis with the given cadence."""
    return FreshnessAnalysis(
        freshness_column="loaded_at",
        latest_value="2026-01-01",
        earliest_value="2025-01-01",
        staleness="2.0h ago",
        staleness_hours=2.0,
        cadence=cadence,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmptyProfiles:
    """Empty profile list returns UNKNOWN."""

    def test_empty_returns_unknown(self) -> None:
        result = classify_table([])
        assert result.value == TableClassification.UNKNOWN

    def test_empty_confidence_low(self) -> None:
        result = classify_table([])
        assert result.confidence <= 0.50


class TestSCD2:
    """Tables with effective_date + end_date patterns are SCD2."""

    def test_scd2_detected(self) -> None:
        profiles = [
            _make_profile("customer_id", ColumnRole.PRIMARY_KEY),
            _make_profile("name", ColumnRole.DIMENSION),
            _make_profile("effective_date", ColumnRole.TIMESTAMP, "date"),
            _make_profile("end_date", ColumnRole.TIMESTAMP, "date"),
        ]
        result = classify_table(profiles)
        assert result.value == TableClassification.SCD2

    def test_scd2_valid_from_valid_to(self) -> None:
        profiles = [
            _make_profile("id", ColumnRole.PRIMARY_KEY),
            _make_profile("valid_from", ColumnRole.TIMESTAMP, "timestamp"),
            _make_profile("valid_to", ColumnRole.TIMESTAMP, "timestamp"),
        ]
        assert classify_table(profiles).value == TableClassification.SCD2

    def test_scd2_confidence_high(self) -> None:
        profiles = [
            _make_profile("effective_date", ColumnRole.TIMESTAMP, "date"),
            _make_profile("end_date", ColumnRole.TIMESTAMP, "date"),
        ]
        assert classify_table(profiles).confidence >= 0.85


class TestBridge:
    """Exactly 2 FK columns = bridge table."""

    def test_bridge_detected(self) -> None:
        profiles = [
            _make_profile("order_id", ColumnRole.FOREIGN_KEY),
            _make_profile("product_id", ColumnRole.FOREIGN_KEY),
        ]
        assert classify_table(profiles).value == TableClassification.BRIDGE

    def test_three_cols_not_bridge(self) -> None:
        profiles = [
            _make_profile("order_id", ColumnRole.FOREIGN_KEY),
            _make_profile("product_id", ColumnRole.FOREIGN_KEY),
            _make_profile("quantity", ColumnRole.MEASURE),
        ]
        result = classify_table(profiles)
        assert result.value != TableClassification.BRIDGE


class TestStaging:
    """Column name patterns indicate a staging/landing table."""

    def test_raw_prefix_detected(self) -> None:
        profiles = [
            _make_profile("raw_invoice_id", ColumnRole.UNKNOWN),
            _make_profile("raw_amount", ColumnRole.UNKNOWN),
        ]
        assert classify_table(profiles).value == TableClassification.STAGING

    def test_stg_prefix_detected(self) -> None:
        profiles = [
            _make_profile("stg_customer", ColumnRole.UNKNOWN),
            _make_profile("amount", ColumnRole.MEASURE),
        ]
        assert classify_table(profiles).value == TableClassification.STAGING

    def test_source_suffix_detected(self) -> None:
        profiles = [
            _make_profile("id", ColumnRole.PRIMARY_KEY),
            _make_profile("value_source", ColumnRole.UNKNOWN),
        ]
        assert classify_table(profiles).value == TableClassification.STAGING


class TestAggregate:
    """Majority of non-key columns starting with aggregate prefixes."""

    def test_aggregate_detected(self) -> None:
        profiles = [
            _make_profile("region", ColumnRole.DIMENSION),
            _make_profile("sum_revenue", ColumnRole.MEASURE),
            _make_profile("avg_order_value", ColumnRole.MEASURE),
            _make_profile("count_orders", ColumnRole.MEASURE),
            _make_profile("max_date", ColumnRole.TIMESTAMP),
        ]
        result = classify_table(profiles)
        assert result.value == TableClassification.AGGREGATE


class TestLookup:
    """Small row count + few columns = lookup table."""

    def test_lookup_detected_with_grain(self) -> None:
        profiles = [
            _make_profile("code", ColumnRole.PRIMARY_KEY, row_count=50),
            _make_profile("description", ColumnRole.DIMENSION, row_count=50),
        ]
        grain = _make_grain(["code"], sample_size=50)
        result = classify_table(profiles, grain=grain)
        assert result.value == TableClassification.LOOKUP

    def test_large_table_not_lookup(self) -> None:
        profiles = [
            _make_profile("code", ColumnRole.PRIMARY_KEY, row_count=50000),
            _make_profile("description", ColumnRole.DIMENSION, row_count=50000),
        ]
        grain = _make_grain(["code"], sample_size=50000)
        result = classify_table(profiles, grain=grain)
        assert result.value != TableClassification.LOOKUP


class TestEventLog:
    """Timestamp in grain + append cadence = event log."""

    def test_event_log_daily(self) -> None:
        profiles = [
            _make_profile("event_id", ColumnRole.PRIMARY_KEY),
            _make_profile("event_ts", ColumnRole.TIMESTAMP, "timestamp"),
            _make_profile("event_type", ColumnRole.DIMENSION),
        ]
        grain = _make_grain(["event_id", "event_ts"])
        freshness = _make_freshness("daily")
        result = classify_table(profiles, grain=grain, freshness=freshness)
        assert result.value == TableClassification.EVENT_LOG

    def test_event_log_hourly(self) -> None:
        profiles = [
            _make_profile("id", ColumnRole.PRIMARY_KEY),
            _make_profile("ts", ColumnRole.TIMESTAMP, "timestamp"),
        ]
        grain = _make_grain(["id", "ts"])
        freshness = _make_freshness("hourly")
        result = classify_table(profiles, grain=grain, freshness=freshness)
        assert result.value == TableClassification.EVENT_LOG


class TestSnapshot:
    """Date column in grain = snapshot table."""

    def test_snapshot_date_in_grain(self) -> None:
        profiles = [
            _make_profile("customer_id", ColumnRole.FOREIGN_KEY),
            _make_profile("snapshot_date", ColumnRole.PARTITION, "date"),
            _make_profile("balance", ColumnRole.MEASURE),
        ]
        grain = _make_grain(["customer_id", "snapshot_date"])
        result = classify_table(profiles, grain=grain)
        assert result.value == TableClassification.SNAPSHOT


class TestFact:
    """Measure + FK columns = fact table."""

    def test_fact_with_measures_and_fks(self) -> None:
        profiles = [
            _make_profile("invoice_id", ColumnRole.PRIMARY_KEY),
            _make_profile("customer_id", ColumnRole.FOREIGN_KEY),
            _make_profile("product_id", ColumnRole.FOREIGN_KEY),
            _make_profile("amount", ColumnRole.MEASURE),
            _make_profile("quantity", ColumnRole.MEASURE),
        ]
        result = classify_table(profiles)
        assert result.value == TableClassification.FACT

    def test_fact_confidence(self) -> None:
        profiles = [
            _make_profile("fk_a", ColumnRole.FOREIGN_KEY),
            _make_profile("revenue", ColumnRole.MEASURE),
        ]
        assert classify_table(profiles).confidence >= 0.75


class TestDimension:
    """Key + descriptive columns, no measures = dimension."""

    def test_dimension_detected(self) -> None:
        profiles = [
            _make_profile("customer_id", ColumnRole.PRIMARY_KEY),
            _make_profile("name", ColumnRole.DIMENSION),
            _make_profile("region", ColumnRole.DIMENSION),
            _make_profile("segment", ColumnRole.DIMENSION),
        ]
        result = classify_table(profiles)
        assert result.value == TableClassification.DIMENSION

    def test_dimension_no_measures(self) -> None:
        profiles = [
            _make_profile("id", ColumnRole.PRIMARY_KEY),
            _make_profile("category", ColumnRole.DIMENSION),
        ]
        assert classify_table(profiles).value == TableClassification.DIMENSION


class TestInferenceContract:
    """Inference metadata is correctly populated."""

    def test_method_is_table_classification(self) -> None:
        result = classify_table([_make_profile("id", ColumnRole.PRIMARY_KEY)])
        assert result.method == "table_classification"

    def test_confidence_in_range(self) -> None:
        result = classify_table([_make_profile("id", ColumnRole.UNKNOWN)])
        assert 0.0 <= result.confidence <= 1.0

    def test_evidence_is_list(self) -> None:
        result = classify_table([_make_profile("id", ColumnRole.PRIMARY_KEY)])
        assert isinstance(result.evidence, list)

    def test_value_is_table_classification(self) -> None:
        result = classify_table([])
        assert isinstance(result.value, TableClassification)

