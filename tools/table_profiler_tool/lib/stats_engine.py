"""Column statistics engine for Table Profiler.

Computes foundational column statistics for pandas and Spark DataFrames using a
single public entry point and engine-specific implementations.
"""

from __future__ import annotations

import math
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from ._sampling import detect_engine
from ._sampling import representative_sample
from .models import ColumnProfile
from .models import SemanticType

sys.dont_write_bytecode = True

TOP_VALUE_LIMIT = 10

try:
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover - pyspark optional for pandas-only tests
    F = None


NUMERIC_PANDAS_KINDS = {"b", "i", "u", "f"}


def compute_column_stats(df: Any) -> list[ColumnProfile]:
    """Compute column statistics for a pandas or Spark DataFrame.

    Args:
        df: Input pandas or Spark DataFrame.

    Returns:
        Column profiles containing foundational statistics for each column.

    Raises:
        TypeError: If the DataFrame engine is unsupported.
    """

    engine = detect_engine(df)
    if engine == "pandas":
        return _compute_column_stats_pandas(df)
    return _compute_column_stats_spark(df)


def column_profiles_as_dicts(df: Any) -> list[dict[str, Any]]:
    """Return column profiles as plain dictionaries for debugging or JSON use."""

    return [asdict(profile) for profile in compute_column_stats(df)]


def _compute_column_stats_pandas(df: pd.DataFrame) -> list[ColumnProfile]:
    """Compute foundational column statistics for a pandas DataFrame."""

    row_count = len(df)
    column_profiles: list[ColumnProfile] = []

    for position, column_name in enumerate(df.columns):
        series = df[column_name]
        non_null = series.dropna()
        non_null_count = int(non_null.shape[0])
        null_count = int(row_count - non_null_count)
        distinct_count = int(non_null.nunique(dropna=True)) if non_null_count else 0
        distinct_pct = (distinct_count / non_null_count) if non_null_count else 0.0
        is_unique = bool(non_null_count > 0 and distinct_count == non_null_count)
        is_constant = bool(non_null_count > 0 and distinct_count == 1)
        is_string = _is_string_like_series(series)
        top_values = _top_values_pandas(non_null)
        sample_values = _normalize_sample_values(representative_sample(non_null.tolist(), n=5))

        min_value = None
        max_value = None
        mean_value = None
        std_value = None
        median_value = None
        min_length = None
        max_length = None
        avg_length = None

        if non_null_count:
            min_value = _normalize_scalar(non_null.min())
            max_value = _normalize_scalar(non_null.max())

        if _is_numeric_series(series):
            numeric_series = pd.to_numeric(non_null, errors="coerce").dropna()
            if not numeric_series.empty:
                min_value = _normalize_scalar(numeric_series.min())
                max_value = _normalize_scalar(numeric_series.max())
                mean_value = _safe_float(numeric_series.mean())
                std_value = _safe_float(numeric_series.std(ddof=1))
                median_value = _safe_float(numeric_series.median())

        if is_string:
            lengths = non_null.astype(str).str.len()
            if not lengths.empty:
                min_length = int(lengths.min())
                max_length = int(lengths.max())
                avg_length = _safe_float(lengths.mean())

        column_profiles.append(
            ColumnProfile(
                name=str(column_name),
                position=position,
                spark_type=str(series.dtype),
                semantic_type=SemanticType.UNKNOWN,
                is_nullable=bool(series.isna().any()),
                row_count=row_count,
                non_null_count=non_null_count,
                null_count=null_count,
                null_pct=_ratio(null_count, row_count),
                effective_null_pct=_ratio(null_count, row_count),
                distinct_count=distinct_count,
                distinct_pct=distinct_pct,
                is_unique=is_unique,
                is_constant=is_constant,
                top_values=top_values,
                sample_values=sample_values,
                min_value=min_value,
                max_value=max_value,
                mean_value=mean_value,
                std_value=std_value,
                median_value=median_value,
                min_length=min_length,
                max_length=max_length,
                avg_length=avg_length,
            )
        )

    return column_profiles


