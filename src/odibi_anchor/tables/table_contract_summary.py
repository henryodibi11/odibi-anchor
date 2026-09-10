"""Table contract summaries for humans and AI.

This module builds a compact, structured table card from an in-memory
DataFrame. The production implementation in this file is pandas-first. The
public API keeps simple engine dispatch so Spark support can be implemented in
Databricks without changing call sites.

The tool follows the odibi_anchor context-generator pattern:

    messy enterprise table -> structured context -> compressed signal -> action

Example:
    >>> import pandas as pd
    >>> from odibi_anchor.tables import table_contract_summary
    >>> df = pd.DataFrame({
    ...     "asset_id": ["A1", "A2"],
    ...     "updated_at": pd.to_datetime(["2026-05-01", "2026-05-02"]),
    ...     "capacity_mw": [100.5, 250.0],
    ... })
    >>> ctx = table_contract_summary(
    ...     df,
    ...     subject="demo.assets",
    ...     candidate_key_columns=["asset_id"],
    ...     reference_time="2026-05-07T00:00:00Z",
    ... )
    >>> ctx["kind"]
    'table_contract_summary'
"""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from odibi_anchor._utils.engine_utils import detect_engine
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

# ── Candidate key detection thresholds ─────────────────────────────────
_KEY_MAX_NULL_RATE = 0.01  # Max null fraction for key candidacy
_KEY_MIN_UNIQUE_RATE = 0.98  # Min uniqueness ratio for key candidacy

_FRESHNESS_NAME_HINTS = {
    "asof",
    "as_of",
    "created",
    "creation",
    "date",
    "datetime",
    "effective",
    "event",
    "extract",
    "ingest",
    "ingestion",
    "load",
    "loaded",
    "modified",
    "snapshot",
    "time",
    "timestamp",
    "updated",
}

_IDENTIFIER_NAME_HINTS = {"id", "key", "code", "number", "num"}
_FLAG_NAME_HINTS = {"active", "enabled", "flag", "has", "is", "valid"}
_TEXT_PREVIEW_LIMIT = 80
_TOP_VALUES_DISPLAY_CAP = 40  # Truncate top_vals display beyond this
_COLUMN_SUMMARY_CAP = 10  # Truncate column summary beyond this
_TOP_VALUE_LIMIT = 3
_FRESHNESS_PARSE_SUCCESS_THRESHOLD = 0.50


