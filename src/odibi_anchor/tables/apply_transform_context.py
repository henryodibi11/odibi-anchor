"""Apply a transform plan to a DataFrame.

Executes the steps from transform_plan_context programmatically — no exec().
Each step action maps to a concrete handler function that interprets the
step dict and applies the transformation.

Supports both Pandas and Spark DataFrames with engine auto-detection.

Example:
    >>> from odibi_anchor.tables import apply_transform_context
    >>> result = apply_transform_context(df, plan_ctx)
    >>> result["df"]  # transformed DataFrame
    >>> result["steps_applied"]  # [1, 2, 3]
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

from odibi_anchor._utils.contract import build_base_context, finalize_context, validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

logger = logging.getLogger(__name__)


# ── SQL apply (self-contained — generate-and-run, no external pipeline runner) ─


def _delta_latest_version(spark: Any, target: str) -> int | None:
    """Best-effort current Delta version of target (for rollback guidance)."""
    try:
        rows = spark.sql(f"DESCRIBE HISTORY {target} LIMIT 1").collect()
        return int(rows[0]["version"]) if rows else None
    except Exception:  # not Delta / no history / table absent
        return None


def apply_sql(
    code_sql: str,
    *,
    spark: Any = None,
    target: str | None = None,
    mode: str = "view",
) -> dict[str, Any]:
    """Run a generated cleanup SQL projection. Fully self-contained.

    Resolves the active SparkSession itself and executes the SQL — there is no
    dependency on any external pipeline runner.

    Args:
        code_sql: The ``code_sql`` projection from ``transform_plan_context``.
            A trailing ``-- REVIEW`` comment block is harmless (ignored by Spark).
        spark: A SparkSession. If None, the active session is used.
        target: Fully-qualified ``catalog.schema.table`` (required for mode="table").
        mode: ``"view"`` (default) runs the SELECT and returns the DataFrame
            without persisting; ``"table"`` runs ``CREATE OR REPLACE TABLE target
            AS <sql>`` (rollback via Delta time travel).

    Returns:
        dict: ``{kind, mode, target, prior_version, row_count, df}``.
    """
    if not code_sql or not str(code_sql).strip():
        raise ValueError("code_sql is empty — nothing to run.")
    if "{source}" in code_sql or "{target}" in code_sql:
        raise ValueError(
            "code_sql still contains a {source}/{target} placeholder — substitute "
            "the real table name(s) before applying."
        )
    if mode not in ("view", "table"):
        raise ValueError(f"mode must be 'view' or 'table', got {mode!r}.")

    if spark is None:
        try:
            from pyspark.sql import SparkSession
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyspark is not available; cannot apply SQL.") from exc
        spark = SparkSession.getActiveSession()
        if spark is None:
            raise RuntimeError(
                "No active SparkSession found. Pass spark=... explicitly "
                "(self-contained apply still needs a session to run against)."
            )

    if mode == "view":
        df = spark.sql(code_sql)
        return {"kind": "apply_sql", "mode": "view", "target": None,
                "prior_version": None, "row_count": None, "df": df}

    if not target:
        raise ValueError("mode='table' requires target='catalog.schema.table'.")
    prior_version = _delta_latest_version(spark, target)
    spark.sql(f"CREATE OR REPLACE TABLE {target} AS\n{code_sql}")
    df = spark.table(target)
    return {"kind": "apply_sql", "mode": "table", "target": target,
            "prior_version": prior_version, "row_count": df.count(), "df": df}


# ── Engine Detection ──────────────────────────────────────────────────────────


def _is_spark_dataframe(df: Any) -> bool:
    """Check if df is a Spark DataFrame."""
    try:
        from pyspark.sql import DataFrame as SparkDataFrame
        return isinstance(df, SparkDataFrame)
    except ImportError:
        return False


def _detect_engine(df: Any) -> str:
    """Auto-detect engine from DataFrame type."""
    if _is_spark_dataframe(df):
        return "spark"
    if isinstance(df, pd.DataFrame):
        return "pandas"
    raise TypeError(f"Expected pandas or Spark DataFrame, got {type(df).__name__}")


def _row_count(df: Any, engine: str) -> int:
    """Get row count for either engine."""
    if engine == "spark":
        return df.count()
    return len(df)


def _estimate_memory_bytes(row_count: int, col_count: int, n_checkpoints: int) -> int:
    """Estimate total memory for cached Spark checkpoints.

    Uses a conservative heuristic of 64 bytes per cell (covers numerics,
    short strings, and JVM object overhead). Actual memory varies by
    data types, compression, and storage level.
    """
    bytes_per_cell = 64  # conservative average across mixed types
    per_checkpoint = row_count * col_count * bytes_per_cell
    return per_checkpoint * n_checkpoints


def _format_bytes(n_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if n_bytes >= 1_073_741_824:
        return f"{n_bytes / 1_073_741_824:.1f} GB"
    if n_bytes >= 1_048_576:
        return f"{n_bytes / 1_048_576:.0f} MB"
    if n_bytes >= 1024:
        return f"{n_bytes / 1024:.0f} KB"
    return f"{n_bytes} B"


# ── Pandas Step Handlers ──────────────────────────────────────────────────────


def _build_rename_map(columns: list[str]) -> dict[str, str]:
    """Build a rename map from column names to snake_case, handling collisions.

    When multiple columns map to the same snake_case name, suffixes _2, _3, etc.
    are appended to avoid duplicate column names.

    Example:
        ["Foo Bar", "foo_bar", "FOO BAR"] → {"Foo Bar": "foo_bar_2", "foo_bar": "foo_bar", "FOO BAR": "foo_bar_3"}
    """
    rename_map: dict[str, str] = {}
    seen: dict[str, int] = {}  # snake_name → count of times seen

    for col in columns:
        snake = re.sub(r"[^\w]+", "_", col).strip("_").lower()

        if snake in seen:
            seen[snake] += 1
            snake = f"{snake}_{seen[snake]}"
        else:
            seen[snake] = 1

    # Second pass: if a snake name was seen >1 times, suffix the FIRST occurrence too?
    # No — keep first occurrence as-is, suffix subsequent ones. Rebuild:
    seen_counts: dict[str, int] = {}
    for col in columns:
        base_snake = re.sub(r"[^\w]+", "_", col).strip("_").lower()
        seen_counts[base_snake] = seen_counts.get(base_snake, 0) + 1

    # Now build the actual map
    used: dict[str, int] = {}  # base_snake → next suffix number
    for col in columns:
        base_snake = re.sub(r"[^\w]+", "_", col).strip("_").lower()

        if seen_counts[base_snake] == 1:
            # No collision — use as-is
            rename_map[col] = base_snake
        else:
            # Collision — first gets base, subsequent get _2, _3...
            occurrence = used.get(base_snake, 0) + 1
            used[base_snake] = occurrence
            if occurrence == 1:
                rename_map[col] = base_snake
            else:
                rename_map[col] = f"{base_snake}_{occurrence}"

    return rename_map


def _apply_standardize_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Rename columns to snake_case with collision detection."""
    rename_map = step.get("rename_map") or _build_rename_map(step["columns"])

    return df.rename(columns=rename_map)