def _compute_column_stats_spark(df: Any) -> list[ColumnProfile]:
    """Compute foundational column statistics for a Spark DataFrame."""

    if F is None:
        raise TypeError("PySpark is not available for Spark statistics.")

    row_count = int(df.count())
    column_profiles: list[ColumnProfile] = []
    schema_by_name = {field.name: field.dataType.simpleString() for field in df.schema.fields}

    for position, column_name in enumerate(df.columns):
        # Pre-compute type info BEFORE building the agg query to avoid
        # invalid implicit CASTs on non-numeric columns (CAST_INVALID_INPUT).
        data_type = schema_by_name[column_name]
        is_numeric = _is_numeric_spark_type(data_type)
        is_string = data_type == "string"

        column_ref = F.col(column_name)
        non_null_expr = column_ref.isNotNull()

        # Build agg expressions conditionally: numeric-only aggregations
        # (mean, stddev, percentile) use a NULL double literal for non-numeric
        # columns to prevent Spark from attempting an implicit CAST to DOUBLE.
        _null_dbl = F.lit(None).cast("double")
        agg_exprs = [
            F.count(F.when(non_null_expr, 1)).alias("non_null_count"),
            F.countDistinct(column_ref).alias("distinct_count"),
            F.min(column_ref).alias("min_value"),
            F.max(column_ref).alias("max_value"),
            (F.mean(column_ref) if is_numeric else _null_dbl).alias("mean_value"),
            (F.stddev_samp(column_ref) if is_numeric else _null_dbl).alias("std_value"),
            (F.percentile_approx(column_ref, 0.5, 10000) if is_numeric else _null_dbl).alias("median_value"),
            F.max(F.length(F.trim(column_ref.cast("string")))).alias("max_length"),
            F.min(F.length(F.trim(column_ref.cast("string")))).alias("min_length"),
            F.avg(F.length(F.trim(column_ref.cast("string")))).alias("avg_length"),
        ]

        try:
            aggregate_row = (
                df.select(column_name)
                .agg(*agg_exprs)
                .collect()[0]
            )
        except Exception as exc:  # noqa: BLE001
            # Degrade a single column rather than crashing all column stats.
            column_profiles.append(
                ColumnProfile(
                    name=column_name,
                    position=position,
                    spark_type=data_type,
                    semantic_type=SemanticType.UNKNOWN,
                    is_nullable=True,
                    row_count=row_count,
                    quality_flags=[f"stats_degraded: {str(exc)[:120]}"],
                )
            )
            continue

        non_null_count = int(aggregate_row["non_null_count"] or 0)
        null_count = int(row_count - non_null_count)
        distinct_count = int(aggregate_row["distinct_count"] or 0)
        distinct_pct = (distinct_count / non_null_count) if non_null_count else 0.0
        is_unique = bool(non_null_count > 0 and distinct_count == non_null_count)
        is_constant = bool(non_null_count > 0 and distinct_count == 1)
        top_values = _top_values_spark(df, column_name, row_count)
        sample_values = _sample_values_spark(df, column_name)

        min_value = _normalize_scalar(aggregate_row["min_value"])
        max_value = _normalize_scalar(aggregate_row["max_value"])
        mean_value = _safe_float(aggregate_row["mean_value"]) if is_numeric else None
        std_value = _safe_float(aggregate_row["std_value"]) if is_numeric else None
        median_value = _safe_float(aggregate_row["median_value"]) if is_numeric else None
        min_length = int(aggregate_row["min_length"]) if is_string and aggregate_row["min_length"] is not None else None
        max_length = int(aggregate_row["max_length"]) if is_string and aggregate_row["max_length"] is not None else None
        avg_length = _safe_float(aggregate_row["avg_length"]) if is_string else None

        column_profiles.append(
            ColumnProfile(
                name=column_name,
                position=position,
                spark_type=data_type,
                semantic_type=SemanticType.UNKNOWN,
                is_nullable=bool(null_count > 0),
                row_count=row_count,
                non_null_count=non_null_count,
                null_count=null_count,
                null_pct=_ratio(null_count, row_count),
                effective_null_pct=_ratio(null_count, row_count),
                distinct_count=distinct_count,
                distinct_pct=distinct_pct,
                is_unique=is_unique,
                is_constant=is_constant,
                top_values=top_values,
                sample_values=sample_values,
                min_value=min_value,
                max_value=max_value,
                mean_value=mean_value,
                std_value=std_value,
                median_value=median_value,
                min_length=min_length,
                max_length=max_length,
                avg_length=avg_length,
            )
        )

    return column_profiles


