"""Extra Findings for Table Profiler (Backlog F19–F24).

Profile-based detections that produce finding strings without
changing classification or requiring expensive data scans.

Public API
----------
detect_extra_findings(profiles, row_count=0) -> list[str]

Features:
- F19: Monotonicity detection (watermark candidates)
- F20: Conditional sparsity explanation (extends co-null interpretation)
- F21: Value concentration / Pareto (single value dominates)
- F23: Empty/near-empty column detection
- F24: Row ordering / natural sort signal
"""

from __future__ import annotations

import sys
from typing import Any

sys.dont_write_bytecode = True

from .models import ColumnProfile, ColumnRole, SemanticType


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# F19: Monotonicity
_MONOTONIC_INT_TYPES = ("long", "integer", "int", "bigint", "smallint", "short")
_MONOTONIC_TIMESTAMP_TYPES = ("timestamp", "date")

# F21: Value concentration
_CONCENTRATION_THRESHOLD = 0.80  # 80% of rows for single value

# F23: Empty/near-empty
_NEAR_EMPTY_THRESHOLD = 0.99  # 99%+ null

# F24: Row ordering — uses min/max + is_unique heuristic


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_extra_findings(
    profiles: list[ColumnProfile] | None,
    row_count: int = 0,
) -> list[str]:
    """Detect extra findings from column profiles.

    Pure profile-based — no data scans required.

    Args:
        profiles: Pre-computed column profiles.
        row_count: Total row count of the table.

    Returns:
        List of finding strings to append to the profile.
    """
    if not profiles:
        return []

    findings: list[str] = []

    # F23: Empty/near-empty columns (check first — most fundamental)
    empty_findings = _detect_empty_columns(profiles)
    findings.extend(empty_findings)

    # F21: Value concentration / Pareto
    concentration_findings = _detect_value_concentration(profiles, row_count)
    findings.extend(concentration_findings)

    # F19: Monotonicity / watermark candidates
    monotonic_findings = _detect_monotonicity(profiles, row_count)
    findings.extend(monotonic_findings)

    # F24: Row ordering signal
    ordering_findings = _detect_ordering_signal(profiles, row_count)
    findings.extend(ordering_findings)

    # F20: Conditional sparsity — interpretation layer
    # (actual co-null detection is in cross_column.py; here we just flag
    # columns with high nulls that have a correlated_nulls explanation)
    sparsity_findings = _detect_conditional_sparsity(profiles)
    findings.extend(sparsity_findings)

    return findings


# ---------------------------------------------------------------------------
# F19: Monotonicity Detection
# ---------------------------------------------------------------------------


def _detect_monotonicity(
    profiles: list[ColumnProfile],
    row_count: int,
) -> list[str]:
    """Detect columns likely to be monotonically increasing (watermark candidates).

    Heuristic: a column is a watermark candidate if:
    - It's numeric or timestamp type
    - It's unique (distinct_count == row_count or is_unique=True)
    - It has min_value < max_value (not constant)
    - It's not flagged as a dimension or flag

    These columns can serve as incremental load watermarks.
    """
    findings: list[str] = []
    candidates: list[str] = []

    for p in profiles:
        if p.role in (ColumnRole.DIMENSION, ColumnRole.FLAG, ColumnRole.FREETEXT):
            continue
        if not p.is_unique:
            continue

        base_type = p.spark_type.lower().split("[")[0].strip()
        is_numeric = any(t in base_type for t in _MONOTONIC_INT_TYPES)
        is_temporal = any(t in base_type for t in _MONOTONIC_TIMESTAMP_TYPES)

        if not (is_numeric or is_temporal):
            continue

        if p.min_value is None or p.max_value is None:
            continue

        try:
            if str(p.min_value) == str(p.max_value):
                continue
        except (TypeError, ValueError):
            continue

        candidates.append(p.name)

    if candidates:
        top = candidates[:3]
        findings.append(
            f"Watermark candidates (monotonic): {', '.join(top)}"
            + (f" (+{len(candidates) - 3} more)" if len(candidates) > 3 else "")
            + " — suitable for incremental load high-water marks"
        )

    return findings


# ---------------------------------------------------------------------------
# F20: Conditional Sparsity
# ---------------------------------------------------------------------------


def _detect_conditional_sparsity(
    profiles: list[ColumnProfile],
) -> list[str]:
    """Flag columns whose nulls are explained by correlated_nulls.

    If a column has high null rate AND has entries in correlated_nulls
    (populated by F14 cross-column analysis), the nulls are likely
    structurally expected rather than data quality issues.
    """
    findings: list[str] = []
    explained: list[str] = []

    for p in profiles:
        if p.null_pct < 0.20:
            continue
        if not hasattr(p, "correlated_nulls") or not p.correlated_nulls:
            continue
        explained.append(f"{p.name} (driven by {p.correlated_nulls[0]})")

    if explained:
        top = explained[:3]
        findings.append(
            f"Conditional sparsity: {', '.join(top)}"
            + (f" (+{len(explained) - 3} more)" if len(explained) > 3 else "")
            + " — nulls are structurally expected, not quality issues"
        )

    return findings


