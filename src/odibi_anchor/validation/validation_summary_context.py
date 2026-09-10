"""Validation summary context generator.

This module turns validation rule results into a compact context packet
for humans and LLMs. It answers "is this data valid?" and "is it safe
to promote?" without multi-round discovery.

Typical usage:
    from odibi_anchor.validation.validation_summary_context import (
        validation_summary_context,
        render_validation_report,
    )

    # Define rules (compatible format for validation checks)
    rules = [
        {"type": "not_null", "columns": ["id", "name"]},
        {"type": "range", "column": "amount", "min": 0},
        {"type": "accepted_values", "column": "status",
         "values": ["active", "pending"]},
    ]

    # Run validation
    ctx = validation_summary_context(df, rules)

    # Check promotion safety
    if not ctx["metrics"]["is_promotion_safe"]:
        print(ctx["blockers"])
        print(ctx["quarantine_call"])  # ready-to-paste fix

    # Markdown for LLM prompts
    report = render_validation_report(ctx)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from odibi_anchor._utils.engine_utils import detect_engine
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

logger = logging.getLogger(__name__)

Engine = Literal["auto", "pandas", "spark"]
OutputFormat = Literal["dict", "markdown"]

# Default severity assignments by rule type
_DEFAULT_SEVERITIES: dict[str, str] = {
    "not_null": "blocker",
    "unique": "blocker",
    "accepted_values": "warning",
    "range": "warning",
    "regex": "warning",
    "custom_sql": "warning",
    "row_count_range": "warning",
    "row_count_max": "warning",
    "row_count_min": "warning",
    "referential_integrity": "warning",
    "not_stale": "warning",
}

# Numeric-like dtype keywords for schema mismatch detection
_NUMERIC_DTYPES = {
    "int", "integer", "long", "short", "byte", "float",
    "double", "decimal", "numeric", "int64", "int32",
    "float64", "float32",
}
_STRING_DTYPES = {"string", "str", "object", "varchar", "char"}


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------


def validation_summary_context(
    df: Any,
    rules: list[dict[str, Any]] | None = None,
    *,
    subject: str = "validation",
    engine: Engine = "auto",
    severity_map: dict[str, str] | None = None,
    sample_failures: int = 5,
    output_format: OutputFormat = "dict",
    spark: Any = None,
    from_manifest: bool = False,
    root: str | None = None,
    frame: Any | None = None,
) -> dict[str, Any] | str:
    """Generate a compact validation summary context packet.

    Evaluates a list of validation rules against a DataFrame and
    produces structured metrics, per-rule results, promotion safety
    assessment, fix code snippets, and quarantine call generation.

    Args:
        df: Pandas or Spark DataFrame to validate.
        rules: List of rule dicts. Supported types: ``not_null``,
            ``unique``, ``accepted_values``, ``range``, ``regex``,
            ``custom_sql``. See module docstring for format.
        subject: Human-readable label for the dataset.
        engine: Execution engine: ``"auto"``, ``"pandas"``, or
            ``"spark"``.
        severity_map: Optional overrides for rule severity. Keys
            are ``"{type}:{column}"`` or ``"{type}"`` for broad
            rules. Values: ``"blocker"``, ``"warning"``, ``"info"``.
        sample_failures: Max failing rows to collect per failed rule.
            Set to 0 to skip sample collection (faster).
        output_format: ``"dict"`` or ``"markdown"``.
        spark: SparkSession. Required when ``df`` is a table name
            string or when engine is ``"spark"``.

    Returns:
        Dictionary or markdown string depending on ``output_format``.

    Raises:
        ValueError: If parameters are invalid or rules are empty.
        TypeError: If df type doesn't match engine.

    Example:
        >>> import pandas as pd
        >>> df = pd.DataFrame({"id": [1, None, 3], "v": [10, -1, 5]})
        >>> rules = [{"type": "not_null", "columns": ["id"]}]
        >>> ctx = validation_summary_context(df, rules)
        >>> ctx["metrics"]["is_promotion_safe"]
        False
    """
    if engine not in {"auto", "pandas", "spark"}:
        raise ValueError(
            "engine must be one of: 'auto', 'pandas', 'spark'"
        )
    validate_output_format(output_format)

    # Normalize rules
    if rules is None:
        rules = []

    # Manifest auto-loading
    manifest_rules_generated = 0
    if from_manifest:
        from pathlib import Path
        from odibi_anchor.codebase._manifest import load_manifest
        manifest_root = root or "."
        manifest = load_manifest(manifest_root)
        if manifest:
            # Resolve df columns for manifest rule generation
            _temp_df, _temp_engine = _resolve_input(df, engine, spark)
            if _temp_engine == "spark" or (
                _temp_engine == "auto" and hasattr(_temp_df, "columns") and hasattr(_temp_df, "count")
            ):
                df_columns = _temp_df.columns
            else:
                df_columns = list(_temp_df.columns) if hasattr(_temp_df, "columns") else []
            manifest_gen = _rules_from_manifest(manifest, df_columns)
            manifest_rules_generated = len(manifest_gen)
            # Merge: explicit rules win on type+column conflict
            explicit_keys = set()
            for r in rules:
                key = (r.get("type", ""), tuple(sorted(r.get("columns", [r.get("column", "")]))))
                explicit_keys.add(key)
            for mr in manifest_gen:
                key = (mr.get("type", ""), tuple(sorted(mr.get("columns", [mr.get("column", "")]))))
                if key not in explicit_keys:
                    rules.append(mr)

    if not rules:
        raise ValueError(
            "rules must be a non-empty list (provide rules or use from_manifest=True)"
        )

    # Tag explicit rules with source if not already tagged
    for r in rules:
        if "source" not in r:
            r["source"] = "explicit"

    if sample_failures < 0:
        raise ValueError("sample_failures must be >= 0")

    # Resolve table name strings
    df, engine = _resolve_input(df, engine, spark)

    resolved_engine = _resolve_engine(df, engine)

    if resolved_engine == "pandas":
        ctx = _validate_pandas(
            df, rules,
            subject=subject,
            severity_map=severity_map,
            sample_failures=sample_failures,
        )
    elif resolved_engine == "spark":
        ctx = _validate_spark(
            df, rules,
            subject=subject,
            severity_map=severity_map,
            sample_failures=sample_failures,
        )
    else:
        raise ValueError(f"Unsupported engine: {resolved_engine}")

    # Inject manifest_rules_generated into metrics
    if "metrics" in ctx:
        ctx["metrics"]["manifest_rules_generated"] = manifest_rules_generated if from_manifest else 0
        new_types_used = list({
            r.get("type", "") for r in rules
            if r.get("type", "") in (
                "row_count_range", "row_count_max", "row_count_min",
                "referential_integrity", "not_stale"
            )
        })
        ctx["metrics"]["new_rule_types_used"] = new_types_used

    # Frame persistence — never fail validation for frame integration
    if frame is not None:
        try:
            from datetime import datetime, timezone
            frame.data_context.tables_profiled.setdefault(subject, {})
            frame.data_context.tables_profiled[subject]["validation"] = {
                "rules_evaluated": ctx["metrics"]["rules_evaluated"],
                "rules_failed": ctx["metrics"]["rules_failed"],
                "is_promotion_safe": ctx["metrics"]["is_promotion_safe"],
                "blocker_count": ctx["metrics"]["blocker_count"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            # Surface blockers as frame findings
            from odibi_anchor._utils._context_frame import Finding
            blocker_rules = [
                r for r in ctx.get("rules", [])
                if not r.get("passed") and r.get("severity") == "blocker"
            ]
            for br in blocker_rules:
                frame.findings.append(Finding(
                    source="validate",
                    category="quality",
                    severity="critical",
                    message=f"Validation blocker on {subject}: {br.get('rule_id', '?')}",
                    subject=subject,
                    evidence={
                        "rule_id": br.get("rule_id"),
                        "failed_count": br.get("failed_count"),
                        "failed_rate": br.get("failed_rate"),
                    },
                    timestamp=datetime.now(timezone.utc),
                ))
        except Exception:
            pass  # SILENT-OK: frame integration is advisory

    if output_format == "markdown":
        return render_validation_report(ctx)
    return ctx


# --- Rendering (moved to _validation_render.py) ---
from odibi_anchor.validation._validation_render import (  # noqa: E402
    render_validation_report,
    render_validation_summary_report,
)


# -------------------------------------------------------------------
# Input resolution
# -------------------------------------------------------------------


def _resolve_input(
    df: Any, engine: Engine, spark: Any
) -> tuple[Any, Engine]:
    """Resolve table name strings to DataFrames.

    Args:
        df: DataFrame or table name string.
        engine: Current engine setting.
        spark: SparkSession (required for strings).

    Returns:
        Tuple of (resolved_df, updated_engine).

    Raises:
        ValueError: If string input without spark session.
    """
    if isinstance(df, str):
        if spark is None:
            raise ValueError(
                "spark= is required when passing table name strings"
            )
        df = spark.table(df)
        engine = "spark"
    return df, engine


def _resolve_engine(df: Any, engine: Engine) -> str:
    """Determine the execution engine from df type.

    Args:
        df: The DataFrame to inspect.
        engine: User-specified engine preference.

    Returns:
        Resolved engine string: "pandas" or "spark".

    Raises:
        TypeError: If df type is unrecognized.
    """
    if engine in {"pandas", "spark"}:
        return engine
    detected = detect_engine(df)
    if detected in {"pandas", "spark"}:
        return detected
    raise TypeError(
        f"Cannot detect engine for type: {type(df).__name__}. "
        f"Pass engine='pandas' or engine='spark' explicitly."
    )


# -------------------------------------------------------------------
# Severity resolution
# -------------------------------------------------------------------


def _resolve_severity(
    rule: dict[str, Any],
    severity_map: dict[str, str] | None,
) -> str:
    """Determine severity for a rule.

    Checks severity_map for specific key (type:column), then
    broad key (type), then falls back to defaults.

    Args:
        rule: The rule dict.
        severity_map: User overrides.

    Returns:
        Severity string: "blocker", "warning", or "info".
    """
    rule_type = rule.get("type", "unknown")
    col = rule.get("column", "")
    if not col:
        cols = rule.get("columns", [])
        col = cols[0] if len(cols) == 1 else ""
    specific_key = f"{rule_type}:{col}" if col else ""

    if severity_map:
        if specific_key and specific_key in severity_map:
            return severity_map[specific_key]
        if rule_type in severity_map:
            return severity_map[rule_type]

    return _DEFAULT_SEVERITIES.get(rule_type, "info")


def _rule_id(rule: dict[str, Any]) -> str:
    """Generate a human-readable rule identifier.

    Args:
        rule: The rule dict.

    Returns:
        String like "not_null:id,name" or "range:amount".
    """
    rule_type = rule.get("type", "unknown")
    col = rule.get("column", "")
    if col:
        return f"{rule_type}:{col}"
    cols = rule.get("columns", [])
    if cols:
        return f"{rule_type}:{','.join(cols[:3])}"
    cond = rule.get("condition", "")
    if cond:
        short = cond[:30].replace(" ", "_")
        return f"{rule_type}:{short}"
    return rule_type


def _rule_detail(rule: dict[str, Any]) -> str:
    """Generate human-readable description of a rule.

    Args:
        rule: The rule dict.

    Returns:
        Description string.
    """
    rule_type = rule.get("type", "unknown")
    if rule_type == "not_null":
        cols = rule.get("columns", [])
        return f"Columns must not be null: {cols}"
    if rule_type == "unique":
        cols = rule.get("columns", [])
        return f"Columns must be unique: {cols}"
    if rule_type == "accepted_values":
        col = rule.get("column", "?")
        vals = rule.get("values", [])
        shown = vals[:5]
        suffix = (
            f"... +{len(vals)-5}" if len(vals) > 5 else ""
        )
        return f"{col} must be in {shown}{suffix}"
    if rule_type == "range":
        col = rule.get("column", "?")
        mn = rule.get("min")
        mx = rule.get("max")
        parts = []
        if mn is not None:
            parts.append(f">= {mn}")
        if mx is not None:
            parts.append(f"<= {mx}")
        return f"{col} must be {' and '.join(parts)}"
    if rule_type == "regex":
        col = rule.get("column", "?")
        pat = rule.get("pattern", "?")
        return f"{col} must match /{pat}/"
    if rule_type == "custom_sql":
        cond = rule.get("condition", "?")
        return f"Condition: {cond}"
    return f"Unknown rule type: {rule_type}"


# -------------------------------------------------------------------
# v2: Fix code generation
# -------------------------------------------------------------------


def _build_fix_expr(
    rule: dict[str, Any], engine_name: str
) -> str:
    """Generate a ready-to-paste fix expression for a rule.

    Produces Spark-style code (col/F functions). The fix removes
    or filters out failing rows.

    Args:
        rule: The rule dict.
        engine_name: "pandas" or "spark".

    Returns:
        Fix expression string, or empty string if none.
    """
    rule_type = rule.get("type", "")

    if rule_type == "not_null":
        cols = rule.get("columns", [])
        if not cols:
            return ""
        if engine_name == "spark":
            conditions = [
                f"col('{c}').isNotNull()" for c in cols
            ]
            return (
                f"df = df.filter({' & '.join(conditions)})"
            )
        # pandas
        conditions = [f"df['{c}'].notna()" for c in cols]
        return f"df = df[{' & '.join(conditions)}]"

    if rule_type == "unique":
        cols = rule.get("columns", [])
        if not cols:
            return ""
        if engine_name == "spark":
            col_list = ", ".join(f"'{c}'" for c in cols)
            return (
                f"df = df.dropDuplicates([{col_list}])"
            )
        col_list = ", ".join(f"'{c}'" for c in cols)
        return (
            f"df = df.drop_duplicates(subset=[{col_list}])"
        )

    if rule_type == "accepted_values":
        col = rule.get("column", "")
        vals = rule.get("values", [])
        if not col:
            return ""
        if engine_name == "spark":
            return (
                f"df = df.filter(col('{col}').isin({vals}))"
            )
        return f"df = df[df['{col}'].isin({vals})]"

    if rule_type == "range":
        col = rule.get("column", "")
        mn = rule.get("min")
        mx = rule.get("max")
        if not col:
            return ""
        parts = []
        if engine_name == "spark":
            if mn is not None:
                parts.append(f"col('{col}') >= {mn}")
            if mx is not None:
                parts.append(f"col('{col}') <= {mx}")
            cond = " & ".join(f"({p})" for p in parts)
            return f"df = df.filter({cond})"
        # pandas
        if mn is not None:
            parts.append(f"df['{col}'] >= {mn}")
        if mx is not None:
            parts.append(f"df['{col}'] <= {mx}")
        cond = " & ".join(f"({p})" for p in parts)
        return f"df = df[{cond}]"

    if rule_type == "regex":
        col = rule.get("column", "")
        pat = rule.get("pattern", "")
        if not col:
            return ""
        if engine_name == "spark":
            return (
                f"df = df.filter(col('{col}').rlike("
                f"'{pat}'))"
            )
        return (
            f"df = df[df['{col}'].str.match(r'{pat}'"
            f", na=False)]"
        )

    if rule_type == "custom_sql":
        cond = rule.get("condition", "")
        if not cond:
            return ""
        if engine_name == "spark":
            return f"df = df.filter('{cond}')"
        return f"df = df.query('{cond}')"

    return ""


# -------------------------------------------------------------------
# v2: Rule-schema mismatch detection
# -------------------------------------------------------------------


def _detect_schema_mismatches(
    df: Any, rules: list[dict[str, Any]], engine_name: str
) -> list[dict[str, Any]]:
    """Detect rules targeting columns with incompatible types.

    For example, a range rule on a string column will silently
    produce meaningless results.

    Args:
        df: The DataFrame.
        rules: List of rule dicts.
        engine_name: "pandas" or "spark".

    Returns:
        List of finding dicts for mismatches.
    """
    findings: list[dict[str, Any]] = []

    # Build column type map
    col_types: dict[str, str] = {}
    if engine_name == "spark":
        try:
            for field in df.schema.fields:
                col_types[field.name] = str(
                    field.dataType
                ).lower()
        except Exception:
            return findings  # SILENT-OK: schema introspection failure — skip type checks
    else:
        try:
            for col_name in df.columns:
                col_types[col_name] = str(
                    df[col_name].dtype
                ).lower()
        except Exception:
            return findings  # SILENT-OK: schema introspection failure — skip type checks

    for rule in rules:
        rule_type = rule.get("type", "")
        # Range rules expect numeric columns
        if rule_type == "range":
            col = rule.get("column", "")
            if col and col in col_types:
                dtype = col_types[col]
                is_numeric = any(
                    t in dtype for t in _NUMERIC_DTYPES
                )
                if not is_numeric:
                    findings.append({
                        "check_type": "rule_schema_mismatch",
                        "detail": (
                            f"Rule 'range:{col}' expects "
                            f"numeric column but '{col}' "
                            f"has dtype '{dtype}'. Results "
                            f"may be unreliable."
                        ),
                        "severity": "warning",
                        "rule_id": f"range:{col}",
                        "column": col,
                        "expected_family": "numeric",
                        "actual_dtype": dtype,
                    })

        # Regex rules expect string columns
        if rule_type == "regex":
            col = rule.get("column", "")
            if col and col in col_types:
                dtype = col_types[col]
                is_string = any(
                    t in dtype for t in _STRING_DTYPES
                )
                if not is_string:
                    findings.append({
                        "check_type": "rule_schema_mismatch",
                        "detail": (
                            f"Rule 'regex:{col}' expects "
                            f"string column but '{col}' "
                            f"has dtype '{dtype}'. Pattern "
                            f"match may fail silently."
                        ),
                        "severity": "warning",
                        "rule_id": f"regex:{col}",
                        "column": col,
                        "expected_family": "string",
                        "actual_dtype": dtype,
                    })

    return findings


# -------------------------------------------------------------------
# v2: Quarantine call generation
# -------------------------------------------------------------------


def _build_quarantine_call(
    rules: list[dict[str, Any]],
    rule_results: list[dict[str, Any]],
) -> str:
    """Generate a ready-to-paste split_valid_invalid() call.

    Only includes failed blocker rules in the quarantine call.

    Args:
        rules: Original rule dicts.
        rule_results: Per-rule evaluation results.

    Returns:
        Python code string for quarantine call, or "".
    """
    # Collect failed blocker rules
    failed_blocker_rules: list[dict[str, Any]] = []
    for i, result in enumerate(rule_results):
        if (
            not result["passed"]
            and result["severity"] == "blocker"
        ):
            failed_blocker_rules.append(rules[i])

    if not failed_blocker_rules:
        return ""

    # Format the rules list
    rule_strs = []
    for r in failed_blocker_rules:
        parts = []
        for k, v in r.items():
            if isinstance(v, str):
                parts.append(f"'{k}': '{v}'")
            else:
                parts.append(f"'{k}': {v}")
        rule_strs.append("{" + ", ".join(parts) + "}")

    rules_formatted = "[\n    " + ",\n    ".join(
        rule_strs
    ) + ",\n]"

    return (
        "from odibi_anchor.validation import split_valid_invalid\n"
        "\n"
        f"result = split_valid_invalid(df, {rules_formatted})\n"
        "valid_df = result.valid_df\n"
        "invalid_df = result.invalid_df\n"
        f"# {len(failed_blocker_rules)} blocker rule(s) applied"
    )


# -------------------------------------------------------------------
# Pandas evaluation
# -------------------------------------------------------------------



# -------------------------------------------------------------------
# Manifest auto-loading
# -------------------------------------------------------------------


def _rules_from_manifest(
    manifest: dict,
    df_columns: list[str],
) -> list[dict[str, Any]]:
    """Auto-generate validation rules from manifest constraints.

    Only generates rules for columns that actually exist in the DataFrame
    to avoid false positives on column-name mismatches.

    Args:
        manifest: Parsed manifest dict from load_manifest().
        df_columns: List of column names in the target DataFrame.

    Returns:
        List of rule dicts tagged with source='manifest'.
    """
    rules: list[dict[str, Any]] = []
    constraints = manifest.get("constraints", {})

    # sensitive_columns → not_null rules
    for col_pattern in constraints.get("sensitive_columns", []):
        for col in df_columns:
            if re.fullmatch(col_pattern, col, re.IGNORECASE):
                rules.append({
                    "type": "not_null",
                    "columns": [col],
                    "severity": "warning",
                    "source": "manifest",
                    "description": f"Sensitive column '{col}' should not be null",
                })

    # max_table_rows_local → row_count_max rule
    max_rows = constraints.get("max_table_rows_local")
    if max_rows:
        rules.append({
            "type": "row_count_max",
            "max": max_rows,
            "severity": "warning",
            "source": "manifest",
        })

    return rules


# -------------------------------------------------------------------
# New rule type evaluators
# -------------------------------------------------------------------


def _evaluate_row_count_range(
    total_rows: int, rule: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate a row_count_range/row_count_max/row_count_min rule.

    DF-level rule — does not produce per-row masks.

    Args:
        total_rows: Actual row count of the DataFrame.
        rule: Rule dict with optional min/max keys.

    Returns:
        Result dict with passed, total_rows, bounds, and detail.
    """
    rule_type = rule.get("type", "row_count_range")
    mn = rule.get("min")
    mx = rule.get("max")

    # Normalize shorthands
    if rule_type == "row_count_max":
        mn = None
    elif rule_type == "row_count_min":
        mx = None

    passed = True
    reasons: list[str] = []

    if mn is not None and total_rows < mn:
        passed = False
        reasons.append(f"row count {total_rows:,} < min {mn:,}")
    if mx is not None and total_rows > mx:
        passed = False
        reasons.append(f"row count {total_rows:,} > max {mx:,}")

    return {
        "passed": passed,
        "total_rows": total_rows,
        "min": mn,
        "max": mx,
        "detail": "; ".join(reasons) if reasons else f"row count {total_rows:,} within bounds",
    }


def _evaluate_referential_integrity(
    series_or_df: Any,
    rule: dict[str, Any],
    engine: str,
) -> dict[str, Any]:
    """Check every non-null value exists in the reference set.

    Args:
        series_or_df: Pandas Series or Spark DataFrame.
        rule: Rule dict with reference_values list.
        engine: 'pandas' or 'spark'.

    Returns:
        Result dict with passed, fail_count, sample_failures.
    """
    ref_values = set(rule.get("reference_values", []))
    col = rule.get("column", "")

    if not ref_values:
        return {"passed": True, "fail_count": 0, "note": "No reference values provided — rule skipped"}

    if engine == "pandas":
        non_null = series_or_df.dropna()
        invalid = non_null[~non_null.isin(ref_values)]
        passed = len(invalid) == 0
        return {
            "passed": passed,
            "fail_count": len(invalid),
            "sample_failures": invalid.head(5).tolist() if not passed else [],
        }
    else:  # spark
        from pyspark.sql import functions as F
        non_null_df = series_or_df.filter(F.col(col).isNotNull())
        invalid_df = non_null_df.filter(~F.col(col).isin(list(ref_values)))
        fail_count = invalid_df.count()
        passed = fail_count == 0
        samples = []
        if not passed:
            sample_rows = invalid_df.select(col).limit(5).collect()
            samples = [row[0] for row in sample_rows]
        return {
            "passed": passed,
            "fail_count": fail_count,
            "sample_failures": samples,
        }


def _evaluate_not_stale(
    series_or_df: Any,
    rule: dict[str, Any],
    engine: str,
) -> dict[str, Any]:
    """Validate max value in a date/timestamp column is within N days of today.

    Args:
        series_or_df: Pandas Series or Spark DataFrame.
        rule: Rule dict with max_age_days and column keys.
        engine: 'pandas' or 'spark'.

    Returns:
        Result dict with passed, max_value, age_days, threshold_days.
    """
    from datetime import datetime, timezone, timedelta

    max_age_days = rule.get("max_age_days", 1)
    col = rule.get("column", "")

    if engine == "pandas":
        import pandas as pd
        max_val = pd.to_datetime(series_or_df, errors="coerce").max()
        if pd.isna(max_val):
            return {"passed": False, "reason": "all values are null or unparseable", "age_days": None}
        # Handle timezone
        if max_val.tzinfo is None:
            age_days = (datetime.now(timezone.utc) - max_val.tz_localize("UTC")).days
        else:
            age_days = (datetime.now(timezone.utc) - max_val).days
        passed = age_days <= max_age_days
        return {
            "passed": passed,
            "max_value": str(max_val),
            "age_days": age_days,
            "threshold_days": max_age_days,
        }
    else:  # spark
        from pyspark.sql import functions as F
        max_row = series_or_df.agg(F.max(F.col(col)).alias("max_val")).collect()
        max_val = max_row[0]["max_val"] if max_row else None
        if max_val is None:
            return {"passed": False, "reason": "all values are null", "age_days": None}
        # Convert to datetime if needed
        if hasattr(max_val, "date"):
            # It's already a datetime/date object
            if hasattr(max_val, "tzinfo") and max_val.tzinfo is not None:
                age_days = (datetime.now(timezone.utc) - max_val).days
            else:
                from datetime import date as date_type
                if isinstance(max_val, date_type) and not isinstance(max_val, datetime):
                    age_days = (datetime.now(timezone.utc).date() - max_val).days
                else:
                    age_days = (datetime.now(timezone.utc) - max_val.replace(tzinfo=timezone.utc)).days
        else:
            # String — try parsing
            import pandas as pd
            parsed = pd.to_datetime(str(max_val), errors="coerce")
            if pd.isna(parsed):
                return {"passed": False, "reason": f"Cannot parse max value: {max_val}", "age_days": None}
            age_days = (datetime.now(timezone.utc) - parsed.tz_localize("UTC")).days
        passed = age_days <= max_age_days
        return {
            "passed": passed,
            "max_value": str(max_val),
            "age_days": age_days,
            "threshold_days": max_age_days,
        }


# -------------------------------------------------------------------
# DF-level rule types (bypass per-row evaluation)
# -------------------------------------------------------------------

_DF_LEVEL_RULE_TYPES = {"row_count_range", "row_count_max", "row_count_min"}
_AGGREGATE_RULE_TYPES = {"referential_integrity", "not_stale"}

# --- Engine-specific checks (moved to _validation_checks.py) ---
from odibi_anchor.validation._validation_checks import (  # noqa: E402
    _evaluate_rule_pandas,
    _validate_pandas,
    _evaluate_rules_spark_batch,
    _spark_rule_mask,
    _spark_unique_count,
    _spark_rows_with_any_failure,
    _spark_sample_failures,
    _spark_df_to_records,
    _validate_spark,
)

# --- Context builders (moved to _validation_build.py) ---
from odibi_anchor.validation._validation_build import (  # noqa: E402
    _build_context,
    _rule_columns,
    _df_to_records,
    _build_summary,
    _build_recommendation,
    _build_findings,
    _build_risks,
    _build_suggested_next_actions,
)
