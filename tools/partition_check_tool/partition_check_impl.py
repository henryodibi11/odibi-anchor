"""Partition diagnostics tool implementation.

Entry point: partition_check_context(table, ...)
Diagnoses small files, partition skew, empty partitions, and file layout
issues on Delta tables. Returns a standard Anchor contract with actionable
recommendations.

Spark-only — partition diagnostics is a Delta/Databricks concept.
"""
from __future__ import annotations

import logging
import statistics
from typing import Any

from odibi_anchor._utils.contract import (
    build_base_context,
    finalize_context,
    validate_output_format,
)
from odibi_anchor._utils.render_utils import (
    render_bullet_section,
    render_header_lines,
    render_metrics_lines,
    render_table,
)

logger = logging.getLogger(__name__)

_DEFAULT_SMALL_FILE_MB = 128
_DEFAULT_SKEW_RATIO = 10.0
_MAX_FILES_TO_COLLECT = 10_000


# ============================================================================
# Spark helpers
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
            "PySpark not available — partition_check requires Spark."
        ) from exc


def _validate_delta_table(spark, table: str) -> dict:
    """Validate table exists and is Delta. Return DESCRIBE DETAIL as dict.

    Raises:
        ValueError: If table does not exist or is not Delta.
    """
    try:
        detail_df = spark.sql(f"DESCRIBE DETAIL {table}")
        row = detail_df.collect()[0]
        detail = row.asDict()
    except Exception as exc:
        raise ValueError(
            f"Table '{table}' does not exist or is not accessible: {exc}. "
            "Check the table name and that you have SELECT permission."
        ) from exc

    fmt = detail.get("format", "")
    if fmt.lower() != "delta":
        raise ValueError(
            f"Table '{table}' is format '{fmt}', not Delta. "
            "Use a Delta-format table or convert with CONVERT TO DELTA "
            "— see anchor('help', 'partition_check')."
        )
    return detail


def _collect_file_metadata(spark, table: str) -> list[dict]:
    """Read per-file metadata from Delta table.

    Strategy:
    1. Get file paths via inputFiles() (works on Spark Connect)
    2. Get file sizes via Hadoop FS (classic Spark) or estimate from
       DESCRIBE DETAIL (Spark Connect fallback)
    Returns list of {path, size_bytes, partition_values}.
    """
    try:
        df = spark.read.table(table)
        input_files = df.inputFiles()

        if len(input_files) > _MAX_FILES_TO_COLLECT:
            logger.warning(
                "Table has %d files, capping at %d for analysis.",
                len(input_files),
                _MAX_FILES_TO_COLLECT,
            )
            input_files = input_files[:_MAX_FILES_TO_COLLECT]

        if not input_files:
            return []

        # Try Hadoop FS for exact per-file sizes (classic Spark only)
        files = _try_hadoop_file_sizes(spark, input_files)
        if files:
            return files

        # Spark Connect fallback: use DESCRIBE DETAIL total size / file count
        # to estimate per-file size, preserving partition info from paths
        logger.info(
            "Using DESCRIBE DETAIL fallback for file sizes (Spark Connect)."
        )
        detail_df = spark.sql(f"DESCRIBE DETAIL {table}")
        detail = detail_df.collect()[0].asDict()
        total_bytes = detail.get("sizeInBytes", 0)
        num_files = max(len(input_files), 1)
        avg_size = total_bytes // num_files

        files = []
        for file_path in input_files:
            partition_values = _extract_partition_values(file_path)
            files.append({
                "path": file_path,
                "size_bytes": avg_size,
                "partition_values": partition_values,
            })
        return files
    except Exception as exc:
        logger.warning("File metadata collection failed: %s", exc)
        return []


def _try_hadoop_file_sizes(spark, input_files: list[str]) -> list[dict]:
    """Attempt to get file sizes via Hadoop FS (classic Spark only).

    Returns empty list if Hadoop FS is not available (e.g. Spark Connect).
    """
    try:
        jvm = spark._jvm
        hadoop_conf = spark._jsc.hadoopConfiguration()
    except (AttributeError, Exception):
        return []  # Spark Connect — _jvm not available

    files = []
    for file_path in input_files:
        try:
            jpath = jvm.org.apache.hadoop.fs.Path(file_path)
            fs = jpath.getFileSystem(hadoop_conf)
            status = fs.getFileStatus(jpath)
            size_bytes = status.getLen()
        except Exception:
            size_bytes = 0

        partition_values = _extract_partition_values(file_path)
        files.append({
            "path": file_path,
            "size_bytes": size_bytes,
            "partition_values": partition_values,
        })
    return files


