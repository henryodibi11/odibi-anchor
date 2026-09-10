"""Duplicate-key context generation for grain and merge safety checks.

This module provides a small, standalone context generator for answering:
"Does this dataset violate its expected business grain?"

The output is a compact dictionary designed for humans, notebooks, logs, and
LLM/Genie handoffs. The pandas implementation is production-ready. The public
API preserves a simple dual-engine dispatch shape so Spark support can be added
in Databricks without changing callers.

Example:
    import pandas as pd
    from odibi_anchor.validation.duplicate_key_context import duplicate_key_context

    df = pd.DataFrame({
        "project_id": ["A", "A", "B"],
        "snapshot_date": ["2026-01-01", "2026-01-01", "2026-01-01"],
        "mw": [10, 12, 20],
    })

    context = duplicate_key_context(
        df,
        keys=["project_id", "snapshot_date"],
        subject="project_snapshot",
    )

    print(context["summary"])
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


SUPPORTED_ENGINES = {"auto", "pandas", "spark"}


def duplicate_key_context(
    df: Any,
    keys: str | Sequence[str],
    *,
    subject: str | None = None,
    engine: str = "auto",
    sample_limit: int = 20,
    include_duplicate_rows: bool = False,
    row_sample_limit: int = 20,
    treat_nulls_as_duplicates: bool = True,
    output_format: str = "dict",
) -> "dict[str, Any] | str":
    """Summarize duplicate-key risk for a DataFrame.

    Use this before Delta merges, table diffs, dimensional modeling, feature
    builds, and quality gates where the provided key columns are expected to
    define one row per business entity or snapshot.

    Args:
        df: Pandas or Spark DataFrame.
        keys: Column name or column names that define the expected grain.
        subject: Optional human-readable dataset name, table name, or step name.
        engine: Execution engine. Use "auto", "pandas", or "spark". Both engines
            are implemented and produce an identical output contract; the Spark
            path uses groupBy/count aggregations.
        sample_limit: Maximum duplicate key-group examples to include.
        include_duplicate_rows: If True, include capped duplicate source-row
            examples in ``sample_duplicate_rows``.
        row_sample_limit: Maximum duplicate source rows to include when
            ``include_duplicate_rows=True``.
        treat_nulls_as_duplicates: If True, null key values participate in
            duplicate grouping. Null key rows are always reported separately as
            a risk signal.

        output_format: Output format. ``"dict"`` returns the structured context
            dictionary. ``"markdown"`` returns a human-readable markdown report.

    Returns:
        Structured context dictionary with duplicate counts, duplicate rates,
        sample duplicate keys, risks, and suggested next actions.

    Raises:
        TypeError: If ``df`` is not compatible with the selected engine.
        ValueError: If ``keys`` or sample limits are invalid, or if key columns
            are missing.
    """
    validate_output_format(output_format)
    key_list = _normalize_keys(keys)
    _validate_non_negative_int("sample_limit", sample_limit)
    _validate_non_negative_int("row_sample_limit", row_sample_limit)

    normalized_engine = engine.lower().strip()
    if normalized_engine not in SUPPORTED_ENGINES:
        raise ValueError(f"Unsupported engine: {engine}. Expected one of {sorted(SUPPORTED_ENGINES)}.")

    if normalized_engine == "auto":
        normalized_engine = _detect_engine(df)

    if normalized_engine == "pandas":
        if not isinstance(df, pd.DataFrame):
            raise TypeError(f"Expected pandas DataFrame for engine='pandas', got {type(df).__name__}.")
        ctx = _duplicate_key_context_pandas(
            df,
            key_list,
            subject=subject,
            sample_limit=sample_limit,
            include_duplicate_rows=include_duplicate_rows,
            row_sample_limit=row_sample_limit,
            treat_nulls_as_duplicates=treat_nulls_as_duplicates,
        )
        if output_format == "markdown":
            return render_duplicate_key_report(ctx)
        return ctx

    if normalized_engine == "spark":
        ctx = _duplicate_key_context_spark(
            df,
            key_list,
            subject=subject,
            sample_limit=sample_limit,
            include_duplicate_rows=include_duplicate_rows,
            row_sample_limit=row_sample_limit,
            treat_nulls_as_duplicates=treat_nulls_as_duplicates,
        )
        if output_format == "markdown":
            return render_duplicate_key_report(ctx)
        return ctx

    raise ValueError(f"Unsupported engine after normalization: {normalized_engine}.")


def _duplicate_key_context_pandas(
    df: pd.DataFrame,
    keys: list[str],
    *,
    subject: str | None,
    sample_limit: int,
    include_duplicate_rows: bool,
    row_sample_limit: int,
    treat_nulls_as_duplicates: bool,
) -> dict[str, Any]:
    """Pandas implementation for duplicate key context generation."""
    _validate_columns_exist(df, keys)

    subject_name = subject or "dataframe"
    row_count = int(len(df))
    key_column_count = len(keys)

    null_mask = df[keys].isna().any(axis=1) if row_count else pd.Series([], dtype=bool)
    all_null_mask = df[keys].isna().all(axis=1) if row_count else pd.Series([], dtype=bool)
    null_key_row_count = int(null_mask.sum())
    all_null_key_row_count = int(all_null_mask.sum())
    null_counts_by_key = {col: int(df[col].isna().sum()) for col in keys}

    group_counts = _build_group_counts(df, keys, treat_nulls_as_duplicates=treat_nulls_as_duplicates)
    duplicate_groups = group_counts[group_counts["_row_count"] > 1].copy()
    duplicate_groups = duplicate_groups.sort_values("_row_count", ascending=False, kind="mergesort")

    unique_key_count = int(len(group_counts))
    duplicate_key_count = int(len(duplicate_groups))
    duplicate_row_count = int(duplicate_groups["_row_count"].sum()) if duplicate_key_count else 0
    excess_duplicate_row_count = int((duplicate_groups["_row_count"] - 1).sum()) if duplicate_key_count else 0
    max_rows_per_key = int(group_counts["_row_count"].max()) if unique_key_count else 0

    samples = _build_duplicate_key_samples(duplicate_groups, keys, sample_limit)
    duplicate_row_samples = []
    if include_duplicate_rows:
        duplicate_row_samples = _build_duplicate_row_samples(df, duplicate_groups, keys, row_sample_limit)

    has_duplicates = duplicate_key_count > 0
    has_null_keys = null_key_row_count > 0
    passed = not has_duplicates and not has_null_keys

    metrics = {
        "engine": "pandas",
        "row_count": row_count,
        "key_columns": keys,
        "key_column_count": key_column_count,
        "unique_key_count": unique_key_count,
        "duplicate_key_count": duplicate_key_count,
        "duplicate_row_count": duplicate_row_count,
        "excess_duplicate_row_count": excess_duplicate_row_count,
        "duplicate_key_rate": _rate(duplicate_key_count, unique_key_count),
        "duplicate_row_rate": _rate(duplicate_row_count, row_count),
        "excess_duplicate_row_rate": _rate(excess_duplicate_row_count, row_count),
        "max_rows_per_key": max_rows_per_key,
        "null_key_row_count": null_key_row_count,
        "all_null_key_row_count": all_null_key_row_count,
        "null_key_rate": _rate(null_key_row_count, row_count),
        "null_counts_by_key": null_counts_by_key,
        "sample_limit": sample_limit,
        "sampled_duplicate_key_count": len(samples),
        "treat_nulls_as_duplicates": treat_nulls_as_duplicates,
    }

    classification = _classify_duplicate_pattern(
        duplicate_row_count=duplicate_row_count,
        total_row_count=row_count,
        max_rows_per_key=max_rows_per_key,
        null_key_count=null_key_row_count,
    )

    findings = _build_findings(
        keys=keys,
        row_count=row_count,
        duplicate_key_count=duplicate_key_count,
        duplicate_row_count=duplicate_row_count,
        excess_duplicate_row_count=excess_duplicate_row_count,
        null_key_row_count=null_key_row_count,
        max_rows_per_key=max_rows_per_key,
    )
    risks = _build_risks(
        keys=keys,
        duplicate_key_count=duplicate_key_count,
        duplicate_row_count=duplicate_row_count,
        excess_duplicate_row_count=excess_duplicate_row_count,
        null_key_row_count=null_key_row_count,
        max_rows_per_key=max_rows_per_key,
    )
    suggested_next_actions = _build_suggested_next_actions(
        keys=keys,
        has_duplicates=has_duplicates,
        has_null_keys=has_null_keys,
        include_duplicate_rows=include_duplicate_rows,
        classification=classification,
    )

    context: dict[str, Any] = {
        "kind": "duplicate_key_context",
        "subject": subject_name,
        "summary": _build_summary(
            subject=subject_name,
            keys=keys,
            row_count=row_count,
            duplicate_key_count=duplicate_key_count,
            duplicate_row_count=duplicate_row_count,
            null_key_row_count=null_key_row_count,
            classification=classification,
        ),
        "passed": passed,
        "has_duplicates": has_duplicates,
        "has_null_keys": has_null_keys,
        "metrics": metrics,
        "findings": findings,
        "risks": risks,
        "samples": {"duplicate_keys": samples, "duplicate_rows": duplicate_row_samples} if samples else {},
        "suggested_next_actions": suggested_next_actions,
        "duplicate_pattern": classification,
    }
    return _json_safe_context(context)


def _duplicate_key_context_spark(
    df: Any,
    keys: list[str],
    *,
    subject: str | None,
    sample_limit: int,
    include_duplicate_rows: bool,
    row_sample_limit: int,
    treat_nulls_as_duplicates: bool,
) -> dict[str, Any]:
    """Spark implementation for duplicate key context generation.

    Uses groupBy/count semantics. Output contract is identical to the pandas
    implementation so callers don't need to distinguish engines.
    """
    # Type check — reject pandas DataFrames when engine="spark" is forced
    if isinstance(df, pd.DataFrame):
        raise TypeError(
            "Expected Spark DataFrame for engine='spark', got pandas DataFrame. "
            "Use engine='pandas' or engine='auto'."
        )

    from pyspark.sql import functions as F  # type: ignore

    # Validate columns exist
    df_columns = set(df.columns)
    missing = [k for k in keys if k not in df_columns]
    if missing:
        raise ValueError(f"Missing key column(s): {missing}. Available columns: {sorted(df_columns)}.")

    subject_name = subject or "dataframe"
    row_count = df.count()
    key_column_count = len(keys)

    # Null key analysis
    null_condition = F.lit(False)
    all_null_condition = F.lit(True)
    null_counts_by_key: dict[str, int] = {}

    for col_name in keys:
        col_is_null = F.col(col_name).isNull()
        null_condition = null_condition | col_is_null
        all_null_condition = all_null_condition & col_is_null

    if row_count > 0:
        null_stats = df.select(
            F.sum(F.when(null_condition, 1).otherwise(0)).alias("null_key_rows"),
            F.sum(F.when(all_null_condition, 1).otherwise(0)).alias("all_null_key_rows"),
            *[F.sum(F.when(F.col(k).isNull(), 1).otherwise(0)).alias(f"null_{k}") for k in keys]
        ).collect()[0]
        null_key_row_count = int(null_stats["null_key_rows"] or 0)
        all_null_key_row_count = int(null_stats["all_null_key_rows"] or 0)
        for k in keys:
            null_counts_by_key[k] = int(null_stats[f"null_{k}"] or 0)
    else:
        null_key_row_count = 0
        all_null_key_row_count = 0
        null_counts_by_key = {k: 0 for k in keys}

    # Group by keys and count
    if treat_nulls_as_duplicates:
        # Fill nulls with sentinel so they group together
        sentinel = "__NULL_SENTINEL__"
        df_for_group = df.select(
            *[F.coalesce(F.col(k).cast("string"), F.lit(sentinel)).alias(k) for k in keys],
            F.lit(1).alias("_one")
        )
        group_counts_df = df_for_group.groupBy(keys).agg(F.count("_one").alias("_row_count"))
    else:
        # Drop rows with any null keys before grouping
        df_for_group = df.dropna(subset=keys)
        group_counts_df = df_for_group.groupBy(keys).agg(F.count(F.lit(1)).alias("_row_count"))

    # Compute aggregate stats on the group counts
    stats = group_counts_df.agg(
        F.count("*").alias("unique_key_count"),
        F.sum(F.when(F.col("_row_count") > 1, 1).otherwise(0)).alias("duplicate_key_count"),
        F.sum(F.when(F.col("_row_count") > 1, F.col("_row_count")).otherwise(0)).alias("duplicate_row_count"),
        F.sum(
            F.when(F.col("_row_count") > 1, F.col("_row_count") - 1).otherwise(0)
        ).alias("excess_duplicate_row_count"),
        F.max("_row_count").alias("max_rows_per_key"),
    ).collect()[0]

    unique_key_count = int(stats["unique_key_count"] or 0)
    duplicate_key_count = int(stats["duplicate_key_count"] or 0)
    duplicate_row_count = int(stats["duplicate_row_count"] or 0)
    excess_duplicate_row_count = int(stats["excess_duplicate_row_count"] or 0)
    max_rows_per_key = int(stats["max_rows_per_key"] or 0)

    # Sample duplicate keys
    samples: list[dict[str, Any]] = []
    duplicate_row_samples: list[dict[str, Any]] = []

    if duplicate_key_count > 0 and sample_limit > 0:
        dup_keys_df = (
            group_counts_df
            .filter(F.col("_row_count") > 1)
            .orderBy(F.col("_row_count").desc())
            .limit(sample_limit)
        )
        dup_rows = dup_keys_df.collect()
        for row in dup_rows:
            key_values = {}
            for k in keys:
                val = row[k]
                if treat_nulls_as_duplicates and val == "__NULL_SENTINEL__":
                    val = None
                key_values[k] = _json_safe_value(val)
            rc = int(row["_row_count"])
            samples.append({
                "key": key_values,
                "row_count": rc,
                "excess_row_count": rc - 1,
            })

    # Sample duplicate rows (full rows that belong to duplicate key groups)
    if include_duplicate_rows and duplicate_key_count > 0 and row_sample_limit > 0:
        # Get top duplicate keys (reuse dup_keys_df)
        dup_keys_for_join = (
            group_counts_df
            .filter(F.col("_row_count") > 1)
            .select(keys)
        )
        joined = df.join(dup_keys_for_join, on=keys, how="inner")
        sample_rows = joined.limit(row_sample_limit).collect()
        for row in sample_rows:
            record = {str(col): _json_safe_value(row[col]) for col in df.columns}
            duplicate_row_samples.append(record)

    # Build output (same contract as pandas)
    has_duplicates = duplicate_key_count > 0
    has_null_keys = null_key_row_count > 0
    passed = not has_duplicates and not has_null_keys

    metrics = {
        "engine": "spark",
        "row_count": row_count,
        "key_columns": keys,
        "key_column_count": key_column_count,
        "unique_key_count": unique_key_count,
        "duplicate_key_count": duplicate_key_count,
        "duplicate_row_count": duplicate_row_count,
        "excess_duplicate_row_count": excess_duplicate_row_count,
        "duplicate_key_rate": _rate(duplicate_key_count, unique_key_count),
        "duplicate_row_rate": _rate(duplicate_row_count, row_count),
        "excess_duplicate_row_rate": _rate(excess_duplicate_row_count, row_count),
        "max_rows_per_key": max_rows_per_key,
        "null_key_row_count": null_key_row_count,
        "all_null_key_row_count": all_null_key_row_count,
        "null_key_rate": _rate(null_key_row_count, row_count),
        "null_counts_by_key": null_counts_by_key,
        "sample_limit": sample_limit,
        "sampled_duplicate_key_count": len(samples),
        "treat_nulls_as_duplicates": treat_nulls_as_duplicates,
    }

    classification = _classify_duplicate_pattern(
        duplicate_row_count=duplicate_row_count,
        total_row_count=row_count,
        max_rows_per_key=max_rows_per_key,
        null_key_count=null_key_row_count,
    )

    findings = _build_findings(
        keys=keys,
        row_count=row_count,
        duplicate_key_count=duplicate_key_count,
        duplicate_row_count=duplicate_row_count,
        excess_duplicate_row_count=excess_duplicate_row_count,
        null_key_row_count=null_key_row_count,
        max_rows_per_key=max_rows_per_key,
    )
    risks = _build_risks(
        keys=keys,
        duplicate_key_count=duplicate_key_count,
        duplicate_row_count=duplicate_row_count,
        excess_duplicate_row_count=excess_duplicate_row_count,
        null_key_row_count=null_key_row_count,
        max_rows_per_key=max_rows_per_key,
    )
    suggested_next_actions = _build_suggested_next_actions(
        keys=keys,
        has_duplicates=has_duplicates,
        has_null_keys=has_null_keys,
        include_duplicate_rows=include_duplicate_rows,
        classification=classification,
    )

    context: dict[str, Any] = {
        "kind": "duplicate_key_context",
        "subject": subject_name,
        "summary": _build_summary(
            subject=subject_name,
            keys=keys,
            row_count=row_count,
            duplicate_key_count=duplicate_key_count,
            duplicate_row_count=duplicate_row_count,
            null_key_row_count=null_key_row_count,
            classification=classification,
        ),
        "passed": passed,
        "has_duplicates": has_duplicates,
        "has_null_keys": has_null_keys,
        "metrics": metrics,
        "findings": findings,
        "risks": risks,
        "samples": {"duplicate_keys": samples, "duplicate_rows": duplicate_row_samples} if samples else {},
        "suggested_next_actions": suggested_next_actions,
        "duplicate_pattern": classification,
    }
    return context


def _normalize_keys(keys: str | Sequence[str]) -> list[str]:
    """Normalize keys argument to a list of strings."""
    if isinstance(keys, str):
        key_list = [keys]
    elif isinstance(keys, Sequence):
        key_list = list(keys)
    else:
        raise TypeError("keys must be a string or a sequence of strings.")

    if not key_list:
        raise ValueError("keys must contain at least one column name.")

    non_strings = [key for key in key_list if not isinstance(key, str)]
    if non_strings:
        raise TypeError(f"All keys must be strings. Invalid values: {non_strings!r}.")

    empty_keys = [key for key in key_list if not key.strip()]
    if empty_keys:
        raise ValueError("keys must not contain empty column names.")

    seen: set[str] = set()
    duplicates: list[str] = []
    for key in key_list:
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        raise ValueError(f"keys contains duplicate column names: {duplicates!r}.")

    return key_list


def _validate_non_negative_int(name: str, value: int) -> None:
    """Validate that a value is a non-negative integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must be >= 0.")


def _validate_columns_exist(df: pd.DataFrame, keys: list[str]) -> None:
    """Validate that all key columns exist in the DataFrame."""
    missing = [key for key in keys if key not in df.columns]
    if missing:
        raise ValueError(f"Missing key column(s): {missing}. Available columns: {list(df.columns)}.")


def _detect_engine(df: Any) -> str:
    """Detect whether the DataFrame is Spark or Pandas."""
    if isinstance(df, pd.DataFrame):
        return "pandas"

    try:
        from pyspark.sql import DataFrame as SparkDataFrame  # type: ignore

        if isinstance(df, SparkDataFrame):
            return "spark"
    except ImportError:
        pass

    if hasattr(df, "groupBy") and hasattr(df, "count") and hasattr(df, "select"):
        return "spark"

    raise TypeError(f"Could not detect DataFrame engine for object of type {type(df).__name__}.")


def _build_group_counts(
    df: pd.DataFrame,
    keys: list[str],
    *,
    treat_nulls_as_duplicates: bool,
) -> pd.DataFrame:
    """Build DataFrame of key groups with row counts."""
    if df.empty:
        return pd.DataFrame(columns=[*keys, "_row_count"])

    return (
        df.groupby(keys, dropna=not treat_nulls_as_duplicates, sort=False)
        .size()
        .reset_index(name="_row_count")
    )


