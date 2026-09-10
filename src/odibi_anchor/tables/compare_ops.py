"""Comparison operations — diff and validate DataFrames.

Compare two DataFrames for row-level differences (CDC-style delete detection)
or structural/value-level validation (cross-check).

All functions are dual-engine (Spark + Pandas) with auto-detection.

Usage:
    from odibi_anchor.tables.compare_ops import cross_check, detect_deletes"""

import logging
from typing import Any

from odibi_anchor._utils.engine_utils import is_spark_df

logger = logging.getLogger(__name__)

ALL_CHECKS = ["row_count", "schema", "columns"]


def cross_check(
    source_df,
    target_df,
    checks: list[str] | None = None,
    engine: str = "spark",
) -> dict[str, Any]:
    """Perform validation checks between two DataFrames.

    Args:
        source_df: Source/expected DataFrame.
        target_df: Target/actual DataFrame.
        checks: List of check names to perform. Options:
            - "row_count": Verify row counts match.
            - "schema": Verify schemas match (column names and types).
            - "columns": Verify column names match (ignores types).
            If None, runs all checks.
        engine: "spark" or "pandas". Auto-detected from source_df.

    Returns:
        Dict with check results: {"check_name": {"passed": bool, "details": str}, ...}
    """
    if is_spark_df(source_df):
        engine = "spark"
    elif engine == "spark" and not is_spark_df(source_df):
        engine = "pandas"

    if checks is None:
        checks = list(ALL_CHECKS)

    results = {}
    for check in checks:
        if check == "row_count":
            results["row_count"] = _check_row_count(source_df, target_df, engine)
        elif check == "schema":
            results["schema"] = _check_schema(source_df, target_df, engine)
        elif check == "columns":
            results["columns"] = _check_columns(source_df, target_df)
        else:
            logger.warning("Unknown check '%s', skipping.", check)

    passed_all = all(r["passed"] for r in results.values())
    logger.info("Cross-check completed: %s", "ALL PASSED" if passed_all else "SOME FAILED")
    return results


def _check_row_count(source_df, target_df, engine) -> dict[str, int]:
    """Compare row counts between source and target."""
    if engine == "spark":
        source_count = source_df.count()
        target_count = target_df.count()
    else:
        source_count = len(source_df)
        target_count = len(target_df)

    passed = source_count == target_count
    details = f"source={source_count}, target={target_count}"
    if not passed:
        details += f" (diff={target_count - source_count})"
    return {"passed": passed, "details": details}


def _check_schema(source_df, target_df, engine) -> dict[str, list]:
    """Compare schemas between source and target DataFrames."""
    if engine == "spark":
        source_schema = {f.name: str(f.dataType) for f in source_df.schema.fields}
        target_schema = {f.name: str(f.dataType) for f in target_df.schema.fields}
    else:
        source_schema = {col: str(dtype) for col, dtype in source_df.dtypes.items()}
        target_schema = {col: str(dtype) for col, dtype in target_df.dtypes.items()}

    passed = source_schema == target_schema
    if passed:
        details = "Schemas match."
    else:
        missing_in_target = set(source_schema) - set(target_schema)
        extra_in_target = set(target_schema) - set(source_schema)
        type_mismatches = {
            k: (source_schema[k], target_schema[k])
            for k in set(source_schema) & set(target_schema)
            if source_schema[k] != target_schema[k]
        }
        parts = []
        if missing_in_target:
            parts.append(f"missing_in_target={missing_in_target}")
        if extra_in_target:
            parts.append(f"extra_in_target={extra_in_target}")
        if type_mismatches:
            parts.append(f"type_mismatches={type_mismatches}")
        details = "; ".join(parts)
    return {"passed": passed, "details": details}


def _check_columns(source_df, target_df) -> dict[str, list]:
    """Compare column sets and flag missing/extra columns."""
    source_cols = set(source_df.columns)
    target_cols = set(target_df.columns)

    passed = source_cols == target_cols
    if passed:
        details = "Column names match."
    else:
        missing = source_cols - target_cols
        extra = target_cols - source_cols
        parts = []
        if missing:
            parts.append(f"missing_in_target={missing}")
        if extra:
            parts.append(f"extra_in_target={extra}")
        details = "; ".join(parts)
    return {"passed": passed, "details": details}


