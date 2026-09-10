"""Cross-Column Dependency Detection for Table Profiler.

Detects hidden business rules encoded in column relationships:
1. Co-null patterns: "Col A is null in 95%+ of cases when Col B = 'X'"
2. Functional dependencies: "Col A uniquely determines Col B"

Public API
----------
detect_cross_column_dependencies(df, profiles) -> CrossColumnResult
"""

from __future__ import annotations

import sys
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from .models import ColumnProfile, ColumnRole, Inference

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum null rate to consider a column as "nullable target" for co-null analysis
_MIN_NULL_RATE = 0.05  # 5%

# Maximum distinct values for a "driver" column (low-cardinality categorical)
_MAX_DRIVER_CARDINALITY = 50

# Threshold: if null_rate of target >= this when driver=value, flag as co-null
_CO_NULL_THRESHOLD = 0.90  # 90% null when driver=value

# Maximum column pairs to test (prevents O(n²) explosion)
_MAX_PAIRS = 50

# Functional dependency: max distinct values in A to check (expensive for high-card)
_MAX_FD_CARDINALITY = 500

# Minimum rows to make functional dependency meaningful
_MIN_ROWS_FOR_FD = 20


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class CoNullPattern:
    """A detected co-null pattern: target is mostly null when driver=value."""

    target_column: str
    driver_column: str
    driver_value: str
    null_rate_when_driven: float  # null rate of target when driver=value
    affected_rows: int
    description: str


@dataclass
class FunctionalDependency:
    """A detected functional dependency: determinant → dependent."""

    determinant: str  # Column A
    dependent: str  # Column B (determined by A)
    confidence: float  # 1.0 = perfect FD
    distinct_determinant: int
    description: str


@dataclass
class CrossColumnResult:
    """Results of cross-column dependency analysis."""

    co_null_patterns: list[CoNullPattern] = field(default_factory=list)
    functional_dependencies: list[FunctionalDependency] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    inference: Inference | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_cross_column_dependencies(
    df: Any,
    profiles: list[ColumnProfile] | None = None,
) -> CrossColumnResult:
    """Detect cross-column dependencies in the DataFrame.

    Strategy:
    1. Identify nullable targets (null_pct >= 5%) and driver columns (low cardinality).
    2. For each (driver, target) pair, check if target is predominantly null
       for specific driver values.
    3. Identify functional dependencies among low-cardinality columns.

    Args:
        df: Input pandas or Spark DataFrame.
        profiles: Pre-computed column profiles (used to filter candidates).

    Returns:
        CrossColumnResult with detected patterns and findings.
    """
    if profiles is None or len(profiles) < 2:
        return CrossColumnResult()

    engine = detect_engine(df)
    row_count = _get_row_count(df, engine)
    if row_count < _MIN_ROWS_FOR_FD:
        return CrossColumnResult()

    # Identify candidates
    nullable_targets = [
        p for p in profiles
        if p.null_pct >= _MIN_NULL_RATE
        and p.role not in (ColumnRole.METADATA, ColumnRole.FREETEXT)
    ]
    driver_cols = [
        p for p in profiles
        if 1 < p.distinct_count <= _MAX_DRIVER_CARDINALITY
        and p.null_pct < 0.50
        and p.role not in (ColumnRole.MEASURE, ColumnRole.FREETEXT, ColumnRole.METADATA)
    ]

    # Co-null analysis
    co_nulls = _detect_co_null_patterns(
        df, nullable_targets, driver_cols, engine, row_count
    )

    # Functional dependency analysis
    fd_candidates = [
        p for p in profiles
        if 2 <= p.distinct_count <= _MAX_FD_CARDINALITY
        and p.null_pct < 0.05
        and p.role not in (ColumnRole.FREETEXT, ColumnRole.METADATA)
    ]
    func_deps = _detect_functional_dependencies(df, fd_candidates, engine, row_count)

    # Build findings
    findings: list[str] = []
    if co_nulls:
        top = co_nulls[0]
        findings.append(
            f"Co-null pattern: {top.target_column} is {top.null_rate_when_driven:.0%} "
            f"null when {top.driver_column}='{top.driver_value}' "
            f"({top.affected_rows:,} rows)"
        )
        if len(co_nulls) > 1:
            findings.append(f"  + {len(co_nulls) - 1} more co-null pattern(s)")

    if func_deps:
        top = func_deps[0]
        findings.append(
            f"Functional dependency: {top.determinant} → {top.dependent} "
            f"(confidence {top.confidence:.0%})"
        )
        if len(func_deps) > 1:
            findings.append(f"  + {len(func_deps) - 1} more functional dependency(ies)")

    evidence = [
        f"nullable_targets={len(nullable_targets)}",
        f"driver_cols={len(driver_cols)}",
        f"co_null_found={len(co_nulls)}",
        f"fd_found={len(func_deps)}",
    ]

    return CrossColumnResult(
        co_null_patterns=co_nulls,
        functional_dependencies=func_deps,
        findings=findings,
        inference=Inference(
            value="cross_column_analyzed",
            confidence=0.80 if (co_nulls or func_deps) else 0.50,
            evidence=evidence,
            sample_size=row_count,
            method="cross_column_dependencies",
        ),
    )


