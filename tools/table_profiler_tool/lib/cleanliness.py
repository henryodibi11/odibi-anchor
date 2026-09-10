"""Cleanliness audit for Table Profiler.

Sampling-based detection of whitespace, null-like sentinels, empty strings,
and invisible characters in string columns.

Public API
----------
audit_cleanliness(df, profiles=None) -> list[FormatIssue]
"""

from __future__ import annotations

import sys
from typing import Any

import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine, get_sample
from .models import ColumnProfile, FormatIssue

try:
    from pyspark.sql import DataFrame as SparkDataFrame  # noqa: F401
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SAMPLE_SIZE = 1_000
_MIN_FLAG_RATE = 0.01  # 1% of non-null sample must trigger the issue

# Null-like sentinel strings (matched case-insensitively after strip)
_NULL_LIKE: frozenset[str] = frozenset({
    "n/a", "na", "n.a.", "n.a",
    "null", "none", "nil",
    "tbd", "tbc", "todo",
    "unknown", "unk",
    "-", "--", "---",
    "#n/a", "#null!", "#ref!",
    "(blank)", "(empty)", "(none)",
    "?", "??",
    "nan", "nat",
    "missing", "not applicable", "not available",
    "void",
})

# Invisible / problematic Unicode characters (represented as substrings to search for)
_INVISIBLE_CHARS: list[tuple[str, str]] = [
    ("​", "zero-width space (U+200B)"),
    ("﻿", "byte-order mark (U+FEFF)"),
    (" ", "non-breaking space (U+00A0)"),
    ("‌", "zero-width non-joiner (U+200C)"),
    ("‍", "zero-width joiner (U+200D)"),
    ("⁠", "word joiner (U+2060)"),
    ("᠎", "Mongolian vowel separator (U+180E)"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_cleanliness(
    df: Any,
    profiles: list[ColumnProfile] | None = None,
) -> list[FormatIssue]:
    """Audit string columns for common cleanliness issues.

    Runs 5 sampling-based checks:
    1. leading_spaces    — values with leading whitespace
    2. trailing_spaces   — values with trailing whitespace
    3. null_like_strings — sentinel values stored as strings (N/A, TBD, etc.)
    4. empty_strings     — empty or whitespace-only values that should be NULL
    5. invisible_chars   — zero-width spaces, BOM, non-breaking spaces

    Args:
        df: Input pandas or Spark DataFrame.
        profiles: Pre-computed column profiles (used to scope to string cols).

    Returns:
        List of FormatIssue objects. Empty list when no issues found.
    """
    if df is None:
        return []

    engine = detect_engine(df)
    string_cols = _string_columns(df, engine, profiles)
    issues: list[FormatIssue] = []

    for col in string_cols:
        sample = _get_string_sample_all(df, col, engine)
        if not sample:
            continue
        non_null = [v for v in sample if v is not None]
        if not non_null:
            continue
        issues += _check_leading_spaces(non_null, col)
        issues += _check_trailing_spaces(non_null, col)
        issues += _check_null_like_strings(non_null, col)
        issues += _check_empty_strings(non_null, col)
        issues += _check_invisible_chars(non_null, col)

    return issues


# ---------------------------------------------------------------------------
# Column selection helpers
# ---------------------------------------------------------------------------


def _string_columns(
    df: Any,
    engine: str,
    profiles: list[ColumnProfile] | None,
) -> list[str]:
    """Return string/object column names."""
    if profiles:
        return [
            p.name for p in profiles
            if any(t in p.spark_type.lower()
                   for t in ("string", "object", "varchar", "str"))
        ]
    if engine == "pandas":
        # Match object dtype AND pandas StringDtype. With future.infer_string
        # (default in pandas 3.0) or an explicit 'string' dtype, text columns are
        # StringDtype, not object — checking only is_object_dtype silently skipped
        # them, so no cleanliness issues (leading spaces, null-like, etc.) fired.
        return [
            c for c in df.columns
            if pd.api.types.is_object_dtype(df[c])
            or pd.api.types.is_string_dtype(df[c])
        ]
    try:
        return [f.name for f in df.schema.fields
                if "StringType" in type(f.dataType).__name__]
    except Exception:  # noqa: BLE001
        return []


def _get_string_sample_all(df: Any, col: str, engine: str) -> list[Any]:
    """Return up to _SAMPLE_SIZE raw values (including None/empty) for a column."""
    try:
        # get_sample filters nulls by default; retrieve raw for empty-string check
        if engine == "pandas":
            raw = df[col].head(_SAMPLE_SIZE).tolist()
            return raw
        return get_sample(df, col, n=_SAMPLE_SIZE)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Check 1 — leading spaces
# ---------------------------------------------------------------------------


def _check_leading_spaces(non_null: list[Any], col: str) -> list[FormatIssue]:
    """Flag values that have leading whitespace."""
    affected = [v for v in non_null if isinstance(v, str) and v != v.lstrip()]
    n = len(non_null)
    if not affected or len(affected) / n < _MIN_FLAG_RATE:
        return []
    return [FormatIssue(
        issue_type="leading_spaces",
        column=col,
        severity="warning",
        description=f"{len(affected)} value(s) have leading whitespace ({len(affected)/n:.1%})",
        affected_count=len(affected),
        affected_pct=round(len(affected) / n, 4),
        examples=[repr(v) for v in affected[:5]],
        fix_suggestion="Apply LTRIM() or str.lstrip() before joining or comparing.",
        code_hint=f"df['{col}'].str.lstrip()",
    )]


# ---------------------------------------------------------------------------
# Check 2 — trailing spaces
# ---------------------------------------------------------------------------


def _check_trailing_spaces(non_null: list[Any], col: str) -> list[FormatIssue]:
    """Flag values that have trailing whitespace."""
    affected = [v for v in non_null if isinstance(v, str) and v != v.rstrip()]
    n = len(non_null)
    if not affected or len(affected) / n < _MIN_FLAG_RATE:
        return []
    return [FormatIssue(
        issue_type="trailing_spaces",
        column=col,
        severity="warning",
        description=f"{len(affected)} value(s) have trailing whitespace ({len(affected)/n:.1%})",
        affected_count=len(affected),
        affected_pct=round(len(affected) / n, 4),
        examples=[repr(v) for v in affected[:5]],
        fix_suggestion="Apply RTRIM() or str.rstrip() before joining or comparing.",
        code_hint=f"df['{col}'].str.rstrip()",
    )]


# ---------------------------------------------------------------------------
# Check 3 — null-like strings
# ---------------------------------------------------------------------------


def _check_null_like_strings(non_null: list[Any], col: str) -> list[FormatIssue]:
    """Flag sentinel strings stored as text instead of NULL."""
    affected = [
        v for v in non_null
        if isinstance(v, str) and v.strip().casefold() in _NULL_LIKE
    ]
    n = len(non_null)
    if not affected or len(affected) / n < _MIN_FLAG_RATE:
        return []
    found_values = list(dict.fromkeys(v.strip() for v in affected))[:8]  # unique, ordered
    return [FormatIssue(
        issue_type="null_like_strings",
        column=col,
        severity="warning",
        description=(
            f"{len(affected)} value(s) are null-like sentinels stored as strings "
            f"({len(affected)/n:.1%}): {found_values[:5]}"
        ),
        affected_count=len(affected),
        affected_pct=round(len(affected) / n, 4),
        examples=found_values[:5],
        fix_suggestion=(
            "Replace with NULL: NULLIF(TRIM(col), '') then filter with WHERE col IS NOT NULL."
        ),
        code_hint=(
            f"df['{col}'].replace({{'N/A': None, 'TBD': None, 'None': None}})"
        ),
        metadata={"null_like_values_found": found_values},
    )]


# ---------------------------------------------------------------------------
# Check 4 — empty strings
# ---------------------------------------------------------------------------


def _check_empty_strings(non_null: list[Any], col: str) -> list[FormatIssue]:
    """Flag values that are empty or whitespace-only (should be NULL)."""
    affected = [
        v for v in non_null
        if isinstance(v, str) and v.strip() == ""
    ]
    n = len(non_null)
    if not affected or len(affected) / n < _MIN_FLAG_RATE:
        return []
    return [FormatIssue(
        issue_type="empty_strings",
        column=col,
        severity="info",
        description=f"{len(affected)} empty or whitespace-only value(s) ({len(affected)/n:.1%})",
        affected_count=len(affected),
        affected_pct=round(len(affected) / n, 4),
        examples=[repr(v) for v in affected[:5]],
        fix_suggestion="Replace with NULL: NULLIF(TRIM(col), '').",
        code_hint=f"df['{col}'].replace('', None)",
    )]


# ---------------------------------------------------------------------------
# Check 5 — invisible characters
# ---------------------------------------------------------------------------


def _check_invisible_chars(non_null: list[Any], col: str) -> list[FormatIssue]:
    """Flag values containing invisible Unicode characters."""
    issues: list[FormatIssue] = []
    n = len(non_null)

    for char, label in _INVISIBLE_CHARS:
        affected = [v for v in non_null if isinstance(v, str) and char in v]
        if not affected or len(affected) / n < _MIN_FLAG_RATE:
            continue
        code_point = f"U+{ord(char):04X}"
        issues.append(FormatIssue(
            issue_type="invisible_chars",
            column=col,
            severity="error",
            description=(
                f"{len(affected)} value(s) contain {label} "
                f"({len(affected)/n:.1%})"
            ),
            affected_count=len(affected),
            affected_pct=round(len(affected) / n, 4),
            examples=[repr(v) for v in affected[:5]],
            fix_suggestion=(
                f"Strip {label} with REGEXP_REPLACE or str.replace."
            ),
            code_hint=(
                f"df['{col}'].str.replace(chr({ord(char)}), '', regex=False)"
            ),
            metadata={"char": char, "char_label": label, "code_point": code_point},
        ))
    return issues
