"""Grain detection for Table Profiler.

Identifies the best candidate primary key (grain) for a DataFrame by testing
single columns and 2-column combinations for uniqueness.  Reports duplicate
rates and which candidates were evaluated.

Public API
----------
detect_grain(df, profiles=None) -> GrainAnalysis
"""

from __future__ import annotations

import itertools
import sys
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from .models import ColumnProfile, CompetingHypothesis, GrainAnalysis, Inference
from .stats_engine import compute_column_stats

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum candidate columns to consider (avoids explosion on wide tables)
_MAX_CANDIDATE_COLUMNS = 10

# Maximum 2-column combinations to test
_MAX_TWO_COL_COMBOS = 20


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_grain(
    df: Any,
    profiles: list[ColumnProfile] | None = None,
) -> GrainAnalysis:
    """Detect the grain (primary key) of a DataFrame.

    Strategy:
    1. Compute column stats if not provided.
    2. Identify candidate key columns (non-null or low-null, non-constant).
    3. Test single-column candidates for uniqueness.
    4. If no single-column key, test 2-column combinations.
    5. Return the best grain with duplicate rate.

    Args:
        df: Input pandas or Spark DataFrame.
        profiles: Pre-computed column profiles (avoids redundant stats pass).

    Returns:
        GrainAnalysis with best_grain, is_unique, duplicate_rate, and candidates_tested.
    """

    engine = detect_engine(df)
    row_count = _get_row_count(df, engine)

    if row_count == 0:
        return GrainAnalysis(
            best_grain=[],
            is_unique=False,
            duplicate_rate=0.0,
            candidates_tested=[],
            inference=Inference(
                value="no_grain",
                confidence=0.0,
                evidence=["empty_dataframe"],
                sample_size=0,
                method="grain_detection",
            ),
        )

    # Get or compute profiles
    if profiles is None:
        profiles = compute_column_stats(df)

    # Identify candidate columns: low null rate, not constant, reasonable cardinality
    candidates = _select_candidates(profiles)

    if not candidates:
        return GrainAnalysis(
            best_grain=[],
            is_unique=False,
            duplicate_rate=1.0,
            candidates_tested=[],
            inference=Inference(
                value="no_candidates",
                confidence=0.0,
                evidence=["no_suitable_candidate_columns"],
                sample_size=row_count,
                method="grain_detection",
            ),
        )

    tested: list[dict[str, Any]] = []

    # Phase 1: Test single-column candidates
    for col_name in candidates:
        tested.append(_score_candidate(df, [col_name], row_count, engine, profiles))

    # Phase 2: If no unique single column, try 2-column combos
    if not any(entry["is_unique"] for entry in tested):
        combos = list(itertools.combinations(candidates, 2))[:_MAX_TWO_COL_COMBOS]
        for combo in combos:
            tested.append(_score_candidate(df, list(combo), row_count, engine, profiles))

    ranked_candidates = _rank_grain_candidates(tested)
    best_candidate = ranked_candidates[0] if ranked_candidates else None

    if best_candidate is None:
        return GrainAnalysis(
            best_grain=[],
            is_unique=False,
            duplicate_rate=1.0,
            candidates_tested=tested,
            runner_up_grains=[],
            null_exclusion_rate=0.0,
            verification_hint="inspect candidate columns with high distinctness and low nulls to define business grain",
            duplicate_concentration=None,
            inference=Inference(
                value="no_grain",
                confidence=0.0,
                evidence=["no_viable_grain_candidate"],
                sample_size=row_count,
                method="grain_detection",
                blocker_reason="candidate search did not produce any viable grain combinations",
                verification_hint="inspect high-cardinality columns or widen the candidate set",
            ),
        )

    best_grain = list(best_candidate["columns"])
    is_unique = bool(best_candidate["is_unique"])
    best_dup_rate = float(best_candidate["duplicate_rate"])

    if is_unique:
        confidence = 0.95
        evidence = [f"unique_grain={best_grain}", f"row_count={row_count}"]
        blocker_reason = ""
    elif best_dup_rate < 0.01:
        confidence = 0.75
        evidence = [f"near_unique_grain={best_grain}", f"dup_rate={best_dup_rate:.4f}"]
        blocker_reason = "small duplicate pocket remains within the best candidate grain"
    else:
        confidence = 0.50
        evidence = [f"best_candidate={best_grain}", f"dup_rate={best_dup_rate:.4f}"]
        blocker_reason = "best candidate still leaves material duplicates"

    runner_up_grains = _build_runner_up_grains(ranked_candidates[1:])
    verification_hint = _build_grain_verification_hint(best_candidate, runner_up_grains)

    return GrainAnalysis(
        best_grain=best_grain,
        is_unique=is_unique,
        duplicate_rate=round(best_dup_rate, 6),
        candidates_tested=tested,
        runner_up_grains=runner_up_grains,
        null_exclusion_rate=round(best_candidate["null_exclusion_rate"], 6),
        verification_hint=verification_hint,
        duplicate_concentration=None,  # Deferred to future enhancement
        inference=Inference(
            value="grain_detected" if best_grain else "no_grain",
            confidence=confidence,
            evidence=evidence,
            sample_size=row_count,
            method="grain_detection",
            runner_ups=_build_grain_runner_up_hypotheses(runner_up_grains),
            blocker_reason=blocker_reason,
            verification_hint=verification_hint,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_row_count(df: Any, engine: str) -> int:
    """Get row count for either engine."""

    if engine == "pandas":
        return len(df)
    return int(df.count())


def _select_candidates(profiles: list[ColumnProfile]) -> list[str]:
    """Select candidate key columns from profiles.

    Criteria:
    - Not constant (distinct_count > 1)
    - Low null rate (< 5%)
    - High distinct percentage (> 50% for single-col consideration)
    - Not freetext (avg_length > 100 disqualifies)

    Returns at most _MAX_CANDIDATE_COLUMNS names, ordered by distinct_pct descending.
    """

    candidates: list[tuple[str, float]] = []

    for p in profiles:
        if p.is_constant:
            continue
        if p.row_count == 0:
            continue
        null_rate = p.null_count / p.row_count if p.row_count > 0 else 1.0
        if null_rate >= 0.05:
            continue
        if p.distinct_pct < 0.50:
            continue
        if p.avg_length is not None and p.avg_length > 100:
            continue
        candidates.append((p.name, p.distinct_pct))

    # Sort by distinct_pct descending (most unique first)
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in candidates[:_MAX_CANDIDATE_COLUMNS]]


def _score_candidate(
    df: Any,
    columns: list[str],
    row_count: int,
    engine: str,
    profiles: list[ColumnProfile],
) -> dict[str, Any]:
    """Score a grain candidate using duplicate rate, null exclusion, and minimality."""

    dup_rate, null_exclusion_rate = _compute_duplicate_rate(df, columns, row_count, engine)
    is_unique = dup_rate == 0.0 and null_exclusion_rate == 0.0
    minimality_bonus = 0.08 if len(columns) == 1 else 0.0
    profile_bonus = _candidate_profile_bonus(columns, profiles)
    score = max(
        0.0,
        min(
            1.0,
            1.0 - dup_rate - (null_exclusion_rate * 0.35) + minimality_bonus + profile_bonus,
        ),
    )

    return {
        "columns": list(columns),
        "is_unique": is_unique,
        "duplicate_rate": round(dup_rate, 6),
        "null_exclusion_rate": round(null_exclusion_rate, 6),
        "score": round(score, 6),
        "verification_hint": _build_candidate_verification_hint(columns, is_unique),
    }


def _compute_duplicate_rate(
    df: Any,
    columns: list[str],
    row_count: int,
    engine: str,
) -> tuple[float, float]:
    """Compute duplicate rate and null exclusion rate for a column combination."""

    if row_count == 0:
        return 0.0, 0.0

    if engine == "pandas":
        return _duplicate_rate_pandas(df, columns, row_count)
    return _duplicate_rate_spark(df, columns, row_count)


def _duplicate_rate_pandas(
    df: pd.DataFrame,
    columns: list[str],
    row_count: int,
) -> tuple[float, float]:
    """Compute duplicate rate and null exclusion rate for pandas DataFrame."""

    subset = df[columns].dropna()
    if subset.empty:
        return 1.0, 1.0

    distinct_count = len(subset.drop_duplicates())
    non_null_count = len(subset)

    if non_null_count == 0:
        return 1.0, 1.0

    duplicates = non_null_count - distinct_count
    duplicate_rate = duplicates / row_count
    null_exclusion_rate = 1.0 - (non_null_count / row_count)
    return duplicate_rate, null_exclusion_rate


def _duplicate_rate_spark(
    df: Any,
    columns: list[str],
    row_count: int,
) -> tuple[float, float]:
    """Compute duplicate rate and null exclusion rate for Spark DataFrame."""

    if F is None or SparkDataFrame is None:
        raise TypeError("PySpark is not available for Spark grain detection.")

    filtered = df
    for col in columns:
        filtered = filtered.where(F.col(col).isNotNull())

    distinct_count = filtered.select(columns).distinct().count()
    non_null_count = filtered.count()

    if non_null_count == 0:
        return 1.0, 1.0

    duplicates = non_null_count - distinct_count
    duplicate_rate = duplicates / row_count
    null_exclusion_rate = 1.0 - (non_null_count / row_count)
    return duplicate_rate, null_exclusion_rate


def _candidate_profile_bonus(
    columns: list[str],
    profiles: list[ColumnProfile],
) -> float:
    """Return a lightweight plausibility bonus from candidate column profiles."""

    profile_by_name = {profile.name: profile for profile in profiles}
    bonus = 0.0
    for column in columns:
        profile = profile_by_name.get(column)
        if profile is None:
            continue
        if profile.is_unique:
            bonus += 0.08
        if profile.distinct_pct >= 0.90:
            bonus += 0.04
    return min(bonus, 0.20)


def _rank_grain_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank grain candidates without widening the existing bounded search."""

    return sorted(
        candidates,
        key=lambda entry: (
            entry["is_unique"],
            entry["score"],
            -entry["null_exclusion_rate"],
            -len(entry["columns"]),
            -entry["duplicate_rate"],
        ),
        reverse=True,
    )


def _build_runner_up_grains(
    candidates: list[dict[str, Any]],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return compact runner-up grain descriptions for downstream consumers."""

    runner_ups: list[dict[str, Any]] = []
    for candidate in candidates[:limit]:
        blocker_reason = _grain_blocker_reason(candidate)
        runner_ups.append(
            {
                "columns": list(candidate["columns"]),
                "is_unique": bool(candidate["is_unique"]),
                "duplicate_rate": float(candidate["duplicate_rate"]),
                "null_exclusion_rate": float(candidate["null_exclusion_rate"]),
                "score": float(candidate["score"]),
                "blocker_reason": blocker_reason,
                "verification_hint": candidate["verification_hint"],
            }
        )
    return runner_ups


def _build_grain_runner_up_hypotheses(
    runner_up_grains: list[dict[str, Any]],
) -> list[CompetingHypothesis]:
    """Convert runner-up grain candidates into reusable competing hypotheses."""

    hypotheses: list[CompetingHypothesis] = []
    for candidate in runner_up_grains:
        hypotheses.append(
            CompetingHypothesis(
                value=candidate["columns"],
                confidence=round(candidate["score"], 3),
                evidence=[
                    f"duplicate_rate={candidate['duplicate_rate']:.4f}",
                    f"null_exclusion_rate={candidate['null_exclusion_rate']:.4f}",
                ],
                counter_signals=_grain_counter_signals(candidate),
                blocker_reason=candidate["blocker_reason"],
                verification_hint=candidate["verification_hint"],
            )
        )
    return hypotheses


def _grain_blocker_reason(candidate: dict[str, Any]) -> str:
    """Explain why a grain candidate did not become the winner."""

    if candidate["null_exclusion_rate"] > 0:
        return "requires filtering null rows before it behaves like a key"
    if candidate["duplicate_rate"] == 0:
        return "is unique but wider than the selected grain"
    return "still leaves duplicate rows"


def _grain_counter_signals(candidate: dict[str, Any]) -> list[str]:
    """Surface concise counter-signals for alternate grain candidates."""

    signals: list[str] = []
    if candidate["duplicate_rate"] > 0:
        signals.append(f"duplicate_rate={candidate['duplicate_rate']:.4f}")
    if candidate["null_exclusion_rate"] > 0:
        signals.append(f"null_exclusion_rate={candidate['null_exclusion_rate']:.4f}")
    return signals


def _build_grain_verification_hint(
    best_candidate: dict[str, Any],
    runner_up_grains: list[dict[str, Any]],
) -> str:
    """Summarize the next verification step for the winning grain candidate."""

    columns = ", ".join(best_candidate["columns"])
    if best_candidate["is_unique"] and runner_up_grains:
        alternate = "+".join(str(value) for value in runner_up_grains[0].get("columns", []))
        return (
            f"validate that ({columns}) matches the intended business grain and not just a load artifact; "
            f"compare it with alternate grain {alternate} before hard-coding joins"
        )
    if best_candidate["is_unique"]:
        return (
            f"validate that ({columns}) matches the intended business grain and not just a load artifact"
        )
    return (
        f"inspect duplicate rows on ({columns}) to determine whether an extra timestamp or partition column is missing"
    )


def _build_candidate_verification_hint(
    columns: list[str],
    is_unique: bool,
) -> str:
    """Return a concise verification step for a candidate grain."""

    joined = ", ".join(columns)
    if is_unique:
        return f"confirm that ({joined}) is the business grain and not just a technical row identifier"
    return f"review duplicate groups for ({joined}) and check whether a missing tie-breaker or partition column is needed"