def _extract_partition_values(file_path: str) -> dict:
    """Extract partition key=value pairs from a Delta file path.

    Delta stores files as: .../key1=value1/key2=value2/part-00000.parquet
    """
    parts = file_path.replace("\\", "/").split("/")
    partition_values = {}
    for part in parts:
        if "=" in part and not part.startswith("part-"):
            key, _, value = part.partition("=")
            if key and value and not key.startswith("_"):
                partition_values[key] = value
    return partition_values


# ============================================================================
# Diagnostic checks
# ============================================================================


def _check_small_files(files: list[dict], threshold_mb: int) -> dict:
    """Detect files below size threshold."""
    if not files:
        return {
            "count": 0,
            "pct": 0.0,
            "threshold_mb": threshold_mb,
            "total_files": 0,
        }

    threshold_bytes = threshold_mb * 1024 * 1024
    small = [f for f in files if f["size_bytes"] < threshold_bytes]

    return {
        "count": len(small),
        "pct": round(len(small) / len(files), 3) if files else 0.0,
        "threshold_mb": threshold_mb,
        "total_files": len(files),
    }


def _compute_file_size_stats(files: list[dict]) -> dict:
    """Compute file size distribution statistics."""
    if not files:
        return {
            "avg_mb": 0.0,
            "median_mb": 0.0,
            "min_mb": 0.0,
            "max_mb": 0.0,
            "p10_mb": 0.0,
            "p90_mb": 0.0,
        }

    sizes_mb = sorted([f["size_bytes"] / (1024 * 1024) for f in files])

    def _percentile(sorted_data: list[float], pct: float) -> float:
        """Simple percentile calculation on sorted data."""
        idx = (pct / 100.0) * (len(sorted_data) - 1)
        lower = int(idx)
        upper = min(lower + 1, len(sorted_data) - 1)
        frac = idx - lower
        return sorted_data[lower] * (1 - frac) + sorted_data[upper] * frac

    return {
        "avg_mb": round(statistics.mean(sizes_mb), 3),
        "median_mb": round(statistics.median(sizes_mb), 3),
        "min_mb": round(sizes_mb[0], 3),
        "max_mb": round(sizes_mb[-1], 3),
        "p10_mb": round(_percentile(sizes_mb, 10), 3),
        "p90_mb": round(_percentile(sizes_mb, 90), 3),
    }


def _check_partition_skew(files: list[dict], threshold: float) -> dict:
    """Detect partitions with disproportionate data volume."""
    if not files:
        return {
            "skewed": False,
            "skew_ratio": 0.0,
            "partition_count": 0,
            "empty_partition_count": 0,
        }

    # Group files by partition
    partitions: dict[str, dict] = {}
    for f in files:
        pv = f["partition_values"]
        key = str(sorted(pv.items())) if pv else "__unpartitioned__"
        if key not in partitions:
            partitions[key] = {
                "values": pv,
                "size_bytes": 0,
                "file_count": 0,
            }
        partitions[key]["size_bytes"] += f["size_bytes"]
        partitions[key]["file_count"] += 1

    # If unpartitioned or single partition, no skew analysis
    if len(partitions) <= 1:
        p = list(partitions.values())[0] if partitions else {}
        return {
            "skewed": False,
            "skew_ratio": 1.0,
            "partition_count": len(partitions),
            "empty_partition_count": 0,
            "largest_partition": {
                "values": p.get("values", {}),
                "size_mb": round(p.get("size_bytes", 0) / (1024 * 1024), 1),
                "file_count": p.get("file_count", 0),
            },
            "smallest_partition": {
                "values": p.get("values", {}),
                "size_mb": round(p.get("size_bytes", 0) / (1024 * 1024), 1),
                "file_count": p.get("file_count", 0),
            },
        }

    total_size = sum(p["size_bytes"] for p in partitions.values())
    sorted_parts = sorted(
        partitions.values(), key=lambda p: p["size_bytes"], reverse=True
    )

    largest = sorted_parts[0]
    # Find smallest non-empty partition
    non_empty = [p for p in sorted_parts if p["size_bytes"] > 0]
    smallest = non_empty[-1] if non_empty else sorted_parts[-1]

    skew_ratio = (
        largest["size_bytes"] / smallest["size_bytes"]
        if smallest["size_bytes"] > 0
        else float("inf")
    )

    empty_count = sum(1 for p in sorted_parts if p["size_bytes"] == 0)

    # Build partition_sizes (top entries)
    partition_sizes = []
    for p in sorted_parts[:10]:
        partition_sizes.append({
            "values": p["values"],
            "size_mb": round(p["size_bytes"] / (1024 * 1024), 1),
            "file_count": p["file_count"],
            "pct_of_total": round(p["size_bytes"] / total_size, 3)
            if total_size > 0
            else 0.0,
        })

    return {
        "skewed": skew_ratio >= threshold,
        "skew_ratio": round(skew_ratio, 1)
        if skew_ratio != float("inf")
        else float("inf"),
        "partition_count": len(partitions),
        "empty_partition_count": empty_count,
        "largest_partition": {
            "values": largest["values"],
            "size_mb": round(largest["size_bytes"] / (1024 * 1024), 1),
            "file_count": largest["file_count"],
        },
        "smallest_partition": {
            "values": smallest["values"],
            "size_mb": round(smallest["size_bytes"] / (1024 * 1024), 1),
            "file_count": smallest["file_count"],
        },
        "partition_sizes": partition_sizes,
    }


