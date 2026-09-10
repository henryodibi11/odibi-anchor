"""Value Stability Analysis for Table Profiler.

For entity-over-time tables (snapshots, SCD2, event logs with repeated entities),
detects which columns are static vs changing across occurrences of the same entity.

Guides SCD strategy and merge logic:
- Static columns: can be denormalized safely, don't need history tracking
- Changing columns: need SCD Type 2 or merge-update logic
- Volatile columns: change almost every occurrence — likely measures or timestamps

Public API
----------
analyze_value_stability(df, grain, profiles, classification) -> ValueStabilityResult | None
"""

from __future__ import annotations

import sys
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine, get_sample
from .models import (
    ColumnProfile,
    ColumnRole,
    GrainAnalysis,
    Inference,
    TableClassification,
)

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
    from pyspark.sql import Window
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None
    Window = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum duplicate rate to run stability analysis (need repeated entities)
_MIN_DUPLICATE_RATE = 0.05  # 5%

# Minimum average rows per entity to make analysis meaningful
_MIN_AVG_ROWS_PER_ENTITY = 1.5

# Maximum entities to sample for analysis (performance bound)
_MAX_ENTITIES_SAMPLE = 500

# Classification thresholds (% of entities where column changes)
_STATIC_THRESHOLD = 0.05       # 0-5% change → static
_MOSTLY_STATIC_THRESHOLD = 0.20  # 5-20% change → mostly_static
_VOLATILE_THRESHOLD = 0.80     # 80%+ change → volatile
# 20-80% → changing

# Columns to exclude from analysis (always vary, not interesting)
_EXCLUDED_ROLES = {ColumnRole.METADATA, ColumnRole.TIMESTAMP, ColumnRole.PARTITION}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class ColumnStability:
    """Stability classification for a single column."""

    column_name: str
    stability: str  # "static" | "mostly_static" | "changing" | "volatile"
    change_rate: float  # fraction of entities where this column has >1 distinct value
    entities_sampled: int
    description: str


@dataclass
class ValueStabilityResult:
    """Results of value stability analysis."""

    entity_key: list[str]
    entity_count: int
    avg_rows_per_entity: float
    column_stabilities: list[ColumnStability] = field(default_factory=list)
    static_columns: list[str] = field(default_factory=list)
    changing_columns: list[str] = field(default_factory=list)
    volatile_columns: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    inference: Inference | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_value_stability(
    df: Any,
    grain: GrainAnalysis | None = None,
    profiles: list[ColumnProfile] | None = None,
    classification: TableClassification = TableClassification.UNKNOWN,
) -> ValueStabilityResult | None:
    """Analyze value stability across repeated entity occurrences.

    Returns None when analysis is not applicable:
    - No grain detected
    - Grain is unique (no repeated entities)
    - Too few rows per entity

    Args:
        df: Input pandas or Spark DataFrame.
        grain: Pre-computed grain analysis (provides entity key).
        profiles: Pre-computed column profiles.
        classification: Table classification (used for relevance scoring).

    Returns:
        ValueStabilityResult or None if analysis is not applicable.
    """
    if grain is None or not grain.best_grain:
        return None

    if grain.is_unique:
        return None

    if grain.duplicate_rate < _MIN_DUPLICATE_RATE:
        return None

    engine = detect_engine(df)
    entity_key = grain.best_grain

    # Compute entity stats
    row_count, entity_count = _get_entity_stats(df, entity_key, engine)
    if entity_count == 0:
        return None

    avg_rows = row_count / entity_count
    if avg_rows < _MIN_AVG_ROWS_PER_ENTITY:
        return None

    # Determine columns to analyze (exclude key columns and irrelevant roles)
    key_set = set(entity_key)
    analyze_cols = _select_columns_to_analyze(profiles, key_set, df, engine)

    if not analyze_cols:
        return None

    # Run stability analysis
    stabilities = _compute_stability(
        df, entity_key, analyze_cols, engine, entity_count
    )

    # Classify results
    static_cols = [s.column_name for s in stabilities if s.stability == "static"]
    changing_cols = [s.column_name for s in stabilities if s.stability == "changing"]
    volatile_cols = [s.column_name for s in stabilities if s.stability == "volatile"]

    # Build findings
    findings = _build_stability_findings(
        stabilities, static_cols, changing_cols, volatile_cols, classification
    )

    # Relevance: higher confidence for entity-over-time table types
    relevance_types = {
        TableClassification.SNAPSHOT, TableClassification.SCD2,
        TableClassification.EVENT_LOG,
    }
    confidence = 0.85 if classification in relevance_types else 0.65

    return ValueStabilityResult(
        entity_key=entity_key,
        entity_count=entity_count,
        avg_rows_per_entity=round(avg_rows, 2),
        column_stabilities=stabilities,
        static_columns=static_cols,
        changing_columns=changing_cols,
        volatile_columns=volatile_cols,
        findings=findings,
        inference=Inference(
            value="value_stability_analyzed",
            confidence=confidence,
            evidence=[
                f"entity_key={entity_key}",
                f"entity_count={entity_count}",
                f"avg_rows={avg_rows:.1f}",
                f"static={len(static_cols)}",
                f"changing={len(changing_cols)}",
                f"volatile={len(volatile_cols)}",
            ],
            sample_size=entity_count,
            method="value_stability",
        ),
    )