def table_contract_summary(
    df: Any,
    *,
    subject: str = "dataframe",
    candidate_key_columns: Sequence[str] | None = None,
    freshness_columns: Sequence[str] | None = None,
    profile_columns: Sequence[str] | None = None,
    sample_columns: Sequence[str] | None = None,
    sample_limit: int = 10,
    max_profile_columns: int = 50,
    high_null_rate_threshold: float = 0.50,
    stale_after_days: float | None = None,
    reference_time: str | datetime | pd.Timestamp | None = None,
    include_value_examples: bool = True,
    engine: str = "auto",
    spark_sample_size: int = 5000,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Generate a compact table contract summary for a DataFrame.

    The summary answers the first questions an engineer or LLM needs before
    working with an unfamiliar dataset: what shape is it, what columns does it
    contain, what is the likely grain, is there a freshness signal, and what
    risks should be checked before using it downstream.

    Args:
        df: Pandas or Spark DataFrame. Both engines are implemented. The Spark
            path computes exact metrics (row count, schema, null profile, key
            checks) from the full table and profiles column detail from a capped
            sample (``spark_sample_size``), producing the same output contract.
        subject: Human-readable label for the table or DataFrame, such as
            ``"catalog.schema.table"`` or ``"asset_measurements_df"``.
        candidate_key_columns: Optional columns expected to define the business
            grain. When provided, the function checks null-key and duplicate-key
            risk.
        freshness_columns: Optional date/timestamp columns to evaluate for
            freshness. When omitted, timestamp-like columns are detected from
            dtype and column names.
        profile_columns: Optional explicit columns to profile. When omitted,
            columns are profiled from left to right up to ``max_profile_columns``.
        sample_columns: Optional explicit columns to include in sample rows.
            When omitted, sample rows are limited to the profiled columns to
            keep output compact for wide tables.
        sample_limit: Maximum sample rows and duplicate/null-key examples to
            include in the output.
        max_profile_columns: Maximum columns to include in the detailed column
            profile. This keeps wide-table output compact enough for notebooks
            and LLM prompts.
        high_null_rate_threshold: Null-rate threshold used to flag high-null
            column risks. Must be between 0 and 1.
        stale_after_days: Optional threshold for flagging stale freshness
            values. If supplied, the latest detected freshness value is compared
            to ``reference_time`` or the current UTC time.
        reference_time: Optional timestamp used for deterministic freshness age
            calculations. Useful in tests, notebooks, and scheduled checks.
        include_value_examples: Whether to include capped top-value examples per
            profiled column.
        engine: Execution engine. Use ``"auto"``, ``"pandas"``, or ``"spark"``.

    Returns:
        Structured context dictionary with table shape, schema, column profile,
        candidate key checks, freshness checks, findings, risks, capped samples,
        and suggested next actions.

    Raises:
        TypeError: If the input is not a supported DataFrame type or configured
            columns are not strings.
        ValueError: If configuration values are invalid or requested columns are
            missing.
    """
    validate_output_format(output_format)

    resolved_engine = (
        detect_engine(df) if engine == "auto" else engine.lower()
    )

    if resolved_engine == "pandas":
        result = _table_contract_summary_pandas(
            df,
            subject=subject,
            candidate_key_columns=candidate_key_columns,
            freshness_columns=freshness_columns,
            profile_columns=profile_columns,
            sample_columns=sample_columns,
            sample_limit=sample_limit,
            max_profile_columns=max_profile_columns,
            high_null_rate_threshold=high_null_rate_threshold,
            stale_after_days=stale_after_days,
            reference_time=reference_time,
            include_value_examples=include_value_examples,
        )
    elif resolved_engine == "spark":
        result = _table_contract_summary_spark(
            df,
            subject=subject,
            candidate_key_columns=candidate_key_columns,
            freshness_columns=freshness_columns,
            profile_columns=profile_columns,
            sample_columns=sample_columns,
            sample_limit=sample_limit,
            max_profile_columns=max_profile_columns,
            high_null_rate_threshold=high_null_rate_threshold,
            stale_after_days=stale_after_days,
            reference_time=reference_time,
            include_value_examples=include_value_examples,
            spark_sample_size=spark_sample_size,
        )
    elif resolved_engine == "unknown":
        raise TypeError(
            "Expected pandas or Spark DataFrame,"
            f" got {type(df).__name__}"
        )
    else:
        raise ValueError(
            "engine must be one of: 'auto', 'pandas', or 'spark'"
        )

    if output_format == "markdown":
        return render_contract_report(result)
    return result


def render_contract_report(
    ctx: dict[str, Any],
    *,
    show_samples: bool = False,
) -> str:
    """Render table_contract_summary output as human-readable markdown.

    Converts the structured context dictionary into a formatted markdown
    string. Useful for notebook display (via ``display(Markdown(...))``)
    or direct inclusion in LLM prompts as token-efficient context.

    Args:
        ctx: Output dictionary from ``table_contract_summary()``.
        show_samples: Whether to include sample rows at the end.
            Defaults to False for compact output.

    Returns:
        Markdown-formatted string.

    Example::

        >>> from IPython.display import display, Markdown
        >>> ctx = table_contract_summary(df, subject="my_table")
        >>> display(Markdown(render_contract_report(ctx)))
    """
    lines: list[str] = []
    m = ctx["metrics"]

    # ─── Header ───
    lines.append(f"# {ctx['subject']}")
    lines.append("")
    lines.append(f"> {ctx['summary']}")
    lines.append("")

    # ─── Overview table ───
    lines.append("## Overview")
    lines.append("")
    lines.append(
        "| Rows | Columns | Profiled"
        " | High-Null Cols | All-Null Cols | Memory |"
    )
    lines.append("| ---: | ---: | ---: | ---: | ---: | --- |")
    mem = m.get("memory_bytes")
    if mem and mem < 1_048_576:
        mem_str = f"{mem / 1024:.0f} KB"
    elif mem:
        mem_str = f"{mem / 1_048_576:.1f} MB"
    else:
        mem_str = "N/A (Spark)"
    lines.append(
        f"| {m['row_count']:,} | {m['column_count']}"
        f" | {m['profiled_column_count']}"
        f" | {m['high_null_column_count']}"
        f" | {m['all_null_column_count']} | {mem_str} |"
    )
    lines.append("")

    # Engine annotation for Spark
    if m.get("source_engine") == "spark":
        sample_sz = m.get("spark_sample_size", "N/A")
        lines.append(
            f"*Engine: Spark (exact metrics from full table,"
            f" profiling sampled at {sample_sz:,} rows)*"
        )
        lines.append("")

    # ─── Candidate Key ───
    if ctx.get("candidate_keys", {}).get("provided_key"):
        pk = ctx["candidate_keys"]["provided_key"]
        lines.append("## Candidate Key")
        lines.append("")

        status_icons = {
            "unique": "PASS",
            "duplicate_keys_found": "FAIL",
            "null_keys_found": "WARN",
            "null_and_duplicate_keys_found": "FAIL",
        }
        cols_str = "`, `".join(pk["columns"])
        icon = status_icons.get(pk["status"], "?")
        lines.append(f"**Columns:** `{cols_str}`  ")
        lines.append(f"**Status:** {icon} — `{pk['status']}`")
        lines.append("")

        if not pk["is_unique"]:
            dup_rate = pk.get("duplicate_key_rate", 0)
            null_rate = pk.get("null_key_rate", 0)
            lines.append("| Metric | Count | Rate |")
            lines.append("| --- | ---: | ---: |")
            lines.append(
                f"| Duplicate key rows"
                f" | {pk['duplicate_key_row_count']:,}"
                f" | {dup_rate:.1%} |"
            )
            lines.append(
                f"| Duplicate key groups"
                f" | {pk['duplicate_key_group_count']:,} | — |"
            )
            lines.append(
                f"| Null key rows"
                f" | {pk['null_key_row_count']:,}"
                f" | {null_rate:.1%} |"
            )
            lines.append("")

            if pk.get("duplicate_key_samples"):
                samples = ", ".join(
                    str(s) for s in pk["duplicate_key_samples"][:3]
                )
                lines.append(f"**Sample duplicates:** {samples}")
                lines.append("")

    # ─── Freshness ───
    fr = ctx.get("freshness", {})
    if fr.get("selected_column"):
        lines.append("## Freshness")
        lines.append("")
        lines.append(
            "| Column | Latest Value | Age (days)"
            " | Threshold | Status |"
        )
        lines.append("| --- | --- | ---: | ---: | --- |")
        threshold = fr.get("stale_after_days", "—")
        is_stale = fr.get("is_stale")
        if is_stale is True:
            status = "STALE"
        elif is_stale is False:
            status = "Fresh"
        else:
            status = "—"
        lines.append(
            f"| `{fr['selected_column']}`"
            f" | {fr['latest_value']}"
            f" | {fr['latest_age_days']:.1f}"
            f" | {threshold} | {status} |"
        )
        lines.append("")

    # ─── Column Profile grid ───
    lines.append("## Column Profile")
    lines.append("")
    lines.append(
        "| # | Column | Type | Role"
        " | Null % | Distinct | Top Values |"
    )
    lines.append(
        "| ---: | --- | --- | ---"
        " | ---: | ---: | --- |"
    )
    for i, col in enumerate(ctx["profile"]["columns"], 1):
        null_pct = f"{col['null_rate']:.0%}"
        top_vals = ""
        if col.get("top_values"):
            top_vals = ", ".join(
                str(tv["value"]) for tv in col["top_values"][:3]
            )
            if len(top_vals) > _TOP_VALUES_DISPLAY_CAP:
                top_vals = top_vals[:37] + "..."
        role = col.get("likely_role", "")
        lines.append(
            f"| {i} | `{col['name']}` | {col['dtype']}"
            f" | {role} | {null_pct}"
            f" | {col['distinct_count']} | {top_vals} |"
        )
    lines.append("")

    if ctx["profile"].get("omitted_columns"):
        n = len(ctx["profile"]["omitted_columns"])
        lines.append(
            f"*{n} columns omitted from profiling"
            " (max_profile_columns limit)*"
        )
        lines.append("")

    # ─── Risks ───
    if ctx["risks"]:
        lines.append("## Risks")
        lines.append("")
        for r in ctx["risks"]:
            sev = r["severity"].upper()
            lines.append(f"- **{sev}** — {r['message']}")
        lines.append("")

    # ─── Suggested Actions ───
    if ctx.get("suggested_next_actions"):
        lines.append("## Suggested Next Actions")
        lines.append("")
        for i, action in enumerate(ctx["suggested_next_actions"], 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    # ─── Sample Rows (optional) ───
    if show_samples and ctx.get("samples"):
        lines.append("## Sample Rows")
        lines.append("")
        cols = list(ctx["samples"][0].keys())
        display_cols = cols[:10]
        lines.append("| " + " | ".join(display_cols) + " |")
        lines.append(
            "| " + " | ".join(["---"] * len(display_cols)) + " |"
        )
        for row in ctx["samples"][:5]:
            vals = [str(row.get(c, ""))[:30] for c in display_cols]
            lines.append("| " + " | ".join(vals) + " |")
        lines.append("")
        if len(cols) > _COLUMN_SUMMARY_CAP:
            lines.append(
                f"*{len(cols) - 10} additional columns not shown*"
            )
            lines.append("")

    return "\n".join(lines)


def _table_contract_summary_pandas(
    df: pd.DataFrame,
    *,
    subject: str,
    candidate_key_columns: Sequence[str] | None,
    freshness_columns: Sequence[str] | None,
    profile_columns: Sequence[str] | None,
    sample_columns: Sequence[str] | None,
    sample_limit: int,
    max_profile_columns: int,
    high_null_rate_threshold: float,
    stale_after_days: float | None,
    reference_time: str | datetime | pd.Timestamp | None,
    include_value_examples: bool,
) -> dict[str, Any]:
    """Pandas implementation of table_contract_summary.

    Computes all metrics, profiling, key checks, freshness,
    findings, risks, samples, and summary from an in-memory
    pandas DataFrame.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"Expected pandas DataFrame, got {type(df).__name__}")

    _validate_limits(
        sample_limit=sample_limit,
        max_profile_columns=max_profile_columns,
        high_null_rate_threshold=high_null_rate_threshold,
        stale_after_days=stale_after_days,
    )

    columns = list(df.columns)
    _validate_dataframe_columns(columns)

    candidate_keys = _normalize_optional_columns(candidate_key_columns, field_name="candidate_key_columns")
    explicit_freshness_columns = _normalize_optional_columns(freshness_columns, field_name="freshness_columns")
    explicit_profile_columns = _normalize_optional_columns(profile_columns, field_name="profile_columns")
    explicit_sample_columns = _normalize_optional_columns(sample_columns, field_name="sample_columns")

    _validate_columns_exist(columns, candidate_keys, field_name="candidate_key_columns")
    _validate_columns_exist(columns, explicit_freshness_columns, field_name="freshness_columns")
    _validate_columns_exist(columns, explicit_profile_columns, field_name="profile_columns")
    _validate_columns_exist(columns, explicit_sample_columns, field_name="sample_columns")

    row_count = int(len(df))
    column_count = int(len(columns))
    profile_target_columns = explicit_profile_columns if explicit_profile_columns else columns
    profiled_columns = profile_target_columns[:max_profile_columns]
    omitted_profile_columns = profile_target_columns[max_profile_columns:]
    unprofiled_table_columns = [column for column in columns if column not in profile_target_columns]
    sample_target_columns = explicit_sample_columns if explicit_sample_columns else profiled_columns

    schema = _build_schema(df, profiled_columns)
    profile_column_records = [
        _build_column_profile(
            df,
            column,
            row_count=row_count,
            include_value_examples=include_value_examples,
        )
        for column in profiled_columns
    ]

    key_context = _build_key_context(
        df,
        candidate_key_columns=candidate_keys,
        profile_columns=profile_column_records,
        sample_limit=sample_limit,
    )
    freshness_context = _build_freshness_context(
        df,
        freshness_columns=explicit_freshness_columns,
        reference_time=reference_time,
        stale_after_days=stale_after_days,
    )

    findings: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    suggested_next_actions: list[str] = []

    metrics = _build_metrics(
        df=df,
        row_count=row_count,
        column_count=column_count,
        profile_columns=profile_column_records,
        profile_target_columns=profile_target_columns,
        omitted_profile_columns=omitted_profile_columns,
        unprofiled_table_columns=unprofiled_table_columns,
        sample_target_columns=sample_target_columns,
        key_context=key_context,
        freshness_context=freshness_context,
        sample_limit=sample_limit,
        high_null_rate_threshold=high_null_rate_threshold,
    )

    _add_shape_findings(
        subject=subject,
        row_count=row_count,
        column_count=column_count,
        omitted_profile_columns=omitted_profile_columns,
        unprofiled_table_columns=unprofiled_table_columns,
        findings=findings,
        risks=risks,
        suggested_next_actions=suggested_next_actions,
    )
    _add_key_findings(
        key_context=key_context,
        findings=findings,
        risks=risks,
        suggested_next_actions=suggested_next_actions,
    )
    _add_freshness_findings(
        freshness_context=freshness_context,
        findings=findings,
        risks=risks,
        suggested_next_actions=suggested_next_actions,
    )
    _add_null_findings(
        profile_columns=profile_column_records,
        high_null_rate_threshold=high_null_rate_threshold,
        findings=findings,
        risks=risks,
        suggested_next_actions=suggested_next_actions,
    )
    _add_constant_column_findings(
        profile_columns=profile_column_records,
        findings=findings,
    )

    samples = _build_samples(df, sample_columns=sample_target_columns, sample_limit=sample_limit)
    summary = _build_summary(
        subject=subject,
        row_count=row_count,
        column_count=column_count,
        key_context=key_context,
        freshness_context=freshness_context,
        risk_count=len(risks),
    )

    return {
        "kind": "table_contract_summary",
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "schema": schema,
        "profile": {
            "columns": profile_column_records,
            "profiled_columns": list(profiled_columns),
            "omitted_columns": list(omitted_profile_columns),
            "omitted_column_count": len(omitted_profile_columns),
            "unprofiled_table_columns": list(unprofiled_table_columns),
            "selection_mode": "explicit" if explicit_profile_columns else "first_n",
        },
        "candidate_keys": key_context,
        "freshness": freshness_context,
        "findings": findings,
        "risks": risks,
        "samples": {"data_preview": samples} if samples else {},
        "suggested_next_actions": _dedupe_strings(
            suggested_next_actions + _build_cw_workflow_hints(risks, key_context)
        ),
    }


