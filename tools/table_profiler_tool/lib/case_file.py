"""Case File — row-level investigation tool.

Investigates specific rows matching a condition. When the profile or microscope
flags anomalies, the case file shows actual rows with full context and
co-occurrence analysis to explain WHY rows are broken.

Public API
----------
case_file(df, *, column=None, filter=None, row_ids=None, key_columns=None,
          subject=None, profile=None, limit=20, context_columns=None) -> dict
"""

from __future__ import annotations

import re
import sys
from typing import Any, cast

import numpy as np
import pandas as pd

sys.dont_write_bytecode = True

from ._sampling import detect_engine
from ._common import NULL_LIKE_SENTINELS, fingerprint_value, to_json_safe, serialize_dict_safe, serialize_samples_safe
from .models import TableProfile

try:
    from pyspark.sql import DataFrame as SparkDataFrame
    from pyspark.sql import functions as F
except ImportError:  # pragma: no cover
    SparkDataFrame = None
    F = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CO_OCCURRENCE_THRESHOLD = 0.50  # 50% concentration minimum (lift filter removes noise)
_CO_OCCURRENCE_MIN_LIFT = 1.5  # Must be 1.5x above table-wide baseline
_CO_OCCURRENCE_MAX_CONSTANT_PCT = 0.90  # Skip table-wide constants
_CO_OCCURRENCE_TOP_N = 20  # Check top N low-cardinality columns
_MAX_ROWS_FOR_CO_OCCURRENCE = 10_000  # Sample if flagged set exceeds this


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def case_file(
    df: Any,
    *,
    column: str | None = None,
    filter: str | None = None,
    row_ids: list[Any] | None = None,
    key_columns: list[str] | None = None,
    subject: str | None = None,
    profile: TableProfile | None = None,
    limit: int = 20,
    context_columns: list[str] | None = None,
) -> dict:
    """Investigate specific rows matching a condition.

    Exactly ONE of these targeting modes must be provided:
      - filter: Smart filter string (see below)
      - row_ids: Explicit row values to look up (requires key_columns)
      - column + no filter: Show rows where this column has any quality issue

    Args:
        df: Spark or pandas DataFrame.
        column: Column to focus the investigation on.
        filter: Smart filter — one of:
            "nulls"          → rows where column IS NULL
            "null_like"      → rows where column is null-like ('', 'N/A', etc.)
            "outliers"       → rows where column value is an IQR outlier
            "duplicates"     → rows sharing a duplicate grain key
            "top:N"          → numeric: N rows with highest values; categorical: N most frequent
            "bottom:N"       → numeric: N rows with lowest values; categorical: N least frequent
            "pattern:XXX"    → rows matching a specific pattern fingerprint
            "format_issues"  → rows flagged by format_checker
            "where:EXPR"     → arbitrary expression (pandas query / Spark filter)
        row_ids: List of key values to look up directly.
        key_columns: Columns to use as row identity (for row_ids and dedup context).
        subject: Optional label.
        profile: Optional pre-computed TableProfile for cross-referencing.
        limit: Max rows to return (default 20).
        context_columns: Which columns to include in output. None = all columns.

    Returns:
        Anchor standard contract dict with kind="case_file".
    """
    engine = detect_engine(df)
    subject = subject or _build_subject(column, filter)

    if engine == "spark":
        return _case_file_spark(
            df, column=column, filter=filter, row_ids=row_ids,
            key_columns=key_columns, subject=subject, profile=profile,
            limit=limit, context_columns=context_columns,
        )

    # pandas path — keep existing logic
    pdf = _to_full_pandas(df, engine)
    total_rows = len(pdf)

    # Apply context_columns filter
    all_columns = list(pdf.columns)
    display_columns = context_columns if context_columns else all_columns

    # Apply targeting mode
    try:
        matched_pdf, filter_desc = _apply_filter(
            pdf, column=column, filter_str=filter, row_ids=row_ids,
            key_columns=key_columns, profile=profile,
        )
    except Exception as e:
        return _error_result(subject, str(e), total_rows)

    matched_count = len(matched_pdf)
    matched_pct = matched_count / total_rows if total_rows > 0 else 0.0

    # Limit output rows
    sample_rows = matched_pdf.head(limit)

    # Convert rows to dicts (only display_columns)
    valid_display_cols = [c for c in display_columns if c in sample_rows.columns]
    rows_data = []
    for _, row in sample_rows[valid_display_cols].iterrows():
        rows_data.append({k: to_json_safe(v) for k, v in row.items()})

    # Co-occurrence detection
    co_occurrences = []
    findings = []
    risks = []

    findings.append(f"{matched_count:,} rows match filter '{filter_desc}'{f' on {column}' if column else ''}")

    if matched_count > 0 and column:
        co_occurrences = _detect_co_occurrences(
            matched_pdf, pdf, column, all_columns, matched_count
        )
        for co in co_occurrences:
            findings.append(
                f"Co-occurrence: {co['pct']:.0%} of flagged rows have "
                f"{co['column']}='{co['value']}' (lift={co['lift']}x vs baseline {co['baseline_pct']:.0%})"
            )

    # Temporal pattern detection
    if matched_count > 0:
        temporal_finding = _detect_temporal_pattern(matched_pdf, all_columns, matched_count)
        if temporal_finding:
            findings.append(temporal_finding)

    # Risk generation
    if matched_pct > 0.1:
        risks.append(
            f"{matched_pct:.0%} of rows are affected — significant data quality issue"
        )
    if column and filter in ("nulls", "null_like") and matched_count > 0:
        risks.append(
            f"Null {column} may break FK relationships — {matched_count:,} orphan rows"
        )

    # Build metrics
    metrics = {
        "matched_rows": matched_count,
        "matched_pct": round(matched_pct, 4),
        "total_rows": total_rows,
        "columns_shown": len(valid_display_cols),
        "filter_applied": filter_desc,
        "target_column": column,
    }

    # Build samples
    samples: dict[str, Any] = {"rows": rows_data}
    if co_occurrences:
        samples["co_occurrences"] = co_occurrences

    # Build summary
    summary_parts = [
        f"{matched_count:,} rows",
        f"with {filter_desc}",
    ]
    if column:
        summary_parts.append(f"on {column}")
    summary_parts.append(f"({matched_pct:.1%} of {total_rows:,})")
    if co_occurrences:
        top_co = co_occurrences[0]
        summary_parts.append(
            f"| co-occurs with {top_co['column']}={top_co['value']} in {top_co['pct']:.0%} of cases"
        )
    summary = " ".join(summary_parts)

    # Suggested next actions
    next_actions = _build_next_actions(column, filter, co_occurrences, matched_count)

    return {
        "kind": "case_file",
        "subject": subject,
        "summary": summary,
        "metrics": serialize_dict_safe(metrics),
        "findings": findings,
        "risks": risks,
        "samples": serialize_samples_safe(samples),
        "suggested_next_actions": next_actions,
    }