def _apply_null_cleanup_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Replace null-like string values with None."""
    col = step["columns"][0]
    if col not in df.columns:
        return df

    values = step.get("values")
    if values is None:
        match = re.search(r"\.replace\(\[(.+?)\],\s*None\)", step["code_pandas"])
        values_str = match.group(1) if match else ""
        values = [v.strip().strip("'\"") for v in values_str.split(",") if v.strip()]
    if values:
        df = df.copy()
        df[col] = df[col].replace(values, None)

    return df


def _apply_cast_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Cast column to numeric type."""
    col = step["columns"][0]
    if col not in df.columns:
        return df

    df = df.copy()
    code = step["code_pandas"]

    if 'astype("Int64")' in code:
        # Integer cast
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    else:
        # Float cast
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def _apply_boolean_cast_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Cast column to boolean via value mapping."""
    col = step["columns"][0]
    if col not in df.columns:
        return df

    df = df.copy()
    bool_map = {
        "true": True, "yes": True, "y": True, "1": True, "t": True, "on": True,
        "false": False, "no": False, "n": False, "0": False, "f": False, "off": False,
    }
    df[col] = df[col].astype(str).str.lower().map(bool_map)

    return df


def _apply_date_parse_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Parse string column as date."""
    col = step["columns"][0]
    if col not in df.columns:
        return df

    df = df.copy()
    code = step["code_pandas"]

    # Extract format if present
    fmt_match = re.search(r'format="([^"]+)"', code)
    if fmt_match:
        fmt = fmt_match.group(1)
        df[col] = pd.to_datetime(df[col], format=fmt, errors="coerce").dt.date
    else:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    return df


def _apply_drop_constant_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Drop constant or entirely null columns."""
    cols_to_drop = [c for c in step["columns"] if c in df.columns]
    if cols_to_drop:
        df = df.drop(columns=cols_to_drop, errors="ignore")
    return df


def _apply_dedup_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Deduplicate using sort + drop_duplicates."""
    code = step["code_pandas"]
    step_columns = step.get("keys") or step.get("columns") or []
    subset_cols = [c for c in step_columns if c in df.columns]
    if not subset_cols:
        if not step_columns:
            # columns=[] or None means "dedup on ALL columns"
            return df.drop_duplicates(keep="first")
        # Columns were specified but none exist in the DataFrame — skip
        return df

    # Extract sort column if present
    sort_col = step.get("order_column")
    if not sort_col:
        sort_match = re.search(r'sort_values\(by=\["([^"]+)"\]', code)
        sort_col = sort_match.group(1) if sort_match else None
    if sort_col:
        if sort_col in df.columns:
            ascending = str(step.get("order_direction", "desc")).lower() == "asc"
            df = df.sort_values(by=[sort_col], ascending=[ascending])

    df = df.drop_duplicates(subset=subset_cols, keep="first")
    return df