# ---------------------------------------------------------------------------
# Private: entity stats
# ---------------------------------------------------------------------------


def _get_entity_stats(df: Any, entity_key: list[str], engine: str) -> tuple[int, int]:
    """Return (row_count, entity_count)."""
    if engine == "pandas":
        row_count = len(df)
        entity_count = df.groupby(entity_key).ngroups
        return row_count, entity_count

    # Spark
    row_count = df.count()
    entity_count = df.select(*entity_key).distinct().count()
    return row_count, entity_count


def _select_columns_to_analyze(
    profiles: list[ColumnProfile] | None,
    key_set: set[str],
    df: Any,
    engine: str,
) -> list[str]:
    """Select columns suitable for stability analysis."""
    if engine == "pandas":
        all_cols = list(df.columns)
    else:
        all_cols = df.columns

    if profiles:
        # Use profiles to filter intelligently
        return [
            p.name for p in profiles
            if p.name not in key_set
            and p.role not in _EXCLUDED_ROLES
            and p.distinct_count > 1  # constant columns are trivially stable
            and p.null_pct < 0.95  # mostly-null columns aren't interesting
            and p.name in all_cols
        ]

    # Fallback: all non-key columns
    return [c for c in all_cols if c not in key_set]


# ---------------------------------------------------------------------------
# Private: stability computation
# ---------------------------------------------------------------------------


def _compute_stability(
    df: Any,
    entity_key: list[str],
    columns: list[str],
    engine: str,
    entity_count: int,
) -> list[ColumnStability]:
    """Compute stability classification for each column."""
    if engine == "pandas":
        return _stability_pandas(df, entity_key, columns, entity_count)
    return _stability_spark(df, entity_key, columns, entity_count)


def _stability_pandas(
    df: pd.DataFrame,
    entity_key: list[str],
    columns: list[str],
    entity_count: int,
) -> list[ColumnStability]:
    """Pandas implementation: compute per-entity distinct counts for each column."""
    stabilities: list[ColumnStability] = []

    # Sample entities if too many
    if entity_count > _MAX_ENTITIES_SAMPLE:
        sampled_entities = (
            df[entity_key].drop_duplicates()
            .sample(n=_MAX_ENTITIES_SAMPLE, random_state=42)
        )
        df_sampled = df.merge(sampled_entities, on=entity_key, how="inner")
        entities_sampled = _MAX_ENTITIES_SAMPLE
    else:
        df_sampled = df
        entities_sampled = entity_count

    grouped = df_sampled.groupby(entity_key)

    for col in columns:
        if col not in df_sampled.columns:
            continue

        # Count entities where this column has >1 distinct non-null value
        nunique_per_entity = grouped[col].nunique()
        entities_with_change = int((nunique_per_entity > 1).sum())
        change_rate = entities_with_change / entities_sampled if entities_sampled > 0 else 0.0

        stability = _classify_stability(change_rate)
        stabilities.append(ColumnStability(
            column_name=col,
            stability=stability,
            change_rate=round(float(change_rate), 4),
            entities_sampled=entities_sampled,
            description=_stability_description(col, stability, change_rate),
        ))

    return stabilities


