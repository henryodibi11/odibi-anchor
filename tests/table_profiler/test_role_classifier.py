"""Unit tests for column role classification module."""

from __future__ import annotations

import pytest

from tools.table_profiler_tool.lib.models import ColumnProfile, ColumnRole, Inference, SemanticType
from tools.table_profiler_tool.lib.role_classifier import infer_column_role


# ---------------------------------------------------------------------------
# Helper — build a minimal ColumnProfile for testing
# ---------------------------------------------------------------------------


def _profile(
    name: str,
    spark_type: str = "object",
    semantic_type: SemanticType = SemanticType.UNKNOWN,
    is_unique: bool = False,
    is_constant: bool = False,
    null_count: int = 0,
    row_count: int = 100,
    non_null_count: int = 100,
    distinct_count: int = 10,
    distinct_pct: float = 0.10,
    avg_length: float | None = None,
    mean_value: float | None = None,
) -> ColumnProfile:
    """Build a minimal ColumnProfile for role classification tests."""

    return ColumnProfile(
        name=name,
        position=0,
        spark_type=spark_type,
        semantic_type=semantic_type,
        is_unique=is_unique,
        is_constant=is_constant,
        null_count=null_count,
        row_count=row_count,
        non_null_count=non_null_count,
        distinct_count=distinct_count,
        distinct_pct=distinct_pct,
        avg_length=avg_length,
        mean_value=mean_value,
    )


# ---------------------------------------------------------------------------
# Tests — one per major role
# ---------------------------------------------------------------------------


class TestTimestamp:
    """TIMESTAMP role detection."""

    def test_temporal_spark_type(self):
        p = _profile("event_time", spark_type="timestamp")
        result = infer_column_role(p)
        assert result.value == ColumnRole.TIMESTAMP
        assert result.confidence >= 0.90

    def test_date_string_with_temporal_name(self):
        p = _profile("created_date", spark_type="object",
                     semantic_type=SemanticType.DATE_STRING)
        result = infer_column_role(p)
        assert result.value == ColumnRole.TIMESTAMP
        assert result.confidence >= 0.85


class TestMetadata:
    """METADATA role detection — audit/ETL columns."""

    def test_loaded_at_column(self):
        p = _profile("loaded_at", spark_type="timestamp")
        result = infer_column_role(p)
        # An audit column that is itself a temporal TYPE is fundamentally a
        # TIMESTAMP — the metadata branch defers to timestamp for temporal types.
        # String audit columns (created_by, etl_batch_id) still classify METADATA.
        assert result.value == ColumnRole.TIMESTAMP
        assert result.confidence >= 0.85

    def test_etl_batch_id(self):
        p = _profile("etl_batch_id", spark_type="string",
                     distinct_count=50, distinct_pct=0.05)
        result = infer_column_role(p)
        assert result.value == ColumnRole.METADATA
        assert result.confidence >= 0.85

    def test_created_by(self):
        p = _profile("created_by", spark_type="string",
                     distinct_count=5, distinct_pct=0.05)
        result = infer_column_role(p)
        assert result.value == ColumnRole.METADATA
        assert result.confidence >= 0.80


class TestFlag:
    """FLAG role detection — boolean indicators."""

    def test_flag_name_with_two_values(self):
        p = _profile("is_active", spark_type="object",
                     semantic_type=SemanticType.BOOLEAN_STRING,
                     distinct_count=2, distinct_pct=0.02)
        result = infer_column_role(p)
        assert result.value == ColumnRole.FLAG
        assert result.confidence >= 0.90

    def test_boolean_without_flag_name(self):
        p = _profile("status_flag_col", spark_type="object",
                     semantic_type=SemanticType.BOOLEAN_STRING,
                     distinct_count=2, distinct_pct=0.02)
        result = infer_column_role(p)
        assert result.value == ColumnRole.FLAG
        assert result.confidence >= 0.78


class TestSurrogateKey:
    """SURROGATE_KEY role detection — unique integer + key name."""

    def test_integer_id_unique(self):
        p = _profile("customer_id", spark_type="bigint",
                     is_unique=True, null_count=0,
                     distinct_count=100, distinct_pct=1.0)
        result = infer_column_role(p)
        assert result.value == ColumnRole.SURROGATE_KEY
        assert result.confidence >= 0.90


class TestNaturalKey:
    """NATURAL_KEY role detection — unique non-integer + key pattern."""

    def test_uuid_key(self):
        p = _profile("order_id", spark_type="string",
                     semantic_type=SemanticType.UUID,
                     is_unique=True, null_count=0,
                     distinct_count=100, distinct_pct=1.0)
        result = infer_column_role(p)
        assert result.value == ColumnRole.NATURAL_KEY
        assert result.confidence >= 0.90

    def test_string_key(self):
        p = _profile("invoice_id", spark_type="string",
                     is_unique=True, null_count=0,
                     distinct_count=100, distinct_pct=1.0)
        result = infer_column_role(p)
        assert result.value == ColumnRole.NATURAL_KEY
        assert result.confidence >= 0.85