# ── Spark Step Handlers ───────────────────────────────────────────────────────


def _apply_standardize_spark(df: Any, step: dict) -> Any:
    """Rename columns to snake_case in Spark with collision detection.

    Uses toDF() for positional rename — avoids withColumnRenamed which
    renames ALL matching columns (case-insensitive) and cascades on duplicates.
    """
    new_names = step.get("output_columns")
    if not new_names or len(new_names) != len(df.columns):
        rename_map = step.get("rename_map") or _build_rename_map(list(df.columns))
        new_names = [rename_map.get(c, c) for c in df.columns]
    return df.toDF(*new_names)


def _apply_null_cleanup_spark(df: Any, step: dict) -> Any:
    """Replace null-like strings with None in Spark."""
    from pyspark.sql import functions as F

    col = step["columns"][0]
    if col not in df.columns:
        return df

    # Native Spark: replace values with None using when/otherwise.
    values = step.get("values")
    if values is None:
        code_match = re.search(r"\.replace\(\[(.+?)\],\s*None\)", step["code_pandas"])
        values_str = code_match.group(1) if code_match else ""
        values = [v.strip().strip("'\"") for v in values_str.split(",") if v.strip()]
    if values:
        df = df.withColumn(
            col,
            F.when(F.col(col).isin(values), F.lit(None)).otherwise(F.col(col)),
        )

    return df


def _apply_cast_spark(df: Any, step: dict) -> Any:
    """Cast column to numeric type in Spark."""
    from pyspark.sql import functions as F

    col = step["columns"][0]
    if col not in df.columns:
        return df

    code = step["code_spark"]
    if '"long"' in code:
        df = df.withColumn(col, F.col(col).cast("long"))
    else:
        df = df.withColumn(col, F.col(col).cast("double"))

    return df


def _apply_boolean_cast_spark(df: Any, step: dict) -> Any:
    """Cast column to boolean in Spark."""
    from pyspark.sql import functions as F

    col = step["columns"][0]
    if col not in df.columns:
        return df

    # Native Spark: normalize case and map known representations.
    lower_col = F.lower(F.col(col))
    true_vals = ["true", "yes", "y", "1", "t", "on"]
    false_vals = ["false", "no", "n", "0", "f", "off"]

    df = df.withColumn(
        col,
        F.when(lower_col.isin(true_vals), F.lit(True))
        .when(lower_col.isin(false_vals), F.lit(False))
        .otherwise(F.lit(None))
    )
    return df


def _apply_date_parse_spark(df: Any, step: dict) -> Any:
    """Parse string column as date in Spark.

    Uses try_to_date (Spark 3.4+/DBR 13.3+) for fault tolerance —
    invalid values become NULL instead of raising DateTimeException.
    """
    from pyspark.sql import functions as F

    col = step["columns"][0]
    if col not in df.columns:
        return df

    code = step["code_spark"]
    # Extract format: F.to_date(F.col("col"), "MM/dd/yyyy")
    fmt_match = re.search(r'to_date\(F\.col\("[^"]+"\),\s*"([^"]+)"\)', code)
    if fmt_match:
        spark_fmt = fmt_match.group(1)
        df = df.withColumn(col, F.try_to_date(F.col(col), F.lit(spark_fmt)))
    else:
        df = df.withColumn(col, F.try_to_date(F.col(col)))

    return df


def _apply_drop_constant_spark(df: Any, step: dict) -> Any:
    """Drop columns in Spark."""
    cols_to_drop = [c for c in step["columns"] if c in df.columns]
    if cols_to_drop:
        df = df.drop(*cols_to_drop)
    return df


def _collision_safe_temp_column(columns: list[str], base: str = "_cw_row_number") -> str:
    """Return a helper name safe under Spark's case-insensitive resolver."""
    used = {name.casefold() for name in columns}
    candidate = base
    while candidate.casefold() in used:
        candidate += "_"
    return candidate