def _build_cw_workflow_hints(
    risks: list[dict[str, Any]],
    key_context: dict[str, Any],
) -> list[str]:
    """Generate anchor() workflow hints based on contract analysis."""
    hints: list[str] = []
    has_key_issues = not key_context.get("is_unique", True)
    has_high_risk = any(r.get("severity") == "high" for r in risks)

    if has_key_issues:
        hints.append(
            "MUST: Run anchor('duplicate', df, ['key_col']) to inspect "
            "duplicate business keys before downstream writes."
        )
    if has_high_risk:
        hints.append(
            "MUST: Resolve high-severity risks before using this table "
            "as a trusted contract. Run anchor('quality', df, subject='...', "
            "keys=[...]) after fixes."
        )
    hints.append(
        "MUST: Use this contract output as evidence in "
        "anchor('task', ..., known_facts=[...]) for downstream work."
    )
    return hints


def _table_contract_summary_spark(
    df: Any,
    *,
    subject: str,
    candidate_key_columns: Sequence[str] | None,
    freshness_columns: Sequence[str] | None,
    profile_columns: Sequence[str] | None,
    sample_columns: Sequence[str] | None,
    sample_limit: int,
    max_profile_columns: int,
    high_null_rate_threshold: float,
    stale_after_days: float | None,
    reference_time: str | datetime | pd.Timestamp | None,
    include_value_examples: bool,
    spark_sample_size: int,
) -> dict[str, Any]:
    """Hybrid Spark path: exact metrics from full table, sample for profiling."""
    from pyspark.sql import functions as F

    # --- Exact metrics from the full Spark DataFrame ---
    spark_row_count = df.count()
    columns = [field.name for field in df.schema.fields]

    # Determine which columns to profile (same logic as pandas path)
    candidate_keys = _normalize_optional_columns(candidate_key_columns, field_name="candidate_key_columns")
    explicit_profile_columns = _normalize_optional_columns(profile_columns, field_name="profile_columns")
    _validate_columns_exist(columns, candidate_keys, field_name="candidate_key_columns")
    _validate_columns_exist(columns, explicit_profile_columns, field_name="profile_columns")

    profile_target_columns = explicit_profile_columns if explicit_profile_columns else columns
    profiled_columns = profile_target_columns[:max_profile_columns]

    # Exact null counts for all profiled columns (single Spark aggregation)
    spark_null_counts: dict[str, int] = {}
    if spark_row_count > 0 and profiled_columns:
        null_exprs = [
            F.sum(F.col(c).isNull().cast("int")).alias(c)
            for c in profiled_columns
        ]
        null_row = df.select(null_exprs).collect()[0]
        spark_null_counts = {c: int(null_row[c] or 0) for c in profiled_columns}

    # Exact candidate key metrics from full table
    spark_key_context: dict[str, Any] | None = None
    if candidate_keys and spark_row_count > 0:
        spark_key_context = _compute_spark_key_metrics(
            df, candidate_keys, spark_row_count, sample_limit
        )

    # --- Sample to pandas for profiling ---
    sample_pdf = df.limit(spark_sample_size).toPandas()

    # Run the full pandas path on the sample
    result = _table_contract_summary_pandas(
        sample_pdf,
        subject=subject,
        candidate_key_columns=candidate_key_columns,
        freshness_columns=freshness_columns,
        profile_columns=profile_columns,
        sample_columns=sample_columns,
        sample_limit=sample_limit,
        max_profile_columns=max_profile_columns,
        high_null_rate_threshold=high_null_rate_threshold,
        stale_after_days=stale_after_days,
        reference_time=reference_time,
        include_value_examples=include_value_examples,
    )

    # --- Patch in exact Spark values ---
    result["metrics"]["row_count"] = spark_row_count
    result["metrics"]["source_engine"] = "spark"
    result["metrics"]["spark_sample_size"] = min(spark_sample_size, spark_row_count)
    result["metrics"].pop("memory_bytes", None)

    # Override null counts and rates with exact values
    for col_profile in result["profile"]["columns"]:
        col_name = col_profile["name"]
        if col_name in spark_null_counts:
            exact_null = spark_null_counts[col_name]
            col_profile["null_count"] = exact_null
            col_profile["null_rate"] = _round_rate(_safe_rate(exact_null, spark_row_count))
            col_profile["non_null_count"] = spark_row_count - exact_null
            col_profile["is_all_null"] = spark_row_count > 0 and exact_null == spark_row_count

    # Recalculate null-based metrics
    high_null_cols = [c for c in result["profile"]["columns"] if c["null_rate"] >= high_null_rate_threshold]
    all_null_cols = [c for c in result["profile"]["columns"] if c["is_all_null"]]
    result["metrics"]["high_null_column_count"] = len(high_null_cols)
    result["metrics"]["all_null_column_count"] = len(all_null_cols)

    # Override key context with exact Spark values
    if spark_key_context is not None:
        result["candidate_keys"]["provided_key"] = spark_key_context
        result["metrics"]["provided_key_is_unique"] = spark_key_context["is_unique"]
        result["metrics"]["provided_key_duplicate_row_count"] = spark_key_context["duplicate_key_row_count"]
        result["metrics"]["provided_key_null_row_count"] = spark_key_context["null_key_row_count"]

    # Rebuild findings/risks with exact values
    result["findings"] = []
    result["risks"] = []
    suggested_next_actions: list[str] = []
    _add_shape_findings(
        subject=subject, row_count=spark_row_count,
        column_count=result["metrics"]["column_count"],
        omitted_profile_columns=result["profile"]["omitted_columns"],
        unprofiled_table_columns=result["profile"]["unprofiled_table_columns"],
        findings=result["findings"], risks=result["risks"],
        suggested_next_actions=suggested_next_actions,
    )
    _add_key_findings(
        key_context=result["candidate_keys"],
        findings=result["findings"], risks=result["risks"],
        suggested_next_actions=suggested_next_actions,
    )
    _add_freshness_findings(
        freshness_context=result["freshness"],
        findings=result["findings"], risks=result["risks"],
        suggested_next_actions=suggested_next_actions,
    )
    _add_null_findings(
        profile_columns=result["profile"]["columns"],
        high_null_rate_threshold=high_null_rate_threshold,
        findings=result["findings"], risks=result["risks"],
        suggested_next_actions=suggested_next_actions,
    )
    _add_constant_column_findings(
        profile_columns=result["profile"]["columns"],
        findings=result["findings"],
    )
    result["suggested_next_actions"] = _dedupe_strings(suggested_next_actions)

    # Rebuild summary with exact row count
    result["summary"] = _build_summary(
        subject=subject, row_count=spark_row_count,
        column_count=result["metrics"]["column_count"],
        key_context=result["candidate_keys"],
        freshness_context=result["freshness"],
        risk_count=len(result["risks"]),
    )

    return result


