"""Join Readiness Profiler for Table Profiler.

Given a source DataFrame and reference tables, computes join readiness metrics:
- Overlap %: what fraction of source key values exist in the target
- Orphan count/pct: source values with no match in target
- Cardinality: 1:1, 1:many, or many:many
- Format compatibility: case/whitespace/dash differences in join keys
- Safe join type recommendation: INNER (full overlap) or LEFT (orphans exist)

Public API
----------
profile_joins(df, reference_tables, profiles=None) -> list[JoinProfile]

reference_tables format:
    For pandas:  {"source_col": (target_df, "target_col")}
    For Spark:   {"source_col": "catalog.schema.table.target_col"}
                 (reads table via spark.table(), extracts last segment as column)
"""

from __future__ import annotations

import sys
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from .models import ColumnProfile, Inference, JoinProfile

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
    from pyspark.sql import SparkSession
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None
    SparkSession = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum distinct values to collect for overlap analysis
_MAX_DISTINCT_VALUES = 100_000

# Cardinality detection: if target has duplicates on the key, it's many-side
_CARDINALITY_THRESHOLD_1_TO_1 = 1.0  # distinct/count ratio must be 1.0

# Format mismatch sample size for comparison
_FORMAT_SAMPLE_SIZE = 100


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def profile_joins(
    df: Any,
    reference_tables: dict[str, Any],
    profiles: list[ColumnProfile] | None = None,
) -> list[JoinProfile]:
    """Profile join relationships between source DataFrame and reference tables.

    Args:
        df: Source DataFrame (pandas or Spark).
        reference_tables: Mapping of source column to target reference.
            - Pandas: {"col": (target_df, "target_col")}
            - Spark:  {"col": "catalog.schema.table.target_col"}
        profiles: Optional pre-computed column profiles (unused currently,
                  reserved for future format hinting).

    Returns:
        List of JoinProfile objects, one per reference_tables entry.
        Entries that fail are silently skipped.
    """
    if not reference_tables:
        return []

    engine = detect_engine(df)
    results: list[JoinProfile] = []

    for source_col, target_spec in reference_tables.items():
        try:
            jp = _profile_single_join(df, source_col, target_spec, engine)
            if jp is not None:
                results.append(jp)
        except Exception:  # noqa: BLE001
            continue  # Skip failed joins gracefully

    return results


# ---------------------------------------------------------------------------
# Private: single join profiling
# ---------------------------------------------------------------------------


def _profile_single_join(
    df: Any,
    source_col: str,
    target_spec: Any,
    engine: str,
) -> JoinProfile | None:
    """Profile a single join relationship."""
    if engine == "pandas":
        return _profile_join_pandas(df, source_col, target_spec)
    return _profile_join_spark(df, source_col, target_spec)


def _profile_join_pandas(
    df: pd.DataFrame,
    source_col: str,
    target_spec: Any,
) -> JoinProfile | None:
    """Pandas implementation of join profiling."""
    if source_col not in df.columns:
        return None

    # Parse target spec: (target_df, "target_col") or (target_df, "target_col", "table_name")
    if not isinstance(target_spec, (tuple, list)) or len(target_spec) < 2:
        return None

    target_df = target_spec[0]
    target_col = target_spec[1]
    target_table = target_spec[2] if len(target_spec) > 2 else "reference"

    if not isinstance(target_df, pd.DataFrame):
        return None
    if target_col not in target_df.columns:
        return None

    # Get distinct non-null values from source
    source_values = set(df[source_col].dropna().unique())
    if not source_values:
        return None

    # Get distinct non-null values from target
    target_values = set(target_df[target_col].dropna().unique())
    if not target_values:
        return None

    # Overlap analysis
    overlap = source_values & target_values
    orphans = source_values - target_values
    overlap_pct = len(overlap) / len(source_values) if source_values else 0.0
    orphan_count = len(orphans)
    orphan_pct = orphan_count / len(source_values) if source_values else 0.0

    # Cardinality detection
    # Source side: how many rows per distinct source key?
    source_row_count = len(df[df[source_col].notna()])
    source_distinct = len(source_values)
    source_ratio = source_distinct / source_row_count if source_row_count > 0 else 0

    # Target side: is the target key unique?
    target_row_count = len(target_df[target_df[target_col].notna()])
    target_distinct = len(target_values)
    target_is_unique = target_distinct == target_row_count

    cardinality = _determine_cardinality(
        source_ratio=source_ratio,
        source_is_unique=(source_distinct == source_row_count),
        target_is_unique=target_is_unique,
    )

    # Format compatibility check
    format_compatible, mismatch_desc = _check_format_compatibility_pandas(
        source_values, target_values
    )

    # Safe join type recommendation
    safe_join_type = "INNER" if overlap_pct >= 0.999 else "LEFT"

    evidence = [
        f"source={source_col}",
        f"target={target_table}.{target_col}",
        f"overlap={overlap_pct:.1%}",
        f"orphans={orphan_count}",
        f"cardinality={cardinality}",
    ]

    return JoinProfile(
        source_column=source_col,
        target_table=target_table,
        target_column=target_col,
        cardinality=cardinality,
        overlap_pct=round(overlap_pct, 4),
        orphan_count=orphan_count,
        orphan_pct=round(orphan_pct, 4),
        format_compatible=format_compatible,
        format_mismatch_description=mismatch_desc,
        safe_join_type=safe_join_type,
        inference=Inference(
            value="join_profiled",
            confidence=0.85,
            evidence=evidence,
            sample_size=len(source_values),
            method="join_profiling",
        ),
    )