# ---------------------------------------------------------------------------
# Private: Filter application
# ---------------------------------------------------------------------------


def _apply_filter(
    pdf: pd.DataFrame,
    *,
    column: str | None,
    filter_str: str | None,
    row_ids: list[Any] | None,
    key_columns: list[str] | None,
    profile: TableProfile | None,
) -> tuple[pd.DataFrame, str]:
    """Apply the targeting mode and return (filtered_df, filter_description)."""

    # Mode 1: row_ids lookup
    if row_ids is not None:
        if not key_columns:
            raise ValueError("row_ids requires key_columns to be specified")
        if len(key_columns) == 1:
            mask = pdf[key_columns[0]].isin(row_ids)
        else:
            # Multi-column key
            mask = pd.Series(False, index=pdf.index)
            for rid in row_ids:
                if isinstance(rid, (list, tuple)):
                    row_mask = pd.Series(True, index=pdf.index)
                    for kc, rv in zip(key_columns, rid):
                        row_mask &= pdf[kc] == rv
                    mask |= row_mask
        return cast(pd.DataFrame, pdf[mask]), "row_ids"

    # Mode 2: filter string
    if filter_str is not None:
        return _apply_smart_filter(pdf, column, filter_str, key_columns, profile)

    # Mode 3: column with no filter — show rows with any quality issue
    if column is not None:
        # Default: show null rows
        mask = pdf[column].isna()
        if mask.sum() == 0:
            # No nulls, show all rows (limited)
            return pdf, "all_rows"
        return cast(pd.DataFrame, pdf[mask]), "quality_issues"

    raise ValueError(
        "Must provide at least one of: filter, row_ids, or column"
    )