def _compute_spark_key_metrics(
    df: Any,
    candidate_keys: list[str],
    row_count: int,
    sample_limit: int,
) -> dict[str, Any]:
    """Compute exact key uniqueness metrics using Spark aggregations."""
    from pyspark.sql import functions as F

    key_columns = list(candidate_keys)

    # Null key rows: any key column is null
    null_condition = F.lit(False)
    for col in key_columns:
        null_condition = null_condition | F.col(col).isNull()

    null_key_row_count = df.filter(null_condition).count()

    # Duplicate key rows: among non-null keys, find groups with count > 1
    non_null_df = df.filter(~null_condition)
    duplicate_groups = (
        non_null_df
        .groupBy(key_columns)
        .agg(F.count("*").alias("_dk_count"))
        .filter("_dk_count > 1")
    )
    duplicate_key_group_count = duplicate_groups.count()

    # Total rows involved in duplicates
    duplicate_key_row_count = 0
    duplicate_key_samples: list[dict[str, Any]] = []
    if duplicate_key_group_count > 0:
        dup_sum = duplicate_groups.agg(F.sum("_dk_count")).collect()[0][0]
        duplicate_key_row_count = int(dup_sum) if dup_sum else 0
        # Collect sample duplicate keys
        sample_rows = duplicate_groups.select(key_columns).limit(sample_limit).collect()
        duplicate_key_samples = _records_to_jsonable(
            [{col: row[col] for col in key_columns} for row in sample_rows]
        )

    # Null key samples
    null_key_samples: list[dict[str, Any]] = []
    if null_key_row_count > 0:
        null_rows = df.filter(null_condition).select(key_columns).limit(sample_limit).collect()
        null_key_samples = _records_to_jsonable(
            [{col: row[col] for col in key_columns} for row in null_rows]
        )

    is_unique = null_key_row_count == 0 and duplicate_key_row_count == 0
    if row_count == 0:
        status = "empty"
    elif null_key_row_count > 0 and duplicate_key_row_count > 0:
        status = "null_and_duplicate_keys_found"
    elif null_key_row_count > 0:
        status = "null_keys_found"
    elif duplicate_key_row_count > 0:
        status = "duplicate_keys_found"
    else:
        status = "unique"

    return {
        "columns": key_columns,
        "status": status,
        "is_unique": is_unique,
        "null_key_row_count": null_key_row_count,
        "null_key_rate": _round_rate(_safe_rate(null_key_row_count, row_count)),
        "duplicate_key_row_count": duplicate_key_row_count,
        "duplicate_key_rate": _round_rate(_safe_rate(duplicate_key_row_count, row_count)),
        "duplicate_key_group_count": duplicate_key_group_count,
        "duplicate_key_samples": duplicate_key_samples,
        "null_key_samples": null_key_samples,
    }


def _validate_limits(
    *,
    sample_limit: int,
    max_profile_columns: int,
    high_null_rate_threshold: float,
    stale_after_days: float | None,
) -> None:
    """Validate numeric configuration parameters."""
    if sample_limit < 0:
        raise ValueError("sample_limit must be >= 0")
    if max_profile_columns <= 0:
        raise ValueError("max_profile_columns must be > 0")
    if not 0 <= high_null_rate_threshold <= 1:
        raise ValueError("high_null_rate_threshold must be between 0 and 1")
    if stale_after_days is not None and stale_after_days < 0:
        raise ValueError("stale_after_days must be >= 0 when provided")


