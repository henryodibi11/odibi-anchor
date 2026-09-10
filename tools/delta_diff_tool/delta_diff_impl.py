"""Delta version diff tool implementation.

Entry point: delta_diff_context(table, keys, ...)
Compares two versions of a Delta table at business-key level.
Returns a standard Anchor contract with row-level changes, Delta history
metadata, and suggested next actions.

Spark-only — Delta versioning is a Spark/Databricks concept.
"""
from __future__ import annotations

import logging
from datetime import datetime
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


# ============================================================================
# Version resolution helpers
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
        raise RuntimeError("PySpark not available — delta_diff requires Spark.") from exc


def _get_delta_history(spark, table: str, limit: int = 50) -> list[dict]:
    """Retrieve Delta table history as list of dicts.

    Note: Uses asDict() to handle Spark Connect Rows which don't support .get().
    """
    hist_df = spark.sql(f"DESCRIBE HISTORY {table} LIMIT {limit}")
    rows = hist_df.collect()
    result = []
    for row in rows:
        d = row.asDict()
        result.append({
            "version": d["version"],
            "timestamp": d["timestamp"],
            "operation": d["operation"],
            "userName": d.get("userName"),
            "operationMetrics": d.get("operationMetrics"),
        })
    return result


def _resolve_version(
    version_spec: int | str | None,
    history: list[dict],
    current_version: int,
    versions_ago: int | None = None,
) -> int:
    """Resolve a version spec to a concrete version number.

    Args:
        version_spec: Version number (int), timestamp string, or None.
        history: Delta history entries.
        current_version: Latest version number.
        versions_ago: If version_spec is None, use this offset from current.

    Returns:
        Resolved integer version number.
    """
    if version_spec is None:
        if versions_ago is not None:
            target = current_version - versions_ago
            if target < 0:
                raise ValueError(
                    f"versions_ago={versions_ago} exceeds available history "
                    f"(current version is {current_version}). "
                    "Use a smaller value — see anchor('help', 'delta_diff')."
                )
            return target
        return current_version

    if isinstance(version_spec, int):
        return version_spec

    # Timestamp string — find closest version at or before that timestamp
    if isinstance(version_spec, str):
        try:
            target_ts = datetime.fromisoformat(version_spec)
        except ValueError:
            # Try common date-only format
            try:
                target_ts = datetime.strptime(version_spec, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(
                    f"Cannot parse timestamp: {version_spec!r}. "
                    f"Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)."
                ) from exc

        # Find the latest version with timestamp <= target
        candidates = [
            h for h in history if h["timestamp"] <= target_ts
        ]
        if not candidates:
            raise ValueError(
                f"No version found at or before {version_spec}. "
                f"Earliest version timestamp: {history[-1]['timestamp']}."
            )
        # History is sorted newest-first — first candidate is the one we want
        return candidates[0]["version"]

    raise TypeError(f"version_spec must be int, str, or None. Got {type(version_spec).__name__}.")


def _read_delta_version(spark, table: str, version: int):
    """Read a specific Delta table version as a DataFrame."""
    return (
        spark.read.format("delta")
        .option("versionAsOf", version)
        .table(table)
    )


def _get_history_entry(history: list[dict], version: int) -> dict | None:
    """Find history entry for a specific version."""
    for entry in history:
        if entry["version"] == version:
            return entry
    return None


# ============================================================================
# NULL-aware change classification
# ============================================================================


def _classify_column_changes(
    old_df,
    new_df,
    keys: list[str],
    changed_columns: list[str],
    sample_limit: int = 20,
) -> dict[str, dict[str, int]]:
    """Classify changes per column into null_to_value, value_to_null, value_changed.

    Operates on the subset of rows that exist in both versions (matched by keys).
    Uses Spark operations — no collect() on full data.
    """
    from pyspark.sql import functions as F

    # Join on keys to get matched rows
    join_cond = [old_df[k] == new_df[k] for k in keys]
    old_aliased = old_df.alias("old")
    new_aliased = new_df.alias("new")
    matched = old_aliased.join(new_aliased, join_cond, "inner")

    result = {}
    for col in changed_columns:
        old_col = F.col(f"old.{col}")
        new_col = F.col(f"new.{col}")

        # Only rows where value actually differs (NULL-safe comparison)
        changed_rows = matched.filter(
            ~old_col.eqNullSafe(new_col)
        )

        counts = changed_rows.agg(
            F.sum(F.when(old_col.isNull() & new_col.isNotNull(), 1).otherwise(0)).alias("null_to_value"),
            F.sum(F.when(old_col.isNotNull() & new_col.isNull(), 1).otherwise(0)).alias("value_to_null"),
            F.sum(
                F.when(old_col.isNotNull() & new_col.isNotNull(), 1).otherwise(0)
            ).alias("value_changed"),
        ).collect()[0]

        result[col] = {
            "null_to_value": int(counts["null_to_value"] or 0),
            "value_to_null": int(counts["value_to_null"] or 0),
            "value_changed": int(counts["value_changed"] or 0),
        }

    return result


# ============================================================================
# Core diff logic
# ============================================================================


def _compute_diff_metrics(
    old_df,
    new_df,
    keys: list[str],
    compare_columns: list[str] | None,
    sample_limit: int,
) -> dict:
    """Compute row-level diff metrics between two Spark DataFrames.

    Returns dict with counts, changed_column_counts, and sample rows.
    """
    from pyspark.sql import functions as F

    old_cols = set(old_df.columns)
    new_cols = set(new_df.columns)

    # Handle schema evolution — use intersection of non-key columns
    common_cols = old_cols & new_cols
    key_set = set(keys)

    if compare_columns is not None:
        non_key_compare = [c for c in compare_columns if c not in key_set and c in common_cols]
    else:
        non_key_compare = sorted(common_cols - key_set)

    schema_changed = old_cols != new_cols
    added_cols = sorted(new_cols - old_cols)
    removed_cols = sorted(old_cols - new_cols)

    # Select only common columns for comparison
    use_cols = sorted(key_set | set(non_key_compare))
    old_select = old_df.select(*use_cols)
    new_select = new_df.select(*use_cols)

    # Key-based set operations
    old_keys_df = old_select.select(*keys).distinct()
    new_keys_df = new_select.select(*keys).distinct()

    added_keys_df = new_keys_df.subtract(old_keys_df)
    removed_keys_df = old_keys_df.subtract(new_keys_df)
    common_keys_df = old_keys_df.intersect(new_keys_df)

    added_count = added_keys_df.count()
    removed_count = removed_keys_df.count()
    old_row_count = old_select.count()
    new_row_count = new_select.count()

    # For common keys, find changed rows
    old_common = old_select.join(common_keys_df, keys, "inner").alias("old")
    new_common = new_select.join(common_keys_df, keys, "inner").alias("new")

    # Build change detection condition on non-key columns
    if non_key_compare:
        change_conds = [
            ~F.col(f"old.{c}").eqNullSafe(F.col(f"new.{c}"))
            for c in non_key_compare
        ]
        any_changed = change_conds[0]
        for cond in change_conds[1:]:
            any_changed = any_changed | cond

        join_cond = [F.col(f"old.{k}") == F.col(f"new.{k}") for k in keys]
        joined = old_common.join(new_common, join_cond, "inner")
        changed_df = joined.filter(any_changed)
        changed_count = changed_df.count()
    else:
        changed_count = 0

    common_count = common_keys_df.count()
    unchanged_count = common_count - changed_count

    # Identify which columns changed
    changed_columns = []
    if changed_count > 0 and non_key_compare:
        for col in non_key_compare:
            col_diff_count = joined.filter(
                ~F.col(f"old.{col}").eqNullSafe(F.col(f"new.{col}"))
            ).count()
            if col_diff_count > 0:
                changed_columns.append(col)

    # NULL-aware breakdown per changed column
    changed_column_counts = {}
    if changed_columns:
        changed_column_counts = _classify_column_changes(
            old_select, new_select, keys, changed_columns, sample_limit
        )

    # Collect samples
    samples = {}
    if added_count > 0:
        added_rows = new_select.join(added_keys_df, keys, "inner").limit(sample_limit)
        samples["added_rows"] = [
            row.asDict() for row in added_rows.collect()
        ]
    if removed_count > 0:
        removed_rows = old_select.join(removed_keys_df, keys, "inner").limit(sample_limit)
        samples["removed_rows"] = [
            row.asDict() for row in removed_rows.collect()
        ]
    if changed_count > 0 and non_key_compare:
        # Show old+new for changed rows
        changed_sample = changed_df.limit(sample_limit).collect()
        samples["changed_rows"] = [
            row.asDict() for row in changed_sample
        ]

    return {
        "old_row_count": old_row_count,
        "new_row_count": new_row_count,
        "added_count": added_count,
        "removed_count": removed_count,
        "changed_count": changed_count,
        "unchanged_count": unchanged_count,
        "changed_columns": changed_columns,
        "changed_column_counts": changed_column_counts,
        "schema_changed": schema_changed,
        "added_cols": added_cols,
        "removed_cols": removed_cols,
        "samples": samples,
    }


# ============================================================================
# Main entry point
# ============================================================================


def delta_diff_context(
    *args,
    table: str | None = None,
    keys: list[str] | None = None,
    old_version: int | str | None = None,
    new_version: int | str | None = None,
    versions_ago: int = 1,
    compare_columns: list[str] | None = None,
    include_coerce_check: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
    sample_limit: int = 20,
    _spark=None,
    **kwargs,
) -> dict | str:
    """Compare two versions of a Delta table at business-key level.

    Args:
        table: Delta table name ("catalog.schema.table"). Can also be
            passed as the first positional arg.
        keys: Business key columns for row-level matching. Can also be
            passed as the second positional arg.
        old_version: Specific version number or timestamp string for old snapshot.
        new_version: Specific version number or timestamp string for new snapshot.
            Defaults to current (latest) version.
        versions_ago: If old_version not specified, compare current vs N versions ago.
            Default 1.
        compare_columns: Limit diff to specific columns (optional).
        include_coerce_check: Run coerce_check on changed columns automatically.
        subject: Display name for the table.
        output_format: "dict" or "markdown".
        sample_limit: Max sample rows per category.
        _spark: SparkSession override for testing (dependency injection).
    """
    # Resolve positional args
    if args:
        if table is None and len(args) >= 1:
            table = args[0]
        if keys is None and len(args) >= 2:
            keys = args[1]

    # Validate inputs
    if not table:
        raise ValueError(
            "table is required (fully qualified Delta table name). "
            "Pass: anchor('delta_diff', 'catalog.schema.table', keys=['id']) "
            "— see anchor('help', 'delta_diff')."
        )
    if not keys:
        raise ValueError(
            "keys is required (list of business key columns). "
            "Pass: anchor('delta_diff', table, keys=['id']) "
            "— see anchor('help', 'delta_diff')."
        )
    if isinstance(keys, str):
        keys = [keys]

    validate_output_format(output_format)

    subject = subject or table
    spark = _spark or _get_spark_session()

    # Get Delta history
    history = _get_delta_history(spark, table)
    if not history:
        raise ValueError(
            f"No history found for table {table}. "
            "Confirm it is a Delta table and you have SELECT permission. "
            "Usage: anchor('delta_diff', 'catalog.schema.table') "
            "— see anchor('help', 'delta_diff')."
        )

    current_version = history[0]["version"]

    # Resolve version numbers
    resolved_new = _resolve_version(new_version, history, current_version)
    resolved_old = _resolve_version(
        old_version, history, current_version,
        versions_ago=versions_ago if old_version is None else None,
    )

    if resolved_old >= resolved_new:
        raise ValueError(
            f"old_version ({resolved_old}) must be less than new_version ({resolved_new}). "
            "Swap the version arguments or use versions_ago= "
            "— see anchor('help', 'delta_diff')."
        )

    # Get history metadata for both versions
    old_history = _get_history_entry(history, resolved_old)
    new_history = _get_history_entry(history, resolved_new)

    # Read both versions
    old_df = _read_delta_version(spark, table, resolved_old)
    new_df = _read_delta_version(spark, table, resolved_new)

    # Compute diff
    diff_result = _compute_diff_metrics(
        old_df, new_df, keys, compare_columns, sample_limit
    )

    # Build metrics
    metrics = {
        "table": table,
        "old_version": resolved_old,
        "new_version": resolved_new,
        "old_timestamp": str(old_history["timestamp"]) if old_history else None,
        "new_timestamp": str(new_history["timestamp"]) if new_history else None,
        "old_operation": old_history["operation"] if old_history else None,
        "new_operation": new_history["operation"] if new_history else None,
        "new_user": new_history.get("userName") if new_history else None,
        "old_row_count": diff_result["old_row_count"],
        "new_row_count": diff_result["new_row_count"],
        "added_count": diff_result["added_count"],
        "removed_count": diff_result["removed_count"],
        "changed_count": diff_result["changed_count"],
        "unchanged_count": diff_result["unchanged_count"],
        "changed_column_counts": diff_result["changed_column_counts"],
        "schema_changed": diff_result["schema_changed"],
    }
    if diff_result["schema_changed"]:
        metrics["added_cols"] = diff_result["added_cols"]
        metrics["removed_cols"] = diff_result["removed_cols"]

    # Build summary
    op_info = ""
    if new_history:
        user_str = f" by {new_history['userName']}" if new_history.get("userName") else ""
        ts_str = f" at {new_history['timestamp']}" if new_history.get("timestamp") else ""
        op_info = f" ({new_history['operation']}{user_str}{ts_str})"

    summary = (
        f"Version {resolved_old}\u2192{resolved_new}{op_info}: "
        f"{diff_result['added_count']} added, "
        f"{diff_result['removed_count']} removed, "
        f"{diff_result['changed_count']} changed, "
        f"{diff_result['unchanged_count']} unchanged"
    )

    # Build findings
    findings = []
    if diff_result["added_count"]:
        findings.append(
            f"{diff_result['added_count']} rows added (new keys not in previous version)"
        )
    if diff_result["removed_count"]:
        findings.append(
            f"{diff_result['removed_count']} rows removed (keys present in old but not new)"
        )
    if diff_result["changed_count"]:
        col_names = ", ".join(diff_result["changed_columns"])
        findings.append(
            f"{diff_result['changed_count']:,} rows changed across "
            f"{len(diff_result['changed_columns'])} column(s) ({col_names})"
        )
        # Detail per column
        for col, counts in diff_result["changed_column_counts"].items():
            total = sum(counts.values())
            parts = []
            if counts["value_changed"]:
                parts.append(f"{counts['value_changed']} value changes")
            if counts["null_to_value"]:
                parts.append(f"{counts['null_to_value']} null\u2192value fills")
            if counts["value_to_null"]:
                parts.append(f"{counts['value_to_null']} value\u2192null clears")
            findings.append(f"  {col}: {total} changes \u2014 {', '.join(parts)}")

    if diff_result["schema_changed"]:
        if diff_result["added_cols"]:
            findings.append(f"Schema: {len(diff_result['added_cols'])} new column(s): {diff_result['added_cols']}")
        if diff_result["removed_cols"]:
            findings.append(f"Schema: {len(diff_result['removed_cols'])} dropped column(s): {diff_result['removed_cols']}")

    if not findings:
        findings.append("No differences found between versions.")

    # Build risks
    risks = []
    if diff_result["removed_count"] > 0:
        pct = diff_result["removed_count"] / max(diff_result["old_row_count"], 1) * 100
        if pct > 5:
            risks.append(f"{pct:.1f}% of rows removed \u2014 verify this was intentional")
    if diff_result["schema_changed"]:
        risks.append("Schema changed between versions \u2014 downstream consumers may break")

    # Optional coerce_check
    coerce_results = None
    if include_coerce_check and diff_result["changed_columns"]:
        try:
            from odibi_anchor.tables.coercion_classifier import coercion_check_context

            coerce_results = coercion_check_context(
                old_df, new_df, keys=keys,
                columns=diff_result["changed_columns"],
                output_format="dict",
            )
        except Exception as exc:
            logger.warning("coerce_check failed: %s", exc)
            risks.append(f"coerce_check failed: {exc}")

    # Build suggested next actions
    suggested = []
    if diff_result["changed_columns"] and not include_coerce_check:
        cols_str = str(diff_result["changed_columns"])
        suggested.append(
            f"Classify change types: anchor('coerce_check', old_df, new_df, "
            f"keys={keys}, columns={cols_str})"
        )
    if diff_result["removed_count"] > 0:
        suggested.append(
            f"Investigate removed rows: anchor('case_file', old_df, "
            f"filter='where:{keys[0]} IN (...removed keys...)')"
        )
    if diff_result["added_count"] > 0:
        suggested.append(
            f"Profile new rows: anchor('profile_table', new_df.filter(...added keys...), "
            f"subject='{table} new rows')"
        )

    # Assemble contract
    ctx = build_base_context(
        kind="delta_diff",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples=diff_result["samples"],
        suggested_next_actions=suggested,
    )

    if coerce_results:
        ctx["coerce_check"] = coerce_results

    return finalize_context(ctx, output_format, render_delta_diff_report)


# ============================================================================
# Markdown renderer
# ============================================================================


def render_delta_diff_report(ctx: dict) -> str:
    """Render delta_diff context as a markdown report."""
    lines = render_header_lines(ctx, "Delta Diff")

    # Version info
    m = ctx["metrics"]
    lines.append("")
    lines.append("## Version Info")
    lines.append("")
    lines.append(f"| | Old (v{m['old_version']}) | New (v{m['new_version']}) |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| Timestamp | {m.get('old_timestamp', 'N/A')} | {m.get('new_timestamp', 'N/A')} |")
    lines.append(f"| Operation | {m.get('old_operation', 'N/A')} | {m.get('new_operation', 'N/A')} |")
    lines.append(f"| Row count | {m['old_row_count']:,} | {m['new_row_count']:,} |")
    if m.get("new_user"):
        lines.append(f"| User | — | {m['new_user']} |")

    # Diff summary metrics
    diff_metrics = {
        "added": m["added_count"],
        "removed": m["removed_count"],
        "changed": m["changed_count"],
        "unchanged": m["unchanged_count"],
    }
    lines.append("")
    lines.extend(render_metrics_lines(diff_metrics))

    # Changed column breakdown
    if m.get("changed_column_counts"):
        lines.append("")
        lines.append("## Changed Columns")
        lines.append("")
        lines.append("| Column | null\u2192value | value\u2192null | value changed | total |")
        lines.append("| --- | --- | --- | --- | --- |")
        for col, counts in m["changed_column_counts"].items():
            total = sum(counts.values())
            lines.append(
                f"| {col} | {counts['null_to_value']} | "
                f"{counts['value_to_null']} | {counts['value_changed']} | {total} |"
            )

    # Findings
    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))

    # Risks
    if ctx.get("risks"):
        lines.extend(render_bullet_section(ctx["risks"], "## Risks"))

    # Samples
    samples = ctx.get("samples", {})
    if samples:
        lines.append("")
        lines.append("## Samples")
        for section_name, rows in samples.items():
            if rows:
                lines.append("")
                lines.append(f"### {section_name.replace('_', ' ').title()}")
                lines.append("")
                if isinstance(rows, list) and rows:
                    cols = list(rows[0].keys())
                    header = "| " + " | ".join(cols) + " |"
                    sep = "| " + " | ".join("---" for _ in cols) + " |"
                    lines.append(header)
                    lines.append(sep)
                    for row in rows[:10]:  # Cap display
                        vals = [str(row.get(c, "")) for c in cols]
                        lines.append("| " + " | ".join(vals) + " |")

    # Suggested next actions
    if ctx.get("suggested_next_actions"):
        lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Suggested Next Actions"))

    return "\n".join(lines)