# ---------------------------------------------------------------------------
# Private: co-null detection
# ---------------------------------------------------------------------------


def _detect_co_null_patterns(
    df: Any,
    nullable_targets: list[ColumnProfile],
    driver_cols: list[ColumnProfile],
    engine: str,
    row_count: int,
) -> list[CoNullPattern]:
    """Detect co-null patterns between nullable targets and driver columns."""
    patterns: list[CoNullPattern] = []
    pairs_tested = 0

    for driver_p in driver_cols:
        for target_p in nullable_targets:
            if driver_p.name == target_p.name:
                continue
            if pairs_tested >= _MAX_PAIRS:
                break

            pairs_tested += 1
            result = _test_co_null_pair(
                df, driver_p.name, target_p.name, engine, row_count
            )
            if result:
                patterns.extend(result)

        if pairs_tested >= _MAX_PAIRS:
            break

    # Sort by null_rate_when_driven descending (strongest patterns first)
    patterns.sort(key=lambda p: p.null_rate_when_driven, reverse=True)
    return patterns[:10]  # Top 10 patterns


def _test_co_null_pair(
    df: Any,
    driver_col: str,
    target_col: str,
    engine: str,
    row_count: int,
) -> list[CoNullPattern]:
    """Test if target is predominantly null for specific driver values."""
    if engine == "pandas":
        return _co_null_pandas(df, driver_col, target_col, row_count)
    return _co_null_spark(df, driver_col, target_col, row_count)


def _co_null_pandas(
    df: pd.DataFrame,
    driver_col: str,
    target_col: str,
    row_count: int,
) -> list[CoNullPattern]:
    """Pandas implementation of co-null detection for a single pair."""
    patterns: list[CoNullPattern] = []

    if driver_col not in df.columns or target_col not in df.columns:
        return patterns

    # Group by driver value, compute null rate of target
    grouped = df.groupby(driver_col, dropna=True)[target_col]
    for driver_value, group in grouped:
        group_size = len(group)
        if group_size < 5:  # Skip tiny groups
            continue

        null_count = group.isna().sum()
        null_rate = null_count / group_size if group_size > 0 else 0.0

        if null_rate >= _CO_NULL_THRESHOLD:
            patterns.append(CoNullPattern(
                target_column=target_col,
                driver_column=driver_col,
                driver_value=str(driver_value),
                null_rate_when_driven=round(float(null_rate), 4),
                affected_rows=int(group_size),
                description=(
                    f"{target_col} is {null_rate:.0%} null when "
                    f"{driver_col}='{driver_value}' ({group_size:,} rows)"
                ),
            ))

    return patterns


def _co_null_spark(
    df: Any,
    driver_col: str,
    target_col: str,
    row_count: int,
) -> list[CoNullPattern]:
    """Spark implementation of co-null detection for a single pair."""
    if F is None:
        return []  # pragma: no cover
    if driver_col not in df.columns or target_col not in df.columns:
        return []

    patterns: list[CoNullPattern] = []

    # Compute null rate of target grouped by driver value
    stats = (
        df.where(F.col(driver_col).isNotNull())
        .groupBy(driver_col)
        .agg(
            F.count("*").alias("_group_size"),
            F.sum(F.when(F.col(target_col).isNull(), 1).otherwise(0)).alias("_null_count"),
        )
        .where(F.col("_group_size") >= 5)
        .withColumn("_null_rate", F.col("_null_count") / F.col("_group_size"))
        .where(F.col("_null_rate") >= _CO_NULL_THRESHOLD)
        .orderBy(F.desc("_null_rate"))
        .limit(5)
        .collect()
    )

    for row in stats:
        driver_value = str(row[driver_col])
        null_rate = float(row["_null_rate"])
        group_size = int(row["_group_size"])

        patterns.append(CoNullPattern(
            target_column=target_col,
            driver_column=driver_col,
            driver_value=driver_value,
            null_rate_when_driven=round(null_rate, 4),
            affected_rows=group_size,
            description=(
                f"{target_col} is {null_rate:.0%} null when "
                f"{driver_col}=\'{driver_value}\' ({group_size:,} rows)"
            ),
        ))

    return patterns