def _validate_dataframe_columns(columns: Sequence[str]) -> None:
    """Raise TypeError if any column name is not a string."""
    if any(not isinstance(column, str) for column in columns):
        raise TypeError("DataFrame columns must all be strings")
    duplicate_columns = _duplicates(columns)
    if duplicate_columns:
        raise ValueError(f"DataFrame contains duplicate column names: {duplicate_columns}")


def _normalize_optional_columns(columns: Sequence[str] | None, *, field_name: str) -> list[str]:
    """Normalize optional column list: dedupe and validate."""
    if columns is None:
        return []
    normalized = list(columns)
    if any(not isinstance(column, str) for column in normalized):
        raise TypeError(f"{field_name} must contain only strings")
    duplicate_columns = _duplicates(normalized)
    if duplicate_columns:
        raise ValueError(f"{field_name} contains duplicate column names: {duplicate_columns}")
    return normalized


def _duplicates(values: Sequence[str]) -> list[str]:
    """Return list of duplicate values preserving first occurrence."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _validate_columns_exist(all_columns: Sequence[str], requested_columns: Sequence[str], *, field_name: str) -> None:
    """Raise ValueError if requested columns are missing."""
    missing_columns = [column for column in requested_columns if column not in all_columns]
    if missing_columns:
        raise ValueError(f"{field_name} not found in DataFrame: {missing_columns}")


def _build_schema(df: pd.DataFrame, columns: Sequence[str]) -> list[dict[str, Any]]:
    """Build schema section: column name + dtype for each column."""
    return [
        {
            "name": column,
            "dtype": str(df[column].dtype),
        }
        for column in columns
    ]


def _build_column_profile(
    df: pd.DataFrame,
    column: str,
    *,
    row_count: int,
    include_value_examples: bool,
) -> dict[str, Any]:
    """Profile a single column for null rate, distinct count, role, stats.

    Args:
        df: Source pandas DataFrame.
        column: Column name to profile.
        row_count: Total rows (for rate calculations).
        include_value_examples: Whether to include top values.

    Returns:
        Dict with profiling metrics for the column.
    """
    series = df[column]
    null_count = int(series.isna().sum())
    non_null_count = int(row_count - null_count)
    distinct_count = _safe_nunique(series)
    null_rate = _safe_rate(null_count, row_count)
    distinct_rate = _safe_rate(distinct_count, row_count)
    unique_rate_non_null = _safe_rate(distinct_count, non_null_count)

    profile: dict[str, Any] = {
        "name": column,
        "dtype": str(series.dtype),
        "likely_role": _infer_likely_role(column, series, row_count, null_count, distinct_count),
        "null_count": null_count,
        "null_rate": _round_rate(null_rate),
        "non_null_count": non_null_count,
        "distinct_count": distinct_count,
        "distinct_rate": _round_rate(distinct_rate),
        "unique_rate_non_null": _round_rate(unique_rate_non_null),
        "is_all_null": row_count > 0 and null_count == row_count,
        "is_constant": row_count > 0 and non_null_count > 0 and distinct_count == 1,
        "is_unique_non_null": row_count > 0 and non_null_count > 0 and distinct_count == non_null_count,
    }

    numeric_stats = _numeric_stats(series)
    if numeric_stats:
        profile["numeric_stats"] = numeric_stats

    if include_value_examples:
        profile["top_values"] = _top_values(series, limit=_TOP_VALUE_LIMIT)

    return profile


def _safe_nunique(series: pd.Series) -> int:
    """Count distinct values, handling unhashable types."""
    try:
        return int(series.nunique(dropna=True))
    except TypeError:
        return int(series.dropna().map(_safe_repr).nunique(dropna=True))


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    """Compute rate safely, returning 0.0 for zero denominator."""
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _round_rate(value: float) -> float:
    """Round a rate to 4 decimal places."""
    return round(float(value), 4)


def _numeric_stats(series: pd.Series) -> dict[str, Any]:
    """Compute min/max/mean/median/std for a numeric series."""
    if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series):
        return {}

    non_null = series.dropna()
    if non_null.empty:
        return {}

    return {
        "min": _to_jsonable(non_null.min()),
        "max": _to_jsonable(non_null.max()),
        "mean": _round_number(non_null.mean()),
        "median": _round_number(non_null.median()),
    }


def _round_number(value: Any, *, digits: int = 4) -> int | float | None:
    """Round numeric value, preserving int type. None for NaN."""
    value = _to_jsonable(value)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    return value


def _top_values(series: pd.Series, *, limit: int) -> list[dict[str, Any]]:
    """Return top N most frequent values with counts and rates."""
    non_null = series.dropna()
    if non_null.empty or limit <= 0:
        return []

    try:
        counts = non_null.value_counts(dropna=True).head(limit)
        return [
            {"value": _to_jsonable(value), "count": int(count)}
            for value, count in counts.items()
        ]
    except TypeError:
        counts = non_null.map(_safe_repr).value_counts(dropna=True).head(limit)
        return [
            {"value": value, "count": int(count)}
            for value, count in counts.items()
        ]


def _infer_likely_role(
    column: str,
    series: pd.Series,
    row_count: int,
    null_count: int,
    distinct_count: int,
) -> str:
    """Infer semantic role from column name patterns and data.

    Roles: identifier, identifier_candidate,
    freshness_or_event_time, boolean_flag, category,
    measure, text_or_descriptor.
    """
    lower_name = column.lower()
    tokens = _name_tokens(lower_name)
    non_null_count = row_count - null_count
    unique_rate = _safe_rate(distinct_count, non_null_count)

    if _looks_like_freshness_name(column) or pd.api.types.is_datetime64_any_dtype(series):
        return "freshness_or_event_time"
    if tokens & _IDENTIFIER_NAME_HINTS or lower_name.endswith("_id"):
        return "identifier"
    if pd.api.types.is_bool_dtype(series) or (row_count > 0 and distinct_count == 2 and _looks_like_flag_name(column)):
        return "flag"
    if pd.api.types.is_numeric_dtype(series):
        return "measure"
    if non_null_count > 0 and unique_rate == 1.0 and row_count > 1:
        return "identifier_candidate"
    if row_count > 0 and distinct_count <= min(20, max(1, row_count * 0.20)):
        return "category"
    return "text_or_descriptor"


def _build_key_context(
    df: pd.DataFrame,
    *,
    candidate_key_columns: Sequence[str],
    profile_columns: Sequence[dict[str, Any]],
    sample_limit: int,
) -> dict[str, Any]:
    """Evaluate candidate key columns for uniqueness and nulls.

    Args:
        df: Source pandas DataFrame.
        candidate_key_columns: Columns to check as composite key.
        profile_columns: Already-built column profiles.
        sample_limit: Max duplicate/null key samples.

    Returns:
        Dict with provided_key status and inferred_unique_columns.
    """
    row_count = len(df)
    inferred_single_column_keys = [
        column["name"]
        for column in profile_columns
        if row_count > 0
        and column["null_count"] == 0
        and column["distinct_count"] == row_count
        and column["likely_role"] in {"identifier", "identifier_candidate"}
    ]
    near_unique_columns = [
        column["name"]
        for column in profile_columns
        if row_count > 0
        and column["name"] not in inferred_single_column_keys
        and column["likely_role"] in {"identifier", "identifier_candidate"}
        and column["null_rate"] <= _KEY_MAX_NULL_RATE
        and column["unique_rate_non_null"] >= _KEY_MIN_UNIQUE_RATE
    ]

    context: dict[str, Any] = {
        "provided": list(candidate_key_columns),
        "provided_key": None,
        "inferred_single_column_keys": inferred_single_column_keys,
        "near_unique_columns": near_unique_columns,
    }

    if not candidate_key_columns:
        return context

    key_columns = list(candidate_key_columns)
    key_df = df[key_columns]
    null_key_mask = key_df.isna().any(axis=1)
    non_null_key_mask = ~null_key_mask
    duplicate_key_mask = pd.Series(False, index=df.index)
    if non_null_key_mask.any():
        duplicate_key_mask.loc[non_null_key_mask] = df.loc[non_null_key_mask].duplicated(
            subset=key_columns,
            keep=False,
        )

    duplicate_key_rows = df.loc[duplicate_key_mask, key_columns]
    duplicate_key_group_count = 0
    if not duplicate_key_rows.empty:
        duplicate_key_group_count = int(duplicate_key_rows.drop_duplicates().shape[0])

    null_key_row_count = int(null_key_mask.sum())
    duplicate_key_row_count = int(duplicate_key_mask.sum())
    is_unique = row_count > 0 and null_key_row_count == 0 and duplicate_key_row_count == 0
    if row_count == 0:
        status = "empty"
    elif null_key_row_count > 0 and duplicate_key_row_count > 0:
        status = "null_and_duplicate_keys_found"
    elif null_key_row_count > 0:
        status = "null_keys_found"
    elif duplicate_key_row_count > 0:
        status = "duplicate_keys_found"
    else:
        status = "unique"

    context["provided_key"] = {
        "columns": key_columns,
        "status": status,
        "is_unique": is_unique,
        "null_key_row_count": null_key_row_count,
        "null_key_rate": _round_rate(_safe_rate(null_key_row_count, row_count)),
        "duplicate_key_row_count": duplicate_key_row_count,
        "duplicate_key_rate": _round_rate(_safe_rate(duplicate_key_row_count, row_count)),
        "duplicate_key_group_count": duplicate_key_group_count,
        "duplicate_key_samples": _records_to_jsonable(
            duplicate_key_rows.drop_duplicates().head(sample_limit).to_dict(orient="records")
        ),
        "null_key_samples": _records_to_jsonable(
            df.loc[null_key_mask, key_columns].head(sample_limit).to_dict(orient="records")
        ),
    }
    return context


def _build_freshness_context(
    df: pd.DataFrame,
    *,
    freshness_columns: Sequence[str],
    reference_time: str | datetime | pd.Timestamp | None,
    stale_after_days: float | None,
) -> dict[str, Any]:
    """Evaluate timestamp columns for freshness and staleness.

    Args:
        df: Source pandas DataFrame.
        freshness_columns: Explicit columns or empty for auto-detect.
        reference_time: Reference point for age calculation.
        stale_after_days: Threshold for staleness (optional).

    Returns:
        Dict with selected_column, latest_value, age, staleness.
    """
    columns_to_check = list(freshness_columns) if freshness_columns else _detect_freshness_columns(df)
    checked_columns = [_build_single_freshness_profile(df[column], column) for column in columns_to_check]
    usable_columns = [column for column in checked_columns if column["is_usable"]]

    usable_columns.sort(
        key=lambda item: (
            item["parse_success_rate"],
            item.get("max_value") or "",
            item["column"],
        ),
        reverse=True,
    )

    selected = usable_columns[0] if usable_columns else None
    reference_time_value = _coerce_reference_time(reference_time)

    if selected is not None and selected.get("max_value") is not None:
        latest = pd.Timestamp(selected["max_value"])
        if latest.tzinfo is None:
            latest = latest.tz_localize("UTC")
        else:
            latest = latest.tz_convert("UTC")
        latest_age_days = round((reference_time_value - latest).total_seconds() / 86400, 4)
        selected["latest_age_days"] = latest_age_days
        selected["is_stale"] = stale_after_days is not None and latest_age_days > stale_after_days
        selected["is_future_dated"] = latest_age_days < 0
    elif selected is not None:
        selected["latest_age_days"] = None
        selected["is_stale"] = False
        selected["is_future_dated"] = False

    return {
        "columns_checked": checked_columns,
        "selected_column": selected["column"] if selected else None,
        "latest_value": selected.get("max_value") if selected else None,
        "latest_age_days": selected.get("latest_age_days") if selected else None,
        "stale_after_days": stale_after_days,
        "is_stale": bool(selected.get("is_stale")) if selected else False,
        "is_future_dated": bool(selected.get("is_future_dated")) if selected else False,
        "reference_time": _timestamp_to_iso(reference_time_value),
    }


def _detect_freshness_columns(df: pd.DataFrame) -> list[str]:
    """Auto-detect timestamp columns suitable for freshness."""
    detected: list[str] = []
    for column in df.columns:
        series = df[column]
        if pd.api.types.is_datetime64_any_dtype(series) or _looks_like_freshness_name(column):
            detected.append(column)
    return detected


def _looks_like_flag_name(column: str) -> bool:
    """True if column name suggests a boolean flag."""
    lower_name = column.lower()
    tokens = _name_tokens(lower_name)
    return bool(tokens & _FLAG_NAME_HINTS or lower_name.startswith(("is_", "has_")) or lower_name.endswith("_flag"))


def _looks_like_freshness_name(column: str) -> bool:
    """True if column name suggests a timestamp column."""
    lower_name = column.lower()
    tokens = _name_tokens(lower_name)
    if tokens & _FRESHNESS_NAME_HINTS:
        return True
    return lower_name.endswith("_dt") or lower_name.endswith("_ts") or lower_name in {"dt", "ts"}


def _name_tokens(name: str) -> set[str]:
    """Split column name into lowercase tokens."""
    tokens = {token for token in re.split(r"[^a-z0-9]+", name.lower()) if token}
    joined = name.lower().replace("-", "_")
    if "as_of" in joined:
        tokens.add("as_of")
    return tokens


def _build_single_freshness_profile(series: pd.Series, column: str) -> dict[str, Any]:
    """Parse a single column as timestamp and return metadata."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        parsed = pd.to_datetime(series, errors="coerce", utc=True)
    non_null_count = int(series.notna().sum())
    parsed_count = int(parsed.notna().sum())
    parse_success_rate = _safe_rate(parsed_count, non_null_count)
    is_usable = non_null_count == 0 or parse_success_rate >= _FRESHNESS_PARSE_SUCCESS_THRESHOLD

    min_value = parsed.min() if parsed_count > 0 else None
    max_value = parsed.max() if parsed_count > 0 else None

    return {
        "column": column,
        "dtype": str(series.dtype),
        "non_null_count": non_null_count,
        "parsed_count": parsed_count,
        "parse_success_rate": _round_rate(parse_success_rate),
        "is_usable": is_usable,
        "min_value": _timestamp_to_iso(min_value),
        "max_value": _timestamp_to_iso(max_value),
    }