def _stability_spark(
    df: Any,
    entity_key: list[str],
    columns: list[str],
    entity_count: int,
) -> list[ColumnStability]:
    """Spark implementation: compute per-entity distinct counts."""
    if F is None:
        return []  # pragma: no cover

    stabilities: list[ColumnStability] = []

    # Sample entities if too many
    if entity_count > _MAX_ENTITIES_SAMPLE:
        entity_df = df.select(*entity_key).distinct().limit(_MAX_ENTITIES_SAMPLE)
        df_sampled = df.join(entity_df, on=entity_key, how="inner")
        entities_sampled = _MAX_ENTITIES_SAMPLE
    else:
        df_sampled = df
        entities_sampled = entity_count

    for col in columns:
        if col not in df_sampled.columns:
            continue

        # Group by entity, count distinct values per entity, then count entities with >1
        entity_nunique = (
            df_sampled.groupBy(*entity_key)
            .agg(F.countDistinct(col).alias("_nunique"))
        )
        entities_with_change = entity_nunique.where(F.col("_nunique") > 1).count()
        change_rate = entities_with_change / entities_sampled if entities_sampled > 0 else 0.0

        stability = _classify_stability(change_rate)
        stabilities.append(ColumnStability(
            column_name=col,
            stability=stability,
            change_rate=round(float(change_rate), 4),
            entities_sampled=entities_sampled,
            description=_stability_description(col, stability, change_rate),
        ))

    return stabilities


# ---------------------------------------------------------------------------
# Private: classification helpers
# ---------------------------------------------------------------------------


def _classify_stability(change_rate: float) -> str:
    """Classify a column's stability based on entity change rate."""
    if change_rate <= _STATIC_THRESHOLD:
        return "static"
    elif change_rate <= _MOSTLY_STATIC_THRESHOLD:
        return "mostly_static"
    elif change_rate >= _VOLATILE_THRESHOLD:
        return "volatile"
    else:
        return "changing"


def _stability_description(col: str, stability: str, change_rate: float) -> str:
    """Generate human-readable description."""
    pct = f"{change_rate:.0%}"
    if stability == "static":
        return f"{col}: static (changes in {pct} of entities)"
    elif stability == "mostly_static":
        return f"{col}: mostly static (changes in {pct} of entities)"
    elif stability == "volatile":
        return f"{col}: volatile (changes in {pct} of entities)"
    else:
        return f"{col}: changing (changes in {pct} of entities)"


def _build_stability_findings(
    stabilities: list[ColumnStability],
    static_cols: list[str],
    changing_cols: list[str],
    volatile_cols: list[str],
    classification: TableClassification,
) -> list[str]:
    """Generate findings from stability analysis."""
    findings: list[str] = []

    total = len(stabilities)
    if total == 0:
        return findings

    if static_cols:
        pct = len(static_cols) / total
        if pct >= 0.5:
            findings.append(
                f"Value stability: {len(static_cols)}/{total} columns are static "
                f"across entities — candidates for denormalization"
            )
        elif static_cols:
            findings.append(
                f"Value stability: {len(static_cols)} static column(s): "
                f"{', '.join(static_cols[:5])}"
            )

    if volatile_cols:
        findings.append(
            f"Value stability: {len(volatile_cols)} volatile column(s) "
            f"(change in 80%+ entities): {', '.join(volatile_cols[:5])}"
        )

    # SCD guidance
    if classification in (TableClassification.SNAPSHOT, TableClassification.SCD2):
        if changing_cols:
            findings.append(
                f"SCD candidates: {', '.join(changing_cols[:5])} "
                f"change across entity snapshots — track with SCD Type 2"
            )

    return findings