def _apply_dedup_spark(df: Any, step: dict) -> Any:
    """Deduplicate in Spark."""
    code = step["code_spark"]
    # Native Spark window dedup.
    from pyspark.sql import functions as F
    from pyspark.sql.window import Window

    step_columns = step.get("keys") or step.get("columns") or []
    subset_cols = [c for c in step_columns if c in df.columns]
    if not subset_cols:
        if not step_columns:
            # columns=[] or None means "dedup on ALL columns"
            return df.drop_duplicates(keep="first")
        # Columns were specified but none exist in the DataFrame — skip
        return df

    # Extract order_by column
    order_col = step.get("order_column")
    direction = str(step.get("order_direction", "desc")).upper()
    if not order_col:
        order_match = re.search(r'order_by="(\w+)\s+(DESC|ASC)"', code)
        if not order_match:
            order_match = re.search(r'orderBy\(F\.col\("(\w+)"\)\.(desc|asc)\(\)\)', code, re.IGNORECASE)
        if order_match:
            order_col = order_match.group(1)
            direction = order_match.group(2).upper()
    if order_col:
        if order_col in df.columns:
            w = Window.partitionBy(*subset_cols)
            if direction == "DESC":
                w = w.orderBy(F.col(order_col).desc())
            else:
                w = w.orderBy(F.col(order_col).asc())
            temp_col = _collision_safe_temp_column(df.columns)
            df = df.withColumn(temp_col, F.row_number().over(w))
            df = df.filter(F.col(temp_col) == 1).drop(temp_col)
    else:
        df = df.dropDuplicates(subset_cols)

    return df


def _apply_trim_pandas(df: pd.DataFrame, step: dict) -> pd.DataFrame:
    """Strip leading/trailing whitespace from a string column.

    Per-element so non-string values (None, numbers in a mixed object column)
    pass through unchanged — unlike Series.str.strip(), which coerces them to NaN.
    """
    col = step["columns"][0]
    if col not in df.columns:
        return df
    df = df.copy()
    df[col] = df[col].map(lambda v: v.strip() if isinstance(v, str) else v)
    return df


def _apply_trim_spark(df: Any, step: dict) -> Any:
    """Strip leading/trailing whitespace from a string column (Spark)."""
    from pyspark.sql import functions as F

    col = step["columns"][0]
    if col not in df.columns:
        return df
    return df.withColumn(col, F.trim(F.col(col)))


# ── Dispatcher ────────────────────────────────────────────────────────────────

_PANDAS_HANDLERS: dict[str, Any] = {
    "standardize_columns": _apply_standardize_pandas,
    "trim": _apply_trim_pandas,
    "null_cleanup": _apply_null_cleanup_pandas,
    "cast": _apply_cast_pandas,
    "boolean_cast": _apply_boolean_cast_pandas,
    "date_parse": _apply_date_parse_pandas,
    "drop_constant": _apply_drop_constant_pandas,
    "dedup": _apply_dedup_pandas,
}

_SPARK_HANDLERS: dict[str, Any] = {
    "standardize_columns": _apply_standardize_spark,
    "trim": _apply_trim_spark,
    "null_cleanup": _apply_null_cleanup_spark,
    "cast": _apply_cast_spark,
    "boolean_cast": _apply_boolean_cast_spark,
    "date_parse": _apply_date_parse_spark,
    "drop_constant": _apply_drop_constant_spark,
    "dedup": _apply_dedup_spark,
}


# ── Eviction Helper ───────────────────────────────────────────────────────────


def _evict_oldest_checkpoint(
    checkpoints: dict[int, Any], max_checkpoints: int, resolved_engine: str
) -> list[int]:
    """Evict oldest checkpoints when limit is exceeded.

    Removes and unpersists the oldest (lowest order number) checkpoints
    until the dict size is within the max_checkpoints limit.

    Args:
        checkpoints: Mutable dict of {step_order: df_state}.
        max_checkpoints: Maximum number of checkpoints to retain.
        resolved_engine: "spark" or "pandas".

    Returns:
        List of evicted step order numbers.
    """
    evicted: list[int] = []
    while len(checkpoints) > max_checkpoints:
        oldest_key = min(checkpoints.keys())
        oldest_df = checkpoints.pop(oldest_key)

        # Unpersist Spark checkpoints to free memory
        if resolved_engine == "spark" and _is_spark_dataframe(oldest_df):
            try:
                if oldest_df.is_cached:
                    oldest_df.unpersist(blocking=False)
            except Exception:
                try:
                    oldest_df.unpersist(blocking=False)
                except Exception:
                    pass  # SILENT-OK: Spark unpersist is best-effort cleanup

        evicted.append(oldest_key)

    if evicted:
        logger.info(
            "Evicted %d checkpoint(s) (steps %s) — max_checkpoints=%d",
            len(evicted), evicted, max_checkpoints,
        )

    return evicted


# ── Main Function ─────────────────────────────────────────────────────────────