def _apply_smart_filter(
    pdf: pd.DataFrame,
    column: str | None,
    filter_str: str,
    key_columns: list[str] | None,
    profile: TableProfile | None,
) -> tuple[pd.DataFrame, str]:
    """Parse and apply a smart filter string."""

    if filter_str == "nulls":
        if not column:
            raise ValueError("filter='nulls' requires a column")
        mask = pdf[column].isna()
        return cast(pd.DataFrame, pdf[mask]), "nulls"

    if filter_str == "null_like":
        if not column:
            raise ValueError("filter='null_like' requires a column")
        mask = pdf[column].isna()
        # Also check string null-likes
        non_null_mask = ~pdf[column].isna()
        str_vals = pdf.loc[non_null_mask, column].astype(str).str.strip().str.lower()
        null_like_mask = str_vals.isin(NULL_LIKE_SENTINELS) | (pdf.loc[non_null_mask, column].astype(str).str.strip() == "")
        # Combine: null OR null-like string
        combined = mask.copy()
        combined.loc[non_null_mask] = null_like_mask
        return cast(pd.DataFrame, pdf[combined]), "null_like"

    if filter_str == "outliers":
        if not column:
            raise ValueError("filter='outliers' requires a column")
        series: pd.Series = cast(pd.Series, pd.to_numeric(pdf[column], errors="coerce"))  # type: ignore[type-arg]
        q1 = float(series.quantile(0.25))
        q3 = float(series.quantile(0.75))
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        mask = (series < lower) | (series > upper)
        return cast(pd.DataFrame, pdf[mask & series.notna()]), "outliers"

    if filter_str == "duplicates":
        keys = key_columns
        if not keys and profile and profile.grain:
            keys = profile.grain.best_grain
        if not keys:
            raise ValueError(
                "filter='duplicates' requires key_columns or a profile with grain"
            )
        dup_mask = pdf.duplicated(subset=keys, keep=False)
        return cast(pd.DataFrame, pdf[dup_mask]), "duplicates"

    # top:N
    top_match = re.match(r"^top:(\d+)$", filter_str)
    if top_match:
        if not column:
            raise ValueError("filter='top:N' requires a column")
        n = int(top_match.group(1))
        if pd.api.types.is_numeric_dtype(pdf[column]):
            return cast(pd.DataFrame, pdf.nlargest(n, column)), f"top:{n}"
        top_vals = pdf[column].value_counts().head(n).index.tolist()
        mask = pdf[column].isin(top_vals)
        return cast(pd.DataFrame, pdf[mask]), f"top:{n}"

    # bottom:N
    bottom_match = re.match(r"^bottom:(\d+)$", filter_str)
    if bottom_match:
        if not column:
            raise ValueError("filter='bottom:N' requires a column")
        n = int(bottom_match.group(1))
        if pd.api.types.is_numeric_dtype(pdf[column]):
            return cast(pd.DataFrame, pdf.nsmallest(n, column)), f"bottom:{n}"
        bottom_vals = pdf[column].value_counts().tail(n).index.tolist()
        mask = pdf[column].isin(bottom_vals)
        return cast(pd.DataFrame, pdf[mask]), f"bottom:{n}"

    # pattern:XXX
    if filter_str.startswith("pattern:"):
        if not column:
            raise ValueError("filter='pattern:XXX' requires a column")
        pattern = filter_str[8:]  # After "pattern:"
        fingerprints = pdf[column].dropna().astype(str).apply(fingerprint_value)
        mask = fingerprints == pattern
        # Realign mask with original index
        full_mask = pd.Series(False, index=pdf.index)
        full_mask.loc[fingerprints.index] = mask
        return cast(pd.DataFrame, pdf[full_mask]), f"pattern:{pattern}"

    # format_issues
    if filter_str == "format_issues":
        if not column:
            raise ValueError("filter='format_issues' requires a column")
        # Flag rows with potential format issues: mixed types, unusual patterns
        non_null = pdf[column].dropna().astype(str)
        fingerprints = non_null.apply(fingerprint_value)
        # Most common pattern
        if len(fingerprints) > 0:
            dominant = fingerprints.mode().iloc[0] if len(fingerprints.mode()) > 0 else ""
            # Rows NOT matching dominant pattern
            mask = fingerprints != dominant
            full_mask = pd.Series(False, index=pdf.index)
            full_mask.loc[fingerprints.index] = mask
            return cast(pd.DataFrame, pdf[full_mask]), "format_issues"
        return pdf.head(0), "format_issues"

    # where:EXPR
    if filter_str.startswith("where:"):
        expr = filter_str[6:]  # After "where:"
        try:
            result = pdf.query(expr)
            return result, f"where:{expr}"
        except Exception as e:
            raise ValueError(f"Invalid where expression '{expr}': {e}")

    raise ValueError(f"Unknown filter: '{filter_str}'")


