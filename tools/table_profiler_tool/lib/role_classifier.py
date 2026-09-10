"""Column role classification for Table Profiler.

Assigns a ColumnRole to each column based on a deterministic decision tree
that combines column name patterns, semantic type, and statistical properties
from the already-populated ColumnProfile.

Public API
----------
infer_column_role(profile: ColumnProfile) -> Inference
"""

from __future__ import annotations

import re
import sys

sys.dont_write_bytecode = True

from .models import ColumnProfile, ColumnRole, CompetingHypothesis, Inference, SemanticType

# ---------------------------------------------------------------------------
# Name pattern registries — compiled regexes for column name matching
# ---------------------------------------------------------------------------

# Timestamp / temporal columns
# Word-boundary note: the outer (?:^|_) group already captures the separator
# before each token.  Do NOT add a leading _ to tokens like "date", "time",
# "at", "ts", "dt" — doing so would require a double-underscore prefix
# (e.g. "__date") which never appears in real column names like "queue_date".
_TIMESTAMP_NAME_RE = re.compile(
    r"(?:^|_)(?:created|updated|modified|deleted|loaded|inserted|processed|"
    r"published|expired|started|ended|occurred|recorded|timestamp|at|ts|dt|"
    r"date|time|datetime)(?:$|_)",
    re.IGNORECASE,
)

# Metadata / audit columns
_METADATA_NAME_RE = re.compile(
    r"(?:^|_)(?:loaded_at|created_at|updated_at|modified_at|inserted_at|"
    r"etl_|dwh_|src_|source_file|batch_id|run_id|ingestion_|_version|"
    r"row_hash|checksum|created_by|updated_by|modified_by)(?:$|_)?",
    re.IGNORECASE,
)

# Primary / surrogate key patterns
_KEY_NAME_RE = re.compile(
    r"(?:^|_)(?:id|pk|key|_id|_pk|_key)$|^id$|_id$|_pk$|_key$",
    re.IGNORECASE,
)

# Foreign key patterns (end with _id but not the table's own pk)
_FK_NAME_RE = re.compile(
    r"(?:^|_)(?:fk_|ref_)|(?:_id|_fk|_ref)$",
    re.IGNORECASE,
)

# Flag / boolean indicator columns
_FLAG_NAME_RE = re.compile(
    r"^(?:is_|has_|was_|can_|should_|will_|flag_|flg_)|"
    r"(?:_flag|_flg|_ind|_indicator|_yn|_bool|_active|_enabled|_deleted)$",
    re.IGNORECASE,
)

# Measure / metric columns
_MEASURE_NAME_RE = re.compile(
    r"(?:^|_)(?:amount|total|sum|count|qty|quantity|price|cost|revenue|"
    r"fee|rate|ratio|score|weight|capacity|mw|kwh|mwh|volume|"
    r"balance|budget|profit|margin|avg|average|pct|percent)(?:$|_)",
    re.IGNORECASE,
)

# Partition columns
_PARTITION_NAME_RE = re.compile(
    r"(?:^|_)(?:partition|part_|year|month|day|region|country|state)(?:$|_)",
    re.IGNORECASE,
)
_FREETEXT_NAME_RE = re.compile(
    r"(?:^|_)(?:notes?|comments?|description|remarks?|memo|narrative|annotation|body|message|summary|details?|text)(?:$|_)",
    re.IGNORECASE,
)

# Numeric Spark types
_NUMERIC_TYPES = frozenset({
    "int", "integer", "bigint", "long", "smallint", "short", "tinyint", "byte",
    "float", "double", "decimal", "float64", "float32", "int64", "int32",
    "int16", "int8", "uint8", "uint16", "uint32", "uint64",
})

# Temporal Spark types
_TEMPORAL_TYPES = frozenset({
    "date", "timestamp", "timestamp_ntz", "datetime64[ns]",
    "datetime64[ns, UTC]", "datetime64",
})

# Minimum distinct_pct for a near-unique CODE column to qualify as NATURAL_KEY.
# Covers business-assigned alphanumeric keys (e.g. "PJM-AG2-073") where snapshot
# duplication slightly depresses distinct_pct below 1.0 without changing semantics.
_NEAR_UNIQUE_NK_THRESHOLD = 0.90
# Maximum null_pct tolerated for a near-unique CODE → NATURAL_KEY classification.
# Sparse codes (high null_pct) may be lookup references rather than natural keys.
_NK_MAX_NULL_PCT = 0.05


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _classify_winner(profile: ColumnProfile) -> Inference:
    """Infer the role of a column from its populated profile.

    Uses a priority-ordered decision tree:
    1. Metadata (audit/ETL columns)
    2. Timestamp (temporal type OR temporal name)
    3. Flag (boolean/2-value)
    4. Primary/Surrogate key (unique + non-null + name/type signal)
    5. Foreign key (name pattern + non-unique)
    6. Measure (numeric + non-unique + name/stats signal)
    7. Partition (name pattern)
    8. Identifier (UUID/code + unique but not integer)
    9. Freetext (long strings, high cardinality)
    10. Dimension (low-to-moderate cardinality string/enum)
    11. Unknown (fallback)

    Args:
        profile: A ColumnProfile with stats and semantic_type already populated.

    Returns:
        Inference with value=ColumnRole, confidence, evidence, and method.
    """

    col_lower = profile.name.lower()
    evidence: list[str] = []
    counter_signals: list[str] = []

    spark_type_lower = profile.spark_type.lower()
    is_temporal_type = any(t in spark_type_lower for t in _TEMPORAL_TYPES)

    # --- 1. Metadata (audit columns that are NOT themselves temporal) ---
    # A temporally-typed audit column (created_at/updated_at/loaded_at as
    # date/timestamp) is fundamentally a TIMESTAMP — defer to the timestamp
    # branch. String audit columns (created_by, batch_id, row_hash, _version)
    # still classify as METADATA.
    if _METADATA_NAME_RE.search(col_lower) and not is_temporal_type:
        evidence.append(f"name_matches_metadata_pattern='{profile.name}'")
        confidence = 0.90
        if profile.is_unique:
            counter_signals.append("column_is_unique_unusual_for_metadata")
            confidence = 0.80
        return _build(ColumnRole.METADATA, confidence, evidence, counter_signals, profile)

    # --- 2. Timestamp ---
    is_temporal_name = bool(_TIMESTAMP_NAME_RE.search(col_lower))

    if is_temporal_type:
        evidence.append(f"spark_type='{profile.spark_type}'_is_temporal")
        if is_temporal_name:
            evidence.append(f"name_matches_timestamp_pattern")
        return _build(ColumnRole.TIMESTAMP, 0.95, evidence, counter_signals, profile)

    if is_temporal_name and profile.semantic_type == SemanticType.DATE_STRING:
        evidence.append("name_matches_timestamp_pattern")
        evidence.append("semantic_type=date_string")
        return _build(ColumnRole.TIMESTAMP, 0.90, evidence, counter_signals, profile)

    # --- 2b. DATE_STRING semantic without temporal name ---
    # Catches date-string columns whose names do not follow any naming
    # convention (e.g. "expiry", "cycle_end") but whose VALUES are clearly
    # parseable dates.  Lower confidence than the name+semantic path (0.85
    # vs 0.90) because the name gives no corroborating signal.
    if profile.semantic_type == SemanticType.DATE_STRING:
        evidence.append("semantic_type=date_string")
        if is_temporal_name:
            evidence.append("name_matches_timestamp_pattern")
        return _build(ColumnRole.TIMESTAMP, 0.85, evidence, counter_signals, profile)

    # --- 3. Flag ---
    is_flag_name = bool(_FLAG_NAME_RE.search(col_lower))
    is_boolean_semantic = profile.semantic_type == SemanticType.BOOLEAN_STRING
    is_two_value = (profile.distinct_count == 2 and profile.non_null_count > 0)

    if is_flag_name and (is_two_value or is_boolean_semantic):
        evidence.append("name_matches_flag_pattern")
        if is_two_value:
            evidence.append("distinct_count=2")
        if is_boolean_semantic:
            evidence.append("semantic_type=boolean_string")
        return _build(ColumnRole.FLAG, 0.92, evidence, counter_signals, profile)

    if is_boolean_semantic and is_two_value:
        evidence.append("semantic_type=boolean_string")
        evidence.append("distinct_count=2")
        if not is_flag_name:
            counter_signals.append("name_does_not_match_flag_pattern")
        return _build(ColumnRole.FLAG, 0.80, evidence, counter_signals, profile)

    # --- 4. Primary / Surrogate Key ---
    is_key_name = bool(_KEY_NAME_RE.search(col_lower))
    is_unique_nonnull = profile.is_unique and profile.null_count == 0
    is_integer_type = _is_integer_type(spark_type_lower)
    is_uuid_semantic = profile.semantic_type == SemanticType.UUID

    if is_unique_nonnull and is_key_name:
        evidence.append("is_unique_and_non_null")
        evidence.append("name_matches_key_pattern")
        if is_integer_type:
            evidence.append("integer_type_suggests_surrogate_key")
            return _build(ColumnRole.SURROGATE_KEY, 0.92, evidence, counter_signals, profile)
        if is_uuid_semantic:
            evidence.append("semantic_type=uuid")
            return _build(ColumnRole.NATURAL_KEY, 0.92, evidence, counter_signals, profile)
        evidence.append("non_integer_type_suggests_natural_key")
        return _build(ColumnRole.NATURAL_KEY, 0.88, evidence, counter_signals, profile)

    if is_unique_nonnull and is_uuid_semantic:
        evidence.append("is_unique_and_non_null")
        evidence.append("semantic_type=uuid")
        if not is_key_name:
            counter_signals.append("name_does_not_match_key_pattern")
        return _build(ColumnRole.NATURAL_KEY, 0.85, evidence, counter_signals, profile)

    # --- 5. Foreign Key ---
    is_fk_name = bool(_FK_NAME_RE.search(col_lower))
    # FK: has _id suffix, is NOT unique (many-to-one), moderate cardinality
    if (is_fk_name or is_key_name) and not profile.is_unique and profile.non_null_count > 0:
        if profile.distinct_pct < 0.80:
            evidence.append("name_matches_fk_pattern")
            evidence.append(f"not_unique_distinct_pct={profile.distinct_pct:.2f}")
            return _build(ColumnRole.FOREIGN_KEY, 0.82, evidence, counter_signals, profile)

    # --- 6. Measure ---
    is_numeric = _is_numeric_type(spark_type_lower)
    is_measure_name = bool(_MEASURE_NAME_RE.search(col_lower))

    if is_numeric and not profile.is_unique:
        if is_measure_name:
            evidence.append("numeric_type")
            evidence.append("name_matches_measure_pattern")
            evidence.append("not_unique")
            return _build(ColumnRole.MEASURE, 0.90, evidence, counter_signals, profile)

        # --- 6a. Partition-like numeric (before generic MEASURE fallback) ---
        # Generalized detection: integer type + very low absolute cardinality +
        # bounded value range typical of period/bucket columns (year, month,
        # quarter, day-of-week, etc.). Uses behavior signals, NOT column names.
        if _is_integer_type(spark_type_lower) and not is_key_name:
            if _is_partition_like_numeric(profile):
                evidence.append("numeric_type")
                evidence.append("integer_type")
                evidence.append(f"low_absolute_cardinality={profile.distinct_count}")
                evidence.append("bounded_value_range_suggests_partition")
                return _build(ColumnRole.PARTITION, 0.82, evidence, counter_signals, profile)

        # Numeric, non-unique, no key name — likely measure
        if not is_key_name:
            evidence.append("numeric_type")
            evidence.append("not_unique")
            evidence.append("no_key_name_pattern")
            if profile.distinct_pct > 0.50:
                counter_signals.append("high_distinct_pct_may_indicate_identifier")
            return _build(ColumnRole.MEASURE, 0.72, evidence, counter_signals, profile)

    # --- 7. Partition ---
    if _PARTITION_NAME_RE.search(col_lower) and not profile.is_unique:
        evidence.append("name_matches_partition_pattern")
        evidence.append("not_unique")
        # Low cardinality strengthens partition signal
        if profile.distinct_count <= 50:
            evidence.append(f"low_cardinality={profile.distinct_count}")
            return _build(ColumnRole.PARTITION, 0.80, evidence, counter_signals, profile)

    # --- 8. Identifier (UUID/code + unique) ---
    is_code_semantic = profile.semantic_type == SemanticType.CODE
    if profile.is_unique and (is_uuid_semantic or is_code_semantic):
        evidence.append("is_unique")
        evidence.append(f"semantic_type={profile.semantic_type.value}")
        return _build(ColumnRole.IDENTIFIER, 0.85, evidence, counter_signals, profile)

    # --- 8b. Near-unique natural key (CODE semantic, not strictly unique) ---
    # Covers business-assigned alphanumeric codes (e.g. "PJM-AG2-073") that are
    # natural keys for their domain but have a small number of duplicates (e.g.
    # caused by snapshot duplication).  Uses behavioral signals only — no column
    # name matching, per the generalized-heuristics convention.
    _is_str_type_8b = ("str" in spark_type_lower or spark_type_lower in ("object", "string"))
    if (
        is_code_semantic
        and _is_str_type_8b
        and not profile.is_unique
        and (profile.distinct_pct or 0.0) >= _NEAR_UNIQUE_NK_THRESHOLD
        and (profile.null_pct or 0.0) < _NK_MAX_NULL_PCT
    ):
        evidence.append("semantic_type=code")
        evidence.append(f"near_unique_distinct_pct={profile.distinct_pct:.3f}")
        counter_signals.append("not_strictly_unique")
        return _build(ColumnRole.NATURAL_KEY, 0.78, evidence, counter_signals, profile)

    # --- 9. Freetext ---
    # But first: check for fixed-length hash/token strings that look like
    # freetext (long + high cardinality) but are actually identifiers.
    if (profile.avg_length is not None and profile.avg_length > 20
            and profile.distinct_pct > 0.80
            and profile.min_length is not None and profile.max_length is not None
            and profile.min_length == profile.max_length
            and _is_hash_like_length(profile.min_length)):
        evidence.append(f"fixed_length={profile.min_length}")
        evidence.append("high_distinct_pct")
        evidence.append("hash_like_fixed_length_string")
        return _build(ColumnRole.IDENTIFIER, 0.84, evidence, counter_signals, profile)

    if profile.avg_length is not None and profile.avg_length > 50:
        if profile.distinct_pct > 0.80:
            evidence.append(f"avg_length={profile.avg_length:.0f}")
            evidence.append("high_distinct_pct")
            return _build(ColumnRole.FREETEXT, 0.82, evidence, counter_signals, profile)

    if profile.semantic_type == SemanticType.FREETEXT:
        evidence.append("semantic_type=freetext")
        return _build(ColumnRole.FREETEXT, 0.80, evidence, counter_signals, profile)
    # Name-pattern check: universal annotation column conventions
    # (notes, comments, description, remarks, memo, narrative, etc.)
    # String-type guard + avg_length >= 8 prevents short status codes
    # mislabelled with a free-text column name.
    _is_str_type = ("str" in spark_type_lower or spark_type_lower in ("object", "string"))
    if (
        _is_str_type
        and _FREETEXT_NAME_RE.search(col_lower)
        and (profile.avg_length is None or profile.avg_length >= 8)
    ):
        evidence.append(f"name_matches_freetext_pattern={profile.name!r}")
        return _build(ColumnRole.FREETEXT, 0.80, evidence, counter_signals, profile)

    # --- 10. Dimension (low-to-moderate cardinality, string/enum) ---
    is_string_type = "str" in spark_type_lower or spark_type_lower == "object" or spark_type_lower == "string"
    is_enum_semantic = profile.semantic_type == SemanticType.ENUM

    if is_enum_semantic:
        evidence.append("semantic_type=enum")
        evidence.append(f"distinct_count={profile.distinct_count}")
        return _build(ColumnRole.DIMENSION, 0.88, evidence, counter_signals, profile)

    if is_string_type and not profile.is_unique and profile.distinct_pct < 0.50:
        evidence.append("string_type")
        evidence.append("not_unique")
        evidence.append(f"low_distinct_pct={profile.distinct_pct:.2f}")
        return _build(ColumnRole.DIMENSION, 0.75, evidence, counter_signals, profile)

    # --- 11. Fallback: Unknown ---
    return _build(
        ColumnRole.UNKNOWN, 0.0, ["no_strong_signal"], counter_signals, profile
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(
    role: ColumnRole,
    confidence: float,
    evidence: list[str],
    counter_signals: list[str],
    profile: ColumnProfile,
) -> Inference:
    """Construct a standardized Inference result."""

    return Inference(
        value=role,
        confidence=round(confidence, 3),
        evidence=evidence,
        counter_signals=counter_signals,
        sample_size=profile.row_count,
        method="heuristic",
    )


def _is_numeric_type(spark_type_lower: str) -> bool:
    """Check if the spark type string represents a numeric type."""

    for t in _NUMERIC_TYPES:
        if t in spark_type_lower:
            return True
    return False


def _is_integer_type(spark_type_lower: str) -> bool:
    """Check if the spark type string represents an integer type."""

    int_types = {"int", "integer", "bigint", "long", "smallint", "short", "tinyint", "byte",
                 "int64", "int32", "int16", "int8"}
    for t in int_types:
        if t in spark_type_lower:
            return True
    return False


def _is_partition_like_numeric(profile: ColumnProfile) -> bool:
    """Detect numeric columns that behave like partition/bucket keys.

    Uses generalized behavioral signals — NOT column names — to identify
    integer columns holding period or bucket values (year, month, quarter,
    day-of-week, version, tier, etc.).

    Signals:
    - Very low absolute cardinality (<=20 distinct values)
    - Integer values within a bounded, "reasonable" range
    - No fractional component (true integers)
    - Values are tightly clustered (range/distinct ratio is small)
    """

    # Must have very low absolute cardinality — the core partition signal
    if profile.distinct_count > 20:
        return False

    # Constant-value columns (distinct_count == 1): allow only when the single value
    # falls in a partition-typical range.  A constant snapshot_year (e.g. 2026) or
    # snapshot_month (e.g. 5) is a valid partition key in a periodic snapshot table.
    # Two distinct ranges are recognised:
    #   - Small ordinals: 1-53  (month 1-12, quarter 1-4, week 1-53, day 1-31, etc.)
    #   - Year range:     1990-2100 (realistic snapshot year window)
    # All other constant integers (e.g. load_batch=500) fall through to MEASURE.
    if profile.distinct_count < 2:
        if (
            profile.distinct_count == 1
            and profile.min_value is not None
        ):
            try:
                v = float(profile.min_value)
                if not ((1 <= v <= 53) or (1990 <= v <= 2100)):
                    return False
                # Value is in a partition-typical range — fall through
            except (TypeError, ValueError):
                return False
        else:
            return False

    # Cardinality must be low relative to row count (repeated values)
    if profile.distinct_pct > 0.10:
        return False

    # Check value range if available (min/max from stats)
    if profile.min_value is not None and profile.max_value is not None:
        try:
            v_min = float(profile.min_value)
            v_max = float(profile.max_value)
            value_range = v_max - v_min
            # Range should be bounded — not wildly spread.
            # Typical: months (1-12), quarters (1-4), years (2000-2026),
            # day-of-week (1-7), versions (1-5), tiers (1-3).
            if value_range > 200:
                return False
            # All values should be non-negative or at least within a small band
            if v_min < -10:
                return False
        except (TypeError, ValueError):
            pass

    return True


def _is_hash_like_length(length: int) -> bool:
    """Check if a fixed string length is consistent with common hash/token formats.

    Common hash lengths: MD5=32, SHA1=40, SHA256=64, SHA512=128,
    base64-encoded hashes, UUIDs without dashes=32, etc.
    """

    _HASH_LENGTHS = frozenset({16, 20, 24, 28, 32, 36, 40, 44, 48, 56, 64, 80, 96, 128})
    return length in _HASH_LENGTHS


# ---------------------------------------------------------------------------
# Phase 6: small-sample confidence calibration
# ---------------------------------------------------------------------------

#: Minimum row count considered statistically reliable for role inference.
#: Below this threshold, confidence is linearly penalised.
_MIN_RELIABLE_SAMPLE: int = 30

#: Confidence floor applied when sample size is below _MIN_RELIABLE_SAMPLE.
_SAMPLE_PENALTY_FLOOR: float = 0.50


def _apply_sample_penalty(confidence: float, sample_size: int) -> float:
    """Linearly degrade confidence for small samples.

    When sample_size < _MIN_RELIABLE_SAMPLE, confidence is scaled by the
    fraction (sample_size / _MIN_RELIABLE_SAMPLE), floored at _SAMPLE_PENALTY_FLOOR.
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
# Phase 4: competing-evidence runner_ups
# ---------------------------------------------------------------------------

#: Minimum confidence for an alternative role to appear as a runner_up.
_RUNNER_UP_THRESHOLD = 0.60


def infer_column_role(profile: ColumnProfile) -> Inference:
    """Infer the role of a column and surface competing-evidence alternatives.

    Calls the priority-ordered decision tree (_classify_winner) to determine
    the primary role, then runs an independent secondary scoring pass to find
    any alternative roles that have meaningful supporting evidence
    (confidence >= _RUNNER_UP_THRESHOLD). Alternatives are attached as
    CompetingHypothesis entries in Inference.runner_ups.

    Args:
        profile: A ColumnProfile with stats and semantic_type already populated.

    Returns:
        Inference with value=ColumnRole, runner_ups populated from secondary pass.
    """
    winner = _classify_winner(profile)
    winner.confidence = _apply_sample_penalty(winner.confidence, profile.row_count)
    winner.runner_ups = _collect_runner_ups(profile, winner.value)
    return winner


def _collect_runner_ups(
    profile: ColumnProfile,
    winner_role: ColumnRole,
) -> list[CompetingHypothesis]:
    """Run all role scorers except the winner and return significant alternatives.

    Args:
        profile: The column profile to score.
        winner_role: The primary role already assigned; this scorer is skipped.

    Returns:
        CompetingHypothesis entries sorted by confidence descending, filtered
        to confidence >= _RUNNER_UP_THRESHOLD.
    """
    scorers = [
        (ColumnRole.PARTITION,   _score_partition),
        (ColumnRole.MEASURE,     _score_measure),
        (ColumnRole.FOREIGN_KEY, _score_foreign_key),
        (ColumnRole.DIMENSION,   _score_dimension),
        (ColumnRole.FLAG,        _score_flag),
        (ColumnRole.TIMESTAMP,   _score_timestamp),
    ]

    candidates: list[CompetingHypothesis] = []
    for role, scorer in scorers:
        if role == winner_role:
            continue
        result = scorer(profile)
        if result is None:
            continue
        confidence, evidence = result
        if confidence >= _RUNNER_UP_THRESHOLD:
            candidates.append(
                CompetingHypothesis(
                    value=role,
                    confidence=round(confidence, 3),
                    evidence=evidence,
                )
            )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def _score_partition(
    profile: ColumnProfile,
) -> tuple[float, list[str]] | None:
    """Score the PARTITION role independently of the winner decision tree.

    Two evidence paths:
    - Name-based: column name matches _PARTITION_NAME_RE and is not unique.
    - Behavior-based: integer type with very low cardinality and bounded range
      (year, month, day-of-week, quarter, tier, etc.) — _is_partition_like_numeric.

    Returns:
        (confidence, evidence) or None if no partition signal found.
    """
    col_lower = profile.name.lower()
    evidence: list[str] = []

    # Name-based signal
    if _PARTITION_NAME_RE.search(col_lower) and not profile.is_unique:
        evidence.append(f"name_matches_partition_pattern='{profile.name}'")
        evidence.append("not_unique")
        if profile.distinct_count <= 50:
            evidence.append(f"low_cardinality={profile.distinct_count}")
        return (0.80, evidence)

    # Behavior-based signal (generalized, name-independent)
    spark_type_lower = profile.spark_type.lower()
    if _is_integer_type(spark_type_lower) and not profile.is_unique:
        if _is_partition_like_numeric(profile):
            evidence.append("integer_type")
            evidence.append(f"low_absolute_cardinality={profile.distinct_count}")
            evidence.append("bounded_value_range_suggests_partition")
            return (0.82, evidence)

    return None


def _score_measure(
    profile: ColumnProfile,
) -> tuple[float, list[str]] | None:
    """Score the MEASURE role independently.

    Fires when the column is numeric and non-unique. Returns a higher
    confidence when the name also matches the measure pattern.
    Skips key-named columns without a measure name to avoid noisy
    runner_ups on FK/PK columns.

    Returns:
        (confidence, evidence) or None.
    """
    spark_type_lower = profile.spark_type.lower()
    if not _is_numeric_type(spark_type_lower) or profile.is_unique:
        return None

    evidence: list[str] = []
    col_lower = profile.name.lower()
    is_measure_name = bool(_MEASURE_NAME_RE.search(col_lower))
    is_key_name = bool(_KEY_NAME_RE.search(col_lower))

    if is_measure_name:
        evidence.append("numeric_type")
        evidence.append("name_matches_measure_pattern")
        evidence.append("not_unique")
        return (0.90, evidence)

    # Numeric + not unique + no key name → weak measure signal
    if not is_key_name:
        evidence.append("numeric_type")
        evidence.append("not_unique")
        evidence.append("no_key_name_pattern")
        return (0.65, evidence)

    return None


def _score_foreign_key(
    profile: ColumnProfile,
) -> tuple[float, list[str]] | None:
    """Score the FOREIGN_KEY role independently.

    Returns:
        (confidence, evidence) or None.
    """
    col_lower = profile.name.lower()
    is_fk_name = bool(_FK_NAME_RE.search(col_lower))
    is_key_name = bool(_KEY_NAME_RE.search(col_lower))

    if (is_fk_name or is_key_name) and not profile.is_unique and profile.non_null_count > 0:
        if profile.distinct_pct < 0.80:
            evidence = [
                "name_matches_fk_or_key_pattern",
                f"not_unique_distinct_pct={profile.distinct_pct:.2f}",
            ]
            return (0.82, evidence)

    return None


def _score_dimension(
    profile: ColumnProfile,
) -> tuple[float, list[str]] | None:
    """Score the DIMENSION role independently.

    Returns:
        (confidence, evidence) or None.
    """
    spark_type_lower = profile.spark_type.lower()
    is_string_type = (
        "str" in spark_type_lower
        or spark_type_lower == "object"
        or spark_type_lower == "string"
    )
    is_enum_semantic = profile.semantic_type == SemanticType.ENUM

    if is_enum_semantic:
        return (0.88, ["semantic_type=enum", f"distinct_count={profile.distinct_count}"])

    if is_string_type and not profile.is_unique and profile.distinct_pct < 0.50:
        evidence = [
            "string_type",
            "not_unique",
            f"low_distinct_pct={profile.distinct_pct:.2f}",
        ]
        return (0.75, evidence)

    return None


def _score_flag(
    profile: ColumnProfile,
) -> tuple[float, list[str]] | None:
    """Score the FLAG role independently.

    Returns:
        (confidence, evidence) or None.
    """
    col_lower = profile.name.lower()
    is_flag_name = bool(_FLAG_NAME_RE.search(col_lower))
    is_boolean_semantic = profile.semantic_type == SemanticType.BOOLEAN_STRING
    is_two_value = profile.distinct_count == 2 and profile.non_null_count > 0

    if is_flag_name and (is_two_value or is_boolean_semantic):
        evidence = ["name_matches_flag_pattern"]
        if is_two_value:
            evidence.append("distinct_count=2")
        if is_boolean_semantic:
            evidence.append("semantic_type=boolean_string")
        return (0.92, evidence)

    if is_boolean_semantic and is_two_value:
        evidence = ["semantic_type=boolean_string", "distinct_count=2"]
        return (0.80, evidence)

    return None


def _score_timestamp(
    profile: ColumnProfile,
) -> tuple[float, list[str]] | None:
    """Score the TIMESTAMP role independently.

    Returns:
        (confidence, evidence) or None.
    """
    spark_type_lower = profile.spark_type.lower()
    is_temporal_type = any(t in spark_type_lower for t in _TEMPORAL_TYPES)
    is_temporal_name = bool(_TIMESTAMP_NAME_RE.search(profile.name.lower()))

    if is_temporal_type:
        evidence = [f"spark_type='{profile.spark_type}'_is_temporal"]
        if is_temporal_name:
            evidence.append("name_matches_timestamp_pattern")
        return (0.95, evidence)

    if is_temporal_name and profile.semantic_type == SemanticType.DATE_STRING:
        evidence = ["name_matches_timestamp_pattern", "semantic_type=date_string"]
        return (0.90, evidence)

    return None
