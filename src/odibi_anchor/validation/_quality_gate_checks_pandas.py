"""Pandas-specific quality gate check functions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from odibi_anchor.validation._quality_gate_helpers import (
    _extract_target_columns,
    _format_age,
    _make_check,
    _pandas_rows_to_dicts,
    _type_category,
)


def _check_dup_keys_pandas(
    df: Any,
    keys: list[str],
    sample_limit: int,
    df_name: str,
) -> dict[str, Any]:
    """Check for duplicate business keys in pandas."""
    dup_mask = df.duplicated(subset=keys, keep=False)
    dup_count = int(dup_mask.sum())

    if dup_count == 0:
        return _make_check(
            check_id="duplicate_keys",
            status="pass",
            severity="blocker",
            detail="No duplicate keys",
            fix_expr="",
            samples=[],
        )

    dup_rows = df[dup_mask]
    group_counts = (
        dup_rows.groupby(keys, dropna=False)
        .size()
        .reset_index(name="_count")
    )
    n_groups = len(group_counts)

    samples = _pandas_rows_to_dicts(
        group_counts.head(sample_limit)
    )

    total_rows = len(df)
    pct = (
        round(dup_count / total_rows * 100, 2)
        if total_rows > 0
        else 0.0
    )

    key_str = ", ".join(f"'{k}'" for k in keys)
    fix_expr = (
        f"{df_name} = {df_name}.drop_duplicates("
        f"subset=[{key_str}], keep='last')"
    )

    # Rows that would be dropped by dedup
    rows_dropped = dup_count - n_groups

    return _make_check(
        check_id="duplicate_keys",
        status="fail",
        severity="blocker",
        detail=(
            f"{n_groups} duplicate key groups "
            f"({dup_count} rows, {pct}%)"
        ),
        fix_expr=fix_expr,
        samples=samples,
        extra={
            "duplicate_row_count": dup_count,
            "duplicate_group_count": n_groups,
            "duplicate_key_pct": pct,
            "rows_dropped_by_fix": rows_dropped,
        },
    )


def _check_null_keys_pandas(
    df: Any,
    keys: list[str],
    sample_limit: int,
    df_name: str,
) -> dict[str, Any]:
    """Check for null values in key columns (pandas)."""
    null_mask = df[keys].isnull().any(axis=1)
    null_count = int(null_mask.sum())

    if null_count == 0:
        return _make_check(
            check_id="null_keys",
            status="pass",
            severity="blocker",
            detail="No null keys",
            fix_expr="",
            samples=[],
        )

    total_rows = len(df)
    pct = (
        round(null_count / total_rows * 100, 2)
        if total_rows > 0
        else 0.0
    )

    per_col: dict[str, int] = {}
    for k in keys:
        nc = int(df[k].isnull().sum())
        if nc > 0:
            per_col[k] = nc

    samples = _pandas_rows_to_dicts(
        df[null_mask].head(sample_limit)
    )

    key_str = ", ".join(f"'{k}'" for k in keys)
    fix_expr = (
        f"{df_name} = {df_name}.dropna("
        f"subset=[{key_str}])"
    )

    return _make_check(
        check_id="null_keys",
        status="fail",
        severity="blocker",
        detail=(
            f"{null_count} rows with null keys "
            f"({pct}%)"
        ),
        fix_expr=fix_expr,
        samples=samples,
        extra={
            "null_key_row_count": null_count,
            "null_key_pct": pct,
            "per_column_nulls": per_col,
            "rows_dropped_by_fix": null_count,
        },
    )


def _check_schema_pandas(
    df: Any,
    target_schema: Any,
    df_name: str,
) -> dict[str, Any]:
    """Check schema compatibility vs target (pandas)."""
    target_cols = _extract_target_columns(
        target_schema
    )
    if target_cols is None:
        return _make_check(
            check_id="schema_compat",
            status="warn",
            severity="warning",
            detail="Could not parse target_schema",
            fix_expr="",
            samples=[],
        )

    source_cols = set(df.columns)
    target_col_names = set(target_cols.keys())

    missing = sorted(target_col_names - source_cols)
    extra = sorted(source_cols - target_col_names)

    # Type compatibility for shared columns
    type_mismatches = _check_type_compat_pandas(
        df, target_cols
    )

    if not missing and not extra and not type_mismatches:
        return _make_check(
            check_id="schema_compat",
            status="pass",
            severity="blocker",
            detail="Schema matches target",
            fix_expr="",
            samples=[],
        )

    parts: list[str] = []
    if missing:
        parts.append(f"missing: {missing}")
    if extra:
        parts.append(f"extra: {extra}")
    if type_mismatches:
        tm_short = [
            f"{t['column']}({t['source_type']}"
            f"->{t['target_type']})"
            for t in type_mismatches[:3]
        ]
        parts.append(
            f"type mismatches: {tm_short}"
        )
    detail = "; ".join(parts)

    # Fix code
    fix_lines: list[str] = []
    if missing:
        for col in missing:
            fix_lines.append(
                f"{df_name}['{col}'] = None"
            )
    if extra:
        keep = sorted(target_col_names & source_cols)
        keep += missing
        col_list = ", ".join(
            f"'{c}'" for c in keep
        )
        fix_lines.append(
            f"{df_name} = {df_name}"
            f"[[{col_list}]]"
        )
    fix_expr = "\n".join(fix_lines)

    # Blocker if columns missing; warn for extra/type
    status = "fail" if missing else "warn"
    severity = "blocker" if missing else "warning"

    return _make_check(
        check_id="schema_compat",
        status=status,
        severity=severity,
        detail=detail,
        fix_expr=fix_expr,
        samples=[],
        extra={
            "missing_columns": missing,
            "extra_columns": extra,
            "type_mismatches": type_mismatches,
        },
    )


def _check_type_compat_pandas(
    df: Any,
    target_cols: dict[str, str],
) -> list[dict[str, str]]:
    """Check type compatibility for shared columns."""
    mismatches: list[dict[str, str]] = []
    for col in df.columns:
        if col not in target_cols:
            continue
        source_type = str(df[col].dtype).lower()
        target_type = target_cols[col].lower()
        # Normalize to categories
        src_cat = _type_category(source_type)
        tgt_cat = _type_category(target_type)
        if src_cat and tgt_cat and src_cat != tgt_cat:
            mismatches.append({
                "column": col,
                "source_type": source_type,
                "target_type": target_cols[col],
                "source_category": src_cat,
                "target_category": tgt_cat,
            })
    return mismatches


def _check_null_cols_pandas(
    df: Any,
    total_rows: int,
    df_name: str,
) -> dict[str, Any]:
    """Detect columns that are 100% null (pandas)."""
    if total_rows == 0:
        return _make_check(
            check_id="null_columns",
            status="pass",
            severity="warning",
            detail="No rows to check",
            fix_expr="",
            samples=[],
        )

    null_counts = df.isnull().sum()
    dead_cols = sorted(
        null_counts[null_counts == total_rows]
        .index.tolist()
    )

    if not dead_cols:
        return _make_check(
            check_id="null_columns",
            status="pass",
            severity="warning",
            detail="No 100% null columns",
            fix_expr="",
            samples=[],
        )

    col_list = ", ".join(
        f"'{c}'" for c in dead_cols
    )
    fix_expr = (
        f"{df_name} = {df_name}.drop("
        f"columns=[{col_list}])"
    )

    return _make_check(
        check_id="null_columns",
        status="warn",
        severity="warning",
        detail=(
            f"{len(dead_cols)} columns are 100% "
            f"null: {dead_cols}"
        ),
        fix_expr=fix_expr,
        samples=[],
        extra={"null_columns": dead_cols},
    )


def _check_completeness_pandas(
    df: Any, total_rows: int
) -> dict[str, Any]:
    """Per-column null percentage summary (pandas)."""
    if total_rows == 0:
        return _make_check(
            check_id="completeness",
            status="pass",
            severity="info",
            detail="No rows to check",
            fix_expr="",
            samples=[],
        )

    null_counts = df.isnull().sum()
    col_stats: list[dict[str, Any]] = []
    for col in df.columns:
        nc = int(null_counts[col])
        pct = round(nc / total_rows * 100, 2)
        col_stats.append({
            "column": col,
            "null_count": nc,
            "null_pct": pct,
            "complete_pct": round(100.0 - pct, 2),
        })

    col_stats.sort(
        key=lambda x: x["null_pct"], reverse=True
    )

    avg_completeness = round(
        sum(
            c["complete_pct"] for c in col_stats
        ) / len(col_stats),
        2,
    ) if col_stats else 100.0

    return _make_check(
        check_id="completeness",
        status="pass",
        severity="info",
        detail=(
            f"Average completeness: "
            f"{avg_completeness}%"
        ),
        fix_expr="",
        samples=col_stats,
        extra={
            "avg_completeness_pct": avg_completeness,
            "column_stats": col_stats,
        },
    )


def _check_freshness_pandas(
    df: Any,
    freshness_column: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Check data freshness via timestamp column."""
    import pandas as pd

    if freshness_column not in df.columns:
        return _make_check(
            check_id="freshness",
            status="warn",
            severity="warning",
            detail=(
                f"Column '{freshness_column}' "
                f"not found"
            ),
            fix_expr="",
            samples=[],
        )

    col = df[freshness_column]

    # Try to convert to datetime
    try:
        col = pd.to_datetime(col, errors="coerce")
    except Exception:
        return _make_check(
            check_id="freshness",
            status="warn",
            severity="warning",
            detail=(
                f"Cannot parse "
                f"'{freshness_column}' as datetime"
            ),
            fix_expr="",
            samples=[],
        )

    max_val = col.max()
    if pd.isna(max_val):
        return _make_check(
            check_id="freshness",
            status="warn",
            severity="warning",
            detail=(
                f"All values in "
                f"'{freshness_column}' are null"
            ),
            fix_expr="",
            samples=[],
        )

    now = datetime.now(timezone.utc)
    # Make max_val tz-aware if naive
    if max_val.tzinfo is None:
        max_val = max_val.tz_localize("UTC")
    age_hours = (
        (now - max_val).total_seconds() / 3600
    )
    max_staleness = thresholds.get(
        "max_staleness_hours", 24
    )

    age_str = _format_age(age_hours)
    max_ts_str = str(max_val)[:19]

    if age_hours > max_staleness:
        return _make_check(
            check_id="freshness",
            status="warn",
            severity="warning",
            detail=(
                f"Data is {age_str} old "
                f"(max: {max_staleness}h). "
                f"Latest: {max_ts_str}"
            ),
            fix_expr=(
                "# Refresh upstream data source"
            ),
            samples=[],
            extra={
                "latest_timestamp": max_ts_str,
                "age_hours": round(age_hours, 1),
                "max_staleness_hours": max_staleness,
                "is_stale": True,
            },
        )

    return _make_check(
        check_id="freshness",
        status="pass",
        severity="warning",
        detail=(
            f"Data is {age_str} old. "
            f"Latest: {max_ts_str}"
        ),
        fix_expr="",
        samples=[],
        extra={
            "latest_timestamp": max_ts_str,
            "age_hours": round(age_hours, 1),
            "max_staleness_hours": max_staleness,
            "is_stale": False,
        },
    )