def apply_transform_context(
    df: Any,
    plan_ctx: dict,
    *,
    steps: list[int] | None = None,
    dry_run: bool = False,
    min_confidence: float = 0.0,
    checkpoint: bool = True,
    spark_persist: str = "cache",
    auto_unpersist: bool = False,
    max_checkpoints: int | None = None,
    engine: str = "auto",
    output_format: str = "dict",
) -> dict | str:
    """Apply a transform plan to a DataFrame.

    Executes the steps from transform_plan_context programmatically.
    Each step action maps to a concrete handler — no exec() is used.

    Args:
        df: Input DataFrame (Pandas or Spark).
        plan_ctx: Output dict from transform_plan_context().
        steps: Optional list of step order numbers to execute.
            If None, all steps are executed. Use to selectively apply.
        dry_run: If True, validates steps without mutating the DataFrame.
            Returns what would be applied without executing.
        min_confidence: Minimum confidence threshold. Steps below this
            value are skipped with a warning in findings.
        checkpoint: If True, stores the DataFrame state before each step
            for undo/rollback. Access via result["checkpoints"][step_order].
            result["df_before"] always holds the original input.
        spark_persist: Spark checkpoint persistence mode when checkpoint=True.
            One of "none", "cache", or "local_checkpoint". Ignored for pandas.
        auto_unpersist: If True, automatically releases cached Spark checkpoints
            after all steps complete. Use for fire-and-forget pipelines where
            rollback is not needed. The result still contains df and df_before
            but checkpoints will be empty. Default False.
        max_checkpoints: Maximum number of checkpoints to retain. When the
            limit is reached, the oldest checkpoint (lowest step order) is
            unpersisted and evicted. Use to bound memory in long pipelines.
            Default None (unlimited — current behavior preserved).
            Evicted checkpoints cannot be rolled back to.
        engine: "auto", "pandas", or "spark". Auto-detects from df type.
        output_format: "dict" or "markdown".

    Returns:
        Context dict with keys:
            - df: Transformed DataFrame (same as input if dry_run=True)
            - df_before: Original input DataFrame (for full rollback)
            - checkpoints: Dict of {step_order: df_before_that_step} (if checkpoint=True)
            - steps_applied: List of step order numbers that were executed
            - steps_skipped: List of step orders skipped (confidence/selection)
            - row_count_before: Row count before transforms
            - row_count_after: Row count after transforms
            - rows_dropped: Difference in row counts
            - columns_renamed: Dict of old→new column names (if standardize applied)
            - step_audit: Per-step row count deltas
            - checkpoints_evicted: List of step orders evicted by max_checkpoints
            - Plus standard contract keys (kind, subject, summary, etc.)

    Raises:
        TypeError: If df is not a supported DataFrame type.
        ValueError: If plan_ctx is missing required keys or output_format invalid.
    """
    validate_output_format(output_format)

    # Validate plan_ctx kind
    expected_kind = "transform_plan_context"
    actual_kind = plan_ctx.get("kind") if isinstance(plan_ctx, dict) else None
    if actual_kind != expected_kind:
        error_msg = f"plan_ctx must have kind='{expected_kind}', got '{actual_kind}'"
        ctx = build_base_context(
            kind="apply_transform_context",
            subject=plan_ctx.get("subject", "unknown") if isinstance(plan_ctx, dict) else "unknown",
            summary=f"Invalid plan context: expected kind='{expected_kind}', got '{actual_kind}'",
            metrics={},
        )
        ctx.update({
            "error": error_msg,
            "findings": [f"Invalid plan_ctx.kind: '{actual_kind}' — expected '{expected_kind}'"],
            "risks": ["Cannot execute transform plan with invalid context type"],
            "df": df,
        })
        if output_format == "markdown":
            return render_apply_transform_report(ctx)
        return ctx

    # Resolve engine
    if engine == "auto":
        resolved_engine = _detect_engine(df)
    elif engine == "spark":
        if not _is_spark_dataframe(df):
            raise TypeError(f'engine="spark" requires a Spark DataFrame, got {type(df).__name__}')
        resolved_engine = "spark"
    else:
        if not isinstance(df, pd.DataFrame):
            raise TypeError(f'engine="pandas" requires a pandas DataFrame, got {type(df).__name__}')
        resolved_engine = "pandas"

    handlers = _SPARK_HANDLERS if resolved_engine == "spark" else _PANDAS_HANDLERS

    if spark_persist not in {"none", "cache", "local_checkpoint"}:
        raise ValueError(
            f"spark_persist must be one of none/cache/local_checkpoint, got {spark_persist!r}"
        )

    # Validate max_checkpoints
    if max_checkpoints is not None and max_checkpoints < 1:
        raise ValueError(
            f"max_checkpoints must be >= 1 or None (unlimited), got {max_checkpoints}"
        )

    # Get plan steps
    all_steps = plan_ctx.get("steps", [])
    subject = plan_ctx.get("subject", "unknown")

    # Filter steps by selection
    if steps is not None:
        selected_steps = [s for s in all_steps if s["order"] in steps]
    else:
        selected_steps = list(all_steps)

    # Track execution
    row_count_before = _row_count(df, resolved_engine)
    steps_applied: list[int] = []
    steps_skipped: list[int] = []
    skipped_reasons: list[str] = []
    step_audit: list[dict] = []
    columns_renamed: dict[str, str] = {}
    checkpoints: dict[int, Any] = {}
    checkpoints_evicted: list[int] = []
    current_df = df

    for step in selected_steps:
        action = step["action"]
        order = step["order"]
        confidence = step.get("confidence", 1.0)

        # Confidence gate
        if confidence < min_confidence:
            steps_skipped.append(order)
            skipped_reasons.append(
                f"Step {order} ({action}) skipped: confidence {confidence:.2f} < threshold {min_confidence:.2f}"
            )
            continue

        # Check handler exists
        handler = handlers.get(action)
        if handler is None:
            steps_skipped.append(order)
            skipped_reasons.append(f"Step {order} ({action}) skipped: no handler for action '{action}'")
            continue

        if dry_run:
            steps_applied.append(order)
            step_audit.append({
                "order": order,
                "action": action,
                "status": "dry_run",
                "confidence": confidence,
            })
            continue

        # Execute the step
        pre_count = _row_count(current_df, resolved_engine)
        if checkpoint and resolved_engine == "pandas":
            checkpoints[order] = current_df.copy()
        elif checkpoint and resolved_engine == "spark":
            spark_checkpoint = current_df
            if spark_persist == "cache":
                spark_checkpoint = spark_checkpoint.cache()
                spark_checkpoint.count()
            elif spark_persist == "local_checkpoint":
                spark_checkpoint = spark_checkpoint.localCheckpoint(eager=True)
            checkpoints[order] = spark_checkpoint
        elif checkpoint:
            checkpoints[order] = current_df

        # Evict oldest checkpoints if limit exceeded
        if checkpoint and max_checkpoints is not None:
            evicted = _evict_oldest_checkpoint(checkpoints, max_checkpoints, resolved_engine)
            checkpoints_evicted.extend(evicted)

        try:
            current_df = handler(current_df, step)
            post_count = _row_count(current_df, resolved_engine)
            steps_applied.append(order)

            audit_entry = {
                "order": order,
                "action": action,
                "status": "applied",
                "confidence": confidence,
                "rows_before": pre_count,
                "rows_after": post_count,
                "rows_delta": post_count - pre_count,
            }

            # Track renames
            if action == "standardize_columns":
                for col in step["columns"]:
                    snake = re.sub(r"[^\w]+", "_", col).strip("_").lower()
                    columns_renamed[col] = snake

            step_audit.append(audit_entry)
            logger.info(
                "Step %d (%s): %d → %d rows (Δ%d)",
                order, action, pre_count, post_count, post_count - pre_count,
            )

        except Exception as e:
            steps_skipped.append(order)
            skipped_reasons.append(f"Step {order} ({action}) failed: {e}")
            step_audit.append({
                "order": order,
                "action": action,
                "status": "error",
                "error": str(e),
            })
            logger.warning("Step %d (%s) failed: %s", order, action, e)

    # Final metrics
    row_count_after = _row_count(current_df, resolved_engine) if not dry_run else row_count_before
    rows_dropped = row_count_before - row_count_after

    # Build output
    metrics = {
        "total_steps_in_plan": len(all_steps),
        "steps_applied": len(steps_applied),
        "steps_skipped": len(steps_skipped),
        "row_count_before": row_count_before,
        "row_count_after": row_count_after,
        "rows_dropped": rows_dropped,
        "columns_renamed_count": len(columns_renamed),
        "engine": resolved_engine,
        "dry_run": dry_run,
        "spark_persist": spark_persist if resolved_engine == "spark" else "n/a",
        "checkpoints_stored": len(checkpoints),
        "checkpoints_evicted": len(checkpoints_evicted),
        "max_checkpoints": max_checkpoints,
        "est_checkpoint_memory_bytes": (
            _estimate_memory_bytes(row_count_before, len(df.columns) if hasattr(df, "columns") else 0, len(checkpoints))
            if checkpoint and resolved_engine == "spark" else 0
        ),
    }

    # Summary
    if dry_run:
        summary = (
            f"Dry run: {len(steps_applied)} steps would be applied to {subject} "
            f"({row_count_before:,} rows), {len(steps_skipped)} skipped"
        )
    else:
        summary = (
            f"Applied {len(steps_applied)} transforms to {subject}: "
            f"{row_count_before:,} → {row_count_after:,} rows "
            f"({rows_dropped:,} dropped), {len(steps_skipped)} skipped"
        )

    # Findings
    findings = []
    if steps_applied:
        findings.append(f"{len(steps_applied)} steps executed successfully")
    if columns_renamed:
        findings.append(f"{len(columns_renamed)} columns renamed to snake_case")
    if rows_dropped > 0:
        findings.append(f"{rows_dropped:,} rows removed (dedup or other filters)")
    if checkpoints_evicted:
        findings.append(
            f"{len(checkpoints_evicted)} checkpoint(s) evicted (steps {checkpoints_evicted}) "
            f"— max_checkpoints={max_checkpoints}"
        )
    findings.extend(skipped_reasons)

    # Risks
    risks = []
    if rows_dropped > row_count_before * 0.5:
        risks.append(f"Over 50% of rows dropped ({rows_dropped:,}/{row_count_before:,}) — verify dedup keys")
    error_steps = [a for a in step_audit if a.get("status") == "error"]
    if error_steps:
        risks.append(f"{len(error_steps)} step(s) failed during execution — review errors")

    # Memory pressure warning for Spark checkpoints
    if checkpoint and not auto_unpersist and resolved_engine == "spark" and not dry_run:
        n_checkpoints = len(checkpoints)
        col_count = len(current_df.columns)
        est_bytes = _estimate_memory_bytes(row_count_before, col_count, n_checkpoints)
        est_size = _format_bytes(est_bytes)

        if spark_persist == "cache" and n_checkpoints >= 5:
            risks.append(
                f"Memory pressure: {n_checkpoints} cached Spark checkpoints (~{est_size} estimated). "
                f"Call anchor('unpersist', result) when rollback is no longer needed, "
                f"or use auto_unpersist=True for fire-and-forget pipelines."
            )
        elif spark_persist == "cache" and est_bytes >= 1_073_741_824:
            # Fewer checkpoints but large data — still warn at 1 GB+
            risks.append(
                f"Memory pressure: {n_checkpoints} cached checkpoint(s) (~{est_size} estimated). "
                f"Large dataset — consider auto_unpersist=True or spark_persist='none'."
            )
        elif spark_persist == "local_checkpoint" and n_checkpoints >= 3:
            risks.append(
                f"Storage pressure: {n_checkpoints} local checkpoints (~{est_size} estimated on disk). "
                f"Call anchor('unpersist', result) to release, or use auto_unpersist=True."
            )

    # Eviction risk warning
    if checkpoints_evicted:
        risks.append(
            f"Rollback unavailable for evicted steps {checkpoints_evicted}. "
            f"Only steps {sorted(checkpoints.keys())} can be rolled back to."
        )

    # Suggested actions
    suggested_next_actions = []
    if not dry_run and steps_applied:
        suggested_next_actions.append("MUST: Run anchor('quality', df, keys=[...]) to validate output")
        suggested_next_actions.append("COULD: Compare row counts with source to verify completeness")
    if dry_run:
        suggested_next_actions.append("MUST: Re-run without dry_run=True to apply transforms")
    if steps_skipped:
        suggested_next_actions.append("COULD: Review skipped steps and adjust min_confidence if needed")
    if checkpoint and not auto_unpersist and len(checkpoints) > 0 and resolved_engine == "spark":
        suggested_next_actions.append(
            "MUST: Call anchor('unpersist', result) when rollback is no longer needed to free memory"
        )

    ctx = build_base_context(
        kind="apply_transform_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_next_actions,
        # Extra keys
        df=current_df if not dry_run else df,
        df_before=df,
        checkpoints=checkpoints if checkpoint else {},
        steps_applied=steps_applied,
        steps_skipped=steps_skipped,
        step_audit=step_audit,
        columns_renamed=columns_renamed,
        row_count_before=row_count_before,
        row_count_after=row_count_after,
        rows_dropped=rows_dropped,
        checkpoints_evicted=checkpoints_evicted,
    )

    # Auto-unpersist if requested (fire-and-forget mode)
    if auto_unpersist and not dry_run and resolved_engine == "spark":
        unpersist(ctx)

    return finalize_context(ctx, output_format, render_apply_transform_report)