def _profile_join_spark(
    df: Any,
    source_col: str,
    target_spec: Any,
) -> JoinProfile | None:
    """Spark implementation of join profiling."""
    if F is None or SparkSession is None:
        return None  # pragma: no cover
    if source_col not in df.columns:
        return None

    # Parse target spec: "catalog.schema.table.column" string
    if isinstance(target_spec, str):
        parts = target_spec.rsplit(".", 1)
        if len(parts) != 2:
            return None
        table_path, target_col = parts
        target_table = table_path

        try:
            spark = SparkSession.getActiveSession()
            if spark is None:
                return None  # pragma: no cover
            target_df = spark.table(table_path)
        except Exception:  # noqa: BLE001
            return None
    elif isinstance(target_spec, (tuple, list)) and len(target_spec) >= 2:
        # Also support (spark_df, "col", "table_name") for testing
        target_df = target_spec[0]
        target_col = target_spec[1]
        target_table = target_spec[2] if len(target_spec) > 2 else "reference"
    else:
        return None

    if target_col not in target_df.columns:
        return None

    # Source distinct count (bounded check)
    source_distinct_count = (
        df.select(source_col).where(F.col(source_col).isNotNull()).distinct().count()
    )
    if source_distinct_count == 0:
        return None
    if source_distinct_count > _MAX_DISTINCT_VALUES:
        # Too many distinct values — sample-based approach
        source_distinct_count = _MAX_DISTINCT_VALUES

    # Target distinct count
    target_distinct_count = (
        target_df.select(target_col).where(F.col(target_col).isNotNull()).distinct().count()
    )
    target_row_count = target_df.where(F.col(target_col).isNotNull()).count()
    target_is_unique = target_distinct_count == target_row_count

    # Overlap via anti-join
    source_keys = df.select(F.col(source_col).alias("_key")).where(F.col("_key").isNotNull()).distinct()
    target_keys = target_df.select(F.col(target_col).alias("_key")).where(F.col("_key").isNotNull()).distinct()

    orphan_df = source_keys.join(target_keys, on="_key", how="left_anti")
    orphan_count = orphan_df.count()
    orphan_pct = orphan_count / source_distinct_count if source_distinct_count > 0 else 0.0
    overlap_pct = 1.0 - orphan_pct

    # Cardinality
    source_row_count = df.where(F.col(source_col).isNotNull()).count()
    source_is_unique = source_distinct_count == source_row_count

    cardinality = _determine_cardinality(
        source_ratio=source_distinct_count / source_row_count if source_row_count > 0 else 0,
        source_is_unique=source_is_unique,
        target_is_unique=target_is_unique,
    )

    # Format compatibility (sample-based)
    source_sample = [
        row["_key"] for row in
        source_keys.orderBy(F.rand(42)).limit(_FORMAT_SAMPLE_SIZE).collect()
    ]
    target_sample = [
        row["_key"] for row in
        target_keys.orderBy(F.rand(42)).limit(_FORMAT_SAMPLE_SIZE).collect()
    ]
    format_compatible, mismatch_desc = _check_format_compatibility_samples(
        source_sample, target_sample
    )

    safe_join_type = "INNER" if overlap_pct >= 0.999 else "LEFT"

    evidence = [
        f"source={source_col}",
        f"target={target_table}.{target_col}",
        f"overlap={overlap_pct:.1%}",
        f"orphans={orphan_count}",
        f"cardinality={cardinality}",
    ]

    return JoinProfile(
        source_column=source_col,
        target_table=target_table,
        target_column=target_col,
        cardinality=cardinality,
        overlap_pct=round(overlap_pct, 4),
        orphan_count=orphan_count,
        orphan_pct=round(orphan_pct, 4),
        format_compatible=format_compatible,
        format_mismatch_description=mismatch_desc,
        safe_join_type=safe_join_type,
        inference=Inference(
            value="join_profiled",
            confidence=0.85,
            evidence=evidence,
            sample_size=source_distinct_count,
            method="join_profiling",
        ),
    )


