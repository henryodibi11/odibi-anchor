"""Duplicate Forensics for Table Profiler.

Detects WHERE duplicates are concentrated in a DataFrame — by time period,
by source/category column, or uniformly distributed.  Distinguishes intentional
snapshot-design duplication from dedup bugs.

Public API
----------
analyze_duplicates(df, grain, profiles, freshness) -> DuplicateForensics | None
"""

from __future__ import annotations

import sys
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from .models import (
    ColumnProfile,
    ColumnRole,
    DuplicateForensics,
    FreshnessAnalysis,
    GrainAnalysis,
    Inference,
)

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum duplicate rate to trigger forensic analysis
_MIN_DUP_RATE = 0.005  # 0.5%

# Concentration threshold — if one dimension explains >= this % of dupes, it's concentrated
_CONCENTRATION_THRESHOLD = 0.50

# Maximum distinct values in a candidate concentration column
_MAX_CONCENTRATION_CARDINALITY = 100

# Top N concentration values to report
_TOP_CONCENTRATION_VALUES = 5

# Temporal recency — fraction of duplicates in most recent N periods to flag as "recent"
_RECENT_PERIOD_COUNT = 3
_RECENT_THRESHOLD = 0.60  # 60% of dupes in last 3 periods = time-concentrated


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_duplicates(
    df: Any,
    grain: GrainAnalysis,
    profiles: list[ColumnProfile] | None = None,
    freshness: FreshnessAnalysis | None = None,
) -> DuplicateForensics | None:
    """Analyze where duplicates are concentrated in the DataFrame.

    Skips analysis (returns None) when:
    - Grain is already unique (no duplicates to analyze)
    - Duplicate rate is below the threshold (trivial amount)
    - No grain columns were identified

    Strategy:
    1. Identify duplicate rows (rows sharing the same grain values with others).
    2. Check temporal concentration: are dupes clustered in recent time periods?
    3. Check source/category concentration: are dupes clustered in a specific
       value of a low-cardinality column?
    4. Detect snapshot pattern: uniform distribution across a partition column
       suggests designed duplication (e.g. monthly snapshots).
    5. Issue verdict: snapshot_design | dedup_needed | partial_overlap | unknown.

    Args:
        df: Input pandas or Spark DataFrame.
        grain: GrainAnalysis from grain detection step.
        profiles: Pre-computed column profiles (helps identify candidate columns).
        freshness: FreshnessAnalysis (provides the temporal column if available).

    Returns:
        DuplicateForensics with concentration analysis, or None if skipped.
    """
    # Guard: skip when no duplicates to analyze
    if grain.is_unique:
        return None
    if grain.duplicate_rate < _MIN_DUP_RATE:
        return None
    if not grain.best_grain:
        return None

    engine = detect_engine(df)
    row_count = _get_row_count(df, engine)
    if row_count == 0:
        return None

    grain_cols = grain.best_grain
    dup_count = int(row_count * grain.duplicate_rate)

    # Identify candidate dimension columns for concentration analysis
    temporal_col = _find_temporal_column(profiles, freshness, grain_cols)
    category_cols = _find_category_columns(profiles, grain_cols)

    # Analyze temporal concentration
    time_concentrated = False
    time_description: str | None = None
    time_values: list[dict[str, Any]] = []

    if temporal_col:
        time_result = _analyze_temporal_concentration(
            df, grain_cols, temporal_col, engine
        )
        if time_result:
            time_concentrated = time_result["is_concentrated"]
            time_description = time_result["description"]
            time_values = time_result["values"]

    # Analyze source/category concentration
    source_concentrated = False
    source_description: str | None = None
    source_values: list[dict[str, Any]] = []
    concentration_col: str | None = None

    for cat_col in category_cols:
        cat_result = _analyze_category_concentration(
            df, grain_cols, cat_col, engine
        )
        if cat_result and cat_result["is_concentrated"]:
            source_concentrated = True
            source_description = cat_result["description"]
            source_values = cat_result["values"]
            concentration_col = cat_col
            break  # Take the first strongly concentrated dimension

    # Determine best concentration column and values
    if time_concentrated and temporal_col:
        best_col = temporal_col
        best_values = time_values
    elif source_concentrated and concentration_col:
        best_col = concentration_col
        best_values = source_values
    elif temporal_col and time_values:
        best_col = temporal_col
        best_values = time_values
    else:
        best_col = concentration_col
        best_values = source_values

    # Detect snapshot pattern
    is_snapshot = _detect_snapshot_pattern(
        df, grain_cols, profiles, temporal_col, engine
    )

    # Issue verdict
    verdict, explanation = _determine_verdict(
        is_snapshot=is_snapshot,
        time_concentrated=time_concentrated,
        source_concentrated=source_concentrated,
        duplicate_rate=grain.duplicate_rate,
        time_description=time_description,
        source_description=source_description,
    )

    evidence = [
        f"grain={grain_cols}",
        f"dup_rate={grain.duplicate_rate:.4f}",
        f"dup_count={dup_count}",
    ]
    if temporal_col:
        evidence.append(f"temporal_col={temporal_col}")
    if concentration_col:
        evidence.append(f"category_col={concentration_col}")
    evidence.append(f"verdict={verdict}")

    return DuplicateForensics(
        grain_columns=grain_cols,
        duplicate_count=dup_count,
        duplicate_rate=grain.duplicate_rate,
        concentration_column=best_col,
        concentration_values=best_values,
        is_time_concentrated=time_concentrated,
        time_concentration_description=time_description,
        is_source_concentrated=source_concentrated,
        source_concentration_description=source_description,
        is_snapshot_pattern=is_snapshot,
        verdict=verdict,
        explanation=explanation,
        inference=Inference(
            value=verdict,
            confidence=0.75 if verdict != "unknown" else 0.40,
            evidence=evidence,
            sample_size=row_count,
            method="duplicate_forensics",
        ),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_row_count(df: Any, engine: str) -> int:
    """Return the row count of the DataFrame."""
    if engine == "pandas":
        return len(df)
    return df.count()


def _find_temporal_column(
    profiles: list[ColumnProfile] | None,
    freshness: FreshnessAnalysis | None,
    grain_cols: list[str],
) -> str | None:
    """Find the best temporal column for time-based concentration analysis."""
    # Prefer the freshness column if available and not part of grain
    if freshness and freshness.freshness_column not in grain_cols:
        return freshness.freshness_column

    if not profiles:
        return None

    # Look for timestamp/date role columns not in the grain
    for p in profiles:
        if p.name in grain_cols:
            continue
        if p.role in (ColumnRole.TIMESTAMP, ColumnRole.METADATA):
            if "date" in p.spark_type.lower() or "timestamp" in p.spark_type.lower():
                return p.name

    return None


def _find_category_columns(
    profiles: list[ColumnProfile] | None,
    grain_cols: list[str],
) -> list[str]:
    """Find low-cardinality categorical columns for source concentration analysis."""
    if not profiles:
        return []

    candidates: list[str] = []
    for p in profiles:
        if p.name in grain_cols:
            continue
        # Low cardinality, not all-null, not a key
        if (
            p.distinct_count > 1
            and p.distinct_count <= _MAX_CONCENTRATION_CARDINALITY
            and p.null_pct < 0.50
            and p.role not in (
                ColumnRole.PRIMARY_KEY,
                ColumnRole.SURROGATE_KEY,
                ColumnRole.NATURAL_KEY,
                ColumnRole.MEASURE,
                ColumnRole.FREETEXT,
            )
        ):
            candidates.append(p.name)

    # Prefer columns with hints of "source" or "partition" in the name
    def _sort_key(name: str) -> int:
        lower = name.lower()
        if any(kw in lower for kw in ("source", "origin", "system", "feed")):
            return 0
        if any(kw in lower for kw in ("type", "category", "status", "batch")):
            return 1
        return 2

    candidates.sort(key=_sort_key)
    return candidates[:5]  # Limit analysis to top 5 candidates


def _analyze_temporal_concentration(
    df: Any,
    grain_cols: list[str],
    temporal_col: str,
    engine: str,
) -> dict[str, Any] | None:
    """Check if duplicates are concentrated in recent time periods."""
    if engine == "pandas":
        return _temporal_concentration_pandas(df, grain_cols, temporal_col)
    return _temporal_concentration_spark(df, grain_cols, temporal_col)


def _temporal_concentration_pandas(
    df: pd.DataFrame,
    grain_cols: list[str],
    temporal_col: str,
) -> dict[str, Any] | None:
    """Pandas implementation of temporal concentration analysis."""
    if temporal_col not in df.columns:
        return None

    # Mark duplicate rows
    dup_mask = df.duplicated(subset=grain_cols, keep=False)
    dup_df = df[dup_mask]

    if dup_df.empty:
        return None

    total_dups = len(dup_df)

    # Coerce temporal column to a period bucket
    temp_series = dup_df[temporal_col].copy()
    try:
        temp_series = pd.to_datetime(temp_series, errors="coerce")
        # Bucket by date (day granularity)
        buckets = temp_series.dt.date
    except Exception:
        # Fall back to raw values
        buckets = temp_series

    # Count duplicates per period
    period_counts = buckets.value_counts().sort_index()
    if period_counts.empty:
        return None

    # Check concentration in most recent periods
    recent_periods = period_counts.tail(_RECENT_PERIOD_COUNT)
    recent_dup_count = recent_periods.sum()
    recent_pct = recent_dup_count / total_dups if total_dups > 0 else 0.0

    is_concentrated = bool(recent_pct >= _RECENT_THRESHOLD)

    # Build top values
    top_periods = period_counts.nlargest(_TOP_CONCENTRATION_VALUES)
    values = [
        {
            "value": str(period),
            "dup_count": int(count),
            "dup_pct": round(count / total_dups, 4),
        }
        for period, count in top_periods.items()
    ]

    description = (
        f"{recent_pct:.0%} of duplicates concentrated in "
        f"last {_RECENT_PERIOD_COUNT} periods of {temporal_col}"
    )

    return {
        "is_concentrated": is_concentrated,
        "description": description,
        "values": values,
    }


def _temporal_concentration_spark(
    df: Any,
    grain_cols: list[str],
    temporal_col: str,
) -> dict[str, Any] | None:
    """Spark implementation of temporal concentration analysis."""
    if F is None:
        return None  # pragma: no cover
    if temporal_col not in df.columns:
        return None

    # Window to mark duplicate rows
    from pyspark.sql import Window

    w = Window.partitionBy(*grain_cols)
    with_count = df.withColumn("_dup_cnt", F.count("*").over(w))
    dup_df = with_count.where(F.col("_dup_cnt") > 1).drop("_dup_cnt")

    total_dups = dup_df.count()
    if total_dups == 0:
        return None

    # Bucket by date
    period_col = F.to_date(F.col(temporal_col))
    period_counts = (
        dup_df.withColumn("_period", period_col)
        .groupBy("_period")
        .agg(F.count("*").alias("_cnt"))
        .orderBy("_period")
        .collect()
    )

    if not period_counts:
        return None

    # Check recent concentration
    recent_rows = period_counts[-_RECENT_PERIOD_COUNT:]
    recent_dup_count = sum(row["_cnt"] for row in recent_rows)
    recent_pct = recent_dup_count / total_dups if total_dups > 0 else 0.0

    is_concentrated = recent_pct >= _RECENT_THRESHOLD

    # Top values
    sorted_by_count = sorted(period_counts, key=lambda r: r["_cnt"], reverse=True)
    values = [
        {
            "value": str(row["_period"]),
            "dup_count": int(row["_cnt"]),
            "dup_pct": round(row["_cnt"] / total_dups, 4),
        }
        for row in sorted_by_count[:_TOP_CONCENTRATION_VALUES]
    ]

    description = (
        f"{recent_pct:.0%} of duplicates concentrated in "
        f"last {_RECENT_PERIOD_COUNT} periods of {temporal_col}"
    )

    return {
        "is_concentrated": is_concentrated,
        "description": description,
        "values": values,
    }


def _analyze_category_concentration(
    df: Any,
    grain_cols: list[str],
    category_col: str,
    engine: str,
) -> dict[str, Any] | None:
    """Check if duplicates are concentrated in specific categorical values."""
    if engine == "pandas":
        return _category_concentration_pandas(df, grain_cols, category_col)
    return _category_concentration_spark(df, grain_cols, category_col)


def _category_concentration_pandas(
    df: pd.DataFrame,
    grain_cols: list[str],
    category_col: str,
) -> dict[str, Any] | None:
    """Pandas implementation of category concentration analysis."""
    if category_col not in df.columns:
        return None

    dup_mask = df.duplicated(subset=grain_cols, keep=False)
    dup_df = df[dup_mask]

    if dup_df.empty:
        return None

    total_dups = len(dup_df)

    # Count duplicates per category value
    cat_counts = dup_df[category_col].value_counts()
    if cat_counts.empty:
        return None

    # Check if top value dominates
    top_value_count = cat_counts.iloc[0]
    top_pct = top_value_count / total_dups

    is_concentrated = bool(top_pct >= _CONCENTRATION_THRESHOLD)

    # Build top values
    values = [
        {
            "value": str(val),
            "dup_count": int(count),
            "dup_pct": round(count / total_dups, 4),
        }
        for val, count in cat_counts.head(_TOP_CONCENTRATION_VALUES).items()
    ]

    top_val_name = str(cat_counts.index[0])
    description = (
        f"{top_pct:.0%} of duplicates where {category_col}=\'{top_val_name}\'"
    )

    return {
        "is_concentrated": is_concentrated,
        "description": description,
        "values": values,
    }


def _category_concentration_spark(
    df: Any,
    grain_cols: list[str],
    category_col: str,
) -> dict[str, Any] | None:
    """Spark implementation of category concentration analysis."""
    if F is None:
        return None  # pragma: no cover
    if category_col not in df.columns:
        return None

    from pyspark.sql import Window

    w = Window.partitionBy(*grain_cols)
    with_count = df.withColumn("_dup_cnt", F.count("*").over(w))
    dup_df = with_count.where(F.col("_dup_cnt") > 1).drop("_dup_cnt")

    total_dups = dup_df.count()
    if total_dups == 0:
        return None

    cat_counts = (
        dup_df.groupBy(category_col)
        .agg(F.count("*").alias("_cnt"))
        .orderBy(F.desc("_cnt"))
        .limit(_TOP_CONCENTRATION_VALUES)
        .collect()
    )

    if not cat_counts:
        return None

    top_value_count = cat_counts[0]["_cnt"]
    top_pct = top_value_count / total_dups

    is_concentrated = top_pct >= _CONCENTRATION_THRESHOLD

    values = [
        {
            "value": str(row[category_col]),
            "dup_count": int(row["_cnt"]),
            "dup_pct": round(row["_cnt"] / total_dups, 4),
        }
        for row in cat_counts
    ]

    top_val_name = str(cat_counts[0][category_col])
    description = (
        f"{top_pct:.0%} of duplicates where {category_col}=\'{top_val_name}\'"
    )

    return {
        "is_concentrated": is_concentrated,
        "description": description,
        "values": values,
    }


def _detect_snapshot_pattern(
    df: Any,
    grain_cols: list[str],
    profiles: list[ColumnProfile] | None,
    temporal_col: str | None,
    engine: str,
) -> bool:
    """Detect if duplicates follow a snapshot/periodic pattern.

    Snapshot pattern indicators:
    - Grain includes a temporal partition column (year, month, snapshot_date)
    - Duplicates are evenly distributed across periods (not skewed)
    - A partition/metadata column has low cardinality matching period count
    """
    if not profiles:
        return False

    # Check if any grain column looks like a temporal partition
    grain_lower = [g.lower() for g in grain_cols]
    temporal_partition_keywords = (
        "snapshot", "period", "month", "quarter", "year",
        "batch", "run_date", "load_date", "extract_date",
    )
    grain_has_temporal = any(
        any(kw in g for kw in temporal_partition_keywords)
        for g in grain_lower
    )
    if grain_has_temporal:
        return True

    # Check if there's a partition-role column not in grain with periodic values
    for p in profiles:
        if p.name in grain_cols:
            continue
        if p.role == ColumnRole.PARTITION:
            # If a partition column exists outside grain, and duplicates exist,
            # it's likely a snapshot design
            if p.distinct_count >= 2 and p.distinct_count <= 50:
                return True

    return False


def _determine_verdict(
    is_snapshot: bool,
    time_concentrated: bool,
    source_concentrated: bool,
    duplicate_rate: float,
    time_description: str | None,
    source_description: str | None,
) -> tuple[str, str]:
    """Determine the duplicate forensics verdict.

    Returns:
        (verdict, explanation) tuple.
    """
    if is_snapshot:
        explanation = (
            "Duplicates appear to be by design — grain includes or relates to "
            "a temporal partition, consistent with periodic snapshot loading."
        )
        return ("snapshot_design", explanation)

    if time_concentrated and time_description:
        explanation = (
            f"Duplicates are concentrated in recent time periods "
            f"({time_description}). This suggests a recent load issue "
            f"or overlapping incremental ingestion."
        )
        return ("dedup_needed", explanation)

    if source_concentrated and source_description:
        explanation = (
            f"Duplicates are concentrated in a specific source/category "
            f"({source_description}). This suggests a source-specific "
            f"ingestion issue or replay."
        )
        return ("dedup_needed", explanation)

    if duplicate_rate > 0.30:
        explanation = (
            "High duplicate rate (>30%) without clear concentration pattern. "
            "May indicate full-table reload without deduplication, or a "
            "snapshot design pattern not captured by column naming."
        )
        return ("partial_overlap", explanation)

    explanation = (
        "Duplicates are present but not strongly concentrated in any "
        "single dimension. Manual investigation recommended."
    )
    return ("unknown", explanation)