# ---------------------------------------------------------------------------
# Private: Co-occurrence detection
# ---------------------------------------------------------------------------


def _detect_co_occurrences(
    matched_pdf: pd.DataFrame,
    full_pdf: pd.DataFrame,
    target_column: str,
    all_columns: list[str],
    matched_count: int,
) -> list[dict]:
    """Detect columns with high RELATIVE value concentration in flagged rows.

    Uses lift-based filtering: a column is only flagged if its mode in the
    flagged subset is both concentrated (>=50%) AND elevated (>=1.5x) relative
    to its baseline frequency in the full table.  Table-wide constants
    (mode_pct >= 90% in full table) are skipped as they provide no signal.
    """
    co_occurrences = []

    # Sample if too large
    analysis_df = matched_pdf
    if len(matched_pdf) > _MAX_ROWS_FOR_CO_OCCURRENCE:
        analysis_df = matched_pdf.sample(n=_MAX_ROWS_FOR_CO_OCCURRENCE, random_state=42)

    analysis_count = len(analysis_df)
    if analysis_count == 0:
        return []

    full_count = len(full_pdf)

    # Check other columns for concentration — filter to eligible
    columns_to_check = [
        c for c in all_columns
        if c != target_column
        and (analysis_df[c].dtype == object or analysis_df[c].nunique() < 50)
    ]

    # Sort by nunique ASC — low-cardinality columns are more likely signal
    columns_to_check.sort(key=lambda c: analysis_df[c].nunique())

    for col in columns_to_check[:_CO_OCCURRENCE_TOP_N]:
        try:
            non_null = analysis_df[col].dropna()
            if len(non_null) == 0:
                continue
            mode_val = non_null.mode()
            if len(mode_val) == 0:
                continue
            mode_val = mode_val.iloc[0]
            mode_count = int((non_null == mode_val).sum())
            flagged_pct = mode_count / analysis_count

            # Must meet minimum concentration
            if flagged_pct < _CO_OCCURRENCE_THRESHOLD:
                continue

            # Skip table-wide constants (no signal)
            full_non_null = full_pdf[col].dropna()
            if len(full_non_null) == 0:
                continue
            full_mode_val = full_non_null.mode().iloc[0]
            full_mode_count = int((full_non_null == full_mode_val).sum())
            full_mode_pct = full_mode_count / full_count if full_count > 0 else 0
            if full_mode_pct >= _CO_OCCURRENCE_MAX_CONSTANT_PCT:
                continue

            # Compute lift: compare same value's frequency in flagged vs full table
            full_val_count = int((full_non_null == mode_val).sum())
            baseline_pct = full_val_count / full_count if full_count > 0 else 0
            lift = flagged_pct / baseline_pct if baseline_pct > 0 else float("inf")

            # Must be meaningfully elevated above baseline
            if lift < _CO_OCCURRENCE_MIN_LIFT:
                continue

            co_occurrences.append({
                "column": col,
                "value": to_json_safe(mode_val),
                "count": mode_count,
                "pct": round(flagged_pct, 4),
                "lift": round(lift, 2),
                "baseline_pct": round(baseline_pct, 4),
            })
        except Exception:
            continue

    # Sort by lift descending (strongest signal first)
    co_occurrences.sort(key=lambda x: x["lift"], reverse=True)
    return co_occurrences