# ---------------------------------------------------------------------------
# Private: helpers
# ---------------------------------------------------------------------------


def _determine_cardinality(
    source_ratio: float,
    source_is_unique: bool,
    target_is_unique: bool,
) -> str:
    """Determine join cardinality based on uniqueness on each side."""
    if source_is_unique and target_is_unique:
        return "1:1"
    if target_is_unique:
        # Source has duplicates, target is unique → many source rows per target
        return "many:1"
    if source_is_unique:
        # Source is unique, target has duplicates → one source to many target
        return "1:many"
    return "many:many"


def _check_format_compatibility_pandas(
    source_values: set[Any],
    target_values: set[Any],
) -> tuple[bool, str | None]:
    """Check format compatibility between source and target value sets."""
    # Sample values for format comparison
    source_sample = list(source_values)[:_FORMAT_SAMPLE_SIZE]
    target_sample = list(target_values)[:_FORMAT_SAMPLE_SIZE]
    return _check_format_compatibility_samples(source_sample, target_sample)


def _check_format_compatibility_samples(
    source_sample: list[Any],
    target_sample: list[Any],
) -> tuple[bool, str | None]:
    """Check format issues from sample values."""
    if not source_sample or not target_sample:
        return True, None

    issues: list[str] = []

    # Convert to strings for comparison
    source_strs = [str(v) for v in source_sample if v is not None]
    target_strs = [str(v) for v in target_sample if v is not None]

    if not source_strs or not target_strs:
        return True, None

    # Check: leading/trailing whitespace
    source_has_spaces = any(s != s.strip() for s in source_strs)
    target_has_spaces = any(s != s.strip() for s in target_strs)
    if source_has_spaces != target_has_spaces:
        side = "source" if source_has_spaces else "target"
        issues.append(f"{side} has leading/trailing whitespace")

    # Check: case mismatch (one side upper, other mixed/lower)
    source_cases = _detect_case_pattern(source_strs)
    target_cases = _detect_case_pattern(target_strs)
    if source_cases != target_cases and source_cases != "mixed" and target_cases != "mixed":
        issues.append(f"case mismatch: source={source_cases}, target={target_cases}")

    # Check: dash/underscore inconsistency
    source_has_dashes = any("-" in s for s in source_strs)
    source_has_underscores = any("_" in s for s in source_strs)
    target_has_dashes = any("-" in s for s in target_strs)
    target_has_underscores = any("_" in s for s in target_strs)

    if source_has_dashes and not target_has_dashes and target_has_underscores:
        issues.append("source uses dashes, target uses underscores")
    elif source_has_underscores and not target_has_underscores and target_has_dashes:
        issues.append("source uses underscores, target uses dashes")

    if issues:
        return False, "; ".join(issues)
    return True, None


def _detect_case_pattern(values: list[str]) -> str:
    """Detect dominant case pattern: upper, lower, or mixed."""
    if not values:
        return "mixed"

    upper_count = sum(1 for v in values if v == v.upper() and v != v.lower())
    lower_count = sum(1 for v in values if v == v.lower() and v != v.upper())
    total = len(values)

    if upper_count > total * 0.8:
        return "upper"
    if lower_count > total * 0.8:
        return "lower"
    return "mixed"