# ── Rollback Helper ───────────────────────────────────────────────────────────


def rollback(result: dict, *, to: int | str) -> Any:
    """Roll back to a named or numbered checkpoint.

    Retrieves the DataFrame state captured before a specific step executed.
    Steps can be referenced by order number (int) or by action name (str).
    When using action name, returns the checkpoint before the FIRST matching
    step of that action type.

    Args:
        result: Output dict from apply_transform_context() (must have
            been run with checkpoint=True).
        to: Step order number (int) or action name (str).
            Valid action names: "standardize_columns", "null_cleanup",
            "cast", "boolean_cast", "date_parse", "drop_constant", "dedup".
            Special values: "start" returns df_before (original input).

    Returns:
        DataFrame at the requested checkpoint state.

    Raises:
        KeyError: If the checkpoint doesn't exist (step not executed or
            checkpoint=False was used).
        ValueError: If action name doesn't match any executed step.

    Example:
        >>> result = anchor("apply_transform", df, plan_ctx)
        >>> before_dedup = rollback(result, to="dedup")
        >>> before_step_3 = rollback(result, to=3)
        >>> original = rollback(result, to="start")
    """
    # Special case: "start" returns original input
    if to == "start":
        if "df_before" not in result:
            raise KeyError("No df_before in result — was checkpoint=True used?")
        return result["df_before"]

    checkpoints = result.get("checkpoints", {})
    if not checkpoints:
        raise KeyError(
            "No checkpoints available. Re-run apply_transform_context with checkpoint=True."
        )

    # Numeric step order
    if isinstance(to, int):
        if to not in checkpoints:
            available = sorted(checkpoints.keys())
            raise KeyError(
                f"No checkpoint for step {to}. Available: {available}"
            )
        return checkpoints[to]

    # Named action — resolve to step order via step_audit
    step_audit = result.get("step_audit", [])
    for entry in step_audit:
        if entry.get("action") == to and entry.get("status") == "applied":
            order = entry["order"]
            if order in checkpoints:
                return checkpoints[order]
            raise KeyError(
                f"Step {order} ({to}) was applied but no checkpoint stored. "
                f"Was checkpoint=True used?"
            )

    # No match found — provide helpful error
    available_actions = [e["action"] for e in step_audit if e.get("status") == "applied"]
    raise ValueError(
        f"No executed step with action '{to}'. "
        f"Available actions: {available_actions}"
    )