def _detect_temporal_pattern(
    matched_pdf: pd.DataFrame,
    all_columns: list[str],
    matched_count: int,
) -> str | None:
    """Check if flagged rows cluster in a time period."""
    # Find datetime columns
    dt_cols = [
        c for c in all_columns
        if pd.api.types.is_datetime64_any_dtype(matched_pdf[c])
    ]
    if not dt_cols:
        return None

    for col in dt_cols[:2]:  # Check first two datetime columns
        try:
            non_null = matched_pdf[col].dropna()
            if len(non_null) == 0:
                continue
            # Check if most rows are before a threshold
            median_date = non_null.median()
            before_median = (non_null <= median_date).sum()
            pct_before = before_median / len(non_null)
            if pct_before > 0.8:
                return (
                    f"Temporal pattern: {pct_before:.0%} of flagged rows "
                    f"are before {median_date}"
                )
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Private: Helpers
# ---------------------------------------------------------------------------


def _to_full_pandas(df: Any, engine: str) -> pd.DataFrame:
    """Convert full DataFrame to pandas.

    For Spark DataFrames, collects all rows (case_file operates on
    pre-filtered bounded sets).
    """
    if engine == "spark":
        # Limit to prevent OOM on huge tables
        count = df.count()
        if count > 500_000:
            return df.sample(False, 500_000 / count).limit(500_000).toPandas()
        return df.toPandas()
    return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame(df)


def _build_subject(column: str | None, filter_str: str | None) -> str:
    """Build a default subject string."""
    parts = []
    if column:
        parts.append(column)
    if filter_str:
        parts.append(filter_str)
    return " ".join(parts) if parts else "case_file"


def _build_next_actions(
    column: str | None,
    filter_str: str | None,
    co_occurrences: list[dict],
    matched_count: int,
) -> list[str]:
    """Generate suggested next actions."""
    actions = []

    if co_occurrences and column:
        top_co = co_occurrences[0]
        actions.append(
            f"Investigate {top_co['column']}='{top_co['value']}' pipeline — "
            f"source of {top_co['pct']:.0%} of affected rows"
        )

    if column and filter_str in ("nulls", "null_like"):
        actions.append(
            f"Run anchor('microscope', df, '{column}') for full column analysis"
        )

    if column and filter_str == "outliers":
        actions.append(
            f"Consider capping outliers or investigating source system"
        )

    if not actions:
        if matched_count > 0:
            actions.append("Review flagged rows and determine if cleanup is needed")
        else:
            actions.append("No rows matched — filter criteria may be too strict")

    return actions


def _error_result(subject: str, error_msg: str, total_rows: int) -> dict:
    """Return a valid Anchor contract dict for error cases."""
    return {
        "kind": "case_file",
        "subject": subject,
        "summary": f"Error: {error_msg}",
        "metrics": {
            "matched_rows": 0,
            "matched_pct": 0.0,
            "total_rows": total_rows,
            "columns_shown": 0,
            "filter_applied": "error",
            "target_column": None,
        },
        "findings": [f"Filter application failed: {error_msg}"],
        "risks": [],
        "samples": {"rows": []},
        "suggested_next_actions": ["Fix the filter expression and retry"],
    }


