"""Spark-specific quality gate check functions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from odibi_anchor.validation._quality_gate_helpers import (
    _build_context,
    _check_row_count,
    _extract_target_columns,
    _format_age,
    _make_check,
    _spark_rows_to_dicts,
    _type_category,
)


def _gate_spark(
    df: Any,
    *,
    keys: list[str] | None,
    target_schema: Any,
    active_checks: list[str],
    subject: str,
    sample_limit: int,
    freshness_column: str | None,
    df_name: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Run quality gate checks on a Spark DataFrame."""
    total_rows = df.count()
    check_results: list[dict[str, Any]] = []

    for check_id in active_checks:
        if check_id == "row_count":
            result = _check_row_count(
                total_rows, thresholds
            )
        elif check_id == "duplicate_keys":
            result = _check_dup_keys_spark(
                df, keys, sample_limit,
                total_rows, df_name,
            )
        elif check_id == "null_keys":
            result = _check_null_keys_spark(
                df, keys, sample_limit,
                total_rows, df_name,
            )
        elif check_id == "schema_compat":
            result = _check_schema_spark(
                df, target_schema, df_name
            )
        elif check_id == "null_columns":
            result = _check_null_cols_spark(
                df, total_rows, df_name
            )
        elif check_id == "completeness":
            result = _check_completeness_spark(
                df, total_rows
            )
        elif check_id == "freshness":
            result = _check_freshness_spark(
                df, freshness_column, thresholds
            )
        else:
            continue
        check_results.append(result)

    return _build_context(
        check_results=check_results,
        total_rows=total_rows,
        subject=subject,
        engine_name="spark",
        df_name=df_name,
    )


def _check_dup_keys_spark(
    df: Any,
    keys: list[str],
    sample_limit: int,
    total_rows: int,
    df_name: str,
) -> dict[str, Any]:
    """Check for duplicate business keys (Spark)."""
    from pyspark.sql import functions as F

    group_df = (
        df.groupBy(keys)
        .agg(F.count("*").alias("_count"))
        .filter(F.col("_count") > 1)
    )

    sample_rows = group_df.limit(
        sample_limit
    ).collect()

    if not sample_rows:
        return _make_check(
            check_id="duplicate_keys",
            status="pass",
            severity="blocker",
            detail="No duplicate keys",
            fix_expr="",
            samples=[],
        )

    n_groups = group_df.count()
    dup_row_count = int(
        group_df.agg(
            F.sum("_count").alias("total")
        ).collect()[0]["total"]
    )

    pct = (
        round(dup_row_count / total_rows * 100, 2)
        if total_rows > 0
        else 0.0
    )

    samples = _spark_rows_to_dicts(sample_rows)
    rows_dropped = dup_row_count - n_groups

    key_str = ", ".join(f"'{k}'" for k in keys)
    fix_expr = (
        f"{df_name} = {df_name}"
        f".dropDuplicates([{key_str}])"
    )

    return _make_check(
        check_id="duplicate_keys",
        status="fail",
        severity="blocker",
        detail=(
            f"{n_groups} duplicate key groups "
            f"({dup_row_count} rows, {pct}%)"
        ),
        fix_expr=fix_expr,
        samples=samples,
        extra={
            "duplicate_row_count": dup_row_count,
            "duplicate_group_count": n_groups,
            "duplicate_key_pct": pct,
            "rows_dropped_by_fix": rows_dropped,
        },
    )


def _check_null_keys_spark(
    df: Any,
    keys: list[str],
    sample_limit: int,
    total_rows: int,
    df_name: str,
) -> dict[str, Any]:
    """Check for null values in key columns (Spark)."""
    from pyspark.sql import functions as F

    conditions = [
        F.col(k).isNull() for k in keys
    ]
    combined = conditions[0]
    for cond in conditions[1:]:
        combined = combined | cond

    null_count = df.filter(combined).count()

    if null_count == 0:
        return _make_check(
            check_id="null_keys",
            status="pass",
            severity="blocker",
            detail="No null keys",
            fix_expr="",
            samples=[],
        )

    pct = (
        round(null_count / total_rows * 100, 2)
        if total_rows > 0
        else 0.0
    )

    # Per-column null counts
    per_col: dict[str, int] = {}
    agg_exprs = [
        F.sum(
            F.when(F.col(k).isNull(), 1)
            .otherwise(0)
        ).alias(k)
        for k in keys
    ]
    counts_row = df.select(agg_exprs).collect()[0]
    for k in keys:
        nc = int(counts_row[k] or 0)
        if nc > 0:
            per_col[k] = nc

    sample_rows = (
        df.filter(combined)
        .limit(sample_limit)
        .collect()
    )
    samples = _spark_rows_to_dicts(sample_rows)

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