# ---------------------------------------------------------------------------
# F21: Value Concentration / Pareto
# ---------------------------------------------------------------------------


def _detect_value_concentration(
    profiles: list[ColumnProfile],
    row_count: int,
) -> list[str]:
    """Detect columns where a single value dominates (>= 80% of rows).

    Uses top_values from ColumnProfile (already computed by stats_engine).
    """
    findings: list[str] = []
    concentrated: list[str] = []

    if row_count <= 0:
        # Try to get row_count from first profile
        if profiles:
            row_count = profiles[0].row_count

    if row_count <= 0:
        return findings

    for p in profiles:
        if p.role in (ColumnRole.PRIMARY_KEY, ColumnRole.SURROGATE_KEY, ColumnRole.NATURAL_KEY):
            continue
        if p.is_unique:
            continue
        if not p.top_values:
            continue

        # top_values[0] is the most frequent value
        top = p.top_values[0]
        top_count = top.get("count", 0) if isinstance(top, dict) else 0

        if top_count <= 0:
            continue

        fraction = top_count / row_count
        if fraction >= _CONCENTRATION_THRESHOLD:
            top_val = top.get("value", "?") if isinstance(top, dict) else "?"
            concentrated.append(
                f"{p.name}='{top_val}' ({fraction:.0%})"
            )

    if concentrated:
        top_items = concentrated[:3]
        findings.append(
            f"Value concentration: {', '.join(top_items)}"
            + (f" (+{len(concentrated) - 3} more)" if len(concentrated) > 3 else "")
            + " — consider whether these are meaningful or degenerate"
        )

    return findings


# ---------------------------------------------------------------------------
# F23: Empty / Near-Empty Column Detection
# ---------------------------------------------------------------------------


def _detect_empty_columns(
    profiles: list[ColumnProfile],
) -> list[str]:
    """Detect columns that are fully null, near-empty, or single-value.

    Categories:
    - Dead columns: 100% null (completely empty)
    - Near-empty: 99%+ null
    - Single-value: distinct_count == 1 (constant column, not useful)
    """
    findings: list[str] = []
    dead_cols: list[str] = []
    near_empty: list[str] = []
    single_value: list[str] = []

    for p in profiles:
        if p.null_pct >= 1.0:
            dead_cols.append(p.name)
        elif p.null_pct >= _NEAR_EMPTY_THRESHOLD:
            near_empty.append(p.name)
        elif p.distinct_count == 1 and p.null_pct < 0.5:
            single_value.append(p.name)

    if dead_cols:
        findings.append(
            f"Dead columns (100% null): {', '.join(dead_cols[:5])}"
            + (f" (+{len(dead_cols) - 5} more)" if len(dead_cols) > 5 else "")
            + " — candidates for removal"
        )

    if near_empty:
        findings.append(
            f"Near-empty columns (99%+ null): {', '.join(near_empty[:5])}"
            + (f" (+{len(near_empty) - 5} more)" if len(near_empty) > 5 else "")
        )

    if single_value:
        findings.append(
            f"Single-value columns: {', '.join(single_value[:5])}"
            + (f" (+{len(single_value) - 5} more)" if len(single_value) > 5 else "")
            + " — carry no information"
        )

    return findings


# ---------------------------------------------------------------------------
# F24: Row Ordering / Natural Sort Signal
# ---------------------------------------------------------------------------


def _detect_ordering_signal(
    profiles: list[ColumnProfile],
    row_count: int,
) -> list[str]:
    """Detect columns that suggest a natural row ordering.

    Heuristic: a column suggests natural ordering if:
    - Numeric/timestamp type AND is_unique AND role is TIMESTAMP or PRIMARY_KEY
    - Or: name contains 'seq', 'order', 'index', 'row_num', 'rownum'

    This is weaker than true sort verification (which needs data scan)
    but still useful for guidance.
    """
    findings: list[str] = []
    order_candidates: list[str] = []

    _ORDER_NAMES = {"seq", "order", "index", "row_num", "rownum", "sequence", "sort_key", "line_num"}

    for p in profiles:
        name_lower = p.name.lower()

        # Name-based signal
        if any(hint in name_lower for hint in _ORDER_NAMES):
            order_candidates.append(p.name)
            continue

        # Role-based: unique timestamps suggest event ordering
        if p.is_unique and p.role == ColumnRole.TIMESTAMP:
            order_candidates.append(p.name)

    if order_candidates:
        top = order_candidates[:3]
        findings.append(
            f"Natural sort candidates: {', '.join(top)}"
            + " — likely define row ordering for this table"
        )

    return findings