# ---------------------------------------------------------------------------
# Spark-first path (Fix 4)
# ---------------------------------------------------------------------------


class _PandasFallback(Exception):
    """Signal that Spark filter fell back to pandas path (for complex filters)."""
    def __init__(self, pdf: "pd.DataFrame", desc: str, total_rows: int):
        self.pdf = pdf
        self.desc = desc
        self.total_rows = total_rows


def _apply_spark_filter(
    df: Any,
    *,
    column: "str | None",
    filter_str: "str | None",
    row_ids: "list | None",
    key_columns: "list[str] | None",
    profile: "TableProfile | None",
) -> "tuple[Any, str]":
    """Apply filter in Spark, returning (filtered_df, filter_desc).

    Raises _PandasFallback for filter types that require pandas analysis.
    """
    if F is None:  # pragma: no cover
        raise _PandasFallback(
            _to_full_pandas(df, "spark"), "pandas_fallback", df.count()
        )

    if row_ids is not None:
        if not key_columns:
            raise ValueError("row_ids requires key_columns")
        if len(key_columns) == 1:
            filtered = df.filter(F.col(key_columns[0]).isin(row_ids))
        else:
            conditions = []
            for rid in row_ids:
                if isinstance(rid, (list, tuple)):
                    cond = F.lit(True)
                    for kc, rv in zip(key_columns, rid):
                        cond = cond & (F.col(kc) == rv)
                    conditions.append(cond)
            if conditions:
                combined = conditions[0]
                for c in conditions[1:]:
                    combined = combined | c
                filtered = df.filter(combined)
            else:
                filtered = df.limit(0)
        return filtered, "row_ids"  # type: ignore[return-value]

    if filter_str is not None:
        if filter_str == "nulls":
            if not column:
                raise ValueError("filter='nulls' requires a column")
            return df.filter(F.col(column).isNull()), "nulls"  # type: ignore[return-value]

        if filter_str == "null_like":
            if not column:
                raise ValueError("filter='null_like' requires a column")
            null_cond = F.col(column).isNull()
            null_like_list = list(NULL_LIKE_SENTINELS)
            str_cond = F.lower(F.trim(F.col(column))).isin(null_like_list)
            empty_cond = F.trim(F.col(column)) == ""
            return df.filter(null_cond | str_cond | empty_cond), "null_like"  # type: ignore[return-value]

        if filter_str == "outliers":
            if not column:
                raise ValueError("filter='outliers' requires a column")
            quantiles = df.approxQuantile(column, [0.25, 0.75], 0.01)
            q1, q3 = quantiles[0], quantiles[1]
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            return df.filter(
                (F.col(column) < lower) | (F.col(column) > upper)  # type: ignore[operator]
            ).filter(F.col(column).isNotNull()), "outliers"  # type: ignore[return-value]

        if filter_str == "duplicates":
            keys = key_columns
            if not keys and profile and profile.grain:
                keys = profile.grain.best_grain
            if not keys:
                raise ValueError("filter='duplicates' requires key_columns or profile with grain")
            from pyspark.sql import Window
            w = Window.partitionBy(*keys)
            with_count = df.withColumn("_dup_cnt", F.count("*").over(w))
            return with_count.filter(F.col("_dup_cnt") > 1).drop("_dup_cnt"), "duplicates"  # type: ignore[return-value]

        if filter_str.startswith("where:"):
            expr = filter_str[6:]
            return df.filter(expr), f"where:{expr}"  # type: ignore[return-value]

        # For top:N, bottom:N, pattern:*, format_issues — fall back to pandas
        pdf = _to_full_pandas(df, "spark")
        filtered_pdf, desc = _apply_smart_filter(pdf, column, filter_str, key_columns, profile)
        raise _PandasFallback(filtered_pdf, desc, len(pdf))

    if column is not None:
        return df.filter(F.col(column).isNull()), "quality_issues"  # type: ignore[return-value]

    raise ValueError("Must provide at least one of: filter, row_ids, or column")


