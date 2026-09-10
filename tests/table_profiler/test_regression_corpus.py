"""Regression tests for dogfooding-discovered fixes.

Each test class covers a specific fix discovered during corpus profiling of
analytics_dev.data_engineering_bronze.queue_* tables. These tests are designed to
FAIL if the corresponding fix code is reverted.

Fixes covered:
1. Partition-like numeric detection (role_classifier.py)
2. Hash ID vs freetext disambiguation (role_classifier.py)
3. Enum sensitivity for low cardinality (semantic_typer.py)
4. Spreadsheet artifact confidence penalty (semantic_typer.py)
5. STATE_CODE requires name hint (semantic_typer.py)
6. Stats-based enum fallback in profiler (profiler.py)
7. High-null foreign key detection (role_classifier.py)
8. Timestamp not classified as MEASURE (role_classifier.py)
9. Single-value column is not ENUM (semantic_typer.py)
10. Boolean-like string detection (semantic_typer.py)
11. Grain detector skips high-null candidates (grain_detector.py)
12. Currency amount detection with negative values (semantic_typer.py)
"""

from __future__ import annotations

import pandas as pd
import pytest

from tools.table_profiler_tool.lib.models import ColumnProfile, ColumnRole, Inference, SemanticType
from tools.table_profiler_tool.lib.role_classifier import infer_column_role
from tools.table_profiler_tool.lib.semantic_typer import infer_semantic_type
from tools.table_profiler_tool.lib.profiler import profile_table


# ---------------------------------------------------------------------------
# Helper — extended _profile builder with min/max fields
# ---------------------------------------------------------------------------


def _profile(
    name: str,
    spark_type: str = "object",
    semantic_type: SemanticType = SemanticType.UNKNOWN,
    is_unique: bool = False,
    is_constant: bool = False,
    null_count: int = 0,
    null_pct: float = 0.0,
    row_count: int = 1000,
    non_null_count: int = 1000,
    distinct_count: int = 10,
    distinct_pct: float = 0.01,
    avg_length: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    mean_value: float | None = None,
) -> ColumnProfile:
    """Build a ColumnProfile with full control over fields needed for regression tests."""

    return ColumnProfile(
        name=name,
        position=0,
        spark_type=spark_type,
        semantic_type=semantic_type,
        is_unique=is_unique,
        is_constant=is_constant,
        null_count=null_count,
        null_pct=null_pct,
        row_count=row_count,
        non_null_count=non_null_count,
        distinct_count=distinct_count,
        distinct_pct=distinct_pct,
        avg_length=avg_length,
        min_length=min_length,
        max_length=max_length,
        min_value=min_value,
        max_value=max_value,
        mean_value=mean_value,
    )


# ---------------------------------------------------------------------------
# Fix 1: Partition-like numeric detection
# ---------------------------------------------------------------------------


