"""Table type classification for Table Profiler.

Deterministic decision-tree classifier that assigns a TableClassification
(fact, dimension, snapshot, event_log, SCD2, lookup, staging, aggregate,
bride, or unknown) based on pre-computed column profiles, grain analysis,
and freshness analysis.

Public API
----------
classify_table(profiles, grain=None, freshness=None) -> Inference
"""

from __future__ import annotations

import sys
from typing import Any

sys.dont_write_bytecode = True

from .models import (
    ColumnRole,
    ColumnProfile,
    CompetingHypothesis,
    FreshnessAnalysis,
    GrainAnalysis,
    Inference,
    SemanticType,
    TableClassification,
)


# ---------------------------------------------------------------------------
# Name-pattern sets for heuristic signals
# ---------------------------------------------------------------------------

_SCD2_START_HINTS = frozenset({"effective_date", "valid_from", "start_date", "effective_start", "row_start"})
_SCD2_END_HINTS = frozenset({"end_date", "valid_to", "expiry_date", "effective_end", "row_end"})

_STAGING_PREFIXES = ("raw_", "src_", "stg_", "landing_")
_STAGING_SUFFIXES = ("_raw", "_source", "_stg", "_landing")

_AGGREGATE_PREFIXES = ("sum_", "avg_", "cnt_", "count_", "total_", "min_", "max_", "median_", "pct_")

