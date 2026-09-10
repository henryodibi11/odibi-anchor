"""Format consistency detection for Table Profiler.

Sampling-based checks for format inconsistencies across string columns.
Never scans the full dataset — all checks operate on a 1 000-row sample
per column.

Public API
----------
detect_format_issues(df, profiles=None) -> list[FormatIssue]
"""

from __future__ import annotations

import re
import sys
from collections import Counter
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
_MIN_FLAG_RATE = 0.01   # 1 % of non-null sample must show the issue to flag
_MAX_ENUM_CARDINALITY = 50  # columns with more distinct values skip case-check

# Date format patterns tried in order
_DATE_PATTERNS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
    "%b %d, %Y",
    "%Y%m%d",
]

# Boolean-like encoding groups — each is a frozenset of lowercase representations
_BOOL_GROUPS: list[frozenset[str]] = [
    frozenset({"y", "n"}),
    frozenset({"yes", "no"}),
    frozenset({"true", "false"}),
    frozenset({"1", "0"}),
    frozenset({"t", "f"}),
    frozenset({"on", "off"}),
    frozenset({"active", "inactive"}),
    frozenset({"enabled", "disabled"}),
]

# Regex for values with embedded units (e.g. "100.5MW", "200 kW", "45%")
# Excludes leading currency symbols — those are handled by semantic_typer
_UNITS_RE = re.compile(r"^\d+(?:\.\d+)?\s*[A-Za-z%]+$")
# Currency pattern: must start with $, £, €, ¥  OR end with known currency code
_CURRENCY_RE = re.compile(
    r"^[\$\£\€\¥]\s*[\d,]+(?:\.\d+)?$"
    r"|^[\d,]+(?:\.\d+)?\s*(USD|EUR|GBP|CAD|AUD|JPY)$",
    re.IGNORECASE,
)
# Numeric with commas: looks like "1,234" or "1,234.56"
_COMMA_NUM_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_format_issues(
    df: Any,
    profiles: list[ColumnProfile] | None = None,
) -> list[FormatIssue]:
    """Detect format inconsistencies in string columns of a DataFrame.

    Runs 6 sampling-based checks:
    1. mixed_date_format       — multiple date formats in same column
    2. mixed_case_enum         — same value in different cases (Active/ACTIVE)
    3. mixed_boolean_encoding  — Y/N mixed with Yes/No or True/False
    4. inconsistent_code_format — structured codes with varying separators
    5. numeric_with_units      — numbers with embedded unit suffixes
    6. numeric_with_commas     — numbers using commas as thousands separators

    Args:
        df: Input pandas or Spark DataFrame.
        profiles: Pre-computed column profiles (used to scope to string cols).

    Returns:
        List of FormatIssue objects, one per (column, issue_type) pair found.
        Empty list when no issues are detected.
    """
    if df is None:
        return []

    engine = detect_engine(df)
    string_cols = _string_columns(df, engine, profiles)
    issues: list[FormatIssue] = []

    for col in string_cols:
        sample = _get_string_sample(df, col, engine)
        if not sample:
            continue
        issues += _check_mixed_date_format(sample, col)
        issues += _check_mixed_case_enum(sample, col)
        issues += _check_mixed_boolean_encoding(sample, col)
        issues += _check_inconsistent_code_format(sample, col)
        issues += _check_numeric_with_units(sample, col)
        issues += _check_numeric_with_commas(sample, col)

    return issues


# ---------------------------------------------------------------------------
# Column selection helpers
# ---------------------------------------------------------------------------


def _string_columns(
    df: Any,
    engine: str,
    profiles: list[ColumnProfile] | None,
) -> list[str]:
    """Return column names that are string/object typed."""
    if profiles:
        return [
            p.name for p in profiles
            if "string" in p.spark_type.lower()
            or "object" in p.spark_type.lower()
            or "varchar" in p.spark_type.lower()
            or p.spark_type.lower() == "str"
        ]
    if engine == "pandas":
        return [
            col for col in df.columns
            if pd.api.types.is_object_dtype(df[col])
        ]
    # Spark without profiles
    try:
        return [f.name for f in df.schema.fields
                if "StringType" in type(f.dataType).__name__]
    except Exception:  # noqa: BLE001
        return []