def _check_schema_spark(
    df: Any,
    target_schema: Any,
    df_name: str,
) -> dict[str, Any]:
    """Check schema compatibility vs target (Spark)."""
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
    type_mismatches = _check_type_compat_spark(
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
        fix_lines.append(
            "from pyspark.sql import functions as F"
        )
        for col in missing:
            fix_lines.append(
                f"{df_name} = {df_name}.withColumn("
                f"'{col}', F.lit(None))"
            )
    if extra:
        keep = sorted(target_col_names & source_cols)
        keep += missing
        col_list = ", ".join(
            f"'{c}'" for c in keep
        )
        fix_lines.append(
            f"{df_name} = {df_name}"
            f".select([{col_list}])"
        )
    fix_expr = "\n".join(fix_lines)

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


def _check_type_compat_spark(
    df: Any,
    target_cols: dict[str, str],
) -> list[dict[str, str]]:
    """Check type compatibility for shared columns."""
    mismatches: list[dict[str, str]] = []
    schema_map = {
        f.name: str(f.dataType)
        for f in df.schema.fields
    }
    for col, target_type in target_cols.items():
        if col not in schema_map:
            continue
        source_type = schema_map[col].lower()
        tgt_lower = target_type.lower()
        src_cat = _type_category(source_type)
        tgt_cat = _type_category(tgt_lower)
        if src_cat and tgt_cat and src_cat != tgt_cat:
            mismatches.append({
                "column": col,
                "source_type": schema_map[col],
                "target_type": target_type,
                "source_category": src_cat,
                "target_category": tgt_cat,
            })
    return mismatches


def _check_null_cols_spark(
    df: Any,
    total_rows: int,
    df_name: str,
) -> dict[str, Any]:
    """Detect columns that are 100% null (Spark)."""
    from pyspark.sql import functions as F

    if total_rows == 0:
        return _make_check(
            check_id="null_columns",
            status="pass",
            severity="warning",
            detail="No rows to check",
            fix_expr="",
            samples=[],
        )

    agg_exprs = [
        F.count(F.col(c)).alias(c)
        for c in df.columns
    ]
    counts_row = df.select(agg_exprs).collect()[0]

    dead_cols = sorted(
        c for c in df.columns
        if int(counts_row[c] or 0) == 0
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
        f"{df_name} = {df_name}.drop({col_list})"
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


def _check_completeness_spark(
    df: Any, total_rows: int
) -> dict[str, Any]:
    """Per-column null percentage summary (Spark)."""
    from pyspark.sql import functions as F

    if total_rows == 0:
        return _make_check(
            check_id="completeness",
            status="pass",
            severity="info",
            detail="No rows to check",
            fix_expr="",
            samples=[],
        )

    agg_exprs = [
        F.sum(
            F.when(F.col(c).isNull(), 1)
            .otherwise(0)
        ).alias(c)
        for c in df.columns
    ]
    null_row = df.select(agg_exprs).collect()[0]

    col_stats: list[dict[str, Any]] = []
    for c in df.columns:
        nc = int(null_row[c] or 0)
        pct = round(nc / total_rows * 100, 2)
        col_stats.append({
            "column": c,
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


def _check_freshness_spark(
    df: Any,
    freshness_column: str,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """Check data freshness via timestamp column."""
    from pyspark.sql import functions as F

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

    max_row = (
        df.select(
            F.max(F.col(freshness_column))
            .alias("max_ts")
        ).collect()[0]
    )
    max_val = max_row["max_ts"]

    if max_val is None:
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
    # Handle both datetime and date objects
    if hasattr(max_val, "hour"):
        if max_val.tzinfo is None:
            from datetime import timezone as tz
            max_val = max_val.replace(
                tzinfo=tz.utc
            )
        age_hours = (
            (now - max_val).total_seconds() / 3600
        )
    else:
        # date object
        from datetime import date
        days_old = (now.date() - max_val).days
        age_hours = days_old * 24.0

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