def _case_file_spark(
    df: Any,
    *,
    column: "str | None",
    filter: "str | None",
    row_ids: "list | None",
    key_columns: "list[str] | None",
    subject: str,
    profile: "TableProfile | None",
    limit: int,
    context_columns: "list[str] | None",
) -> dict:
    """Spark-optimized case_file: filter in Spark, collect only matched rows."""
    total_rows = df.count()
    all_columns = df.columns

    # Select only needed columns before collecting
    needed_cols = set(context_columns or all_columns)
    if column:
        needed_cols.add(column)
    if key_columns:
        needed_cols.update(key_columns)
    needed_cols_list = [c for c in all_columns if c in needed_cols]

    try:
        filtered_df, filter_desc = _apply_spark_filter(
            df, column=column, filter_str=filter, row_ids=row_ids,
            key_columns=key_columns, profile=profile,
        )
        # Get exact matched count in Spark (cheap after filter pushdown)
        matched_count = filtered_df.count()
        matched_pct = matched_count / total_rows if total_rows > 0 else 0.0
        # Collect only limited rows to pandas
        sample_pdf = filtered_df.select(*[c for c in needed_cols_list if c in all_columns]).limit(limit).toPandas()
    except _PandasFallback as fb:
        # Complex filters (top:N, pattern:*) fell back to pandas path
        matched_count = len(fb.pdf)
        total_rows = fb.total_rows
        matched_pct = matched_count / total_rows if total_rows > 0 else 0.0
        filter_desc = fb.desc
        sample_pdf = fb.pdf.head(limit)
    except Exception as e:
        return _error_result(subject, str(e), total_rows)

    # Build rows data
    display_columns = context_columns if context_columns else all_columns
    valid_display = [c for c in display_columns if c in sample_pdf.columns]
    rows_data = []
    for _, row in sample_pdf[valid_display].iterrows():
        rows_data.append({k: to_json_safe(v) for k, v in row.items()})

    # Co-occurrence: sample matched set, then analyze in pandas
    co_occurrences = []
    findings = []
    risks = []
    findings.append(
        f"{matched_count:,} rows match filter '{filter_desc}'"
        f"{f' on {column}' if column else ''}"
    )

    if matched_count > 0 and column:
        co_sample_size = min(matched_count, _MAX_ROWS_FOR_CO_OCCURRENCE)
        try:
            co_matched = filtered_df.limit(co_sample_size).toPandas()
            baseline_size = min(total_rows, _MAX_ROWS_FOR_CO_OCCURRENCE * 5)
            co_baseline = df.limit(baseline_size).toPandas()
            co_occurrences = _detect_co_occurrences(
                co_matched, co_baseline, column, list(co_matched.columns), len(co_matched)
            )
            for co in co_occurrences:
                findings.append(
                    f"Co-occurrence: {co['pct']:.0%} of flagged rows have "
                    f"{co['column']}='{co['value']}' "
                    f"(lift={co['lift']}x vs baseline {co['baseline_pct']:.0%})"
                )
        except Exception:  # noqa: BLE001
            pass

    if matched_pct > 0.1:
        risks.append(f"{matched_pct:.0%} of rows are affected — significant data quality issue")
    if column and filter in ("nulls", "null_like") and matched_count > 0:
        risks.append(f"Null {column} may break FK relationships — {matched_count:,} orphan rows")

    metrics = {
        "matched_rows": matched_count,
        "total_rows": total_rows,
        "matched_pct": round(matched_pct, 4),
        "filter_applied": filter_desc,
        "sample_returned": len(rows_data),
        "sampled": False,
    }

    next_actions = _build_next_actions(column, None, co_occurrences, matched_count)
    summary = f"{matched_count:,} rows match '{filter_desc}' ({matched_pct:.1%} of {total_rows:,})"

    return {
        "kind": "case_file",
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "findings": findings,
        "risks": risks,
        "samples": {
            "rows": rows_data,
            "co_occurrences": co_occurrences,
        },
        "suggested_next_actions": next_actions,
    }