def _get_string_sample(df: Any, col: str, engine: str) -> list[str]:
    """Return up to _SAMPLE_SIZE non-null string values for a column."""
    try:
        raw = get_sample(df, col, n=_SAMPLE_SIZE)
        return [str(v) for v in raw if v is not None and str(v).strip() != ""]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Check 1 — mixed date formats
# ---------------------------------------------------------------------------


def _check_mixed_date_format(sample: list[str], col: str) -> list[FormatIssue]:
    """Flag columns where 2+ date formats each represent >1% of values."""
    # Pre-filter: dates always contain digits. Skip pure-text values to avoid
    # expensive pd.to_datetime exception overhead on non-date columns.
    date_candidates = [v for v in sample if any(c.isdigit() for c in v)]
    if not date_candidates:
        return []

    format_hits: Counter[str] = Counter()
    n = len(sample)  # Use full sample size for threshold (preserves sensitivity)

    for val in date_candidates:
        for fmt in _DATE_PATTERNS:
            try:
                pd.to_datetime(val, format=fmt, errors="raise")
                format_hits[fmt] += 1
                break  # first match wins
            except (ValueError, TypeError):
                continue

    # Only formats with > _MIN_FLAG_RATE share
    significant = {fmt: cnt for fmt, cnt in format_hits.items()
                   if cnt / n >= _MIN_FLAG_RATE}
    if len(significant) < 2:
        return []

    top = sorted(significant.items(), key=lambda x: -x[1])[:4]
    desc_parts = ", ".join(f"{fmt}({cnt/n:.0%})" for fmt, cnt in top)
    examples = [v for v in sample if _matches_any_date(v)][:5]

    return [FormatIssue(
        issue_type="mixed_date_format",
        column=col,
        severity="error",
        description=f"Multiple date formats detected: {desc_parts}",
        affected_count=sum(significant.values()),
        affected_pct=round(sum(significant.values()) / n, 4),
        examples=examples,
        fix_suggestion="Use TRY_CAST with explicit format or multi-format parser before casting.",
        code_hint="pd.to_datetime(col, infer_datetime_format=True, errors='coerce')",
        metadata={"formats": {fmt: cnt for fmt, cnt in top}},
    )]


def _matches_any_date(val: str) -> bool:
    for fmt in _DATE_PATTERNS:
        try:
            pd.to_datetime(val, format=fmt, errors="raise")
            return True
        except (ValueError, TypeError):
            continue
    return False


# ---------------------------------------------------------------------------
# Check 2 — mixed case enum
# ---------------------------------------------------------------------------


def _check_mixed_case_enum(sample: list[str], col: str) -> list[FormatIssue]:
    """Flag low-cardinality columns where same value appears in different cases."""
    raw_distinct = set(sample)
    if len(raw_distinct) > _MAX_ENUM_CARDINALITY:
        return []

    folded_distinct = set(v.casefold() for v in raw_distinct)
    if len(folded_distinct) >= len(raw_distinct):
        return []  # no case-folding reduction

    # Find values that differ only by case
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for v in raw_distinct:
        groups[v.casefold()].append(v)
    mixed = {k: vs for k, vs in groups.items() if len(vs) > 1}
    if not mixed:
        return []

    n = len(sample)
    affected = sum(1 for v in sample if v.casefold() in mixed)
    examples = [variant for variants in list(mixed.values())[:3] for variant in variants][:6]

    return [FormatIssue(
        issue_type="mixed_case_enum",
        column=col,
        severity="warning",
        description=f"{len(mixed)} value(s) appear in multiple cases (e.g. {examples[:2]})",
        affected_count=affected,
        affected_pct=round(affected / n, 4),
        examples=examples,
        fix_suggestion="Apply UPPER() or LOWER() before grouping, joining, or filtering.",
        code_hint=f"df['{col}'].str.upper()",
        metadata={"mixed_values": dict(list(mixed.items())[:5])},
    )]