def _build_duplicate_key_samples(
    duplicate_groups: pd.DataFrame,
    keys: list[str],
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Build sample rows for each duplicate key group."""
    if sample_limit == 0 or duplicate_groups.empty:
        return []

    samples: list[dict[str, Any]] = []
    for _, row in duplicate_groups.head(sample_limit).iterrows():
        key_values = {key: _json_safe_value(row[key]) for key in keys}
        row_count = int(row["_row_count"])
        samples.append({
            "key": key_values,
            "row_count": row_count,
            "excess_row_count": row_count - 1,
        })
    return samples


def _build_duplicate_row_samples(
    df: pd.DataFrame,
    duplicate_groups: pd.DataFrame,
    keys: list[str],
    row_sample_limit: int,
) -> list[dict[str, Any]]:
    """Build full row samples for duplicate key groups."""
    if row_sample_limit == 0 or duplicate_groups.empty:
        return []

    marker = duplicate_groups[[*keys, "_row_count"]].rename(columns={"_row_count": "_duplicate_group_row_count"})
    duplicate_rows = df.merge(marker, on=keys, how="inner")
    return [_json_safe_record(record) for record in duplicate_rows.head(row_sample_limit).to_dict(orient="records")]


def _classify_duplicate_pattern(
    duplicate_row_count: int,
    total_row_count: int,
    max_rows_per_key: int,
    null_key_count: int,
) -> dict[str, str]:
    """Classify the nature of the duplicate issue.

    Returns dict with 'pattern' and 'interpretation'.
    """
    if total_row_count == 0:
        return {"pattern": "empty", "interpretation": "No data to evaluate."}

    dup_rate = duplicate_row_count / total_row_count

    if null_key_count > duplicate_row_count * 0.5:
        return {
            "pattern": "null_key_contamination",
            "interpretation": "Most duplicates come from null key values — this is a key completeness problem, not a dedup problem. Fix nulls first.",
        }
    if dup_rate < 0.01 and max_rows_per_key <= 3:
        return {
            "pattern": "localized_hotspot",
            "interpretation": "Low duplicate rate with small groups — likely replay, late-arriving, or edge-case duplicates. Safe to dedup.",
        }
    if dup_rate < 0.05 and max_rows_per_key <= 10:
        return {
            "pattern": "moderate_overlap",
            "interpretation": "Moderate duplicates — could be CDC replay or partial re-extract. Inspect the largest duplicate groups before deduping.",
        }
    return {
        "pattern": "systemic_grain_mismatch",
        "interpretation": f"High duplicate rate ({dup_rate:.1%}) with up to {max_rows_per_key} rows per key — the declared key likely does not represent the real business grain. Re-evaluate the key definition.",
    }


def _build_findings(
    *,
    keys: list[str],
    row_count: int,
    duplicate_key_count: int,
    duplicate_row_count: int,
    excess_duplicate_row_count: int,
    null_key_row_count: int,
    max_rows_per_key: int,
) -> list[str]:
    """Build human-readable findings list from duplicate analysis."""
    key_text = ", ".join(keys)

    if row_count == 0:
        return [f"No rows available to validate duplicate keys for [{key_text}]."]

    findings = [f"Validated {row_count:,} row(s) against key column(s): [{key_text}]."]

    if duplicate_key_count == 0:
        findings.append("No duplicate key groups were found.")
    else:
        findings.append(
            f"Found {duplicate_key_count:,} duplicate key group(s) affecting "
            f"{duplicate_row_count:,} row(s)."
        )
        findings.append(f"Duplicate groups create {excess_duplicate_row_count:,} excess row(s) beyond expected grain.")
        findings.append(f"Largest duplicate key group contains {max_rows_per_key:,} row(s).")

    if null_key_row_count > 0:
        findings.append(f"Found {null_key_row_count:,} row(s) with at least one null key value.")

    return findings


def _build_risks(
    *,
    keys: list[str],
    duplicate_key_count: int,
    duplicate_row_count: int,
    excess_duplicate_row_count: int,
    null_key_row_count: int,
    max_rows_per_key: int,
) -> list[str]:
    """Build risk assessment strings based on duplicate severity."""
    risks: list[str] = []
    key_text = ", ".join(keys)

    if duplicate_key_count > 0:
        risks.append(
            f"If [{key_text}] is the intended business grain, "
            f"{duplicate_row_count:,} duplicates will cause row multiplication in joins. "
            f"If the grain requires additional columns, the duplicates may resolve with a composite key."
        )
        risks.append(
            f"Duplicate keys affect {duplicate_row_count:,} row(s) "
            f"and create {excess_duplicate_row_count:,} excess row(s)."
        )

    if max_rows_per_key > 2:
        risks.append(
            f"At least one key appears {max_rows_per_key:,} times; "
            f"a simple keep-first dedupe may hide data-quality issues."
        )

    if null_key_row_count > 0:
        risks.append("Null key values can cause failed merges, orphan records, or unmatchable rows.")

    return risks


def _build_suggested_next_actions(
    *,
    keys: list[str],
    has_duplicates: bool,
    has_null_keys: bool,
    include_duplicate_rows: bool,
    classification: dict[str, str],
) -> list[str]:
    """Build actionable next-step recommendations."""
    actions: list[str] = []
    key_text = ", ".join(keys)
    pattern = classification.get("pattern", "")

    if has_duplicates:
        actions.append(
            f"MUST: Review the sampled duplicate keys and confirm [{key_text}] is the intended business grain.",
        )

        if pattern == "localized_hotspot":
            actions.append(
                f"Safe to dedup with standard dedup logic on [{key_text}]. "
                "Run anchor('transform', profile) to auto-generate cleanup."
            )
        elif pattern == "systemic_grain_mismatch":
            actions.append(
                f"Do not dedup — re-evaluate the key definition for [{key_text}]. "
                "Try adding columns to form a composite key."
            )
        elif pattern == "null_key_contamination":
            actions.append(
                f"Fix null keys in [{key_text}] first — dedup without fixing nulls will silently drop rows."
            )
        else:
            actions.append(
                "MUST: Add a deterministic tie-breaker before deduplication, "
                "such as latest effective timestamp, source priority, or ingestion timestamp.",
            )

        actions.append(
            "MUST: Run this check before diff_tables_by_key, dimension builds, and Delta merge operations.",
        )
        if not include_duplicate_rows:
            actions.append(
                "MUST: Re-run with include_duplicate_rows=True if row-level "
                "examples are needed for debugging."
            )

    if has_null_keys:
        actions.append(
            f"MUST: Quarantine, backfill, or explicitly filter rows with null "
            f"key values in [{key_text}] before merge/diff logic."
        )

    if not actions:
        actions.append("Proceed with merge, diff, or validation steps that require this grain.")
        actions.append("MUST: Consider adding duplicate_key_context as a pre-write quality gate for this dataset.")

    # anchor() workflow hints
    if has_duplicates:
        actions.append(
            "MUST: Run anchor('transform', explore_ctx) to auto-generate a dedup plan, "
            "or anchor('apply_transform', df, plan_ctx) if you already have one."
        )
    actions.append(
        "MUST: Run anchor('quality', df, subject='...', keys=[...]) after resolving "
        "duplicates to confirm write-safety."
    )
    actions.append("SKILL: Load skills/data-reconciliation/SKILL.md for grain and deduplication controls.")
    return actions


def _build_summary(
    *,
    subject: str,
    keys: list[str],
    row_count: int,
    duplicate_key_count: int,
    duplicate_row_count: int,
    null_key_row_count: int,
    classification: dict[str, str],
) -> str:
    """Build a short natural-language summary of duplicate analysis."""
    key_text = ", ".join(keys)
    interpretation = classification.get("interpretation", "")

    if row_count == 0:
        return f"{subject}: no rows to validate for key(s) [{key_text}]."

    if duplicate_key_count == 0 and null_key_row_count == 0:
        return f"{subject}: no duplicate keys found across {row_count:,} row(s) for key(s) [{key_text}]."

    if duplicate_key_count == 0:
        return (
            f"{subject}: no duplicate keys found, but {null_key_row_count:,} row(s) have null key values "
            f"for key(s) [{key_text}]."
        )

    null_note = f" Also found {null_key_row_count:,} null-key row(s)." if null_key_row_count else ""
    interp_note = f" {interpretation}" if interpretation else ""
    return (
        f"{subject}: found {duplicate_key_count:,} duplicate key group(s) affecting "
        f"{duplicate_row_count:,} of {row_count:,} row(s) for key(s) [{key_text}].{null_note}{interp_note}"
    )


def _rate(numerator: int, denominator: int) -> float:
    """Compute a safe ratio, returning 0.0 if denominator is zero."""
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


def _json_safe_context(context: dict[str, Any]) -> dict[str, Any]:
    """Make a context dict JSON-serializable by cleaning values."""
    return {key: _json_safe_value(value) for key, value in context.items()}


def _json_safe_record(record: dict[str, Any]) -> dict[str, Any]:
    """Make a single record dict JSON-serializable."""
    return {str(key): _json_safe_value(value) for key, value in record.items()}


def _json_safe_value(value: Any) -> Any:
    """Convert a single value to a JSON-safe representation."""
    if _is_missing(value):
        return None

    if isinstance(value, dict):
        return {str(key): _json_safe_value(inner_value) for key, inner_value in value.items()}

    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]

    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]

    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, np.generic):
        return _json_safe_value(value.item())

    return value


def _is_missing(value: Any) -> bool:
    """Check if a value is considered missing (None, NaN, NaT, empty)."""
    if value is None:
        return True

    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False

    if isinstance(result, (bool, np.bool_)):
        return bool(result)

    return False



def render_duplicate_key_report(ctx: dict) -> str:
    """Render a duplicate key context dict as structured markdown.

    Args:
        ctx: Dictionary from ``duplicate_key_context()``.

    Returns:
        Markdown-formatted string.

    Raises:
        ValueError: If ctx is missing required keys.
    """
    required = {"kind", "subject", "summary", "metrics"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    m = ctx["metrics"]

    icon = "\u2705" if ctx.get("passed") else "\u274c"
    lines.append(f"# Duplicate Key Check: {ctx['subject']}")
    lines.append("")
    lines.append(f"{icon} **{'UNIQUE' if ctx.get('passed') else 'DUPLICATES FOUND'}**")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Metrics table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total rows | {m['row_count']:,} |")
    lines.append(f"| Key columns | {', '.join(f'`{c}`' for c in m['key_columns'])} |")
    lines.append(f"| Unique keys | {m['unique_key_count']:,} |")
    lines.append(f"| Duplicate key groups | {m['duplicate_key_count']:,} |")
    lines.append(f"| Duplicate rows | {m['duplicate_row_count']:,} |")
    lines.append(f"| Excess rows (removable) | {m['excess_duplicate_row_count']:,} |")
    dup_rate = m.get("duplicate_key_rate", 0)
    lines.append(f"| Duplicate key rate | {dup_rate:.1%} |")
    lines.append(f"| Max rows per key | {m['max_rows_per_key']} |")
    null_rows = m.get("null_key_row_count", 0)
    if null_rows > 0:
        lines.append(f"| Null key rows | {null_rows:,} |")
    lines.append("")

    # Top duplicate key samples
    raw_samples = ctx.get("samples")
    samples = (
        raw_samples.get("duplicate_keys", []) if isinstance(raw_samples, dict)
        else raw_samples or []
    )
    if samples:
        lines.append("## Top Duplicate Keys")
        lines.append("")
        lines.append("| Key | Row Count | Excess |")
        lines.append("| --- | --- | --- |")
        for s in samples[:10]:
            key_str = ", ".join(f"{k}={v}" for k, v in s.get("key", {}).items())
            lines.append(f"| {key_str} | {s.get('row_count', '?')} | {s.get('excess_row_count', '?')} |")
        if len(samples) > 10:
            lines.append(f"| ... | ({len(samples) - 10} more) | |")
        lines.append("")

    # Null key info
    if ctx.get("has_null_keys"):
        lines.append("## Null Key Warning")
        lines.append("")
        null_counts = m.get("null_counts_by_key") or {}
        for col, count in null_counts.items():
            if count > 0:
                lines.append(f"- `{col}`: {count:,} null rows")
        lines.append("")

    # Risks
    risks = ctx.get("risks") or []
    if risks:
        lines.append("## Risks")
        lines.append("")
        for r in risks:
            if isinstance(r, str):
                lines.append(f"- \u26a0\ufe0f {r}")
            elif isinstance(r, dict):
                lines.append(f"- \u26a0\ufe0f {r.get('message', r)}")
        lines.append("")

    # Suggested next actions
    actions = ctx.get("suggested_next_actions") or []
    if actions:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for i, action in enumerate(actions, 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    return "\n".join(lines)