def detect_deletes(
    spark,
    current_df,
    previous_df,
    keys: list[str],
    threshold: float = 0.1,
    soft_delete_col: str = "_is_deleted",
) -> Any:
    """Detect deleted records by comparing current vs previous snapshots.

    Compares keys in previous_df against current_df. Keys present in previous
    but absent in current are treated as deletes. The result is the current_df
    with deleted rows from previous appended and flagged.

    Args:
        spark: SparkSession (None for Pandas).
        current_df: Current snapshot DataFrame.
        previous_df: Previous snapshot DataFrame.
        keys: Key columns to identify unique records.
        threshold: Maximum fraction of deletes allowed (0.0 to 1.0).
            If exceeded, raises ValueError to prevent accidental mass deletes.
        soft_delete_col: Name of the boolean column to flag deleted rows.

    Returns:
        DataFrame with all current rows (soft_delete_col=False) plus deleted
        rows from previous (soft_delete_col=True).

    Raises:
        ValueError: If delete percentage exceeds threshold.
    """
    if is_spark_df(current_df):
        return _detect_deletes_spark(spark, current_df, previous_df, keys, threshold, soft_delete_col)
    else:
        return _detect_deletes_pandas(current_df, previous_df, keys, threshold, soft_delete_col)


def _detect_deletes_spark(spark, current_df, previous_df, keys, threshold, soft_delete_col) -> list[dict]:
    """Detect rows in source missing from target using Spark."""
    from pyspark.sql import functions as F

    current_keys = current_df.select(keys).distinct()
    previous_keys = previous_df.select(keys).distinct()

    deleted_keys = previous_keys.join(current_keys, on=keys, how="left_anti")

    previous_count = previous_keys.count()
    deleted_count = deleted_keys.count()

    logger.info("Delete detection: %d previous keys, %d deleted keys", previous_count, deleted_count)

    if previous_count > 0 and (deleted_count / previous_count) > threshold:
        raise ValueError(
            f"Delete percentage {deleted_count / previous_count:.2%} exceeds "
            f"threshold {threshold:.2%} ({deleted_count}/{previous_count} keys). "
            f"Aborting to prevent accidental mass deletes."
        )

    current_marked = current_df.withColumn(soft_delete_col, F.lit(False).cast("boolean"))

    deleted_rows = previous_df.join(deleted_keys, on=keys, how="inner")
    deleted_rows = deleted_rows.withColumn(soft_delete_col, F.lit(True).cast("boolean"))

    result = current_marked.unionByName(deleted_rows, allowMissingColumns=True)

    logger.info("Result contains %d current rows + %d deleted rows", current_df.count(), deleted_count)
    return result


def _detect_deletes_pandas(current_df, previous_df, keys, threshold, soft_delete_col) -> list[dict]:
    """Detect rows in source missing from target using Pandas."""
    import pandas as pd

    current_keys = current_df[keys].drop_duplicates()
    previous_keys = previous_df[keys].drop_duplicates()

    merged = previous_keys.merge(current_keys, on=keys, how="left", indicator=True)
    deleted_keys = merged[merged["_merge"] == "left_only"][keys]

    previous_count = len(previous_keys)
    deleted_count = len(deleted_keys)

    logger.info("Delete detection: %d previous keys, %d deleted keys", previous_count, deleted_count)

    if previous_count > 0 and (deleted_count / previous_count) > threshold:
        raise ValueError(
            f"Delete percentage {deleted_count / previous_count:.2%} exceeds "
            f"threshold {threshold:.2%} ({deleted_count}/{previous_count} keys). "
            f"Aborting to prevent accidental mass deletes."
        )

    current_marked = current_df.copy()
    current_marked[soft_delete_col] = False

    deleted_rows = previous_df.merge(deleted_keys, on=keys, how="inner")
    deleted_rows[soft_delete_col] = True

    result = pd.concat([current_marked, deleted_rows], ignore_index=True)
    result[soft_delete_col] = result[soft_delete_col].astype(bool)

    logger.info("Result contains %d current rows + %d deleted rows", len(current_df), deleted_count)
    return result