class TestForeignKey:
    """FOREIGN_KEY role detection — key name + non-unique."""

    def test_fk_pattern(self):
        p = _profile("customer_id", spark_type="bigint",
                     is_unique=False, null_count=0,
                     distinct_count=20, distinct_pct=0.20)
        result = infer_column_role(p)
        assert result.value == ColumnRole.FOREIGN_KEY
        assert result.confidence >= 0.80


class TestMeasure:
    """MEASURE role detection — numeric, non-unique."""

    def test_numeric_with_measure_name(self):
        p = _profile("total_amount", spark_type="double",
                     is_unique=False, distinct_count=80, distinct_pct=0.80,
                     mean_value=1500.0)
        result = infer_column_role(p)
        assert result.value == ColumnRole.MEASURE
        assert result.confidence >= 0.85

    def test_numeric_without_measure_name(self):
        p = _profile("value_x", spark_type="float",
                     is_unique=False, distinct_count=50, distinct_pct=0.50,
                     mean_value=42.0)
        result = infer_column_role(p)
        assert result.value == ColumnRole.MEASURE
        assert result.confidence >= 0.65


class TestIdentifier:
    """IDENTIFIER role — unique code/UUID without key name pattern."""

    def test_unique_code_column(self):
        p = _profile("reference_code", spark_type="string",
                     semantic_type=SemanticType.CODE,
                     is_unique=True, null_count=0,
                     distinct_count=100, distinct_pct=1.0)
        result = infer_column_role(p)
        assert result.value == ColumnRole.IDENTIFIER
        assert result.confidence >= 0.80


class TestFreetext:
    """FREETEXT role — long strings, high cardinality."""

    def test_long_string_high_cardinality(self):
        p = _profile("description", spark_type="string",
                     is_unique=False, distinct_count=95, distinct_pct=0.95,
                     avg_length=120.0)
        result = infer_column_role(p)
        assert result.value == ColumnRole.FREETEXT
        assert result.confidence >= 0.80


class TestDimension:
    """DIMENSION role — low cardinality string/enum."""

    def test_enum_semantic_type(self):
        p = _profile("status", spark_type="object",
                     semantic_type=SemanticType.ENUM,
                     is_unique=False, distinct_count=5, distinct_pct=0.05)
        result = infer_column_role(p)
        assert result.value == ColumnRole.DIMENSION
        assert result.confidence >= 0.85

    def test_low_cardinality_string(self):
        p = _profile("department", spark_type="string",
                     is_unique=False, distinct_count=8, distinct_pct=0.08)
        result = infer_column_role(p)
        assert result.value == ColumnRole.DIMENSION
        assert result.confidence >= 0.70


class TestUnknown:
    """Fallback when no strong signal exists."""

    def test_ambiguous_column(self):
        p = _profile("col_xyz", spark_type="string",
                     is_unique=True, null_count=5,
                     distinct_count=95, distinct_pct=0.95)
        result = infer_column_role(p)
        # Unique string with nulls and no name pattern — ambiguous
        assert result.value == ColumnRole.UNKNOWN
        assert result.confidence == 0.0


class TestCorpusRegressions:
    """Focused fixtures for dogfooding regressions already covered more broadly elsewhere."""

    @pytest.mark.parametrize(
        ("name", "spark_type", "distinct_count", "distinct_pct", "min_value", "max_value"),
        [
            ("snapshot_year", "long", 7, 0.007, 2020, 2026),
            ("snapshot_month", "integer", 12, 0.012, 1, 12),
        ],
    )
    def test_partition_like_numeric_columns(self, name, spark_type, distinct_count, distinct_pct, min_value, max_value):
        p = _profile(
            name,
            spark_type=spark_type,
            distinct_count=distinct_count,
            distinct_pct=distinct_pct,
        )
        p.min_value = min_value
        p.max_value = max_value

        result = infer_column_role(p)

        assert result.value == ColumnRole.PARTITION
        assert "bounded_value_range_suggests_partition" in result.evidence

    def test_hash_like_change_id_is_identifier(self):
        p = _profile(
            "Change_Id",
            spark_type="string",
            is_unique=False,
            distinct_count=990,
            distinct_pct=0.99,
            avg_length=64.0,
        )
        p.min_length = 64
        p.max_length = 64

        result = infer_column_role(p)

        assert result.value == ColumnRole.IDENTIFIER
        assert "hash_like_fixed_length_string" in result.evidence


class TestInferenceContract:
    """Verify Inference output contract."""

    def test_output_shape(self):
        p = _profile("amount", spark_type="double",
                     is_unique=False, distinct_count=50, distinct_pct=0.50,
                     mean_value=100.0)
        result = infer_column_role(p)
        assert isinstance(result, Inference)
        assert isinstance(result.value, ColumnRole)
        assert isinstance(result.confidence, float)
        assert isinstance(result.evidence, list)
        assert isinstance(result.counter_signals, list)
        assert result.method == "heuristic"
        assert result.sample_size == 100