def _coerce_reference_time(reference_time: str | datetime | pd.Timestamp | None) -> pd.Timestamp:
    """Coerce reference_time to timezone-aware UTC Timestamp."""
    if reference_time is None:
        return pd.Timestamp.now(tz="UTC")

    timestamp = pd.Timestamp(reference_time)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _timestamp_to_iso(value: Any) -> str | None:
    """Convert timestamp-like value to ISO 8601 string."""
    if value is None or pd.isna(value):
        return None
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()


def _build_metrics(
    *,
    df: pd.DataFrame,
    row_count: int,
    column_count: int,
    profile_columns: Sequence[dict[str, Any]],
    profile_target_columns: Sequence[str],
    omitted_profile_columns: Sequence[str],
    unprofiled_table_columns: Sequence[str],
    sample_target_columns: Sequence[str],
    key_context: dict[str, Any],
    freshness_context: dict[str, Any],
    sample_limit: int,
    high_null_rate_threshold: float,
) -> dict[str, Any]:
    """Assemble the top-level metrics dict from components.

    Aggregates row count, column counts, profiling stats,
    null counts, sample metadata, and Spark engine info.
    """
    high_null_columns = [column for column in profile_columns if column["null_rate"] >= high_null_rate_threshold]
    all_null_columns = [column for column in profile_columns if column["is_all_null"]]
    constant_columns = [column for column in profile_columns if column["is_constant"]]
    provided_key = key_context.get("provided_key") or {}
    duplicate_row_count = _safe_duplicate_row_count(df)

    return {
        "row_count": row_count,
        "column_count": column_count,
        "profile_target_column_count": len(profile_target_columns),
        "profiled_column_count": len(profile_columns),
        "omitted_column_count": len(omitted_profile_columns),
        "unprofiled_table_column_count": len(unprofiled_table_columns),
        "sample_row_count": min(sample_limit, row_count),
        "sample_column_count": len(sample_target_columns),
        "all_null_column_count": len(all_null_columns),
        "high_null_column_count": len(high_null_columns),
        "constant_column_count": len(constant_columns),
        "duplicate_row_count": duplicate_row_count,
        "inferred_single_column_key_count": len(key_context.get("inferred_single_column_keys", [])),
        "provided_key_is_unique": provided_key.get("is_unique"),
        "provided_key_duplicate_row_count": provided_key.get("duplicate_key_row_count"),
        "provided_key_null_row_count": provided_key.get("null_key_row_count"),
        "detected_freshness_column_count": len(freshness_context.get("columns_checked", [])),
        "memory_bytes": int(df.memory_usage(deep=True).sum()),
    }