# ---------------------------------------------------------------------------
# Check 3 — mixed boolean encoding
# ---------------------------------------------------------------------------


def _check_mixed_boolean_encoding(sample: list[str], col: str) -> list[FormatIssue]:
    """Flag columns where boolean-like values use more than one encoding scheme."""
    folded = [v.strip().casefold() for v in sample]
    matched_groups = [
        grp for grp in _BOOL_GROUPS
        if any(v in grp for v in folded)
    ]
    if len(matched_groups) < 2:
        return []

    # Verify each matched group has at least one representative
    active = [grp for grp in matched_groups
              if sum(1 for v in folded if v in grp) / len(folded) >= _MIN_FLAG_RATE]
    if len(active) < 2:
        return []

    n = len(sample)
    affected = sum(1 for v in folded
                   if any(v in grp for grp in active))
    examples = list({v for v in sample if v.strip().casefold() in
                     {x for grp in active for x in grp}})[:6]
    encodings = [str(sorted(grp)) for grp in active[:3]]

    return [FormatIssue(
        issue_type="mixed_boolean_encoding",
        column=col,
        severity="warning",
        description=f"Multiple boolean encodings detected: {encodings}",
        affected_count=affected,
        affected_pct=round(affected / n, 4),
        examples=examples,
        fix_suggestion="Standardize to a single encoding with CASE WHEN before use.",
        code_hint="CASE WHEN col IN ('Y','Yes','True','1') THEN TRUE ELSE FALSE END",
        metadata={"encodings_found": encodings},
    )]



def _prefix_overlap_detected(
    dominant_vals: list[str],
    minority_vals: list[str],
    threshold: float = 0.20,
) -> bool:
    """Return True if minority and dominant separator groups share >= threshold of first-token prefixes.

    When the two groups share no common prefixes, they belong to different naming
    conventions (e.g. ERCOT 'ISA-3D2' vs MISO 'Cycle 3') rather than the same
    code series formatted inconsistently.  In that case the separator difference
    is not a meaningful format problem.

    Args:
        dominant_vals:  Values whose first separator matches the dominant char.
        minority_vals:  Values whose first separator does NOT match dominant.
        threshold:      Minimum overlap fraction to consider groups related.
    """
    if not minority_vals:
        return False
    _sep_pat = re.compile(r"[-_\s]")

    def _first_token(v: str) -> str:
        parts = _sep_pat.split(v, 1)
        return parts[0].upper() if parts else v.upper()

    dom_prefixes = {_first_token(v) for v in dominant_vals}
    min_prefixes = {_first_token(v) for v in minority_vals}
    overlap = dom_prefixes & min_prefixes
    return len(overlap) / len(min_prefixes) >= threshold

# ---------------------------------------------------------------------------
# Check 4 — inconsistent code format
# ---------------------------------------------------------------------------


