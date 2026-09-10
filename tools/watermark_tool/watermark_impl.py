"""Watermark debugger tool implementation.

Entry point: watermark_debug_context(source, target, watermark_col=...)
Diagnoses incremental load staleness between source and target tables.
Returns a standard Anchor contract with lag, pending rows, NULL watermarks,
timeline gaps, cadence inference, and suggested next actions.

Dual-engine: Spark for production, Pandas for testing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from odibi_anchor._utils.contract import (
    build_base_context,
    finalize_context,
    validate_output_format,
)
from odibi_anchor._utils.engine_utils import detect_engine, is_pandas_df, is_spark_df
from odibi_anchor._utils.render_utils import (
    render_bullet_section,
    render_header_lines,
    render_metrics_lines,
    render_table,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Helpers — input resolution
# ============================================================================


def _get_spark_session():
    """Get active SparkSession or raise."""
    try:
        from pyspark.sql import SparkSession

        spark = SparkSession.getActiveSession()
        if spark is None:
            raise RuntimeError("No active SparkSession found.")
        return spark
    except ImportError as exc:
        raise RuntimeError(
            "PySpark not available — watermark tool requires Spark for table name inputs."
        ) from exc


def _resolve_input(source_or_target: Any, label: str) -> Any:
    """Resolve a table name string to a DataFrame, or pass through a DataFrame.

    Args:
        source_or_target: Table name string ("catalog.schema.table") or DataFrame.
        label: "source" or "target" for error messages.

    Returns:
        DataFrame (Spark or Pandas).
    """
    if isinstance(source_or_target, str):
        spark = _get_spark_session()
        try:
            return spark.table(source_or_target)
        except Exception as exc:
            raise ValueError(
                f"Cannot read {label} table '{source_or_target}': {exc}. "
                "Check the table name and that you have SELECT permission."
            ) from exc
    if is_spark_df(source_or_target) or is_pandas_df(source_or_target):
        return source_or_target
    raise TypeError(
        f"{label} must be a table name string or DataFrame, got {type(source_or_target).__name__}. "
        "Pass a table name string or a Pandas/Spark DataFrame "
        "— see anchor('help', 'watermark')."
    )


# ============================================================================
# Helpers — statistics computation
# ============================================================================


def _compute_stats_pandas(df, watermark_col: str, lookback_days: int) -> dict:
    """Compute watermark statistics using Pandas operations.

    Returns dict with: max_watermark, null_count, total_count, daily_counts.
    """
    import pandas as pd

    total = len(df)
    null_count = int(df[watermark_col].isna().sum())
    non_null = df[df[watermark_col].notna()]

    if len(non_null) == 0:
        return {
            "max_watermark": None,
            "null_count": null_count,
            "total_count": total,
            "daily_counts": pd.Series(dtype="int64"),
        }

    max_wm = non_null[watermark_col].max()
    # Ensure it's a Python datetime-compatible value
    if hasattr(max_wm, "to_pydatetime"):
        max_wm_dt = max_wm.to_pydatetime()
    else:
        max_wm_dt = max_wm

    # Daily counts for gap detection (within lookback window)
    cutoff = max_wm - pd.Timedelta(days=lookback_days)
    windowed = non_null[non_null[watermark_col] >= cutoff].copy()
    windowed["_wm_date"] = pd.to_datetime(windowed[watermark_col]).dt.date
    daily_counts = windowed.groupby("_wm_date").size()

    return {
        "max_watermark": max_wm_dt,
        "null_count": null_count,
        "total_count": total,
        "daily_counts": daily_counts,
    }


def _compute_stats_spark(df, watermark_col: str, lookback_days: int) -> dict:
    """Compute watermark statistics using Spark operations.

    Returns dict with: max_watermark, null_count, total_count, daily_counts.
    """
    from pyspark.sql import functions as F

    import pandas as pd

    # Aggregations in one pass
    agg_row = df.agg(
        F.max(F.col(watermark_col)).alias("max_wm"),
        F.count(F.when(F.col(watermark_col).isNull(), 1)).alias("null_count"),
        F.count(F.lit(1)).alias("total_count"),
    ).first()

    agg = agg_row.asDict()
    max_wm = agg["max_wm"]
    null_count = int(agg["null_count"])
    total_count = int(agg["total_count"])

    if max_wm is None:
        return {
            "max_watermark": None,
            "null_count": null_count,
            "total_count": total_count,
            "daily_counts": pd.Series(dtype="int64"),
        }

    # Daily counts within lookback window
    cutoff = max_wm - timedelta(days=lookback_days)
    daily_df = (
        df.where(F.col(watermark_col) >= F.lit(cutoff))
        .withColumn("_wm_date", F.to_date(F.col(watermark_col)))
        .groupBy("_wm_date")
        .count()
        .orderBy("_wm_date")
    )
    daily_pd = daily_df.toPandas()
    if len(daily_pd) > 0:
        daily_counts = daily_pd.set_index("_wm_date")["count"]
    else:
        daily_counts = pd.Series(dtype="int64")

    return {
        "max_watermark": max_wm,
        "null_count": null_count,
        "total_count": total_count,
        "daily_counts": daily_counts,
    }


def _count_pending_pandas(source_df, watermark_col: str, target_max) -> int:
    """Count source rows newer than target watermark (Pandas)."""
    if target_max is None:
        return len(source_df)
    return int((source_df[watermark_col] > target_max).sum())


def _count_pending_spark(source_df, watermark_col: str, target_max) -> int:
    """Count source rows newer than target watermark (Spark)."""
    from pyspark.sql import functions as F

    if target_max is None:
        return source_df.count()
    return source_df.where(F.col(watermark_col) > F.lit(target_max)).count()


# ============================================================================
# Helpers — gap detection and cadence
# ============================================================================


def _detect_gaps(daily_counts, lookback_days: int) -> list[dict]:
    """Find missing date ranges in the watermark timeline.

    Args:
        daily_counts: Series indexed by date with row counts.
        lookback_days: Window to analyze.

    Returns:
        List of gap dicts: {"start": date_str, "end": date_str, "days": int}
    """
    import pandas as pd

    if daily_counts is None or len(daily_counts) < 2:
        return []

    # Build complete date range from min to max date in the series
    dates = sorted(daily_counts.index)
    min_date = pd.Timestamp(dates[0])
    max_date = pd.Timestamp(dates[-1])

    if min_date == max_date:
        return []

    full_range: list[pd.Timestamp] = list(pd.date_range(start=min_date, end=max_date, freq="D"))
    present_dates = {pd.Timestamp(d) for d in dates}

    # Find consecutive missing dates
    gaps = []
    gap_start: pd.Timestamp | None = None

    for d in full_range:
        if d not in present_dates:
            if gap_start is None:
                gap_start = d
        else:
            if gap_start is not None:
                gap_end = d - pd.Timedelta(days=1)
                gap_days = (gap_end - gap_start).days + 1
                if gap_days >= 1:
                    gaps.append({
                        "start": str(gap_start.date()),
                        "end": str((d - pd.Timedelta(days=1)).date()),
                        "days": gap_days,
                    })
                gap_start = None

    # Handle trailing gap
    if gap_start is not None:
        gap_end_ts = full_range[-1]
        gap_days = (gap_end_ts - gap_start).days + 1
        gaps.append({
            "start": str(gap_start.date()),
            "end": str(gap_end_ts.date()),
            "days": gap_days,
        })

    return gaps


def _infer_cadence(daily_counts) -> str:
    """Infer load cadence from daily row counts.

    Returns: "hourly", "daily", "weekly", "irregular", or "unknown".
    """
    if daily_counts is None or len(daily_counts) < 2:
        return "unknown"

    import pandas as pd
    import numpy as np

    dates = sorted(daily_counts.index)
    if len(dates) < 2:
        return "unknown"

    # Calculate deltas between consecutive dates
    deltas = []
    for i in range(1, len(dates)):
        d1 = pd.Timestamp(dates[i - 1])
        d2 = pd.Timestamp(dates[i])
        deltas.append((d2 - d1).days)

    if not deltas:
        return "unknown"

    median_delta = float(np.median(deltas))

    if median_delta <= 1:
        return "daily"
    elif median_delta <= 2:
        return "daily"  # mostly daily with occasional gaps
    elif 5 <= median_delta <= 8:
        return "weekly"
    else:
        return "irregular"


# ============================================================================
# Helpers — staleness classification
# ============================================================================


def _classify_staleness(
    target_max,
    source_max,
    pending_count: int,
    lookback_days: int,
) -> str:
    """Classify staleness into one of 5 categories.

    Returns: "up_to_date", "stale", "watermark_null", "very_stale", "filter_mismatch"
    """
    if target_max is None:
        return "watermark_null"

    if source_max is None:
        # Source has no data — can't determine staleness
        return "up_to_date"

    if target_max >= source_max:
        return "up_to_date"

    # Target is behind source
    from datetime import datetime, timedelta
    import pandas as pd

    # Calculate lag
    try:
        if hasattr(target_max, "to_pydatetime"):
            t_max = target_max.to_pydatetime()
        else:
            t_max = target_max

        if hasattr(source_max, "to_pydatetime"):
            s_max = source_max.to_pydatetime()
        else:
            s_max = source_max

        lag = s_max - t_max
        lag_days = lag.total_seconds() / 86400
    except (TypeError, AttributeError):
        lag_days = lookback_days  # Can't compute — assume very stale

    if lag_days > lookback_days:
        return "very_stale"

    if pending_count == 0 and target_max < source_max:
        return "filter_mismatch"

    return "stale"


# ============================================================================
# Helpers — sample collection
# ============================================================================


def _collect_samples_pandas(
    source_df, target_df, source_wm_col: str, target_wm_col: str,
    target_max, sample_limit: int,
) -> dict:
    """Collect sample rows for diagnostics (Pandas)."""
    samples = {}

    # Pending rows sample
    if target_max is not None:
        pending = source_df[source_df[source_wm_col] > target_max]
        if len(pending) > 0:
            samples["pending_rows_sample"] = (
                pending.head(sample_limit).to_dict(orient="records")
            )

    # NULL watermark sample from target
    null_wm = target_df[target_df[target_wm_col].isna()]
    if len(null_wm) > 0:
        samples["null_watermark_sample"] = (
            null_wm.head(sample_limit).to_dict(orient="records")
        )

    return samples


def _collect_samples_spark(
    source_df, target_df, source_wm_col: str, target_wm_col: str,
    target_max, sample_limit: int,
) -> dict:
    """Collect sample rows for diagnostics (Spark)."""
    from pyspark.sql import functions as F

    samples = {}

    # Pending rows sample
    if target_max is not None:
        pending = source_df.where(F.col(source_wm_col) > F.lit(target_max))
        pending_rows = pending.limit(sample_limit).toPandas()
        if len(pending_rows) > 0:
            samples["pending_rows_sample"] = pending_rows.to_dict(orient="records")

    # NULL watermark sample from target
    null_wm = target_df.where(F.col(target_wm_col).isNull())
    null_rows = null_wm.limit(sample_limit).toPandas()
    if len(null_rows) > 0:
        samples["null_watermark_sample"] = null_rows.to_dict(orient="records")

    return samples


# ============================================================================
# Main entry point
# ============================================================================


def watermark_debug_context(
    *args,
    source=None,
    target=None,
    watermark_col: str | None = None,
    source_watermark_col: str | None = None,
    target_watermark_col: str | None = None,
    lookback_days: int = 30,
    subject: str | None = None,
    output_format: str = "dict",
    sample_limit: int = 10,
    **kwargs,
) -> dict | str:
    """Diagnose incremental load staleness between source and target.

    Args:
        source: Source table name ("catalog.schema.table") or DataFrame.
        target: Target table name ("catalog.schema.table") or DataFrame.
        watermark_col: Watermark column name (same in both tables).
        source_watermark_col: Source-side watermark column (when names differ).
        target_watermark_col: Target-side watermark column (when names differ).
        lookback_days: How many days back to analyze for gap detection. Default 30.
        subject: Display name for the pipeline.
        output_format: "dict" or "markdown".
        sample_limit: Max sample values to include.

    Returns:
        Anchor standard contract dict or markdown string.
    """
    validate_output_format(output_format)

    # ── Resolve positional args ──────────────────────────────────────────
    if args:
        if len(args) >= 1 and source is None:
            source = args[0]
        if len(args) >= 2 and target is None:
            target = args[1]

    if source is None:
        raise ValueError(
            "source is required — pass a table name string or DataFrame: "
            "anchor('watermark', source, target, watermark_col='date') "
            "— see anchor('help', 'watermark')."
        )
    if target is None:
        raise ValueError(
            "target is required — pass a table name string or DataFrame: "
            "anchor('watermark', source, target, watermark_col='date') "
            "— see anchor('help', 'watermark')."
        )

    # ── Resolve watermark column names ───────────────────────────────────
    src_wm_col = source_watermark_col or watermark_col
    tgt_wm_col = target_watermark_col or watermark_col

    if src_wm_col is None:
        raise ValueError(
            "watermark_col (or source_watermark_col) is required — "
            "pass the date/timestamp column name: "
            "anchor('watermark', source, target, watermark_col='date') "
            "— see anchor('help', 'watermark')."
        )
    if tgt_wm_col is None:
        raise ValueError(
            "watermark_col (or target_watermark_col) is required — "
            "pass the date/timestamp column name: "
            "anchor('watermark', source, target, watermark_col='date') "
            "— see anchor('help', 'watermark')."
        )

    # ── Resolve inputs to DataFrames ─────────────────────────────────────
    source_df = _resolve_input(source, "source")
    target_df = _resolve_input(target, "target")

    # ── Detect engine ────────────────────────────────────────────────────
    engine = detect_engine(source_df)
    if engine == "unknown":
        engine = detect_engine(target_df)

    # ── Compute statistics ───────────────────────────────────────────────
    if engine == "spark":
        source_stats = _compute_stats_spark(source_df, src_wm_col, lookback_days)
        target_stats = _compute_stats_spark(target_df, tgt_wm_col, lookback_days)
        pending_count = _count_pending_spark(source_df, src_wm_col, target_stats["max_watermark"])
        samples = _collect_samples_spark(
            source_df, target_df, src_wm_col, tgt_wm_col,
            target_stats["max_watermark"], sample_limit,
        )
    else:
        source_stats = _compute_stats_pandas(source_df, src_wm_col, lookback_days)
        target_stats = _compute_stats_pandas(target_df, tgt_wm_col, lookback_days)
        pending_count = _count_pending_pandas(source_df, src_wm_col, target_stats["max_watermark"])
        samples = _collect_samples_pandas(
            source_df, target_df, src_wm_col, tgt_wm_col,
            target_stats["max_watermark"], sample_limit,
        )

    # ── Derived metrics ──────────────────────────────────────────────────
    target_max = target_stats["max_watermark"]
    source_max = source_stats["max_watermark"]

    # Lag calculation
    lag_hours = None
    lag_days_val = None
    if target_max is not None and source_max is not None and source_max > target_max:
        try:
            import pandas as pd
            t_max = pd.Timestamp(target_max)
            s_max = pd.Timestamp(source_max)
            lag = s_max - t_max
            lag_hours = round(lag.total_seconds() / 3600, 1)
            lag_days_val = round(lag.total_seconds() / 86400, 1)
        except (TypeError, ValueError):
            pass

    # Staleness classification
    staleness = _classify_staleness(target_max, source_max, pending_count, lookback_days)

    # Gap detection (use target daily counts)
    gaps = _detect_gaps(target_stats["daily_counts"], lookback_days)

    # Cadence inference
    cadence = _infer_cadence(target_stats["daily_counts"])

    # Type compatibility check
    type_compatible = True  # Default — sophisticated check would compare dtypes

    # ── Build subject ────────────────────────────────────────────────────
    if subject is None:
        src_name = source if isinstance(source, str) else "source_df"
        tgt_name = target if isinstance(target, str) else "target_df"
        subject = f"{src_name} → {tgt_name}"

    # ── Build summary ────────────────────────────────────────────────────
    if staleness == "up_to_date":
        summary = f"UP TO DATE: target watermark matches source ({target_max})."
    elif staleness == "watermark_null":
        summary = (
            f"WATERMARK NULL: target MAX({tgt_wm_col}) returned NULL — "
            f"{target_stats['null_count']} NULL values in watermark column."
        )
    elif staleness == "very_stale":
        summary = (
            f"VERY STALE: target watermark is {target_max}, source has data through "
            f"{source_max}. {lag_days_val or '>30'}-day lag, {pending_count:,} rows pending."
        )
    elif staleness == "filter_mismatch":
        summary = (
            f"FILTER MISMATCH: target watermark ({target_max}) < source ({source_max}) "
            f"but 0 pending rows — watermark filter may not match source format."
        )
    else:  # stale
        summary = (
            f"STALE: target watermark is {target_max}, source has "
            f"{pending_count:,} rows through {source_max}. "
            f"{lag_days_val or '?'}-day lag, {pending_count:,} rows pending."
        )

    # ── Null watermark percentage ────────────────────────────────────────
    target_null_pct = (
        round(target_stats["null_count"] / target_stats["total_count"], 4)
        if target_stats["total_count"] > 0
        else 0
    )

    # ── Build metrics ────────────────────────────────────────────────────
    metrics = {
        "source_table": source if isinstance(source, str) else "(DataFrame)",
        "target_table": target if isinstance(target, str) else "(DataFrame)",
        "watermark_column": watermark_col or f"{src_wm_col}/{tgt_wm_col}",
        "target_watermark": str(target_max) if target_max is not None else None,
        "source_watermark": str(source_max) if source_max is not None else None,
        "lag_hours": lag_hours,
        "lag_days": lag_days_val,
        "pending_count": pending_count,
        "source_total": source_stats["total_count"],
        "target_total": target_stats["total_count"],
        "source_null_watermark_count": source_stats["null_count"],
        "target_null_watermark_count": target_stats["null_count"],
        "target_null_watermark_pct": target_null_pct,
        "staleness": staleness,
        "gaps": gaps,
        "typical_cadence": cadence,
        "type_compatible": type_compatible,
    }

    # ── Build findings ───────────────────────────────────────────────────
    findings = []

    if staleness == "up_to_date":
        findings.append(
            f"Target watermark ({target_max}) is current with source ({source_max})."
        )
    elif staleness == "watermark_null":
        findings.append(
            f"Target MAX({tgt_wm_col}) is NULL — {target_stats['null_count']} "
            f"rows have NULL watermark out of {target_stats['total_count']:,} total."
        )
    else:
        findings.append(
            f"Target watermark ({target_max}) is {lag_hours or '?'} hours behind "
            f"source ({source_max})."
        )
        findings.append(
            f"{pending_count:,} rows in source have {src_wm_col} > target watermark — pending load."
        )

    if target_stats["null_count"] > 0 and staleness != "watermark_null":
        findings.append(
            f"{target_stats['null_count']} rows in target have NULL watermark "
            f"— these don't participate in MAX()."
        )

    if gaps:
        for gap in gaps[:3]:  # Show at most 3 gaps
            findings.append(
                f"{gap['days']}-day gap detected: {gap['start']} to {gap['end']}"
            )

    findings.append(f"Typical load cadence is {cadence} based on last {lookback_days} days.")

    # ── Build risks ──────────────────────────────────────────────────────
    risks = []

    if target_stats["null_count"] > 0:
        risks.append(
            f"{target_stats['null_count']} NULL watermark rows in target — "
            f"if this count grows, MAX() becomes unreliable."
        )

    if staleness == "filter_mismatch":
        risks.append(
            "Filter mismatch detected — watermark comparison may have type/format "
            "incompatibility between source and target."
        )

    if len(gaps) > 2:
        risks.append(
            f"{len(gaps)} timeline gaps detected — data may be missing, not just delayed."
        )

    # ── Build suggested_next_actions ─────────────────────────────────────
    actions = []

    if staleness in ("stale", "very_stale"):
        actions.append(
            f"Load pending rows: pipeline should pick up {pending_count:,} rows "
            f"with {src_wm_col} > '{target_max}'"
        )

    if target_stats["null_count"] > 0:
        actions.append(
            f"Fix NULL watermarks: UPDATE target SET {tgt_wm_col} = <fallback_col> "
            f"WHERE {tgt_wm_col} IS NULL"
        )

    if gaps:
        gap = gaps[0]
        actions.append(
            f"Investigate {gap['days']}-day gap ({gap['start']} to {gap['end']}): "
            f"check if source had data for those dates"
        )

    if staleness == "filter_mismatch":
        actions.append(
            f"Check watermark filter logic — source MAX is {source_max} but "
            f"no rows match filter > {target_max}. Possible type mismatch or timezone issue."
        )

    # ── Add gap dates to samples ─────────────────────────────────────────
    if gaps:
        all_gap_dates = []
        for gap in gaps[:3]:
            all_gap_dates.append(f"{gap['start']} to {gap['end']}")
        samples["gap_dates"] = all_gap_dates

    # ── Build contract ───────────────────────────────────────────────────
    ctx = build_base_context(
        kind="watermark",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples=samples,
        suggested_next_actions=actions,
    )

    return finalize_context(ctx, output_format, render_watermark_report)


# ============================================================================
# Markdown renderer
# ============================================================================


def render_watermark_report(ctx: dict) -> str:
    """Render watermark debug context as markdown."""
    lines = []

    # Header
    staleness = ctx["metrics"].get("staleness", "unknown")
    badge = {
        "up_to_date": "✅",
        "stale": "⚠️",
        "very_stale": "🔴",
        "watermark_null": "❌",
        "filter_mismatch": "🔶",
    }.get(staleness, "❓")

    lines.extend(render_header_lines(ctx, f"Watermark Debug {badge}"))
    lines.append(f"**Staleness:** {staleness.upper().replace('_', ' ')}")
    lines.append("")

    # Key metrics
    m = ctx["metrics"]
    display_metrics = {
        "Source": m.get("source_table", "?"),
        "Target": m.get("target_table", "?"),
        "Watermark Column": m.get("watermark_column", "?"),
        "Target Watermark": m.get("target_watermark") or "NULL",
        "Source Watermark": m.get("source_watermark") or "NULL",
        "Lag (hours)": m.get("lag_hours") or "—",
        "Lag (days)": m.get("lag_days") or "—",
        "Pending Rows": f"{m.get('pending_count', 0):,}",
        "Source Total": f"{m.get('source_total', 0):,}",
        "Target Total": f"{m.get('target_total', 0):,}",
        "Target NULL Watermarks": f"{m.get('target_null_watermark_count', 0):,} ({m.get('target_null_watermark_pct', 0):.2%})",
        "Cadence": m.get("typical_cadence", "?"),
    }
    lines.extend(render_metrics_lines(display_metrics))
    lines.append("")

    # Gaps table
    gaps = m.get("gaps", [])
    if gaps:
        lines.append("## Timeline Gaps")
        lines.append("")
        gap_rows = [[g["start"], g["end"], str(g["days"])] for g in gaps]
        lines.extend(render_table(gap_rows, ["Start", "End", "Days"]))
        lines.append("")

    # Findings
    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))
    lines.append("")

    # Risks
    if ctx.get("risks"):
        lines.extend(render_bullet_section(ctx["risks"], "## Risks"))
        lines.append("")

    # Suggested next actions
    if ctx.get("suggested_next_actions"):
        lines.append("## Suggested Next Actions")
        lines.append("")
        for i, action in enumerate(ctx["suggested_next_actions"], 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    return "\n".join(lines)