# ── Unpersist Helper ──────────────────────────────────────────────────────────


def unpersist(result: dict, *, blocking: bool = False) -> dict:
    """Release cached Spark checkpoints to free executor memory.

    Call when you no longer need rollback capability. Only affects Spark
    DataFrames that were persisted via spark_persist="cache" or
    "local_checkpoint". No-op for pandas results or spark_persist="none".

    Args:
        result: Output dict from apply_transform_context().
        blocking: If True, waits for all blocks to be removed from
            storage before returning. Default False (async release).

    Returns:
        The same result dict with checkpoints cleared (empty dict).
        The df and df_before references remain valid.

    Example:
        >>> result = anchor("apply_transform", spark_df, plan_ctx)
        >>> cleaned_df = result["df"]
        >>> # Done with rollback — free memory
        >>> anchor("unpersist", result)
    """
    checkpoints = result.get("checkpoints", {})
    released = 0

    for order, checkpoint_df in list(checkpoints.items()):
        if _is_spark_dataframe(checkpoint_df):
            try:
                if checkpoint_df.is_cached:
                    checkpoint_df.unpersist(blocking=blocking)
                    released += 1
            except Exception:
                # localCheckpoint DFs may not support is_cached;
                # best-effort unpersist
                try:
                    checkpoint_df.unpersist(blocking=blocking)
                    released += 1
                except Exception:
                    pass  # SILENT-OK: Spark unpersist is best-effort cleanup

    # Clear the dict to prevent accidental reuse
    result["checkpoints"] = {}

    if released > 0:
        logger.info("Unpersisted %d Spark checkpoint(s)", released)

    return result


# ── Render Function ───────────────────────────────────────────────────────────


def render_apply_transform_report(ctx: dict) -> str:
    """Render apply_transform_context output as markdown.

    Args:
        ctx: Context dict from apply_transform_context().

    Returns:
        Formatted markdown report string.
    """
    lines = render_header_lines(ctx, "Apply Transform")
    lines.extend(render_metrics_lines(ctx["metrics"]))
    lines.append("")

    # Step audit table
    step_audit = ctx.get("step_audit", [])
    if step_audit:
        lines.append("## Step Audit")
        lines.append("")
        lines.append("| Order | Action | Status | Rows Δ | Confidence |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in step_audit:
            delta = entry.get("rows_delta", "—")
            if isinstance(delta, int):
                delta = f"{delta:+d}" if delta != 0 else "0"
            lines.append(
                f"| {entry['order']} | {entry['action']} | {entry['status']} "
                f"| {delta} | {entry.get('confidence', '—'):.2f} |"
            )
        lines.append("")

    # Column renames
    columns_renamed = ctx.get("columns_renamed", {})
    if columns_renamed:
        lines.append("## Columns Renamed")
        lines.append("")
        for old, new in list(columns_renamed.items())[:10]:
            lines.append(f"- `{old}` → `{new}`")
        if len(columns_renamed) > 10:
            lines.append(f"- ... and {len(columns_renamed) - 10} more")
        lines.append("")

    # Findings
    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))
    lines.append("")

    # Risks
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))
    lines.append("")

    # Next actions
    lines.extend(render_bullet_section(ctx.get("suggested_next_actions", []), "## Suggested Next Actions"))

    return "\n".join(lines)