def _top_values_pandas(series: pd.Series) -> list[dict[str, Any]]:
    """Return top values for a pandas Series."""

    if series.empty:
        return []

    counts = series.value_counts(dropna=True).head(TOP_VALUE_LIMIT)
    total = int(series.shape[0])
    results: list[dict[str, Any]] = []
    for value, count in counts.items():
        results.append(
            {
                "value": _normalize_scalar(value),
                "count": int(count),
                "pct": _ratio(int(count), total),
            }
        )
    return results


def _top_values_spark(df: Any, column_name: str, row_count: int) -> list[dict[str, Any]]:
    """Return top values for a Spark DataFrame column."""

    if F is None:
        return []

    grouped = (
        df.select(column_name)
        .where(F.col(column_name).isNotNull())
        .groupBy(column_name)
        .count()
        .orderBy(F.desc("count"), F.asc(column_name))
        .limit(TOP_VALUE_LIMIT)
        .collect()
    )
    non_null_total = row_count - int(df.where(F.col(column_name).isNull()).count())
    results: list[dict[str, Any]] = []
    for row in grouped:
        count = int(row["count"])
        results.append(
            {
                "value": _normalize_scalar(row[column_name]),
                "count": count,
                "pct": _ratio(count, non_null_total),
            }
        )
    return results


def _sample_values_spark(df: Any, column_name: str) -> list[str]:
    """Return representative sample values for a Spark column."""

    if F is None:
        return []

    sampled_rows = (
        df.select(column_name)
        .where(F.col(column_name).isNotNull())
        .limit(5)
        .collect()
    )
    return _normalize_sample_values([row[column_name] for row in sampled_rows])


def _is_numeric_series(series: pd.Series) -> bool:
    """Return True when a pandas Series is numeric-like."""

    return getattr(series.dtype, "kind", None) in NUMERIC_PANDAS_KINDS


def _is_string_like_series(series: pd.Series) -> bool:
    """Return True when a pandas Series should get string-specific stats."""

    if pd.api.types.is_string_dtype(series.dtype):
        return True
    non_null = series.dropna()
    if non_null.empty:
        return False
    return all(isinstance(value, str) for value in non_null.head(20).tolist())


def _is_numeric_spark_type(data_type: str) -> bool:
    """Return True when a Spark simpleString type is numeric."""

    numeric_prefixes = (
        "tinyint",
        "smallint",
        "int",
        "bigint",
        "float",
        "double",
        "decimal",
        "long",
        "short",
    )
    return data_type.startswith(numeric_prefixes)


def _ratio(numerator: int, denominator: int) -> float:
    """Return a safe float ratio."""

    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _safe_float(value: Any) -> float | None:
    """Convert supported numeric scalars to float while preserving nulls."""

    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, bool)):
        return float(value)
    return float(value)


def _normalize_scalar(value: Any) -> Any:
    """Normalize scalars for JSON-friendly profile output."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return str(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _normalize_sample_values(values: list[Any]) -> list[str]:
    """Convert representative values to compact display strings."""

    normalized: list[str] = []
    for value in values:
        scalar = _normalize_scalar(value)
        normalized.append("None" if scalar is None else str(scalar))
    return normalized
