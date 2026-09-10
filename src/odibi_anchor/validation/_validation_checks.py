"""Engine-specific validation check functions (pandas & Spark).

Internal module — not part of the public API.
"""

from __future__ import annotations

import logging
from typing import Any

from odibi_anchor.validation.validation_summary_context import (
    _build_fix_expr,
    _build_quarantine_call,
    _detect_schema_mismatches,
    _evaluate_not_stale,
    _evaluate_row_count_range,
    _resolve_severity,
    _rule_detail,
    _rule_id,
    _DF_LEVEL_RULE_TYPES,
    _AGGREGATE_RULE_TYPES,
)
from odibi_anchor.validation._validation_build import (
    _build_context,
    _df_to_records,
    _rule_columns,
)

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Pandas evaluation
# -------------------------------------------------------------------


def _evaluate_rule_pandas(
    df: Any, rule: dict[str, Any]
) -> Any:
    """Evaluate a single rule and return a boolean Series.

    True = row passes the rule. Uses pandas operations.

    Args:
        df: Pandas DataFrame.
        rule: Rule dict.

    Returns:
        Pandas boolean Series (True = pass).
    """
    import pandas as pd

    n = len(df)
    rule_type = rule.get("type", "")

    if rule_type == "not_null":
        cols = [
            c for c in rule.get("columns", [])
            if c in df.columns
        ]
        if not cols:
            return pd.Series([True] * n, index=df.index)
        mask = df[cols[0]].notna()
        for c in cols[1:]:
            mask = mask & df[c].notna()
        return mask

    if rule_type == "unique":
        cols = [
            c for c in rule.get("columns", [])
            if c in df.columns
        ]
        if not cols:
            return pd.Series([True] * n, index=df.index)
        return ~df.duplicated(subset=cols, keep=False)

    if rule_type == "accepted_values":
        col = rule.get("column", "")
        if col not in df.columns:
            return pd.Series([True] * n, index=df.index)
        return df[col].isin(rule.get("values", []))

    if rule_type == "range":
        col = rule.get("column", "")
        if col not in df.columns:
            return pd.Series([True] * n, index=df.index)
        mask = pd.Series([True] * n, index=df.index)
        mn = rule.get("min")
        mx = rule.get("max")
        try:
            if mn is not None:
                mask = mask & (df[col] >= mn)
            if mx is not None:
                mask = mask & (df[col] <= mx)
        except (TypeError, ValueError):
            logger.warning(
                "range check on '%s' failed (type mismatch)",
                col,
            )
            return pd.Series([True] * n, index=df.index)
        return mask

    if rule_type == "regex":
        col = rule.get("column", "")
        if col not in df.columns:
            return pd.Series([True] * n, index=df.index)
        pattern = rule.get("pattern", "")
        try:
            return (
                df[col].isna()
                | df[col].astype(str).str.match(
                    pattern, na=True
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "regex check on '%s' failed: %s", col, exc
            )
            return pd.Series([True] * n, index=df.index)

    if rule_type == "custom_sql":
        cond = rule.get("condition", "True")
        try:
            valid_idx = df.query(cond).index
            return pd.Series(
                df.index.isin(valid_idx), index=df.index
            )
        except Exception as exc:
            logger.warning(
                "custom_sql evaluation failed: %s", exc
            )
            return pd.Series([True] * n, index=df.index)

    if rule_type == "referential_integrity":
        col = rule.get("column", "")
        if col not in df.columns:
            return pd.Series([True] * n, index=df.index)
        ref_values = set(rule.get("reference_values", []))
        if not ref_values:
            return pd.Series([True] * n, index=df.index)
        # Non-null values must be in reference set; nulls pass
        return df[col].isna() | df[col].isin(ref_values)

    if rule_type == "not_stale":
        # not_stale is DF-level — all-True if fresh, all-False if stale
        col = rule.get("column", "")
        if col not in df.columns:
            return pd.Series([True] * n, index=df.index)
        result = _evaluate_not_stale(df[col], rule, "pandas")
        if result["passed"]:
            return pd.Series([True] * n, index=df.index)
        else:
            return pd.Series([False] * n, index=df.index)

    # Unknown rule type — pass all
    return pd.Series([True] * n, index=df.index)


def _validate_pandas(
    df: Any,
    rules: list[dict[str, Any]],
    *,
    subject: str,
    severity_map: dict[str, str] | None,
    sample_failures: int,
) -> dict[str, Any]:
    """Run validation on a pandas DataFrame.

    Evaluates all rules, computes per-rule metrics, collects
    sample failures, generates fix code, and builds output.
    Rules with schema mismatches are skipped and marked as
    errors instead of being evaluated.

    Args:
        df: Pandas DataFrame.
        rules: List of rule dicts.
        subject: Human label.
        severity_map: Severity overrides.
        sample_failures: Max samples per failed rule.

    Returns:
        Validation context dictionary.
    """
    import pandas as pd


    total_rows = len(df)
    rule_results: list[dict[str, Any]] = []
    all_pass_masks: list[Any] = []

    # Handle DF-level rules (row_count_*) separately — they don't produce per-row masks
    df_level_rules = [r for r in rules if r.get("type", "") in _DF_LEVEL_RULE_TYPES]
    per_row_rules = [r for r in rules if r.get("type", "") not in _DF_LEVEL_RULE_TYPES]

    for rule in df_level_rules:
        rid = _rule_id(rule)
        severity = _resolve_severity(rule, severity_map)
        result = _evaluate_row_count_range(total_rows, rule)
        rule_results.append({
            "rule_id": rid,
            "rule_type": rule.get("type", "unknown"),
            "columns": [],
            "severity": severity,
            "passed": result["passed"],
            "failed_count": 0 if result["passed"] else total_rows,
            "failed_rate": 0.0 if result["passed"] else 1.0,
            "detail": result["detail"],
            "fix_expr": "",
            "sample_failures": [],
            "source": rule.get("source", "explicit"),
        })

    # Pre-filter: detect schema mismatches BEFORE evaluation
    schema_findings = _detect_schema_mismatches(
        df, per_row_rules, "pandas"
    )
    mismatched_ids = {
        f.get("rule_id", "")
        for f in schema_findings
        if f.get("check_type") == "rule_schema_mismatch"
    }

    for rule in per_row_rules:
        rid = _rule_id(rule)
        severity = _resolve_severity(rule, severity_map)

        # Skip evaluation for schema-mismatched rules
        if rid in mismatched_ids:
            all_pass_masks.append(
                pd.Series([False] * total_rows, index=df.index)
            )
            mismatch_detail = next(
                (f["detail"] for f in schema_findings
                 if f.get("rule_id") == rid),
                "Schema mismatch — rule not evaluated",
            )
            rule_results.append({
                "rule_id": rid,
                "rule_type": rule.get("type", "unknown"),
                "columns": _rule_columns(rule),
                "severity": severity,
                "passed": False,
                "status": "error",
                "failed_count": total_rows,
                "failed_rate": 1.0,
                "detail": mismatch_detail,
                "fix_expr": (
                    "# Cannot evaluate: column type "
                    "incompatible with rule"
                ),
                "sample_failures": [],
            })
            continue

        mask = _evaluate_rule_pandas(df, rule)
        all_pass_masks.append(mask)

        failed_count = int((~mask).sum())
        passed = failed_count == 0
        failed_rate = (
            failed_count / total_rows
            if total_rows > 0 else 0.0
        )

        # Fix expression (v2)
        fix_expr = ""
        if not passed:
            fix_expr = _build_fix_expr(rule, "pandas")

        # Collect samples
        samples: list[dict[str, Any]] = []
        if not passed and sample_failures > 0:
            failing = df[~mask].head(sample_failures)
            samples = _df_to_records(failing)

        rule_results.append({
            "rule_id": rid,
            "rule_type": rule.get("type", "unknown"),
            "columns": _rule_columns(rule),
            "severity": severity,
            "passed": passed,
            "failed_count": failed_count,
            "failed_rate": round(failed_rate, 6),
            "detail": _rule_detail(rule),
            "fix_expr": fix_expr,
            "sample_failures": samples,
            "source": rule.get("source", "explicit"),
        })

    # Quarantine call (v2)
    quarantine_call = _build_quarantine_call(
        rules, rule_results
    )

    return _build_context(
        rule_results=rule_results,
        all_pass_masks=all_pass_masks,
        total_rows=total_rows,
        engine_name="pandas",
        subject=subject,
        df=df,
        schema_findings=schema_findings,
        quarantine_call=quarantine_call,
        rules=rules,
    )


# -------------------------------------------------------------------
# Spark evaluation
# -------------------------------------------------------------------


def _evaluate_rules_spark_batch(
    df: Any, rules: list[dict[str, Any]]
) -> list[int]:
    """Evaluate all rules in a single Spark action.

    Builds a select with sum(when(~rule, 1)) for each rule,
    then collects in one pass.

    Args:
        df: Spark DataFrame.
        rules: List of rule dicts.

    Returns:
        List of failure counts (one per rule).
    """
    from pyspark.sql import functions as F

    exprs = []
    for i, rule in enumerate(rules):
        mask_expr = _spark_rule_mask(df, rule)
        if mask_expr is None:
            exprs.append(F.lit(-1).alias(f"r{i}"))
        else:
            exprs.append(
                F.sum(
                    F.when(~mask_expr, 1).otherwise(0)
                ).alias(f"r{i}")
            )

    result = df.select(*exprs).collect()[0]
    return [
        int(result[f"r{i}"]) if result[f"r{i}"] is not None
        else 0
        for i in range(len(rules))
    ]


def _spark_rule_mask(df: Any, rule: dict[str, Any]) -> Any:
    """Build a Spark Column expression for a rule.

    Returns a boolean Column (True = pass) or None if the rule
    can't be expressed as a simple filter (e.g., unique).

    Args:
        df: Spark DataFrame.
        rule: Rule dict.

    Returns:
        Spark Column or None.
    """
    from pyspark.sql import functions as F

    rule_type = rule.get("type", "")
    df_cols = df.columns

    if rule_type == "not_null":
        cols = [
            c for c in rule.get("columns", [])
            if c in df_cols
        ]
        if not cols:
            return F.lit(True)
        mask = F.col(cols[0]).isNotNull()
        for c in cols[1:]:
            mask = mask & F.col(c).isNotNull()
        return mask

    if rule_type == "accepted_values":
        col = rule.get("column", "")
        if col not in df_cols:
            return F.lit(True)
        return F.col(col).isin(rule.get("values", []))

    if rule_type == "range":
        col = rule.get("column", "")
        if col not in df_cols:
            return F.lit(True)
        cond = F.lit(True)
        mn = rule.get("min")
        mx = rule.get("max")
        if mn is not None:
            cond = cond & (F.col(col) >= mn)
        if mx is not None:
            cond = cond & (F.col(col) <= mx)
        return cond

    if rule_type == "regex":
        col = rule.get("column", "")
        if col not in df_cols:
            return F.lit(True)
        pattern = rule.get("pattern", "")
        return (
            F.col(col).isNull()
            | F.col(col).rlike(pattern)
        )

    if rule_type == "custom_sql":
        cond = rule.get("condition", "true")
        try:
            return F.expr(cond)
        except Exception:
            return F.lit(True)  # SILENT-OK: custom_sql expression parse failure — default to pass

    if rule_type == "unique":
        return None

    if rule_type == "referential_integrity":
        col = rule.get("column", "")
        if col not in df_cols:
            return F.lit(True)
        ref_values = list(rule.get("reference_values", []))
        if not ref_values:
            return F.lit(True)
        # Non-null values must be in reference set; nulls pass
        return F.col(col).isNull() | F.col(col).isin(ref_values)

    if rule_type == "not_stale":
        # Aggregate rule — cannot express as row mask
        return None

    return F.lit(True)


def _spark_unique_count(
    df: Any, rule: dict[str, Any]
) -> int:
    """Count rows violating a uniqueness rule via Spark.

    Uses groupBy + having count > 1, then sums duplicates.

    Args:
        df: Spark DataFrame.
        rule: Rule dict with "columns" key.

    Returns:
        Number of rows in duplicate groups.
    """
    from pyspark.sql import functions as F

    cols = [
        c for c in rule.get("columns", [])
        if c in df.columns
    ]
    if not cols:
        return 0

    dup_groups = (
        df.groupBy(*cols)
        .agg(F.count("*").alias("_cnt"))
        .filter(F.col("_cnt") > 1)
    )
    total_dups = dup_groups.agg(
        F.sum("_cnt").alias("total")
    ).collect()[0]["total"]
    return int(total_dups) if total_dups else 0


def _spark_rows_with_any_failure(
    df: Any, rules: list[dict[str, Any]]
) -> int:
    """Count rows failing at least one rule.

    Combines all batchable masks into one filter, then counts.

    Args:
        df: Spark DataFrame.
        rules: List of rule dicts.

    Returns:
        Count of rows with at least one failure.
    """
    from pyspark.sql import functions as F

    masks = []
    for rule in rules:
        m = _spark_rule_mask(df, rule)
        if m is not None:
            masks.append(m)

    if not masks:
        return 0

    combined = masks[0]
    for m in masks[1:]:
        combined = combined & m

    fail_count = df.filter(~combined).count()
    return int(fail_count)


def _spark_sample_failures(
    df: Any, rule: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    """Collect sample failing rows for a rule via Spark.

    Args:
        df: Spark DataFrame.
        rule: Rule dict.
        limit: Max rows to collect.

    Returns:
        List of row dicts.
    """
    from pyspark.sql import functions as F

    if limit <= 0:
        return []

    rule_type = rule.get("type", "")

    if rule_type == "unique":
        cols = [
            c for c in rule.get("columns", [])
            if c in df.columns
        ]
        if not cols:
            return []
        from pyspark.sql.window import Window
        w = Window.partitionBy(*cols)
        tagged = df.withColumn(
            "_cnt", F.count("*").over(w)
        )
        failing = tagged.filter(
            F.col("_cnt") > 1
        ).drop("_cnt")
        return _spark_df_to_records(failing, limit)

    mask = _spark_rule_mask(df, rule)
    if mask is None:
        return []

    failing = df.filter(~mask)
    return _spark_df_to_records(failing, limit)


def _spark_df_to_records(
    df: Any, limit: int
) -> list[dict[str, Any]]:
    """Convert Spark DataFrame rows to JSON-safe dicts.

    Args:
        df: Spark DataFrame.
        limit: Max rows.

    Returns:
        List of dicts.
    """
    rows = df.limit(limit).toPandas()
    return _df_to_records(rows)


def _validate_spark(
    df: Any,
    rules: list[dict[str, Any]],
    *,
    subject: str,
    severity_map: dict[str, str] | None,
    sample_failures: int,
) -> dict[str, Any]:
    """Run validation on a Spark DataFrame.

    Uses batched evaluation for efficiency: one Spark action for
    all non-unique rules, separate actions for unique rules.
    Rules with schema mismatches are skipped and marked as
    errors instead of being evaluated.

    Args:
        df: Spark DataFrame.
        rules: List of rule dicts.
        subject: Human label.
        severity_map: Severity overrides.
        sample_failures: Max samples per failed rule.

    Returns:
        Validation context dictionary.
    """
    total_rows = df.count()

    rule_results: list[dict[str, Any]] = []

    # Handle DF-level rules (row_count_*) separately
    df_level_rules = [r for r in rules if r.get("type", "") in _DF_LEVEL_RULE_TYPES]
    per_row_rules = [r for r in rules if r.get("type", "") not in _DF_LEVEL_RULE_TYPES]

    for rule in df_level_rules:
        rid = _rule_id(rule)
        severity = _resolve_severity(rule, severity_map)
        result = _evaluate_row_count_range(total_rows, rule)
        rule_results.append({
            "rule_id": rid,
            "rule_type": rule.get("type", "unknown"),
            "columns": [],
            "severity": severity,
            "passed": result["passed"],
            "failed_count": 0 if result["passed"] else total_rows,
            "failed_rate": 0.0 if result["passed"] else 1.0,
            "detail": result["detail"],
            "fix_expr": "",
            "sample_failures": [],
            "source": rule.get("source", "explicit"),
        })

    # Pre-filter: detect schema mismatches BEFORE evaluation
    schema_findings = _detect_schema_mismatches(
        df, per_row_rules, "spark"
    )
    mismatched_ids = {
        f.get("rule_id", "")
        for f in schema_findings
        if f.get("check_type") == "rule_schema_mismatch"
    }

    # Only batch-evaluate non-mismatched rules (exclude aggregate-only and unique)
    eval_rules = [
        r for r in per_row_rules
        if _rule_id(r) not in mismatched_ids
        and r.get("type", "") not in _AGGREGATE_RULE_TYPES
        and r.get("type", "") != "unique"
    ]
    batch_counts = _evaluate_rules_spark_batch(
        df, eval_rules
    )

    eval_idx = 0

    for rule in per_row_rules:
        rid = _rule_id(rule)
        rule_type = rule.get("type", "")
        severity = _resolve_severity(rule, severity_map)

        # Skip evaluation for schema-mismatched rules
        if rid in mismatched_ids:
            mismatch_detail = next(
                (f["detail"] for f in schema_findings
                 if f.get("rule_id") == rid),
                "Schema mismatch — rule not evaluated",
            )
            rule_results.append({
                "rule_id": rid,
                "rule_type": rule_type or "unknown",
                "columns": _rule_columns(rule),
                "severity": severity,
                "passed": False,
                "status": "error",
                "failed_count": total_rows,
                "failed_rate": 1.0,
                "detail": mismatch_detail,
                "fix_expr": (
                    "# Cannot evaluate: column type "
                    "incompatible with rule"
                ),
                "sample_failures": [],
            })
            continue

        if rule_type == "unique":
            failed_count = _spark_unique_count(df, rule)
        elif rule_type == "not_stale":
            # Aggregate rule — evaluate via helper
            col = rule.get("column", "")
            stale_result = _evaluate_not_stale(df, rule, "spark")
            if stale_result["passed"]:
                failed_count = 0
            else:
                failed_count = total_rows  # All rows "stale"
        elif rule_type == "referential_integrity":
            # referential_integrity has a mask in _spark_rule_mask, uses batch
            failed_count = batch_counts[eval_idx]
            eval_idx += 1
        else:
            failed_count = batch_counts[eval_idx]
            eval_idx += 1

        passed = failed_count == 0
        failed_rate = (
            failed_count / total_rows
            if total_rows > 0 else 0.0
        )

        # Fix expression (v2)
        fix_expr = ""
        if not passed:
            fix_expr = _build_fix_expr(rule, "spark")

        # Collect samples for failed rules
        samples: list[dict[str, Any]] = []
        if not passed and sample_failures > 0:
            samples = _spark_sample_failures(
                df, rule, sample_failures
            )

        rule_results.append({
            "rule_id": rid,
            "rule_type": rule_type or "unknown",
            "columns": _rule_columns(rule),
            "severity": severity,
            "passed": passed,
            "failed_count": failed_count,
            "failed_rate": round(failed_rate, 6),
            "detail": _rule_detail(rule),
            "fix_expr": fix_expr,
            "sample_failures": samples,
            "source": rule.get("source", "explicit"),
        })

    # Compute rows with any failure (only non-mismatched, non-aggregate)
    rows_with_failure = _spark_rows_with_any_failure(
        df, eval_rules
    )

    # Quarantine call (v2)
    quarantine_call = _build_quarantine_call(
        rules, rule_results
    )

    return _build_context(
        rule_results=rule_results,
        all_pass_masks=None,
        total_rows=total_rows,
        engine_name="spark",
        subject=subject,
        df=None,
        rows_with_failure_override=rows_with_failure,
        schema_findings=schema_findings,
        quarantine_call=quarantine_call,
        rules=rules,
    )