def _safe_duplicate_row_count(df: pd.DataFrame) -> int | None:
    """Count duplicate rows in DataFrame, or None on failure."""
    try:
        return int(df.duplicated(keep=False).sum())
    except TypeError:
        return None


def _add_shape_findings(
    *,
    subject: str,
    row_count: int,
    column_count: int,
    omitted_profile_columns: Sequence[str],
    unprofiled_table_columns: Sequence[str],
    findings: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    suggested_next_actions: list[str],
) -> None:
    """Append shape-related findings to output lists.

    Mutates findings, risks, and suggested_next_actions.
    """
    findings.append(
        {
            "severity": "info",
            "message": f"{subject} has {row_count:,} rows and {column_count:,} columns.",
        }
    )

    if row_count == 0:
        risks.append(
            {
                "severity": "high",
                "message": "The dataset has zero rows, so downstream transformations may produce empty outputs.",
            }
        )
        suggested_next_actions.append(
            "Trace upstream filters, joins, and source"
            " availability before using this dataset."
        )

    if omitted_profile_columns:
        risks.append(
            {
                "severity": "medium",
                "message": (
                    f"{len(omitted_profile_columns)} columns"
                    " were omitted from the detailed profile"
                    " to keep output compact."
                ),
                "columns": list(omitted_profile_columns[:10]),
            }
        )
        suggested_next_actions.append(
            "Increase max_profile_columns or profile"
            " selected columns when reviewing a wide table."
        )

    if unprofiled_table_columns:
        findings.append(
            {
                "severity": "info",
                "message": f"{len(unprofiled_table_columns)} table columns were excluded by profile_columns.",
                "columns": list(unprofiled_table_columns[:10]),
            }
        )


def _add_key_findings(
    *,
    key_context: dict[str, Any],
    findings: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    suggested_next_actions: list[str],
) -> None:
    """Append candidate-key findings to output lists.

    Mutates findings, risks, and suggested_next_actions.
    """
    provided_key = key_context.get("provided_key")
    inferred_keys = key_context.get("inferred_single_column_keys", [])

    if provided_key:
        if provided_key["status"] == "unique":
            findings.append(
                {
                    "severity": "info",
                    "message": f"Provided candidate key is unique: {provided_key['columns']}.",
                    "columns": provided_key["columns"],
                }
            )
        if provided_key["null_key_row_count"] > 0:
            risks.append(
                {
                    "severity": "high",
                    "message": (
                        f"Provided candidate key has"
                        f" {provided_key['null_key_row_count']:,}"
                        " rows with null key values."
                    ),
                    "columns": provided_key["columns"],
                }
            )
            suggested_next_actions.append("Handle null key rows before merges, joins, or uniqueness assertions.")
        if provided_key["duplicate_key_row_count"] > 0:
            risks.append(
                {
                    "severity": "high",
                    "message": (
                        f"Provided candidate key has"
                        f" {provided_key['duplicate_key_row_count']:,}"
                        " duplicate-key rows."
                    ),
                    "columns": provided_key["columns"],
                }
            )
            suggested_next_actions.append(
                "Deduplicate rows or refine the business"
                " key before using this table as a keyed"
                " dataset."
            )
        return

    if inferred_keys:
        findings.append(
            {
                "severity": "info",
                "message": f"Likely single-column key candidates detected: {inferred_keys[:5]}.",
                "columns": inferred_keys[:5],
            }
        )
        suggested_next_actions.append(
            "Confirm the business grain with domain"
            " owners before using inferred key"
            " candidates."
        )
    else:
        risks.append(
            {
                "severity": "medium",
                "message": (
                    "No candidate key was provided and no"
                    " perfect single-column key was detected"
                    " in profiled columns."
                ),
            }
        )
        suggested_next_actions.append(
            "Pass candidate_key_columns when table grain"
            " matters for joins, merges, or validation."
        )


