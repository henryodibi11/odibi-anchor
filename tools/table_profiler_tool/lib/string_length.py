"""String Length Analysis for Table Profiler.

Analyzes pre-computed string length statistics (min_length, max_length, avg_length)
from ColumnProfile objects to detect:
- Truncation risk: max_length hitting common VARCHAR boundaries
- Length outliers: max_length vastly exceeds avg_length
- Uniform length: min==max suggesting fixed-width codes or identifiers

Does NOT recompute lengths — stats_engine already handles that.

Public API
----------
detect_string_length_issues(profiles) -> list[FormatIssue]
"""

from __future__ import annotations

import sys
from typing import Any

sys.dont_write_bytecode = True

from .models import ColumnProfile, FormatIssue

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Common VARCHAR limits that indicate possible truncation
_TRUNCATION_BOUNDARIES: tuple[int, ...] = (255, 256, 500, 1000, 2000, 4000, 8000)

# Tolerance: max_length must be within this range of a boundary to trigger
_BOUNDARY_TOLERANCE = 1  # exact match or off-by-one

# Ratio threshold: max_length / avg_length above this suggests length outliers
_OUTLIER_RATIO_THRESHOLD = 10.0

# Minimum max_length to trigger outlier detection (avoids noise on short columns)
_OUTLIER_MIN_MAX_LENGTH = 50

# String type markers (matches profiler.py)
_STRING_TYPE_MARKERS = ("string", "object", "varchar", "str", "char")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_string_length_issues(
    profiles: list[ColumnProfile],
) -> list[FormatIssue]:
    """Analyze string column length distributions for potential issues.

    Examines pre-computed min_length, max_length, avg_length from each
    ColumnProfile and emits FormatIssue objects for:
    1. Truncation risk — max_length at or near a common VARCHAR boundary
    2. Length outliers — max_length >> avg_length (extreme variance)
    3. Uniform length — min == max (fixed-width column)

    Args:
        profiles: List of ColumnProfile objects with length stats populated.

    Returns:
        List of FormatIssue objects (may be empty).
    """
    issues: list[FormatIssue] = []

    for profile in profiles:
        if not _is_string_profile(profile):
            continue
        if profile.max_length is None:
            continue

        # Check truncation risk
        truncation = _check_truncation_risk(profile)
        if truncation:
            issues.append(truncation)

        # Check length outlier
        outlier = _check_length_outlier(profile)
        if outlier:
            issues.append(outlier)

        # Check uniform length (informational)
        uniform = _check_uniform_length(profile)
        if uniform:
            issues.append(uniform)

    return issues


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_string_profile(profile: ColumnProfile) -> bool:
    """Return True if the column is a string type."""
    t = profile.spark_type.lower()
    return any(marker in t for marker in _STRING_TYPE_MARKERS)


def _check_truncation_risk(profile: ColumnProfile) -> FormatIssue | None:
    """Detect if max_length hits a common VARCHAR boundary."""
    max_len = profile.max_length
    if max_len is None:
        return None

    avg_len = profile.avg_length or 0.0

    # Find the closest boundary within tolerance
    best_boundary: int | None = None
    best_distance = _BOUNDARY_TOLERANCE + 1
    for boundary in _TRUNCATION_BOUNDARIES:
        distance = abs(max_len - boundary)
        if distance <= _BOUNDARY_TOLERANCE and distance < best_distance:
            best_distance = distance
            best_boundary = boundary

    if best_boundary is None:
        return None

    # Additional check: if avg_length is close to max, it's probably
    # natural content length, not truncation
    if avg_len > 0 and (max_len / avg_len) < 2.0:
        return None

    return FormatIssue(
        issue_type="truncation_risk",
        column=profile.name,
        severity="warning",
        description=(
            f"Max length ({max_len}) hits VARCHAR({best_boundary}) boundary. "
            f"Values may be truncated at the source or during ingestion."
        ),
        affected_count=0,  # Not counting exact-boundary rows here
        affected_pct=0.0,
        examples=[f"max_length={max_len}", f"avg_length={avg_len:.1f}"],
        fix_suggestion=(
            f"Check source system for VARCHAR({best_boundary}) definition. "
            f"If truncation confirmed, request wider column or handle overflow."
        ),
        metadata={
            "boundary": best_boundary,
            "max_length": max_len,
            "avg_length": avg_len,
            "ratio": round(max_len / avg_len, 2) if avg_len > 0 else None,
        },
    )


def _check_length_outlier(profile: ColumnProfile) -> FormatIssue | None:
    """Detect extreme length variance (max >> avg)."""
    max_len = profile.max_length
    avg_len = profile.avg_length

    if max_len is None or avg_len is None or avg_len <= 0:
        return None
    if max_len < _OUTLIER_MIN_MAX_LENGTH:
        return None

    ratio = max_len / avg_len
    if ratio < _OUTLIER_RATIO_THRESHOLD:
        return None

    return FormatIssue(
        issue_type="length_outlier",
        column=profile.name,
        severity="info",
        description=(
            f"Extreme length variance: max={max_len} vs avg={avg_len:.1f} "
            f"(ratio {ratio:.0f}x). Some values may contain embedded data, "
            f"JSON, or concatenated content."
        ),
        affected_count=0,
        affected_pct=0.0,
        examples=[
            f"max_length={max_len}",
            f"avg_length={avg_len:.1f}",
            f"ratio={ratio:.1f}x",
        ],
        fix_suggestion=(
            "Inspect the longest values — they may be embedded JSON, "
            "error messages, or concatenated content that belongs in a separate column."
        ),
        metadata={
            "max_length": max_len,
            "avg_length": avg_len,
            "min_length": profile.min_length,
            "ratio": round(ratio, 2),
        },
    )


def _check_uniform_length(profile: ColumnProfile) -> FormatIssue | None:
    """Detect fixed-width columns (min == max length)."""
    min_len = profile.min_length
    max_len = profile.max_length

    if min_len is None or max_len is None:
        return None
    if min_len != max_len:
        return None
    # Skip trivially short (single-char flags like "Y"/"N" — not interesting)
    if max_len <= 2:
        return None
    # Skip constant columns (only 1 distinct value — already flagged elsewhere)
    if profile.is_constant:
        return None

    return FormatIssue(
        issue_type="uniform_length",
        column=profile.name,
        severity="info",
        description=(
            f"All values are exactly {max_len} characters. "
            f"This suggests a fixed-width code, identifier, or padded field."
        ),
        affected_count=profile.non_null_count,
        affected_pct=1.0,
        examples=[f"length={max_len}"],
        fix_suggestion=(
            "Fixed-width columns are often codes or identifiers. "
            "Verify this is intentional — padding may need TRIM() before joins."
        ),
        metadata={
            "uniform_length": max_len,
            "distinct_count": profile.distinct_count,
        },
    )