def _check_empty_partitions(files: list[dict]) -> dict:
    """Detect partitions with zero data files."""
    if not files:
        return {"empty_count": 0, "empty_partitions": []}

    # Group files by partition
    partitions: dict[str, dict] = {}
    for f in files:
        pv = f["partition_values"]
        key = str(sorted(pv.items())) if pv else "__unpartitioned__"
        if key not in partitions:
            partitions[key] = {"values": pv, "size_bytes": 0}
        partitions[key]["size_bytes"] += f["size_bytes"]

    empty = [p for p in partitions.values() if p["size_bytes"] == 0]
    return {
        "empty_count": len(empty),
        "empty_partitions": [{"values": p["values"]} for p in empty],
    }


def _estimate_query_cost(
    file_metrics: dict, small_file_metrics: dict, partition_metrics: dict
) -> dict:
    """Estimate query performance impact of current file layout.

    Heuristic: overhead grows with small file count and partition skew.
    """
    small_pct = small_file_metrics.get("pct", 0.0)
    total_files = small_file_metrics.get("total_files", 0)
    skew_ratio = partition_metrics.get("skew_ratio", 1.0)

    if total_files == 0:
        return {
            "scan_overhead_ratio": 1.0,
            "estimated_speedup_after_optimize": "1x (already optimal)",
            "explanation": "No files to analyze.",
        }

    # Overhead model:
    # - Optimal: ~100 files of 128-256MB each for a 10-25GB table
    # - Each 10x over optimal file count adds ~2x overhead
    avg_mb = file_metrics.get("avg_mb", 128)
    target_file_size_mb = 128
    target_files = max(1, int(total_files * avg_mb / target_file_size_mb))

    file_overhead = total_files / target_files if target_files > 0 else 1.0

    # Skew adds to overhead (uneven parallelism)
    skew_overhead = 1.0
    if isinstance(skew_ratio, (int, float)) and skew_ratio > 5:
        skew_overhead = min(skew_ratio / 5, 3.0)  # Cap at 3x

    total_overhead = round(file_overhead * skew_overhead, 1)
    total_overhead = max(1.0, total_overhead)

    # Estimate speedup range
    if total_overhead <= 1.5:
        speedup = "1-1.5x (minor improvement)"
    elif total_overhead <= 3.0:
        speedup = "2-3x"
    elif total_overhead <= 6.0:
        speedup = "3-6x"
    elif total_overhead <= 10.0:
        speedup = "5-8x"
    else:
        speedup = "8-10x+"

    explanation_parts = []
    if small_pct > 0.5:
        explanation_parts.append(
            f"{int(small_pct * 100)}% of files are below target size — "
            f"excessive file listing overhead"
        )
    if isinstance(skew_ratio, (int, float)) and skew_ratio > 5:
        explanation_parts.append(
            f"Partition skew of {skew_ratio}x causes uneven parallelism"
        )
    if not explanation_parts:
        explanation_parts.append("File layout is reasonably balanced.")

    return {
        "scan_overhead_ratio": total_overhead,
        "estimated_speedup_after_optimize": speedup,
        "explanation": "; ".join(explanation_parts),
    }