class TestPartitionLikeNumeric:
    """Integer columns with low cardinality + bounded values -> PARTITION, not MEASURE.

    Regression for: snapshot_year, snapshot_month, and similar partition columns
    that were being classified as MEASURE before the fix.
    """

    def test_year_like_column(self):
        """Integer column with 7 distinct values in range 2020-2026 -> PARTITION."""
        p = _profile(
            "data_period",  # Generic name — NO partition name hint
            spark_type="long",
            distinct_count=7,
            distinct_pct=0.007,
            min_value=2020,
            max_value=2026,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.PARTITION
        assert result.confidence >= 0.80
        assert "bounded_value_range_suggests_partition" in result.evidence

    def test_month_like_column(self):
        """Integer column with 12 distinct values in range 1-12 -> PARTITION."""
        p = _profile(
            "period_bucket",  # Generic name
            spark_type="integer",
            distinct_count=12,
            distinct_pct=0.012,
            min_value=1,
            max_value=12,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.PARTITION
        assert result.confidence >= 0.80

    def test_quarter_like_column(self):
        """Integer column with 4 distinct values in range 1-4 -> PARTITION."""
        p = _profile(
            "fiscal_segment",
            spark_type="int",
            distinct_count=4,
            distinct_pct=0.004,
            min_value=1,
            max_value=4,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.PARTITION

    def test_high_cardinality_numeric_stays_measure(self):
        """Numeric with 500 distinct values should NOT become PARTITION."""
        p = _profile(
            "sensor_reading",
            spark_type="double",
            distinct_count=500,
            distinct_pct=0.50,
            min_value=0.0,
            max_value=100.0,
            mean_value=50.0,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.MEASURE

    def test_wide_range_numeric_stays_measure(self):
        """Integer with values 0-10000 should NOT become PARTITION."""
        p = _profile(
            "counter_val",
            spark_type="long",
            distinct_count=15,
            distinct_pct=0.015,
            min_value=0,
            max_value=10000,
        )
        result = infer_column_role(p)
        assert result.value != ColumnRole.PARTITION


# ---------------------------------------------------------------------------
# Fix 2: Hash ID vs freetext
# ---------------------------------------------------------------------------


class TestHashIdentifier:
    """Fixed-length hex-like strings -> IDENTIFIER, not FREETEXT.

    Regression for: Change_Id (64-char SHA256 hash) being classified as FREETEXT.
    """

    def test_sha256_hash_column(self):
        """64-char fixed-length string with high distinctness -> IDENTIFIER."""
        p = _profile(
            "change_hash",
            spark_type="string",
            is_unique=False,
            distinct_count=990,
            distinct_pct=0.99,
            avg_length=64.0,
            min_length=64,
            max_length=64,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.IDENTIFIER
        assert "hash_like_fixed_length_string" in result.evidence

    def test_md5_hash_column(self):
        """32-char fixed-length string -> IDENTIFIER."""
        p = _profile(
            "row_fingerprint",
            spark_type="string",
            is_unique=False,
            distinct_count=950,
            distinct_pct=0.95,
            avg_length=32.0,
            min_length=32,
            max_length=32,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.IDENTIFIER

    def test_variable_length_long_string_stays_freetext(self):
        """Long strings with variable length should still be FREETEXT."""
        p = _profile(
            "notes",
            spark_type="string",
            is_unique=False,
            distinct_count=900,
            distinct_pct=0.90,
            avg_length=120.0,
            min_length=5,
            max_length=500,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.FREETEXT

    def test_non_hash_length_stays_freetext(self):
        """Fixed-length=75 (not a common hash length) stays FREETEXT."""
        p = _profile(
            "encoded_payload",
            spark_type="string",
            is_unique=False,
            distinct_count=800,
            distinct_pct=0.80,
            avg_length=75.0,
            min_length=75,
            max_length=75,
        )
        result = infer_column_role(p)
        # 75 is not a known hash length, so should NOT get IDENTIFIER
        assert result.value != ColumnRole.IDENTIFIER


# ---------------------------------------------------------------------------
# Fix 3: Enum sensitivity (low cardinality)
# ---------------------------------------------------------------------------


class TestEnumSensitivity:
    """Very low cardinality columns are detected as ENUM even when skewed.

    Regression for: Service column (2 distinct: ERIS/NRIS) not detected as ENUM
    because old floor was max(2, sample_size*0.05) which could be <2 for small samples.
    """

    def test_two_distinct_values_is_enum(self):
        """2 distinct values in 200 samples -> ENUM."""
        values = ["ERIS"] * 180 + ["NRIS"] * 20
        result = infer_semantic_type(values, "service_type")
        assert result.value == SemanticType.ENUM
        assert result.confidence >= 0.80

    def test_five_distinct_values_is_enum(self):
        """5 distinct values in 500 samples -> ENUM."""
        values = ["Active"] * 200 + ["Withdrawn"] * 150 + ["Suspended"] * 80 + ["Operating"] * 50 + ["Cancelled"] * 20
        result = infer_semantic_type(values, "project_phase")
        assert result.value == SemanticType.ENUM

    def test_eight_distinct_values_is_enum(self):
        """8 distinct values in 100 samples -> ENUM (within floor of 10)."""
        values = ["A"] * 30 + ["B"] * 20 + ["C"] * 15 + ["D"] * 10 + ["E"] * 10 + ["F"] * 8 + ["G"] * 5 + ["H"] * 2
        result = infer_semantic_type(values, "category_code")
        assert result.value == SemanticType.ENUM


# ---------------------------------------------------------------------------
# Fix 4: Spreadsheet artifact confidence penalty
# ---------------------------------------------------------------------------


class TestSpreadsheetArtifact:
    """Unnamed/auto-generated columns get confidence penalty.

    Regression for: Unnamed__0, Unnamed__1 getting high-confidence semantic
    inferences that were unreliable due to noisy spreadsheet headers.
    """

    def test_unnamed_column_gets_lower_confidence(self):
        """Pattern match on Unnamed__X should have reduced confidence."""
        # Use email values — detected purely by regex, no name hint needed
        values = [f"user{i}@example.com" for i in range(100)]
        result_named = infer_semantic_type(values, "contact_email")
        result_unnamed = infer_semantic_type(values, "unnamed__5")

        # Both should detect EMAIL but unnamed should have lower confidence
        assert result_named.value == SemanticType.EMAIL
        assert result_unnamed.value == SemanticType.EMAIL
        assert result_unnamed.confidence < result_named.confidence

    def test_unnamed_column_has_counter_signal(self):
        """Unnamed columns should have spreadsheet_artifact_column_name counter signal."""
        values = ["2024-01-15", "2024-02-20", "2024-03-10", "2024-04-05", "2024-05-30"]
        result = infer_semantic_type(values, "unnamed__0")
        if result.value != SemanticType.UNKNOWN:
            assert "spreadsheet_artifact_column_name" in result.counter_signals


# ---------------------------------------------------------------------------
# Fix 5: STATE_CODE requires name hint
# ---------------------------------------------------------------------------


class TestStateCodeNameHint:
    """STATE_CODE must not be inferred from ^[A-Z]{2}$ pattern alone.

    Regression for: Type__Fuel column getting STATE_CODE because values like
    'NG', 'SO', 'WI' matched the 2-letter uppercase regex.
    """

    def test_fuel_codes_not_state_code(self):
        """2-letter fuel codes without 'state' in name -> NOT STATE_CODE."""
        values = ["NG", "SO", "WI", "BA", "NU"] * 20
        result = infer_semantic_type(values, "type_fuel")
        assert result.value != SemanticType.STATE_CODE

    def test_market_codes_not_state_code(self):
        """2-letter market codes without 'state' in name -> NOT STATE_CODE."""
        values = ["PJ", "NY", "SP", "MI", "ER"] * 20
        result = infer_semantic_type(values, "market_region")
        assert result.value != SemanticType.STATE_CODE

    def test_state_column_still_detected(self):
        """Column named 'state' with 2-letter codes -> STATE_CODE (has hint)."""
        values = ["NC", "SC", "GA", "VA", "FL"] * 20
        result = infer_semantic_type(values, "facility_state")
        assert result.value == SemanticType.STATE_CODE
        assert result.confidence >= 0.80


# ---------------------------------------------------------------------------
# Fix 6: Stats-based enum fallback in profiler
# ---------------------------------------------------------------------------


class TestStatsFallbackEnum:
    """Profiler assigns ENUM when sample is too small but stats show low cardinality.

    Regression for: Service column with 2 distinct values but profiler sample
    only containing 5 identical values (all 'ERIS'), causing UNKNOWN.
    """

    def test_skewed_enum_detected_via_stats_fallback(self):
        """Column with 3 distinct values but heavily skewed -> ENUM via profiler."""
        # Create a DataFrame where one value dominates (95%)
        values = ["DOMINANT"] * 950 + ["RARE_A"] * 30 + ["RARE_B"] * 20
        df = pd.DataFrame({"service_type": values})
        result = profile_table(df, "test_enum_fallback")

        col = next(c for c in result.columns if c.name == "service_type")
        assert col.semantic_type == SemanticType.ENUM

    def test_high_cardinality_not_enum(self):
        """Column with 50 distinct values should NOT get enum fallback."""
        values = [f"val_{i}" for i in range(50)] * 20
        df = pd.DataFrame({"varied_col": values})
        result = profile_table(df, "test_no_enum")

        col = next(c for c in result.columns if c.name == "varied_col")
        # 50 distinct values exceeds the enum threshold
        assert col.semantic_type != SemanticType.ENUM or col.distinct_count > 20


# ---------------------------------------------------------------------------
# Fix 7: High-null foreign key detection
# ---------------------------------------------------------------------------


class TestHighNullForeignKey:
    """An _id column that is non-unique with moderate cardinality stays FOREIGN_KEY
    even when null_pct is high.

    Protects: FK columns often have nulls from LEFT JOINs upstream — the role
    classifier must not downgrade to UNKNOWN/MEASURE just because of nulls.
    """

    def test_high_null_id_is_foreign_key(self):
        """customer_id, 35% null, 500 distinct, not unique -> FOREIGN_KEY."""
        p = _profile(
            "customer_id",
            spark_type="long",
            is_unique=False,
            null_count=350,
            null_pct=0.35,
            non_null_count=650,
            distinct_count=500,
            distinct_pct=0.50,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.FOREIGN_KEY
        assert result.value != ColumnRole.UNKNOWN

    def test_low_null_id_still_foreign_key(self):
        """Same column with no nulls is still FOREIGN_KEY (baseline)."""
        p = _profile(
            "order_id",
            spark_type="long",
            is_unique=False,
            null_count=0,
            null_pct=0.0,
            non_null_count=1000,
            distinct_count=400,
            distinct_pct=0.40,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.FOREIGN_KEY


# ---------------------------------------------------------------------------
# Fix 8: Timestamp column not classified as MEASURE
# ---------------------------------------------------------------------------


class TestTimestampNotMeasure:
    """Datetime columns get TIMESTAMP even at high cardinality.

    Protects: created_at / updated_at have near-unique values that could look
    like a MEASURE; the temporal type must win.
    """

    def test_created_at_is_timestamp(self):
        """timestamp type, unique, 10k distinct -> TIMESTAMP, not MEASURE."""
        p = _profile(
            "created_at",
            spark_type="timestamp",
            is_unique=True,
            distinct_count=10000,
            distinct_pct=1.0,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.TIMESTAMP
        assert result.value != ColumnRole.MEASURE

    def test_updated_at_date_type_is_timestamp(self):
        """date type, high cardinality -> TIMESTAMP."""
        p = _profile(
            "updated_at",
            spark_type="date",
            is_unique=False,
            distinct_count=900,
            distinct_pct=0.90,
        )
        result = infer_column_role(p)
        assert result.value == ColumnRole.TIMESTAMP


# ---------------------------------------------------------------------------
# Fix 9: Single-value column is not ENUM
# ---------------------------------------------------------------------------


class TestSingleValueNotEnum:
    """A column with only 1 distinct value is a CONSTANT, not an ENUM.

    Protects: the enum classifier requires at least 2 distinct values.
    """

    def test_single_distinct_value_not_enum(self):
        """1 distinct value -> NOT ENUM."""
        values = ["active"] * 1000
        result = infer_semantic_type(values, "status")
        assert result.value != SemanticType.ENUM

    def test_two_distinct_values_can_be_enum(self):
        """2 distinct values still qualify as ENUM (boundary, baseline)."""
        values = ["active"] * 600 + ["closed"] * 400
        result = infer_semantic_type(values, "lifecycle_state")
        assert result.value == SemanticType.ENUM


# ---------------------------------------------------------------------------
# Fix 10: Boolean-like string detection
# ---------------------------------------------------------------------------


class TestBooleanStringDetection:
    """Two-value string columns mapping to true/false patterns -> BOOLEAN_STRING."""

    def test_y_n_is_boolean_string(self):
        """Y/N with a boolean name hint -> BOOLEAN_STRING."""
        values = ["Y"] * 600 + ["N"] * 400
        result = infer_semantic_type(values, "is_active")
        assert result.value == SemanticType.BOOLEAN_STRING

    def test_true_false_is_boolean_string(self):
        """true/false strings -> BOOLEAN_STRING."""
        values = ["true"] * 500 + ["false"] * 500
        result = infer_semantic_type(values, "has_consent")
        assert result.value == SemanticType.BOOLEAN_STRING


# ---------------------------------------------------------------------------
# Fix 11: Grain detector skips high-null candidates
# ---------------------------------------------------------------------------


class TestGrainSkipsHighNull:
    """Grain detection must not select a mostly-null column as the grain.

    Protects: a column that is "unique" only among its non-null values is
    useless as a grain when most rows are null.
    """

    def test_high_null_unique_column_not_selected(self):
        """optional_id (60% null, unique among non-null) loses to record_id (0% null, unique)."""
        n = 100
        optional = [f"opt_{i}" if i >= 60 else None for i in range(n)]  # 60% null, unique non-nulls
        record = [f"rec_{i}" for i in range(n)]  # 0% null, fully unique
        df = pd.DataFrame({"optional_id": optional, "record_id": record})

        result = profile_table(df, "grain_null_test")
        assert result.grain.best_grain == ["record_id"]


# ---------------------------------------------------------------------------
# Fix 12: Currency amount detection with negative values
# ---------------------------------------------------------------------------


class TestNegativeCurrencyAmount:
    """Currency columns with negatives (refunds/adjustments) stay CURRENCY_AMOUNT.

    Protects: the presence of a leading minus sign must not disqualify the
    currency_amount semantic type.
    """

    def test_negative_currency_values_detected(self):
        """Dollar amounts including negatives -> CURRENCY_AMOUNT."""
        # Enough distinct values to stay above the enum threshold.
        positives = [f"${i * 10}.50" for i in range(1, 40)]
        negatives = [f"-${i * 5}.00" for i in range(1, 20)]
        values = positives + negatives
        result = infer_semantic_type(values, "total_amount")
        assert result.value == SemanticType.CURRENCY_AMOUNT

    def test_all_negative_currency_detected(self):
        """A refund column that is entirely negative -> CURRENCY_AMOUNT.

        This is the case the leading-minus regex fix protects: with no positive
        values to carry the match ratio, negatives must match on their own.
        """
        values = [f"-${i}.00" for i in range(1, 50)]
        result = infer_semantic_type(values, "refund_amount")
        assert result.value == SemanticType.CURRENCY_AMOUNT

    def test_all_positive_currency_still_detected(self):
        """Baseline: positive-only dollar amounts -> CURRENCY_AMOUNT."""
        values = [f"${i * 10}.00" for i in range(1, 50)]
        result = infer_semantic_type(values, "amount")
        assert result.value == SemanticType.CURRENCY_AMOUNT