def _check_inconsistent_code_format(sample: list[str], col: str) -> list[FormatIssue]:
    """Flag structured codes where the separator character varies.

    Detects columns where values look like structured codes (e.g. PRJ-001,
    INV_2024_Q1) but use different separators across rows.
    """
    # Only check if a majority look like structured codes
    code_re = re.compile(r"^[A-Za-z0-9]+[-_\s][A-Za-z0-9]")
    code_vals = [v for v in sample if code_re.match(v)]
    if len(code_vals) / max(len(sample), 1) < 0.50:
        return []

    # Extract the separator used in each value
    sep_re = re.compile(r"(?<=[A-Za-z0-9])([-_\s])(?=[A-Za-z0-9])")
    sep_counts: Counter[str] = Counter()
    for v in code_vals:
        seps = sep_re.findall(v)
        if seps:
            sep_counts[seps[0]] += 1  # use the first separator found

    if len(sep_counts) < 2:
        return []  # uniform separators

    n = len(sample)
    dominant = sep_counts.most_common(1)[0][0]

    # Guard: if space is the dominant separator the column stores natural-language
    # phrases (e.g. 'Not Required', 'See Note'), not structured codes.  The few
    # minority-separator values (e.g. date strings like '2024-02-09 00:00:00')
    # are not meaningful code inconsistencies in this context.
    if dominant == " ":
        return []

    minority_count = sum(cnt for sep, cnt in sep_counts.items() if sep != dominant)
    if minority_count / n < _MIN_FLAG_RATE:
        return []

    # Guard: if minority-separator values share no common first-token prefixes
    # with dominant-separator values, the two groups belong to different naming
    # conventions (e.g. ERCOT 'ISA-3D2' vs MISO 'Cycle 3') rather than the
    # same code series with inconsistent formatting.  Suppress in that case.
    dom_vals = [v for v in code_vals
                if (m := sep_re.search(v)) and m.group(1) == dominant]
    min_vals = [v for v in code_vals
                if (m := sep_re.search(v)) and m.group(1) != dominant]
    if not _prefix_overlap_detected(dom_vals, min_vals):
        return []

    sep_summary = ", ".join(
        f"{repr(sep)}({cnt})" for sep, cnt in sep_counts.most_common()
    )
    examples = [
        v for v in code_vals
        if (m := sep_re.search(v)) and m.group(1) != dominant
    ][:5]

    return [FormatIssue(
        issue_type="inconsistent_code_format",
        column=col,
        severity="error",
        description=f"Code separators vary: {sep_summary}",
        affected_count=minority_count,
        affected_pct=round(minority_count / n, 4),
        examples=examples,
        fix_suggestion="Normalize with REGEXP_REPLACE to a single separator before joining.",
        code_hint=f"df['{col}'].str.replace(r'[-_ ]', '-', regex=True)",
        metadata={"separator_counts": dict(sep_counts)},
    )]


# ---------------------------------------------------------------------------
# Check 5 — numeric with embedded units
# ---------------------------------------------------------------------------


def _check_numeric_with_units(sample: list[str], col: str) -> list[FormatIssue]:
    """Flag values that mix numeric content with unit suffixes (100.5MW, 45%).

    Excludes pure currency values (e.g. $100, 100 USD) which are a valid
    semantic type rather than a format inconsistency.
    """
    unit_vals = [
        v for v in sample
        if _UNITS_RE.match(v.strip())
        and not _CURRENCY_RE.match(v.strip())
    ]
    n = len(sample)
    if not unit_vals or len(unit_vals) / n < _MIN_FLAG_RATE:
        return []

    return [FormatIssue(
        issue_type="numeric_with_units",
        column=col,
        severity="error",
        description=(
            f"{len(unit_vals)} value(s) contain embedded units "
            f"({len(unit_vals)/n:.1%} of sample)"
        ),
        affected_count=len(unit_vals),
        affected_pct=round(len(unit_vals) / n, 4),
        examples=unit_vals[:5],
        fix_suggestion=(
            "Extract numeric portion with REGEXP_EXTRACT; "
            "store unit in a separate column."
        ),
        code_hint=(
            f"df['{col}'].str.extract(r'([0-9.]+)')[0].astype(float)"
        ),
        metadata={},
    )]


# ---------------------------------------------------------------------------
# Check 6 — numeric with commas
# ---------------------------------------------------------------------------


def _check_numeric_with_commas(sample: list[str], col: str) -> list[FormatIssue]:
    """Flag values using commas as thousands separators (1,234.56).

    These pass visual inspection but fail CAST(col AS DOUBLE) silently.
    """
    comma_vals = [v for v in sample if _COMMA_NUM_RE.match(v.strip())]
    n = len(sample)
    if not comma_vals or len(comma_vals) / n < _MIN_FLAG_RATE:
        return []

    return [FormatIssue(
        issue_type="numeric_with_commas",
        column=col,
        severity="warning",
        description=(
            f"{len(comma_vals)} value(s) use comma thousands separators "
            f"({len(comma_vals)/n:.1%} of sample)"
        ),
        affected_count=len(comma_vals),
        affected_pct=round(len(comma_vals) / n, 4),
        examples=comma_vals[:5],
        fix_suggestion="Strip commas before casting: REGEXP_REPLACE(col, ',', '').",
        code_hint=f"df['{col}'].str.replace(',', '').astype(float)",
        metadata={},
    )]