# ============================================================================
# Findings & recommendations
# ============================================================================


def _build_findings(
    file_metrics: dict,
    small_file_metrics: dict,
    partition_metrics: dict,
    empty_metrics: dict,
    total_files: int,
    total_size_mb: float,
) -> list[str]:
    """Generate human-readable findings from diagnostic results."""
    findings = []

    # Small files
    sf_count = small_file_metrics.get("count", 0)
    sf_pct = small_file_metrics.get("pct", 0.0)
    threshold = small_file_metrics.get("threshold_mb", _DEFAULT_SMALL_FILE_MB)
    if sf_count > 0:
        findings.append(
            f"{sf_count:,} of {total_files:,} files ({int(sf_pct * 100)}%) "
            f"are below {threshold}MB — excessive file listing overhead on reads"
        )

    # Median vs target
    median = file_metrics.get("median_mb", 0)
    if median > 0 and median < threshold:
        findings.append(
            f"Median file size {median:.1f}MB vs target {threshold}-256MB "
            f"— table needs OPTIMIZE"
        )

    # Partition skew
    if partition_metrics.get("skewed"):
        largest = partition_metrics.get("largest_partition", {})
        smallest = partition_metrics.get("smallest_partition", {})
        ratio = partition_metrics.get("skew_ratio", 0)
        if largest and smallest:
            lv = _format_partition_values(largest.get("values", {}))
            sv = _format_partition_values(smallest.get("values", {}))
            findings.append(
                f"Partition '{lv}' has {largest.get('size_mb', 0):.0f}MB "
                f"— severe skew"
            )
            findings.append(
                f"Partition '{sv}' has {smallest.get('size_mb', 0):.0f}MB "
                f"— {ratio}x smaller than largest"
            )

    # Empty partitions
    empty_count = empty_metrics.get("empty_count", 0)
    if empty_count > 0:
        findings.append(
            f"{empty_count} partition(s) are empty (0 bytes) "
            f"— scan overhead with zero benefit"
        )

    # File size range
    min_mb = file_metrics.get("min_mb", 0)
    max_mb = file_metrics.get("max_mb", 0)
    if max_mb > 0 and min_mb > 0 and (max_mb / max(min_mb, 0.001)) > 100:
        findings.append(
            f"File sizes range from {min_mb:.3f}MB to {max_mb:.1f}MB "
            f"— highly unbalanced within partitions"
        )

    if not findings:
        findings.append(
            "File layout appears healthy — no significant issues detected."
        )

    return findings


def _build_risks(
    small_file_metrics: dict,
    partition_metrics: dict,
    empty_metrics: dict,
    cost_metrics: dict,
) -> list[str]:
    """Generate risk statements."""
    risks = []
    speedup = cost_metrics.get("estimated_speedup_after_optimize", "1x")

    if small_file_metrics.get("pct", 0) > 0.3:
        count = small_file_metrics.get("count", 0)
        risks.append(
            f"Small file problem causes {speedup} query slowdown — "
            f"every query must open {count:,} tiny files"
        )

    if partition_metrics.get("skewed"):
        risks.append(
            "Partition skew means queries filtering on large partitions "
            "are disproportionately slow"
        )

    if empty_metrics.get("empty_count", 0) > 0:
        risks.append(
            "Empty partitions add scan overhead with zero benefit"
        )

    return risks


def _build_suggested_actions(table: str, partition_metrics: dict) -> list[str]:
    """Generate actionable next steps."""
    actions = [
        f"Run OPTIMIZE: OPTIMIZE {table}",
    ]

    largest = partition_metrics.get("largest_partition", {})
    if largest.get("values"):
        actions.append(
            f"Add ZORDER for common filter columns: "
            f"OPTIMIZE {table} ZORDER BY (<your_filter_columns>)"
        )

    empty_count = partition_metrics.get("empty_partition_count", 0)
    if empty_count > 0:
        actions.append(
            f"Investigate {empty_count} empty partition(s) — "
            f"consider cleanup if data source is retired"
        )

    if partition_metrics.get("partition_count", 0) > 50:
        actions.append(
            "Consider reducing partition cardinality — "
            "high partition count increases metadata overhead"
        )

    actions.append(
        "Schedule regular OPTIMIZE: run weekly or after bulk loads"
    )
    actions.append(
        f"After OPTIMIZE, re-run: anchor('partition_check', '{table}') to verify"
    )

    return actions


def _format_partition_values(values: dict) -> str:
    """Format partition values dict as readable string."""
    if not values:
        return "(unpartitioned)"
    return ", ".join(f"{k}={v}" for k, v in values.items())


# ============================================================================
# Main entry point
# ============================================================================


def partition_check_context(
    *args,
    table: str | None = None,
    spark=None,
    small_file_threshold_mb: int = _DEFAULT_SMALL_FILE_MB,
    skew_ratio_threshold: float = _DEFAULT_SKEW_RATIO,
    subject: str | None = None,
    output_format: str = "dict",
    **kwargs,
) -> dict | str:
    """Diagnose partition and file layout issues on a Delta table.

    Args:
        table: Fully qualified Delta table name (e.g., "catalog.schema.table").
            Can also be passed as first positional arg.
        spark: SparkSession. If None, uses active session.
        small_file_threshold_mb: Files below this size (MB) are flagged. Default 128.
        skew_ratio_threshold: Partition is skewed if largest/smallest > this. Default 10.
        subject: Optional display name override.
        output_format: "dict" or "markdown".

    Raises:
        ValueError: If table is not a Delta table or does not exist.
        RuntimeError: If no active SparkSession and spark not provided.

    Note:
        Spark-only. No Pandas equivalent — partition diagnostics is a
        Delta/Databricks concept.
    """
    validate_output_format(output_format)

    # Resolve table from positional args
    if table is None and args:
        table = args[0]
        args = args[1:]

    if not table:
        raise ValueError(
            "table is required. Usage: anchor('partition_check', 'catalog.schema.table')"
        )

    if spark is None:
        spark = _get_spark_session()

    subject = subject or table

    # Step 1: Validate Delta table and get DESCRIBE DETAIL
    detail = _validate_delta_table(spark, table)
    total_files_detail = detail.get("numFiles", 0)
    total_size_bytes = detail.get("sizeInBytes", 0)
    total_size_mb = round(total_size_bytes / (1024 * 1024), 1)
    partition_columns = detail.get("partitionColumns", []) or []

    # Step 2: Collect file-level metadata
    files = _collect_file_metadata(spark, table)

    # If file collection failed, use table-level fallback
    total_files = len(files) if files else total_files_detail

    # Step 3: Run diagnostics
    file_size_stats = _compute_file_size_stats(files)
    small_file_metrics = _check_small_files(files, small_file_threshold_mb)
    partition_metrics = _check_partition_skew(files, skew_ratio_threshold)
    empty_metrics = _check_empty_partitions(files)
    cost_metrics = _estimate_query_cost(
        file_size_stats, small_file_metrics, partition_metrics
    )

    # Step 4: Build findings and recommendations
    findings = _build_findings(
        file_size_stats,
        small_file_metrics,
        partition_metrics,
        empty_metrics,
        total_files,
        total_size_mb,
    )
    risks = _build_risks(
        small_file_metrics, partition_metrics, empty_metrics, cost_metrics
    )
    suggested_actions = _build_suggested_actions(table, partition_metrics)

    # Step 5: Build summary
    issues = []
    if small_file_metrics.get("count", 0) > 0:
        issues.append(
            f"{small_file_metrics['count']} small files "
            f"({int(small_file_metrics['pct'] * 100)}%)"
        )
    if partition_metrics.get("skewed"):
        issues.append(
            f"partition skew {partition_metrics['skew_ratio']}x"
        )
    if empty_metrics.get("empty_count", 0) > 0:
        issues.append(
            f"{empty_metrics['empty_count']} empty partition(s)"
        )

    if issues:
        speedup = cost_metrics.get("estimated_speedup_after_optimize", "")
        summary = (
            f"\u26a0\ufe0f {', '.join(issues)}. "
            f"OPTIMIZE recommended — estimated {speedup} query speedup."
        )
    else:
        summary = (
            f"\u2705 File layout is healthy: {total_files:,} files, "
            f"{total_size_mb:.0f}MB total. No action needed."
        )

    # Step 6: Assemble Anchor contract
    metrics = {
        "total_files": total_files,
        "total_size_mb": total_size_mb,
        "partition_columns": partition_columns,
        "file_size": file_size_stats,
        "small_files": small_file_metrics,
        "partition_skew": partition_metrics,
        "estimated_query_cost": cost_metrics,
    }

    ctx = build_base_context(
        kind="partition_check",
        subject=subject,
        summary=summary,
        metrics=metrics,
    )

    ctx["findings"] = findings
    ctx["risks"] = risks
    ctx["samples"] = {
        "largest_partitions": (
            partition_metrics.get("partition_sizes", [])[:5]
        ),
        "empty_partitions": empty_metrics.get("empty_partitions", [])[:10],
    }
    ctx["suggested_next_actions"] = suggested_actions

    return finalize_context(ctx, output_format=output_format, render_fn=render_partition_check_report)


# ============================================================================
# Markdown renderer
# ============================================================================


def render_partition_check_report(ctx: dict) -> str:
    """Render partition_check context as a markdown report."""
    lines = render_header_lines(ctx, "Partition Diagnostics")

    metrics = ctx.get("metrics", {})
    file_size = metrics.get("file_size", {})
    small_files = metrics.get("small_files", {})
    partition_skew = metrics.get("partition_skew", {})
    cost = metrics.get("estimated_query_cost", {})

    # Table overview
    overview_rows = [
        ["Total Files", f"{metrics.get('total_files', 0):,}"],
        ["Total Size", f"{metrics.get('total_size_mb', 0):.1f} MB"],
        ["Partition Columns", ", ".join(metrics.get("partition_columns", [])) or "None"],
        ["Partitions", str(partition_skew.get("partition_count", 0))],
    ]
    lines.append("\n## Table Overview\n")
    lines.extend(render_table(overview_rows, ["Metric", "Value"]))

    # File size distribution
    size_rows = [
        ["Average", f"{file_size.get('avg_mb', 0):.1f} MB"],
        ["Median", f"{file_size.get('median_mb', 0):.1f} MB"],
        ["Min", f"{file_size.get('min_mb', 0):.3f} MB"],
        ["Max", f"{file_size.get('max_mb', 0):.1f} MB"],
        ["P10", f"{file_size.get('p10_mb', 0):.2f} MB"],
        ["P90", f"{file_size.get('p90_mb', 0):.1f} MB"],
    ]
    lines.append("\n## File Size Distribution\n")
    lines.extend(render_table(size_rows, ["Statistic", "Value"]))

    # Small files
    lines.append("\n## Small Files\n")
    sf_count = small_files.get("count", 0)
    sf_pct = small_files.get("pct", 0)
    lines.append(
        f"**{sf_count:,}** of {small_files.get('total_files', 0):,} files "
        f"({int(sf_pct * 100)}%) below {small_files.get('threshold_mb', 128)}MB threshold\n"
    )

    # Partition skew
    if partition_skew.get("partition_count", 0) > 1:
        lines.append("\n## Partition Skew\n")
        skewed = "\u26a0\ufe0f YES" if partition_skew.get("skewed") else "\u2705 NO"
        lines.append(f"**Skewed:** {skewed} (ratio: {partition_skew.get('skew_ratio', 0)}x)\n")

        if partition_skew.get("partition_sizes"):
            part_rows = []
            for p in partition_skew["partition_sizes"][:10]:
                part_rows.append([
                    _format_partition_values(p.get("values", {})),
                    f"{p.get('size_mb', 0):.1f} MB",
                    str(p.get("file_count", 0)),
                    f"{p.get('pct_of_total', 0) * 100:.1f}%",
                ])
            lines.extend(
                render_table(part_rows, ["Partition", "Size", "Files", "% Total"])
            )

    # Query cost
    lines.append("\n## Estimated Query Impact\n")
    lines.append(
        f"- Scan overhead ratio: **{cost.get('scan_overhead_ratio', 1.0)}x**\n"
        f"- Estimated speedup after OPTIMIZE: **{cost.get('estimated_speedup_after_optimize', 'N/A')}**\n"
        f"- {cost.get('explanation', '')}\n"
    )

    # Findings
    lines.extend(render_bullet_section(ctx.get("findings", []), "Findings"))

    # Risks
    if ctx.get("risks"):
        lines.extend(render_bullet_section(ctx["risks"], "Risks"))

    # Suggested actions
    lines.extend(
        render_bullet_section(
            ctx.get("suggested_next_actions", []), "Suggested Actions"
        )
    )

    return "\n".join(lines)