_LOOKUP_MAX_ROWS = 1_000
_LOOKUP_MAX_COLS = 5



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _classify_table_winner(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None = None,
    freshness: FreshnessAnalysis | None = None,
) -> Inference:
    """Classify a table using a deterministic decision tree.

    Operates entirely on pre-computed analysis objects — no DataFrame needed.

    Decision order (first match wins):
    1. SCD2         — has both effective_date and end_date column patterns
    2. BRIDGE       — exactly 2 columns, both are FOREIGN_KEY role
    3. STAGING      — column names suggest raw/landing zone
    4. AGGREGATE    — majority of non-key columns are pre-aggregated (sum_, avg_, etc.)
    5. LOOKUP       — small table (<= 1000 rows) with few wide columns
    6. EVENT_LOG    — grain contains a TIMESTAMP column + append-like cadence
    7. SNAPSHOT     — grain contains a date/partition column
    8. FACT         — has MEASURE columns + foreign key columns
    9. DIMENSION    — mostly descriptive, low measure count
    10. UNKNOWN     — no signals matched

    Args:
        profiles: Column profiles (from compute_column_stats + infer_* passes).
        grain:    GrainAnalysis from detect_grain() — optional.
        freshness: FreshnessAnalysis from detect_freshness() — optional.

    Returns:
        Inference with value=TableClassification, confidence, evidence, method.
    """
    if not profiles:
        return _make_inference(TableClassification.UNKNOWN, 0.40, ["no_profiles"])

    names_lower = [p.name.lower() for p in profiles]
    roles = [p.role for p in profiles]

    # ─ 1. SCD2 ─────────────────────────────────────────────────────────────────────
    has_start = any(n in _SCD2_START_HINTS for n in names_lower)
    has_end = any(n in _SCD2_END_HINTS for n in names_lower)
    if has_start and has_end:
        return _make_inference(
            TableClassification.SCD2, 0.90,
            [f"scd2_start={_first_match(names_lower, _SCD2_START_HINTS)}",
             f"scd2_end={_first_match(names_lower, _SCD2_END_HINTS)}"],
        )

    # ─ 2. BRIDGE ──────────────────────────────────────────────────────────────────
    fk_cols = [p.name for p in profiles if p.role == ColumnRole.FOREIGN_KEY]
    if len(profiles) == 2 and len(fk_cols) == 2:
        return _make_inference(
            TableClassification.BRIDGE, 0.90,
            [f"two_fk_cols={fk_cols}", "col_count=2"],
        )

    # ─ 3. STAGING ───────────────────────────────────────────────────────────────
    staging_col = _first_staging_col(names_lower)
    if staging_col:
        return _make_inference(
            TableClassification.STAGING, 0.85,
            [f"staging_col_pattern={staging_col}"],
        )

    # ─ 4. AGGREGATE ────────────────────────────────────────────────────────────
    non_key = [n for n, r in zip(names_lower, roles)
               if r not in (ColumnRole.PRIMARY_KEY, ColumnRole.SURROGATE_KEY,
                            ColumnRole.NATURAL_KEY, ColumnRole.FOREIGN_KEY)]
    agg_cols = [n for n in non_key if n.startswith(_AGGREGATE_PREFIXES)]
    if non_key and len(agg_cols) / len(non_key) >= 0.60:
        return _make_inference(
            TableClassification.AGGREGATE, 0.85,
            [f"aggregate_cols={agg_cols[:3]}", f"ratio={len(agg_cols)}/{len(non_key)}"],
        )

    # ─ 5. LOOKUP ─────────────────────────────────────────────────────────────────
    row_count = _get_row_count(profiles, grain)
    if row_count is not None and row_count <= _LOOKUP_MAX_ROWS and len(profiles) <= _LOOKUP_MAX_COLS:
        return _make_inference(
            TableClassification.LOOKUP, 0.80,
            [f"row_count={row_count}", f"col_count={len(profiles)}"],
        )

    # ─ 6. EVENT_LOG ───────────────────────────────────────────────────────────
    has_ts_in_grain = _grain_contains_timestamp(profiles, grain)
    append_cadence = freshness is not None and freshness.cadence in ("hourly", "daily")
    if has_ts_in_grain and append_cadence:
        return _make_inference(
            TableClassification.EVENT_LOG, 0.85,
            [f"timestamp_in_grain={has_ts_in_grain}",
             f"cadence={freshness.cadence if freshness else None}"],  # type: ignore[union-attr]
        )

    # ─ 7. SNAPSHOT ─────────────────────────────────────────────────────────────
    has_date_in_grain = _grain_contains_date(profiles, grain)
    if has_date_in_grain:
        return _make_inference(
            TableClassification.SNAPSHOT, 0.80,
            [f"date_in_grain={has_date_in_grain}"],
        )

    # ─ 8. FACT ─────────────────────────────────────────────────────────────────────
    measure_count = roles.count(ColumnRole.MEASURE)
    fk_count = roles.count(ColumnRole.FOREIGN_KEY)
    if measure_count >= 1 and fk_count >= 1:
        return _make_inference(
            TableClassification.FACT, 0.80,
            [f"measure_count={measure_count}", f"fk_count={fk_count}"],
        )
    if measure_count >= 2:
        return _make_inference(
            TableClassification.FACT, 0.70,
            [f"measure_count={measure_count}", "no_explicit_fk"],
        )

    # ─ 9. DIMENSION ────────────────────────────────────────────────────────────
    dim_count = roles.count(ColumnRole.DIMENSION)
    key_count = sum(1 for r in roles if r in (
        ColumnRole.PRIMARY_KEY, ColumnRole.SURROGATE_KEY, ColumnRole.NATURAL_KEY
    ))
    if key_count >= 1 and dim_count >= 1 and measure_count == 0:
        return _make_inference(
            TableClassification.DIMENSION, 0.75,
            [f"key_count={key_count}", f"dim_count={dim_count}", "no_measures"],
        )
    if dim_count >= 2 and measure_count == 0:
        return _make_inference(
            TableClassification.DIMENSION, 0.65,
            [f"dim_count={dim_count}", "no_measures", "no_explicit_key"],
        )

    # ─ 10. UNKNOWN fallback ───────────────────────────────────────────────────────
    return _make_inference(
        TableClassification.UNKNOWN, 0.40,
        [f"col_count={len(profiles)}",
         f"measure_count={measure_count}",
         f"fk_count={fk_count}"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inference(
    classification: TableClassification,
    confidence: float,
    evidence: list[str],
) -> Inference:
    """Wrap a classification result in an Inference object."""
    return Inference(
        value=classification,
        confidence=confidence,
        evidence=evidence,
        method="table_classification",
    )


def _first_match(names: list[str], hint_set: frozenset[str]) -> str | None:
    """Return the first name that appears in hint_set, or None."""
    for n in names:
        if n in hint_set:
            return n
    return None


def _first_staging_col(names_lower: list[str]) -> str | None:
    """Return the first column name matching a staging pattern, or None."""
    for n in names_lower:
        if n.startswith(_STAGING_PREFIXES) or n.endswith(_STAGING_SUFFIXES):
            return n
    return None


def _get_row_count(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
) -> int | None:
    """Estimate row count from grain inference sample_size or profile row_count."""
    if grain is not None and grain.inference is not None:
        s = grain.inference.sample_size
        if s > 0:
            return s
    # Fall back to profile row_count if available
    for p in profiles:
        if p.row_count > 0:
            return p.row_count
    return None


def _grain_contains_timestamp(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
) -> bool:
    """Return True if any column in the grain has a TIMESTAMP role."""
    if grain is None or not grain.best_grain:
        return False
    grain_cols = set(grain.best_grain)
    for p in profiles:
        if p.name in grain_cols and p.role == ColumnRole.TIMESTAMP:
            return True
    return False


def _grain_contains_date(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
) -> bool:
    """Return True if any grain column is a date/timestamp type or has date-like role."""
    if grain is None or not grain.best_grain:
        return False
    grain_cols = set(grain.best_grain)
    for p in profiles:
        if p.name not in grain_cols:
            continue
        if p.role in (ColumnRole.TIMESTAMP, ColumnRole.PARTITION):
            return True
        base_type = p.spark_type.lower().split("[")[0].strip()
        if any(t in base_type for t in ("timestamp", "date", "datetime")):
            return True
        if p.semantic_type == SemanticType.DATE_STRING:
            return True
    return False


# ---------------------------------------------------------------------------
# Phase 6: small-sample confidence calibration
# ---------------------------------------------------------------------------

#: Minimum row count considered statistically reliable for table classification.
_MIN_RELIABLE_SAMPLE: int = 30

#: Confidence floor applied when sample size is below _MIN_RELIABLE_SAMPLE.
_SAMPLE_PENALTY_FLOOR: float = 0.50


def _apply_sample_penalty(confidence: float, sample_size: int) -> float:
    """Linearly degrade confidence for small samples.

    When sample_size < _MIN_RELIABLE_SAMPLE, confidence is scaled by
    (sample_size / _MIN_RELIABLE_SAMPLE), floored at _SAMPLE_PENALTY_FLOOR.
    Samples at or above the threshold are returned unchanged.

    Args:
        confidence:  Raw classifier confidence (0.0–1.0).
        sample_size: Number of rows the inference was drawn from.

    Returns:
        Penalised confidence, unchanged if sample is large enough.
    """
    if sample_size <= 0 or sample_size >= _MIN_RELIABLE_SAMPLE:
        return confidence
    penalty = sample_size / _MIN_RELIABLE_SAMPLE
    return max(_SAMPLE_PENALTY_FLOOR, confidence * penalty)

# ---------------------------------------------------------------------------
# Phase 5: competing-evidence runner_ups
# ---------------------------------------------------------------------------

#: Minimum confidence for an alternative table type to appear as a runner_up.
_RUNNER_UP_THRESHOLD = 0.60


def classify_table(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None = None,
    freshness: FreshnessAnalysis | None = None,
) -> Inference:
    """Classify a table and surface competing-evidence alternatives.

    Calls the priority-ordered decision tree (_classify_table_winner) to
    determine the primary classification, then runs an independent secondary
    scoring pass to find alternative table types with meaningful supporting
    evidence (confidence >= _RUNNER_UP_THRESHOLD). Alternatives are attached
    as CompetingHypothesis entries in Inference.runner_ups.

    Args:
        profiles: Column profiles (from compute_column_stats + infer_* passes).
        grain:    GrainAnalysis from detect_grain() — optional.
        freshness: FreshnessAnalysis from detect_freshness() — optional.

    Returns:
        Inference with value=TableClassification, runner_ups populated.
    """
    winner = _classify_table_winner(profiles, grain, freshness)
    row_count = profiles[0].row_count if profiles else 0
    winner.sample_size = row_count
    winner.confidence = _apply_sample_penalty(winner.confidence, row_count)
    winner.runner_ups = _collect_table_runner_ups(profiles, grain, freshness, winner.value)
    return winner


def _collect_table_runner_ups(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
    freshness: FreshnessAnalysis | None,
    winner_class: TableClassification,
) -> list[CompetingHypothesis]:
    """Run all table type scorers except the winner and return significant alternatives.

    Args:
        profiles:     Column profiles.
        grain:        GrainAnalysis (optional).
        freshness:    FreshnessAnalysis (optional).
        winner_class: Primary classification to skip.

    Returns:
        CompetingHypothesis entries sorted by confidence descending, filtered
        to confidence >= _RUNNER_UP_THRESHOLD.
    """
    scorers = [
        (TableClassification.FACT,      lambda: _score_fact(profiles, grain)),
        (TableClassification.DIMENSION, lambda: _score_dimension(profiles)),
        (TableClassification.SNAPSHOT,  lambda: _score_snapshot(profiles, grain)),
        (TableClassification.EVENT_LOG, lambda: _score_event_log(profiles, grain, freshness)),
    ]

    candidates: list[CompetingHypothesis] = []
    for table_type, scorer in scorers:
        if table_type == winner_class:
            continue
        result = scorer()
        if result is None:
            continue
        confidence, evidence = result
        if confidence >= _RUNNER_UP_THRESHOLD:
            candidates.append(
                CompetingHypothesis(
                    value=table_type,
                    confidence=round(confidence, 3),
                    evidence=evidence,
                )
            )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def _score_fact(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
) -> tuple[float, list[str]] | None:
    """Score the FACT table type independently.

    Strong signal: at least 1 MEASURE + at least 1 FOREIGN_KEY column.
    Weak signal:   at least 2 MEASURE columns (no explicit FK).

    Returns:
        (confidence, evidence) or None if no fact signal found.
    """
    roles = [p.role for p in profiles]
    measure_count = roles.count(ColumnRole.MEASURE)
    fk_count      = roles.count(ColumnRole.FOREIGN_KEY)

    if measure_count >= 1 and fk_count >= 1:
        return (0.80, [f"measure_count={measure_count}", f"fk_count={fk_count}"])

    if measure_count >= 2:
        return (0.70, [f"measure_count={measure_count}", "no_explicit_fk"])

    return None


def _score_dimension(
    profiles: list[ColumnProfile],
) -> tuple[float, list[str]] | None:
    """Score the DIMENSION table type independently.

    Note: does NOT exclude on measure_count — this is an independent scorer.
    A FACT table with many dimension columns is a meaningful DIMENSION signal.

    Strong signal: at least 1 explicit key + at least 1 DIMENSION column.
    Weak signal:   at least 2 DIMENSION columns without an explicit key.

    Returns:
        (confidence, evidence) or None.
    """
    roles = [p.role for p in profiles]
    key_count = sum(
        1 for r in roles
        if r in (ColumnRole.PRIMARY_KEY, ColumnRole.SURROGATE_KEY, ColumnRole.NATURAL_KEY)
    )
    dim_count = roles.count(ColumnRole.DIMENSION)

    if key_count >= 1 and dim_count >= 1:
        return (0.75, [f"key_count={key_count}", f"dim_count={dim_count}"])

    if dim_count >= 2:
        return (0.65, [f"dim_count={dim_count}", "no_explicit_key"])

    return None


def _score_snapshot(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
) -> tuple[float, list[str]] | None:
    """Score the SNAPSHOT table type independently.

    Fires when any column has a TIMESTAMP role or temporal spark type,
    indicating the table may represent periodic snapshots of state.

    Returns:
        (confidence, evidence) or None.
    """
    ts_col = next(
        (p for p in profiles if p.role == ColumnRole.TIMESTAMP),
        None,
    )
    if ts_col is None:
        # Fall back to spark type
        ts_col = next(
            (p for p in profiles
             if any(t in p.spark_type.lower() for t in ("timestamp", "date", "datetime"))),
            None,
        )

    if ts_col is None:
        return None

    evidence = [f"temporal_column='{ts_col.name}'", f"role={ts_col.role.value}"]
    return (0.72, evidence)


def _score_event_log(
    profiles: list[ColumnProfile],
    grain: GrainAnalysis | None,
    freshness: FreshnessAnalysis | None,
) -> tuple[float, list[str]] | None:
    """Score the EVENT_LOG table type independently.

    Fires when a TIMESTAMP column is part of the grain AND cadence suggests
    high-frequency append (hourly or daily).

    Returns:
        (confidence, evidence) or None.
    """
    has_ts_in_grain = _grain_contains_timestamp(profiles, grain)
    append_cadence  = freshness is not None and freshness.cadence in ("hourly", "daily")

    if has_ts_in_grain and append_cadence:
        cadence = freshness.cadence if freshness else None  # type: ignore[union-attr]
        return (0.80, [f"timestamp_in_grain=True", f"cadence={cadence}"])

    return None