# ---------------------------------------------------------------------------
# Private: functional dependency detection
# ---------------------------------------------------------------------------


def _detect_functional_dependencies(
    df: Any,
    candidates: list[ColumnProfile],
    engine: str,
    row_count: int,
) -> list[FunctionalDependency]:
    """Detect pairwise functional dependencies among candidate columns."""
    if len(candidates) < 2:
        return []

    deps: list[FunctionalDependency] = []
    pairs_tested = 0

    for i, col_a in enumerate(candidates):
        for col_b in candidates[i + 1:]:
            if pairs_tested >= _MAX_PAIRS:
                break
            pairs_tested += 1

            # Check A → B
            fd = _test_functional_dependency(
                df, col_a.name, col_b.name,
                col_a.distinct_count, engine, row_count
            )
            if fd:
                deps.append(fd)

            # Check B → A
            fd = _test_functional_dependency(
                df, col_b.name, col_a.name,
                col_b.distinct_count, engine, row_count
            )
            if fd:
                deps.append(fd)

        if pairs_tested >= _MAX_PAIRS:
            break

    # Sort by confidence descending
    deps.sort(key=lambda d: d.confidence, reverse=True)
    return deps[:10]


def _test_functional_dependency(
    df: Any,
    determinant: str,
    dependent: str,
    det_distinct: int,
    engine: str,
    row_count: int,
) -> FunctionalDependency | None:
    """Test if determinant functionally determines dependent (A → B)."""
    if engine == "pandas":
        return _fd_pandas(df, determinant, dependent, det_distinct)
    return _fd_spark(df, determinant, dependent, det_distinct)


def _fd_pandas(
    df: pd.DataFrame,
    determinant: str,
    dependent: str,
    det_distinct: int,
) -> FunctionalDependency | None:
    """Pandas implementation: check if A → B."""
    if determinant not in df.columns or dependent not in df.columns:
        return None

    # Drop rows where either is null
    subset = df[[determinant, dependent]].dropna()
    if subset.empty:
        return None

    # Count distinct values of (A, B) pair vs distinct values of A alone
    distinct_a = subset[determinant].nunique()
    distinct_ab = subset.drop_duplicates().shape[0]

    if distinct_a == 0:
        return None

    # If distinct(A) == distinct(A,B), then A → B (perfect FD)
    # Allow near-perfect: confidence = distinct_a / distinct_ab
    confidence = distinct_a / distinct_ab if distinct_ab > 0 else 0.0

    if confidence < 0.95:
        return None

    # Don't flag trivial cases (A = B, or B is constant)
    distinct_b = subset[dependent].nunique()
    if distinct_b <= 1:
        return None
    if distinct_a == distinct_ab == distinct_b:
        # A and B have same cardinality and same distinct pairs
        # Could be 1:1 mapping — still valid FD but skip if they're the "same column"
        pass

    return FunctionalDependency(
        determinant=determinant,
        dependent=dependent,
        confidence=round(confidence, 4),
        distinct_determinant=distinct_a,
        description=(
            f"{determinant} → {dependent} "
            f"(confidence {confidence:.0%}, {distinct_a} distinct values)"
        ),
    )


def _fd_spark(
    df: Any,
    determinant: str,
    dependent: str,
    det_distinct: int,
) -> FunctionalDependency | None:
    """Spark implementation: check if A → B."""
    if F is None:
        return None  # pragma: no cover
    if determinant not in df.columns or dependent not in df.columns:
        return None

    subset = df.select(determinant, dependent).where(
        F.col(determinant).isNotNull() & F.col(dependent).isNotNull()
    )

    distinct_a = subset.select(determinant).distinct().count()
    distinct_ab = subset.distinct().count()

    if distinct_a == 0 or distinct_ab == 0:
        return None

    confidence = distinct_a / distinct_ab

    if confidence < 0.95:
        return None

    distinct_b = subset.select(dependent).distinct().count()
    if distinct_b <= 1:
        return None

    return FunctionalDependency(
        determinant=determinant,
        dependent=dependent,
        confidence=round(confidence, 4),
        distinct_determinant=distinct_a,
        description=(
            f"{determinant} → {dependent} "
            f"(confidence {confidence:.0%}, {distinct_a} distinct values)"
        ),
    )


# ---------------------------------------------------------------------------
# Private: helpers
# ---------------------------------------------------------------------------


def _get_row_count(df: Any, engine: str) -> int:
    """Return the row count of the DataFrame."""
    if engine == "pandas":
        return len(df)
    return df.count()