def _add_freshness_findings(
    *,
    freshness_context: dict[str, Any],
    findings: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    suggested_next_actions: list[str],
) -> None:
    """Append freshness findings to output lists.

    Mutates findings, risks, and suggested_next_actions.
    """
    selected_column = freshness_context.get("selected_column")
    latest_value = freshness_context.get("latest_value")
    unusable_columns = [
        column["column"]
        for column in freshness_context.get("columns_checked", [])
        if not column.get("is_usable")
    ]

    if unusable_columns:
        risks.append(
            {
                "severity": "medium",
                "message": f"Freshness-like columns could not be parsed reliably: {unusable_columns[:10]}.",
                "columns": unusable_columns[:10],
            }
        )
        suggested_next_actions.append("Pass a reliable freshness column explicitly or fix timestamp parsing upstream.")

    if selected_column:
        findings.append(
            {
                "severity": "info",
                "message": f"Freshness signal detected from {selected_column}: latest value is {latest_value}.",
                "columns": [selected_column],
            }
        )
        if freshness_context.get("is_stale"):
            risks.append(
                {
                    "severity": "medium",
                    "message": (
                        f"Latest freshness value is {freshness_context.get('latest_age_days')} days old, "
                        f"which exceeds stale_after_days={freshness_context.get('stale_after_days')}."
                    ),
                    "columns": [selected_column],
                }
            )
            suggested_next_actions.append(
                "Check source load health and upstream"
                " schedule if freshness is older than"
                " expected."
            )
        if freshness_context.get("is_future_dated"):
            risks.append(
                {
                    "severity": "medium",
                    "message": (
                        f"Latest freshness value is in the future relative to reference_time "
                        f"({freshness_context.get('reference_time')})."
                    ),
                    "columns": [selected_column],
                }
            )
            suggested_next_actions.append(
                "Check timezone handling or upstream"
                " timestamp generation for future-dated"
                " records."
            )
        return

    risks.append(
        {
            "severity": "medium",
            "message": "No freshness column was detected.",
        }
    )
    suggested_next_actions.append(
        "Pass freshness_columns explicitly if the"
        " table has a reliable load, update, or"
        " event timestamp."
    )


def _add_null_findings(
    *,
    profile_columns: Sequence[dict[str, Any]],
    high_null_rate_threshold: float,
    findings: list[dict[str, Any]],
    risks: list[dict[str, Any]],
    suggested_next_actions: list[str],
) -> None:
    """Append null-related findings to output lists.

    Mutates findings, risks, and suggested_next_actions.
    """
    all_null_columns = [column["name"] for column in profile_columns if column["is_all_null"]]
    high_null_columns = [
        column["name"]
        for column in profile_columns
        if column["null_rate"] >= high_null_rate_threshold and not column["is_all_null"]
    ]

    if all_null_columns:
        risks.append(
            {
                "severity": "high",
                "message": f"All-null columns detected: {all_null_columns[:10]}.",
                "columns": all_null_columns[:10],
            }
        )
        suggested_next_actions.append(
            "Remove, backfill, or explain all-null"
            " columns before treating this as a trusted"
            " table contract."
        )

    if high_null_columns:
        risks.append(
            {
                "severity": "medium",
                "message": (
                    f"Columns at or above the high-null threshold ({high_null_rate_threshold:.0%}) detected: "
                    f"{high_null_columns[:10]}."
                ),
                "columns": high_null_columns[:10],
            }
        )
        suggested_next_actions.append(
            "Review high-null columns for optionality,"
            " source gaps, or transformation defects."
        )

    if not all_null_columns and not high_null_columns:
        findings.append(
            {
                "severity": "info",
                "message": "No high-null or all-null columns were detected in profiled columns.",
            }
        )


def _add_constant_column_findings(
    *,
    profile_columns: Sequence[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> None:
    """Append findings for single-value columns."""
    constant_columns = [column["name"] for column in profile_columns if column["is_constant"]]
    if constant_columns:
        findings.append(
            {
                "severity": "info",
                "message": f"Constant columns detected in profiled columns: {constant_columns[:10]}.",
                "columns": constant_columns[:10],
            }
        )


def _build_samples(
    df: pd.DataFrame,
    *,
    sample_columns: Sequence[str],
    sample_limit: int,
) -> list[dict[str, Any]]:
    """Extract capped sample rows as JSON-safe dicts.

    Args:
        df: Source pandas DataFrame.
        sample_columns: Columns to include in each row.
        sample_limit: Maximum number of rows.

    Returns:
        List of row dicts with JSON-serializable values.
    """
    if sample_limit <= 0 or df.empty:
        return []
    return _records_to_jsonable(df.loc[:, list(sample_columns)].head(sample_limit).to_dict(orient="records"))


def _build_summary(
    *,
    subject: str,
    row_count: int,
    column_count: int,
    key_context: dict[str, Any],
    freshness_context: dict[str, Any],
    risk_count: int,
) -> str:
    """Build one-line natural-language summary of the contract."""
    # Lead with a "so what" verdict synthesized from key + freshness + risks,
    # then the supporting facts. Cautious — these are signals, not guarantees.
    provided_key_v = key_context.get("provided_key")
    key_is_unique = bool(provided_key_v and provided_key_v.get("status") == "unique")
    key_is_confirmed = key_is_unique or bool(provided_key_v)
    has_freshness = bool(freshness_context.get("selected_column"))
    if row_count == 0:
        verdict = "Table is empty — nothing to contract against yet"
    elif not key_is_confirmed and not key_context.get("inferred_single_column_keys"):
        verdict = "Grain looks unconfirmed — verify a key before joining or merging"
    elif key_is_unique and has_freshness and risk_count == 0:
        verdict = "Looks ready to use as a reliable source"
    elif key_is_unique and risk_count == 0:
        verdict = "Grain looks solid; no freshness signal to monitor staleness"
    elif risk_count > 0:
        verdict = f"Usable but {risk_count} risk(s) warrant a look before downstream use"
    else:
        verdict = "Usable; confirm the grain and freshness fit your use"

    parts = [f"{verdict}. {subject} has {row_count:,} rows and {column_count:,} columns"]

    provided_key = key_context.get("provided_key")
    if provided_key and provided_key.get("status") == "unique":
        parts.append(f"provided key {provided_key['columns']} is unique")
    elif provided_key:
        parts.append(f"provided key {provided_key['columns']} status is {provided_key['status']}")
    elif key_context.get("inferred_single_column_keys"):
        parts.append(f"inferred key candidates include {key_context['inferred_single_column_keys'][:3]}")
    else:
        parts.append("no key is confirmed")

    if freshness_context.get("selected_column"):
        parts.append(
            f"latest {freshness_context['selected_column']} is {freshness_context.get('latest_value')}"
        )
    else:
        parts.append("no freshness signal was detected")

    parts.append(f"{risk_count} risk(s) flagged")
    return "; ".join(parts) + "."


def _records_to_jsonable(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert record dicts to JSON-serializable form."""
    return [
        {str(key): _to_jsonable(value) for key, value in record.items()}
        for record in records
    ]


def _to_jsonable(value: Any) -> Any:
    """Convert a single value to a JSON-serializable Python type.

    Handles numpy scalars, NaN/Inf, Decimal, datetime, date,
    Timestamp, bytes, sets, and nested structures.
    """
    if value is None:
        return None

    if isinstance(value, pd.Timestamp):
        return _timestamp_to_iso(value)

    if isinstance(value, pd.Timedelta):
        return str(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if hasattr(value, "item"):
        try:
            return _to_jsonable(value.item())
        except (TypeError, ValueError, OverflowError):
            pass

    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None

    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]

    if isinstance(value, str) and len(value) > _TEXT_PREVIEW_LIMIT:
        return value[: _TEXT_PREVIEW_LIMIT - 3] + "..."

    return value


def _safe_repr(value: Any) -> str:
    """Safe string repr, capped at _TEXT_PREVIEW_LIMIT chars."""
    rendered = repr(_to_jsonable(value))
    if len(rendered) > _TEXT_PREVIEW_LIMIT:
        return rendered[: _TEXT_PREVIEW_LIMIT - 3] + "..."
    return rendered


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    """Deduplicate strings preserving insertion order."""
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped