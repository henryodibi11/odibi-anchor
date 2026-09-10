"""Transform plan generator from profile dicts.

Given a profile dict (from profile_table or any profiler output),
generates a reviewable, ordered transform plan with executable code for both
Spark and Pandas.

Zero dependency on external frameworks — all logic is self-contained.

Example:
    >>> from odibi_anchor.tables import transform_plan_context
    >>> profile = {"summary": {"row_count": 100, "column_count": 3}, "columns": {}, ...}
    >>> ctx = transform_plan_context(profile, subject="my_table")
    >>> ctx["kind"]
    'transform_plan_context'
"""

from __future__ import annotations

import re
from typing import Any

from odibi_anchor._utils.contract import build_base_context, finalize_context, validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


# ── Confidence thresholds for transform step generation ───────────────
_CAST_PARSEABLE_THRESHOLD = 0.95  # Min parseable rate to auto-generate cast
_HIGH_CONFIDENCE = 0.9  # Confidence for well-supported type casts
_MEDIUM_CONFIDENCE = 0.7  # Confidence for heuristic-based steps
_LOW_CONFIDENCE = 0.55  # Confidence for speculative steps
_DEDUP_CONFIDENCE = 0.6  # Confidence for auto-dedup recommendation
_LOW_CONF_CAST_THRESHOLD = 0.99  # Below this, cast is flagged for review

# ── Strftime → Spark date format mapping ─────────────────────────────────────
_STRFTIME_TO_SPARK = {
    "%Y": "yyyy",
    "%m": "MM",
    "%d": "dd",
    "%H": "HH",
    "%M": "mm",
    "%S": "ss",
    "%I": "hh",
    "%p": "a",
    "%b": "MMM",
    "%B": "MMMM",
}

# ── Boolean true/false value sets ─────────────────────────────────────────────
_BOOL_TRUE_VALUES = {"true", "yes", "y", "1", "t", "on"}
_BOOL_FALSE_VALUES = {"false", "no", "n", "0", "f", "off"}
_BOOL_ALL_VALUES = _BOOL_TRUE_VALUES | _BOOL_FALSE_VALUES


def _to_snake_case(name: str) -> str:
    """Convert a column name to snake_case."""
    return re.sub(r"[^\w]+", "_", name).strip("_").lower()


def _needs_snake_case(name: str) -> bool:
    """Check if a column name is NOT already valid snake_case."""
    return _to_snake_case(name) != name


def _strftime_to_spark_format(fmt: str) -> str:
    """Convert Python strftime format to Spark date format string."""
    result = fmt
    for py_fmt, spark_fmt in _STRFTIME_TO_SPARK.items():
        result = result.replace(py_fmt, spark_fmt)
    return result


def _is_numeric_string(value: str) -> bool:
    """Check if a string value can be parsed as a number."""
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def _is_integer_string(value: str) -> bool:
    """Check if a string value is an integer (no decimal point)."""
    try:
        float_val = float(value)
        return float_val == int(float_val)
    except (ValueError, TypeError):
        return False


def _should_skip_column(col: str, custom_overrides: dict | None) -> bool:
    """Check if a column should be skipped based on custom_overrides."""
    if not custom_overrides or col not in custom_overrides:
        return False
    override = custom_overrides[col]
    if override == "skip" or override is False:
        return True
    if isinstance(override, dict) and override.get("skip"):
        return True
    return False


def _build_column_rename_map(
    columns: dict,
    custom_overrides: dict | None = None,
) -> dict[str, str]:
    """Build collision-safe final names for every input column."""
    unchanged = {
        col_name
        for col_name in columns
        if _should_skip_column(col_name, custom_overrides)
        or _to_snake_case(col_name) == col_name
    }
    used = {name.casefold() for name in unchanged}
    rename_map: dict[str, str] = {}
    for col_name in columns:
        if col_name in unchanged:
            rename_map[col_name] = col_name
            continue
        base = _to_snake_case(col_name)
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        rename_map[col_name] = candidate
        used.add(candidate.casefold())
    return rename_map


def _get_snake_name(col: str, rename_map: dict[str, str]) -> str:
    """Get the snake_case version of a column name."""
    return rename_map.get(col, col)


# ── Step Generators ───────────────────────────────────────────────────────────


def _gen_standardize_step(
    profile: dict,
    custom_overrides: dict | None,
) -> list[dict]:
    """Generate column standardization step if any columns need renaming."""
    columns = profile.get("columns", {})
    if not columns:
        return []

    rename_map = _build_column_rename_map(columns, custom_overrides)
    changed_map = {old: new for old, new in rename_map.items() if old != new}
    if not changed_map:
        return []

    # Build provider-neutral code from the exact rename map.
    rename_dict_str = ", ".join(f"{old!r}: {new!r}" for old, new in changed_map.items())
    output_columns = list(rename_map.values())
    code_spark = f"df = df.toDF(*{output_columns!r})"
    code_pandas = f'df = df.rename(columns={{{rename_dict_str}}})'

    return [{
        "order": 0,  # placeholder — assigned later
        "action": "standardize_columns",
        "description": "Standardize column names to snake_case",
        "function": "standardize_columns",
        "code_spark": code_spark,
        "code_pandas": code_pandas,
        "columns": list(columns),
        "rename_map": rename_map,
        "output_columns": output_columns,
        "confidence": 1.0,
        "source": "columns",
    }]


def _gen_trim_steps(
    profile: dict,
    rename_map: dict[str, str],
    custom_overrides: dict | None,
) -> list[dict]:
    """Generate whitespace-trim steps for columns with leading/trailing spaces.

    Trim is the unambiguously-safe cleanup — leading/trailing whitespace is
    virtually always junk — so it executes as a real step. (Case-normalization
    stays suggest-only in column_transforms because case can be meaningful.)
    Previously trim was detected only in column_transforms (SQL-only), so
    apply_transform never applied it on a pandas DataFrame.
    """
    columns = profile.get("columns", {})
    steps = []
    for col_name, col in columns.items():
        if _should_skip_column(col_name, custom_overrides):
            continue
        if not (col.get("has_leading_spaces") or col.get("has_trailing_spaces")):
            continue
        snake_col = _get_snake_name(col_name, rename_map)
        steps.append({
            "order": 0,
            "action": "trim",
            "description": f"Strip leading/trailing whitespace in '{snake_col}'",
            "function": "trim",
            "code_spark": (
                f'df = df.withColumn("{snake_col}", F.trim(F.col("{snake_col}")))'
            ),
            "code_pandas": (
                f'df["{snake_col}"] = df["{snake_col}"]'
                f'.map(lambda v: v.strip() if isinstance(v, str) else v)'
            ),
            "columns": [snake_col],
            "confidence": 1.0,
            "source": "whitespace",
        })
    return steps


def _gen_null_cleanup_steps(
    profile: dict,
    rename_map: dict[str, str],
    custom_overrides: dict | None,
) -> list[dict]:
    """Generate null-like string cleanup steps."""
    null_like_strings = profile.get("null_like_strings", {})
    if not null_like_strings:
        return []

    steps = []
    for col, values in null_like_strings.items():
        if not values:
            continue
        if _should_skip_column(col, custom_overrides):
            continue

        snake_col = _get_snake_name(col, rename_map)
        values_list = [repr(v) for v in values]
        values_str = ", ".join(values_list)

        code_spark = (
            f'df = df.withColumn("{snake_col}", '
            f'F.when(F.col("{snake_col}").isin([{values_str}]), F.lit(None))'
            f'.otherwise(F.col("{snake_col}")))'
        )
        code_pandas = (
            f'df["{snake_col}"] = df["{snake_col}"].replace('
            f'[{values_str}], None)'
        )

        steps.append({
            "order": 0,
            "action": "null_cleanup",
            "description": f"Replace null-like strings in '{snake_col}' with None",
            "function": "withColumn/when",
            "code_spark": code_spark,
            "code_pandas": code_pandas,
            "columns": [snake_col],
            "values": list(values),
            "confidence": 1.0,
            "source": "null_like_strings",
        })

    return steps


def _gen_cast_steps(
    profile: dict,
    rename_map: dict[str, str],
    custom_overrides: dict | None,
) -> list[dict]:
    """Generate type cast steps from type_mismatches and post-null-cleanup inference."""
    steps = []
    type_mismatches = profile.get("type_mismatches", {})
    null_like_strings = profile.get("null_like_strings", {})
    columns = profile.get("columns", {})

    # Track which columns are handled by type_mismatches
    handled_by_mismatch = set()

    # A. From type_mismatches (numeric casts)
    for col, mismatch in type_mismatches.items():
        if _should_skip_column(col, custom_overrides):
            continue

        inferred = mismatch.get("inferred", "")
        parseable_pct = mismatch.get("parseable_pct", 0.0)

        # Skip dates (handled by date step) and booleans (handled separately)
        if inferred in ("date", "boolean"):
            handled_by_mismatch.add(col)
            continue

        if inferred in ("float", "int") and parseable_pct >= 95.0:
            handled_by_mismatch.add(col)
            snake_col = _get_snake_name(col, rename_map)
            confidence = parseable_pct / 100.0

            if inferred == "int":
                code_spark = f'df = df.withColumn("{snake_col}", F.col("{snake_col}").cast("long"))'
                code_pandas = (
                    f'df["{snake_col}"] = pd.to_numeric(df["{snake_col}"], errors="coerce").astype("Int64")'
                )
            else:
                code_spark = f'df = df.withColumn("{snake_col}", F.col("{snake_col}").cast("double"))'
                code_pandas = f'df["{snake_col}"] = pd.to_numeric(df["{snake_col}"], errors="coerce")'

            steps.append({
                "order": 0,
                "action": "cast",
                "description": f"Cast '{snake_col}' from {mismatch.get('declared', 'object')} to {inferred}",
                "function": "withColumn/cast",
                "code_spark": code_spark,
                "code_pandas": code_pandas,
                "columns": [snake_col],
                "confidence": confidence,
                "source": "type_mismatches",
            })

    # B. Post-null-cleanup inference from top_values
    for col in null_like_strings:
        if col in handled_by_mismatch:
            continue
        if _should_skip_column(col, custom_overrides):
            continue
        if col not in columns:
            continue

        col_info = columns[col]
        top_values = col_info.get("top_values", [])
        if not top_values:
            continue

        # Get null-like values for this column
        null_vals = set(str(v).lower() for v in null_like_strings.get(col, []))

        # Filter out null-like values from top_values
        non_null_values = [
            (val, count) for val, count in top_values
            if str(val).lower() not in null_vals and val is not None and str(val).strip() != ""
        ]

        if not non_null_values:
            continue

        # Check if remaining values are numeric
        numeric_count = sum(1 for val, _ in non_null_values if _is_numeric_string(str(val)))
        total_count = len(non_null_values)

        if total_count == 0:
            continue

        parseable_rate = numeric_count / total_count
        if parseable_rate < _CAST_PARSEABLE_THRESHOLD:
            continue

        snake_col = _get_snake_name(col, rename_map)

        # Check if all are integers
        all_int = all(
            _is_integer_string(str(val))
            for val, _ in non_null_values
            if _is_numeric_string(str(val))
        )

        if all_int:
            code_spark = f'df = df.withColumn("{snake_col}", F.col("{snake_col}").cast("long"))'
            code_pandas = (
                f'df["{snake_col}"] = pd.to_numeric(df["{snake_col}"], errors="coerce").astype("Int64")'
            )
            cast_type = "int"
        else:
            code_spark = f'df = df.withColumn("{snake_col}", F.col("{snake_col}").cast("double"))'
            code_pandas = f'df["{snake_col}"] = pd.to_numeric(df["{snake_col}"], errors="coerce")'
            cast_type = "float"

        steps.append({
            "order": 0,
            "action": "cast",
            "description": f"Cast '{snake_col}' to {cast_type} (inferred from non-null values)",
            "function": "withColumn/cast",
            "code_spark": code_spark,
            "code_pandas": code_pandas,
            "columns": [snake_col],
            "confidence": 0.95,
            "source": "top_values",
        })

    # C. Boolean inference from type_mismatches
    for col, mismatch in type_mismatches.items():
        if _should_skip_column(col, custom_overrides):
            continue
        if mismatch.get("inferred") != "boolean":
            continue

        snake_col = _get_snake_name(col, rename_map)

        # Build native mappings.
        true_map = ", ".join(f'"{v}": True' for v in sorted(_BOOL_TRUE_VALUES))
        false_map = ", ".join(f'"{v}": False' for v in sorted(_BOOL_FALSE_VALUES))
        replace_map_str = f"{{{true_map}, {false_map}}}"

        true_values = ", ".join(repr(v) for v in sorted(_BOOL_TRUE_VALUES))
        false_values = ", ".join(repr(v) for v in sorted(_BOOL_FALSE_VALUES))
        code_spark = (
            f'df = df.withColumn("{snake_col}", '
            f'F.when(F.lower(F.col("{snake_col}")).isin([{true_values}]), F.lit(True))'
            f'.when(F.lower(F.col("{snake_col}")).isin([{false_values}]), F.lit(False))'
            '.otherwise(F.lit(None)))'
        )
        code_pandas = (
            f"_bool_map = {replace_map_str}\n"
            f'df["{snake_col}"] = df["{snake_col}"].str.lower().map(_bool_map)'
        )

        steps.append({
            "order": 0,
            "action": "boolean_cast",
            "description": f"Cast '{snake_col}' to boolean via value mapping",
            "function": "withColumn/when",
            "code_spark": code_spark,
            "code_pandas": code_pandas,
            "columns": [snake_col],
            "confidence": 0.85,
            "source": "type_mismatches",
        })

    return steps


def _gen_date_parse_steps(
    profile: dict,
    rename_map: dict[str, str],
    custom_overrides: dict | None,
) -> list[dict]:
    """Generate date parsing steps from type_mismatches."""
    type_mismatches = profile.get("type_mismatches", {})
    columns = profile.get("columns", {})
    steps = []

    for col, mismatch in type_mismatches.items():
        if mismatch.get("inferred") != "date":
            continue
        if _should_skip_column(col, custom_overrides):
            continue

        snake_col = _get_snake_name(col, rename_map)

        # Get date format: prefer type_mismatches, fallback to columns
        date_format = mismatch.get("date_format")
        if not date_format and col in columns:
            date_format = columns[col].get("date_format")

        # Determine confidence from date_formats match rate
        confidence = _HIGH_CONFIDENCE
        if col in columns:
            date_formats = columns[col].get("date_formats", [])
            if date_formats:
                confidence = max(rate for _, rate in date_formats)

        if not date_format:
            # Fallback: generic parse without format
            code_spark = f'df = df.withColumn("{snake_col}", F.to_date(F.col("{snake_col}")))'
            code_pandas = (
                f'df["{snake_col}"] = pd.to_datetime(df["{snake_col}"], errors="coerce").dt.date'
            )
        else:
            spark_format = _strftime_to_spark_format(date_format)
            code_spark = (
                f'df = df.withColumn("{snake_col}", F.to_date(F.col("{snake_col}"), "{spark_format}"))'
            )
            code_pandas = (
                f'df["{snake_col}"] = pd.to_datetime('
                f'df["{snake_col}"], format="{date_format}", errors="coerce").dt.date'
            )

        steps.append({
            "order": 0,
            "action": "date_parse",
            "description": f"Parse '{snake_col}' as date"
            + (f" (format: {date_format})" if date_format else ""),
            "function": "to_date",
            "code_spark": code_spark,
            "code_pandas": code_pandas,
            "columns": [snake_col],
            "confidence": confidence,
            "source": "type_mismatches",
        })

    return steps


def _gen_drop_constant_steps(
    profile: dict,
    rename_map: dict[str, str],
    custom_overrides: dict | None,
) -> list[dict]:
    """Generate drop steps for constant or entirely null columns."""
    anomalies = profile.get("anomalies", [])
    if not anomalies:
        return []

    drop_cols = []
    for anomaly in anomalies:
        # Match patterns: "'col_name' is constant" or "'col_name' is entirely null"
        match = re.match(r"'([^']+)'\s+is\s+(constant|entirely null)", anomaly)
        if match:
            col_name = match.group(1)
            if not _should_skip_column(col_name, custom_overrides):
                snake_col = _get_snake_name(col_name, rename_map)
                drop_cols.append(snake_col)

    if not drop_cols:
        return []

    cols_str = ", ".join(f'"{c}"' for c in drop_cols)
    code_spark = f"df = df.drop({cols_str})"
    code_pandas = f'df = df.drop(columns=[{cols_str}], errors="ignore")'

    return [{
        "order": 0,
        "action": "drop_constant",
        "description": f"Drop constant/null columns: {', '.join(drop_cols)}",
        "function": "drop",
        "code_spark": code_spark,
        "code_pandas": code_pandas,
        "columns": drop_cols,
        "confidence": 1.0,
        "source": "anomalies",
    }]


def _gen_dedup_step(
    profile: dict,
    rename_map: dict[str, str],
    custom_overrides: dict | None,
) -> list[dict]:
    """Generate deduplication step based on key_candidates and snapshot_columns."""
    key_candidates = profile.get("key_candidates", [])
    snapshot_columns = profile.get("snapshot_columns", [])
    columns = profile.get("columns", {})

    if not key_candidates:
        return []

    # Find best key candidate
    best_key = None
    confidence = _MEDIUM_CONFIDENCE

    # Prefer single-column candidates with key-like names
    for candidate in key_candidates:
        if len(candidate) == 1:
            col = candidate[0]
            col_lower = col.lower()
            if col_lower.endswith("_id") or col_lower.endswith("_key") or col_lower == "id":
                best_key = list(candidate)
                confidence = _MEDIUM_CONFIDENCE
                break

    # Fallback: first candidate
    if best_key is None:
        best_key = list(key_candidates[0])
        confidence = _LOW_CONFIDENCE

    # Apply rename map to key columns
    best_key = [_get_snake_name(k, rename_map) for k in best_key]

    # Determine order_by
    order_by = None
    order_col = None

    # If snapshot columns exist, add to keys and order by snapshot desc
    if snapshot_columns:
        snapshot_col = _get_snake_name(snapshot_columns[0], rename_map)
        if snapshot_col not in best_key:
            best_key.append(snapshot_col)
        order_by = f"{snapshot_col} DESC"
        order_col = snapshot_col
        confidence = _DEDUP_CONFIDENCE
    else:
        # Infer order_by from timestamp-like columns
        ts_patterns = [
            "updated_at", "created_at", "modified_date",
            "modified_at", "timestamp", "file_modified_at",
        ]
        all_cols = [_get_snake_name(c, rename_map) for c in columns]
        for pattern in ts_patterns:
            for col in all_cols:
                if pattern in col.lower():
                    order_by = f"{col} DESC"
                    order_col = col
                    break
            if order_by:
                break

    # Build code
    keys_str = ", ".join(f'"{k}"' for k in best_key)

    if order_by:
        direction = "desc" if order_by.endswith(" DESC") else "asc"
        code_spark = (
            '_cw_temp = "_cw_row_number"\n'
            '_cw_used = {name.casefold() for name in df.columns}\n'
            'while _cw_temp.casefold() in _cw_used:\n    _cw_temp += "_"\n'
            f'_cw_window = Window.partitionBy({keys_str})'
            f'.orderBy(F.col("{order_col}").{direction}())\n'
            'df = df.withColumn(_cw_temp, F.row_number().over(_cw_window))'
            '.filter(F.col(_cw_temp) == 1).drop(_cw_temp)'
        )
        code_pandas = (
            f'df = df.sort_values(by=["{order_col}"], ascending=[False])'
            f'.drop_duplicates(subset=[{keys_str}], keep="first")'
        )
    else:
        code_spark = f"df = df.dropDuplicates([{keys_str}])"
        code_pandas = f'df = df.drop_duplicates(subset=[{keys_str}], keep="first")'

    return [{
        "order": 0,
        "action": "dedup",
        "description": f"Deduplicate on keys [{', '.join(best_key)}]"
        + (f" ordered by {order_by}" if order_by else ""),
        "function": "dropDuplicates/window",
        "code_spark": code_spark,
        "code_pandas": code_pandas,
        "columns": best_key,
        "keys": best_key,
        "order_column": order_col,
        "order_direction": "desc" if order_col else None,
        "confidence": confidence,
        "source": "key_candidates",
    }]


# ── Code Assembly ─────────────────────────────────────────────────────────────


def _assemble_code_spark(steps: list[dict]) -> str:
    """Assemble full executable Spark code from step list."""
    if not steps:
        return "# No transforms generated"

    # Determine needed imports
    needs_F = any("F." in s["code_spark"] for s in steps)
    needs_window = any("Window." in s["code_spark"] for s in steps)

    lines: list[str] = []

    # Spark SQL functions import
    if needs_F:
        lines.append("from pyspark.sql import functions as F")
    if needs_window:
        lines.append("from pyspark.sql.window import Window")

    if lines:
        lines.append("")

    # Step code
    for step in steps:
        lines.append(f"# {step['description']}")
        lines.append(step["code_spark"])
        lines.append("")

    return "\n".join(lines).rstrip()


def _assemble_code_pandas(steps: list[dict]) -> str:
    """Assemble full executable Pandas code from step list."""
    if not steps:
        return "# No transforms generated"

    lines = ["import pandas as pd", ""]

    for step in steps:
        lines.append(f"# {step['description']}")
        # Handle multi-line code (e.g., boolean cast)
        for code_line in step["code_pandas"].split("\n"):
            lines.append(code_line)
        lines.append("")

    return "\n".join(lines).rstrip()


# ── Main Function ─────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# SQL-first column transforms (per-column decisions + assembled projection)
# ──────────────────────────────────────────────────────────────────────────────

_AUTO_APPLY_THRESHOLD = 0.95  # min confidence for a lossy transform to auto-apply
_SAFE_AUTO_THRESHOLD = 0.90   # min confidence for a safe transform to auto-apply

# Destructiveness tiers (see TRANSFORM_GENERATOR_SQL_SPEC).
_TIER_SAFE = "safe"            # idempotent, no data loss
_TIER_LOSSY = "lossy"          # TRY_CAST may null unparseable rows
_TIER_DESTRUCTIVE = "destructive"  # drops rows/cols or nulls real values — never auto


def _q(name: str) -> str:
    """Backtick-quote a Spark SQL identifier (handles spaces/special chars)."""
    return "`" + str(name).replace("`", "``") + "`"


def _sql_str_list(values: list) -> str:
    """Render a list of values as a SQL string IN-list: 'a', 'b'."""
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def _apply_sql_op(inner: str, op: dict) -> str:
    """Wrap an inner SQL expression with one transform op. Composes inside-out."""
    t = op["id"]
    if t == "null_cleanup":
        return f"CASE WHEN TRIM({inner}) IN ({_sql_str_list(op['tokens'])}) THEN NULL ELSE {inner} END"
    if t == "map_sentinel":
        return f"CASE WHEN {inner} IN ({_sql_str_list(op['tokens'])}) THEN NULL ELSE {inner} END"
    if t == "trim":
        return f"NULLIF(TRIM({inner}), '')"
    if t == "normalize_case":
        return f"{op.get('case_fn', 'UPPER')}(TRIM({inner}))"
    if t == "cast":
        return f"TRY_CAST({inner} AS {op['target_type']})"
    if t == "parse_currency":
        return f"TRY_CAST(REGEXP_REPLACE({inner}, '[$,]', '') AS DECIMAL(18,2))"
    if t == "parse_percentage":
        return f"TRY_CAST(REGEXP_REPLACE({inner}, '%', '') AS DOUBLE) / 100"
    if t == "strip_units":
        return f"TRY_CAST(REGEXP_REPLACE({inner}, '[^0-9.-]', '') AS DOUBLE)"
    if t == "boolean_cast":
        return (
            f"CASE WHEN LOWER(TRIM({inner})) IN ({_sql_str_list(op['true_values'])}) THEN TRUE "
            f"WHEN LOWER(TRIM({inner})) IN ({_sql_str_list(op['false_values'])}) THEN FALSE END"
        )
    if t == "date_parse":
        if op.get("format"):
            return f"TO_DATE({inner}, '{op['format']}')"
        return f"TRY_CAST({inner} AS DATE)"
    return inner


# Composition order: clean values → trim/case → type-parse (inner → outer).
_OP_COMPOSE_RANK = {
    "null_cleanup": 1, "map_sentinel": 1,
    "trim": 2, "normalize_case": 2,
    "cast": 3, "parse_currency": 3, "parse_percentage": 3,
    "strip_units": 3, "boolean_cast": 3, "date_parse": 3,
}


def _auto_apply(tier: str, confidence: float, lossy_threshold: float = _AUTO_APPLY_THRESHOLD) -> bool:
    if tier == _TIER_DESTRUCTIVE:
        return False
    if tier == _TIER_SAFE:
        return confidence >= _SAFE_AUTO_THRESHOLD
    return confidence >= lossy_threshold  # lossy


def _detect_parse_op(col_name: str, col: dict, type_mismatches: dict) -> dict | None:
    """Pick the single most-specific type-parse op for a column, if any."""
    semantic = col.get("semantic_type", "unknown")
    inferred = col.get("inferred_type", "string")
    mismatch = type_mismatches.get(col_name, {})
    parseable = mismatch.get("parseable_pct")
    conf = (parseable / 100.0 if parseable and parseable > 1 else parseable) or _HIGH_CONFIDENCE

    base = {"column": col_name, "tier": _TIER_LOSSY}
    # Precedence: date > boolean > currency > percentage > units > numeric
    if inferred == "date" or semantic == "date_string":
        fmt = mismatch.get("date_format") or col.get("date_format")
        return {**base, "id": "date_parse", "format": fmt,
                "confidence": conf if fmt else min(conf, 0.85),
                "issue": "date stored as text", "rationale": f"date_format={fmt or 'inferred'}"}
    if inferred == "boolean" or semantic == "boolean_string":
        return {**base, "id": "boolean_cast", "true_values": sorted(_BOOL_TRUE_VALUES),
                "false_values": sorted(_BOOL_FALSE_VALUES), "confidence": max(conf, _HIGH_CONFIDENCE),
                "issue": "boolean stored as text", "rationale": "2-value boolean string"}
    if semantic == "currency_amount":
        return {**base, "id": "parse_currency", "confidence": conf,
                "issue": "currency stored as text", "rationale": "semantic_type=currency_amount; strip $ , then TRY_CAST"}
    if semantic == "percentage":
        return {**base, "id": "parse_percentage", "confidence": conf,
                "issue": "percentage stored as text", "rationale": "semantic_type=percentage; strip % then /100"}
    if col.get("has_embedded_units") and inferred in ("float", "int"):
        return {**base, "id": "strip_units", "confidence": min(conf, _MEDIUM_CONFIDENCE + 0.15),
                "issue": "numeric with embedded units", "rationale": "strip non-numeric chars then TRY_CAST"}
    if inferred in ("float", "int") and col.get("declared_dtype", "object") == "object":
        target = "BIGINT" if inferred == "int" else "DOUBLE"
        return {**base, "id": "cast", "target_type": target, "confidence": conf,
                "issue": f"numeric ({inferred}) stored as text", "rationale": f"TRY_CAST to {target}"}
    return None


def _build_column_transforms(
    profile: dict,
    *,
    auto_apply_threshold: float,
    drop_null_threshold: float | None,
    rename_map: dict,
    custom_overrides: dict | None,
) -> tuple[list[dict], list[str], dict[str, list[dict]], dict | None]:
    """Return (column_transforms, needs_review, auto_ops_by_col, dedup_decision)."""
    columns = profile.get("columns", {})
    type_mismatches = profile.get("type_mismatches", {})
    null_like_strings = profile.get("null_like_strings", {})

    column_transforms: list[dict] = []
    needs_review: list[str] = []
    auto_ops_by_col: dict[str, list[dict]] = {}

    def _finalize(op: dict, col_name: str, out_name: str) -> None:
        op.setdefault("tier", _TIER_SAFE)
        op["destructive"] = op["tier"] == _TIER_DESTRUCTIVE
        op["auto_apply"] = _auto_apply(op["tier"], op.get("confidence", 1.0), auto_apply_threshold)
        op["output_name"] = out_name
        if op.get("kind") == "drop":
            sql = f"-- omit {_q(col_name)} ({op['issue']})"
        else:
            sql = f"{_apply_sql_op(_q(col_name), op)} AS {_q(out_name)}"
        column_transforms.append({
            "column": col_name, "issue": op["issue"], "transform": op["id"],
            "sql": sql, "confidence": round(op.get("confidence", 1.0), 3),
            "destructive": op["destructive"], "auto_apply": op["auto_apply"],
            "rationale": op.get("rationale", ""), "output_name": out_name,
        })
        if op["auto_apply"] and op.get("kind") != "drop":
            auto_ops_by_col.setdefault(col_name, []).append(op)
        elif not op["auto_apply"]:
            needs_review.append(col_name)

    for col_name, col in columns.items():
        if _should_skip_column(col_name, custom_overrides):
            continue
        out_name = rename_map.get(col_name, col_name)
        if out_name != col_name:
            _finalize({"id": "standardize_columns", "tier": _TIER_SAFE, "confidence": 1.0,
                       "issue": "non-snake-case column name", "rationale": f"rename to {out_name}"},
                      col_name, out_name)

        parse_op = _detect_parse_op(col_name, col, type_mismatches)
        if parse_op:
            _finalize(parse_op, col_name, out_name)
        else:
            # String cleaning only when there is no type-parse (TRY_CAST subsumes it).
            if col.get("has_mixed_case") and (col.get("normalization_gap") or 0) > 0:
                _finalize({"id": "normalize_case", "tier": _TIER_SAFE, "confidence": _HIGH_CONFIDENCE,
                           "case_fn": "UPPER", "issue": "inconsistent casing/whitespace",
                           "rationale": "collapse case + trim variants"}, col_name, out_name)
            elif col.get("has_leading_spaces") or col.get("has_trailing_spaces"):
                _finalize({"id": "trim", "tier": _TIER_SAFE, "confidence": 1.0,
                           "issue": "leading/trailing whitespace", "rationale": "TRIM + empty→NULL"},
                          col_name, out_name)
            tokens = null_like_strings.get(col_name) or col.get("null_like_values") or []
            if tokens:
                _finalize({"id": "null_cleanup", "tier": _TIER_SAFE, "confidence": 1.0, "tokens": list(tokens),
                           "issue": f"null-like sentinels stored as text: {list(tokens)[:5]}",
                           "rationale": "map known null-like tokens to NULL"}, col_name, out_name)

        # Destructive (always review)
        sentinels = col.get("sentinel_values") or []
        if sentinels:
            _finalize({"id": "map_sentinel", "tier": _TIER_DESTRUCTIVE, "confidence": _MEDIUM_CONFIDENCE,
                       "tokens": list(sentinels), "issue": f"possible sentinel values: {list(sentinels)[:5]}",
                       "rationale": "verify these aren't real values before mapping to NULL"}, col_name, out_name)
        if col.get("is_constant"):
            _finalize({"id": "drop_constant", "kind": "drop", "tier": _TIER_DESTRUCTIVE, "confidence": 1.0,
                       "issue": "constant column", "rationale": "single distinct value — candidate to drop"},
                      col_name, out_name)
        elif drop_null_threshold is not None and (col.get("null_rate") or 0) >= drop_null_threshold:
            _finalize({"id": "drop_high_null", "kind": "drop", "tier": _TIER_DESTRUCTIVE,
                       "confidence": col.get("null_rate", 0.0),
                       "issue": f"{(col.get('null_rate') or 0):.0%} null",
                       "rationale": "mostly null — candidate to drop"}, col_name, out_name)

    # Table-level dedup (always review)
    dedup_decision = None
    key_candidates = profile.get("key_candidates", [])
    if key_candidates:
        first = key_candidates[0]
        keys = list(first) if isinstance(first, (list, tuple)) else [first]
        snap = profile.get("snapshot_columns") or []
        tiebreak = snap[0] if snap else keys[0]
        qualify = (
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {', '.join(_q(k) for k in keys)} "
            f"ORDER BY {_q(tiebreak)} DESC) = 1"
        )
        # key_candidates are derived from already-unique columns (is_unique +
        # no nulls), so this is a PROACTIVE grain guard, not an observed-duplicates
        # finding — don't claim "duplicate keys" exist when they don't.
        dedup_decision = {
            "column": "<table>", "issue": f"enforce unique grain on {keys}", "transform": "dedup",
            "sql": qualify, "confidence": _DEDUP_CONFIDENCE, "destructive": True,
            "auto_apply": False,
            "rationale": f"guard grain {keys} (e.g. on refresh); keep latest by {tiebreak}; verify key + tiebreak",
            "output_name": None,
        }
        column_transforms.append(dedup_decision)
        needs_review.append("<dedup>")

    return column_transforms, sorted(set(needs_review)), auto_ops_by_col, dedup_decision


def _assemble_code_sql(
    profile: dict,
    auto_ops_by_col: dict[str, list[dict]],
    rename_map: dict,
    review_transforms: list[dict],
    *,
    source: str,
) -> str:
    """Assemble the confidence-gated SELECT projection from auto-applied ops."""
    columns = profile.get("columns", {})
    select_lines: list[str] = []
    for col_name in columns:
        out_name = rename_map.get(col_name, col_name)
        ops = sorted(auto_ops_by_col.get(col_name, []), key=lambda o: _OP_COMPOSE_RANK.get(o["id"], 9))
        expr = _q(col_name)
        for op in ops:
            expr = _apply_sql_op(expr, op)
        if expr == _q(col_name) and out_name == col_name:
            select_lines.append(f"  {expr}")  # plain passthrough
        else:
            select_lines.append(f"  {expr} AS {_q(out_name)}")

    sql = "SELECT\n" + ",\n".join(select_lines) + f"\nFROM {source}"

    review = [t for t in review_transforms if not t["auto_apply"]]
    if review:
        sql += "\n\n-- REVIEW (not auto-applied; confirm before including):"
        for t in review:
            sql += f"\n--   {t['column']}: {t['transform']} (conf {t['confidence']:.2f}) | {t['sql']}"
    return sql


def transform_plan_context(
    profile: dict,
    *,
    subject: str | None = None,
    layer: str = "silver",
    include_cast: bool = True,
    include_null_clean: bool = True,
    include_dedup: bool = True,
    include_drop_constant: bool = False,
    include_standardize: bool = True,
    custom_overrides: dict | None = None,
    auto_apply_threshold: float = _AUTO_APPLY_THRESHOLD,
    drop_null_threshold: float | None = None,
    source: str | None = None,
    target: str | None = None,
    output_format: str = "dict",
) -> dict | str:
    """Generate a reviewable transform plan from a profile dict.

    Processes the profile and generates ordered transform steps with
    executable code for both Spark and Pandas engines.

    Args:
        profile: Either a transform-format dict (with 'columns', 'type_mismatches',
            etc.) OR the raw output from anchor("profile_table"). If a
            profiler output is detected (has a recognized 'kind' key),
            it is auto-adapted to transform format.
        subject: Human-readable label for the table being transformed.
        layer: Target medallion layer (reserved for future use).
        include_cast: Whether to generate type cast steps.
        include_null_clean: Whether to generate null-like cleanup steps.
        include_dedup: Whether to generate deduplication step.
        include_drop_constant: Whether to drop constant/null columns.
        include_standardize: Whether to standardize column names.
        custom_overrides: Per-column overrides. Use {"col": "skip"} or
            {"col": False} or {"col": {"skip": True}} to skip a column.
        output_format: "dict" or "markdown".

    Returns:
        Standard context dict with extra keys: steps, code_spark, code_pandas.

    Raises:
        ValueError: If output_format is invalid.
    """
    validate_output_format(output_format)

    # ── Auto-detect profiler output and adapt ─────────────────────────────────
    _PROFILER_KINDS = {"dataset_profile_context", "exploration_context", "profile_table"}
    if profile.get("kind") in _PROFILER_KINDS:
        profile = adapt_profile_to_transform_input(profile)

    if subject is None:
        subject = profile.get("summary", {}).get("table_name", "unknown_table")

    # Build rename map for downstream steps
    columns = profile.get("columns", {})
    rename_map = _build_column_rename_map(columns, custom_overrides) if include_standardize else {}

    # ── Generate steps in order ───────────────────────────────────────────────
    all_steps: list[dict] = []

    if include_standardize:
        all_steps.extend(_gen_standardize_step(profile, custom_overrides))

    if include_null_clean:
        # Trim BEFORE null-cleanup so " N/A " -> "N/A" is then mapped to NULL.
        all_steps.extend(_gen_trim_steps(profile, rename_map, custom_overrides))
        all_steps.extend(_gen_null_cleanup_steps(profile, rename_map, custom_overrides))

    if include_cast:
        all_steps.extend(_gen_cast_steps(profile, rename_map, custom_overrides))

    # Date parse (always if type_mismatches has date entries)
    all_steps.extend(_gen_date_parse_steps(profile, rename_map, custom_overrides))

    if include_drop_constant:
        all_steps.extend(_gen_drop_constant_steps(profile, rename_map, custom_overrides))

    if include_dedup:
        all_steps.extend(_gen_dedup_step(profile, rename_map, custom_overrides))

    # ── Assign order numbers ──────────────────────────────────────────────────
    for i, step in enumerate(all_steps, start=1):
        step["order"] = i

    # ── Assemble full code blocks (legacy DataFrame path, frozen) ─────────────
    code_spark = _assemble_code_spark(all_steps)
    code_pandas = _assemble_code_pandas(all_steps)

    # ── SQL-first path: per-column decisions + confidence-gated projection ────
    column_transforms, needs_review, _auto_ops_by_col, _dedup = _build_column_transforms(
        profile,
        auto_apply_threshold=auto_apply_threshold,
        drop_null_threshold=drop_null_threshold,
        rename_map=rename_map,
        custom_overrides=custom_overrides,
    )
    _source = source or (subject if subject and subject.count(".") == 2 else "{source}")
    code_sql = _assemble_code_sql(profile, _auto_ops_by_col, rename_map, column_transforms, source=_source)
    _target = target or "{target}"
    code_sql_ctas = f"CREATE OR REPLACE TABLE {_target} AS\n{code_sql}"
    code_sql_view = f"CREATE OR REPLACE TEMP VIEW {_target}_clean AS\n{code_sql}"
    _auto_apply_count = sum(1 for t in column_transforms if t["auto_apply"])
    _review_count = sum(1 for t in column_transforms if not t["auto_apply"])

    # ── Build metrics ─────────────────────────────────────────────────────────
    step_counts: dict[str, int] = {}
    columns_affected: set[str] = set()
    for step in all_steps:
        action = step["action"]
        step_counts[action] = step_counts.get(action, 0) + 1
        columns_affected.update(step["columns"])

    min_confidence = min((s["confidence"] for s in all_steps), default=1.0)

    metrics = {
        "total_steps": len(all_steps),
        "columns_affected": len(columns_affected),
        "step_counts": step_counts,
        "has_dedup": any(s["action"] == "dedup" for s in all_steps),
        "min_confidence": min_confidence,
        "column_transform_count": len(column_transforms),
        "auto_apply_count": _auto_apply_count,
        "needs_review_count": _review_count,
    }

    # ── Build summary string ──────────────────────────────────────────────────
    if all_steps:
        parts = []
        for action, count in step_counts.items():
            label = action.replace("_", " ")
            parts.append(f"{count} {label}{'s' if count > 1 else ''}")
        summary = f"{len(all_steps)} transforms: {', '.join(parts)}"
    else:
        summary = "0 transforms: profile requires no automated cleanup"

    # ── Build findings ────────────────────────────────────────────────────────
    findings = []
    changed_names = sum(old != new for old, new in rename_map.items())
    if changed_names and include_standardize:
        findings.append(f"{changed_names} columns need snake_case standardization")
    null_cols = sum(1 for v in profile.get("null_like_strings", {}).values() if v)
    if null_cols and include_null_clean:
        findings.append(f"{null_cols} columns contain null-like string values")
    type_mismatches = profile.get("type_mismatches", {})
    if type_mismatches and include_cast:
        findings.append(f"{len(type_mismatches)} columns have type mismatches")
    if profile.get("key_candidates") and include_dedup:
        findings.append(f"Dedup key candidates detected: {profile['key_candidates']}")

    # ── Build risks ───────────────────────────────────────────────────────────
    risks = []
    low_conf_casts = [s for s in all_steps if s["action"] == "cast" and s["confidence"] < _LOW_CONF_CAST_THRESHOLD]
    if low_conf_casts:
        risks.append(f"{len(low_conf_casts)} casts below 99% confidence — review before applying")
    if any(s["action"] == "dedup" and s["confidence"] < _MEDIUM_CONFIDENCE for s in all_steps):
        risks.append("Dedup key selection has low confidence — verify key choice")

    # ── Suggested actions ─────────────────────────────────────────────────────
    suggested_next_actions = []
    if all_steps:
        suggested_next_actions.append("MUST: Review generated code before executing")
        if low_conf_casts:
            suggested_next_actions.append("MUST: Validate cast columns with sample data")
        if any(s["action"] == "dedup" for s in all_steps):
            suggested_next_actions.append("MUST: Confirm dedup key choice with domain expert")
    suggested_next_actions.append("COULD: Run anchor('quality', df) after applying transforms")

    # ── Build context ─────────────────────────────────────────────────────────
    ctx = build_base_context(
        kind="transform_plan_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_next_actions,
        steps=all_steps,
        code_spark=code_spark,
        code_pandas=code_pandas,
        column_transforms=column_transforms,
        needs_review=needs_review,
        code_sql=code_sql,
        code_sql_ctas=code_sql_ctas,
        code_sql_view=code_sql_view,
        layer=layer,
    )

    return finalize_context(ctx, output_format, render_transform_plan_report)


# ── Render Function ───────────────────────────────────────────────────────────


def render_transform_plan_report(ctx: dict) -> str:
    """Render transform plan context dict as a markdown report.

    Args:
        ctx: Context dict produced by transform_plan_context.

    Returns:
        Formatted markdown string with summary, metrics, steps, and code.
    """
    lines = render_header_lines(ctx, "Transform Plan")
    lines.extend(render_metrics_lines(ctx["metrics"], exclude={"step_counts"}))
    lines.append("")

    # Step counts breakdown
    step_counts = ctx["metrics"].get("step_counts", {})
    if step_counts:
        lines.append("### Step Breakdown")
        for action, count in step_counts.items():
            lines.append(f"- {action}: {count}")
        lines.append("")

    # Steps table
    steps = ctx.get("steps", [])
    if steps:
        lines.append("## Steps")
        lines.append("")
        lines.append("| Order | Action | Columns | Confidence | Source |")
        lines.append("| --- | --- | --- | --- | --- |")
        for step in steps:
            cols = ", ".join(step["columns"][:3])
            if len(step["columns"]) > 3:
                cols += f" (+{len(step['columns']) - 3})"
            lines.append(
                f"| {step['order']} | {step['action']} | {cols} "
                f"| {step['confidence']:.2f} | {step['source']} |"
            )
        lines.append("")

    # Column transforms (SQL-first, per-column decisions)
    column_transforms = ctx.get("column_transforms", [])
    if column_transforms:
        lines.append("## Column Transforms")
        lines.append("")
        lines.append("| Column | Transform | Confidence | Apply | Issue |")
        lines.append("| --- | --- | --- | --- | --- |")
        for t in column_transforms:
            apply_label = "auto" if t["auto_apply"] else "review"
            lines.append(
                f"| {t['column']} | {t['transform']} | {t['confidence']:.2f} "
                f"| {apply_label} | {t['issue']} |"
            )
        lines.append("")

    # Generated cleanup SQL (the runnable deliverable)
    if ctx.get("code_sql"):
        lines.append("## Cleanup SQL")
        lines.append("")
        lines.append("```sql")
        lines.append(ctx["code_sql"])
        lines.append("```")
        lines.append("")

    # Spark code block (legacy DataFrame path)
    lines.append("## Spark Code")
    lines.append("")
    lines.append("```python")
    lines.append(ctx.get("code_spark", "# No code"))
    lines.append("```")
    lines.append("")

    # Pandas code block
    lines.append("## Pandas Code")
    lines.append("")
    lines.append("```python")
    lines.append(ctx.get("code_pandas", "# No code"))
    lines.append("```")
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


# ── Profile Adapter ───────────────────────────────────────────────────────────


def adapt_profile_to_transform_input(profile_ctx: dict) -> dict:
    """Adapt dataset_profile_context or exploration_context output to transform input.

    Bridges the profiler output format to the flat dict expected by
    transform_plan_context. Handles profile_table and legacy output shapes.

    Args:
        profile_ctx: Output dict from anchor("profile_table") or any profiler.
            Must contain 'column_profiles' or 'columns' key with per-column
            profile data.

    Returns:
        Dict in transform_plan_context input format with keys:
        columns, null_like_strings, type_mismatches, key_candidates,
        anomalies, snapshot_columns, summary.

    Example:
        >>> profile_ctx = anchor("profile_table", df, subject="invoices", output_format="dict")
        >>> transform_input = adapt_profile_to_transform_input(profile_ctx)
        >>> plan = transform_plan_context(transform_input, subject="invoices")
    """
    # Detect input shape: dataset_profile_context uses 'column_profiles',
    # exploration_context uses 'column_profiles' too but with different internals
    column_profiles = profile_ctx.get("column_profiles", {})
    metrics = profile_ctx.get("metrics", {})

    # Type mapping: profiler inferred_type → transform plan categories
    _TYPE_MAP = {
        "numeric_float_like": "float",
        "numeric_integer_like": "int",
        "numeric_float": "float",
        "numeric_integer": "int",
        "numeric": "float",
        "datetime_like": "date",
        "datetime": "date",
        "boolean_like": "boolean",
        "boolean": "boolean",
        "categorical": "string",
        "text": "string",
        "unknown": "string",
    }

    # Build columns dict in transform_plan format
    columns = {}
    for col_name, prof in column_profiles.items():
        inferred = prof.get("inferred_type", "string")
        mapped_type = _TYPE_MAP.get(inferred, "string")

        # Extract top_values as list of tuples (value, count)
        top_values_raw = prof.get("top_values", [])
        top_values = []
        for tv in top_values_raw:
            if isinstance(tv, dict):
                top_values.append((tv.get("value", ""), tv.get("count", 0)))
            elif isinstance(tv, (list, tuple)) and len(tv) >= 2:
                top_values.append((tv[0], tv[1]))

        columns[col_name] = {
            "name": col_name,
            "declared_dtype": prof.get("dtype", "object"),
            "inferred_type": mapped_type,
            "null_rate": prof.get("null_pct", 0.0),
            "distinct_count": prof.get("distinct_count", 0),
            "unique_rate": prof.get("distinct_pct", 0.0),
            "date_format": None,
            "date_formats": [],
            "top_values": top_values,
            # Signals consumed by the SQL column-transform rules.
            "semantic_type": prof.get("semantic_type", "unknown"),
            "is_constant": bool(prof.get("is_constant", False)),
            "is_unique": bool(prof.get("is_unique", False)),
            "null_like_values": list(prof.get("null_like_values") or []),
            "has_leading_spaces": bool(prof.get("has_leading_spaces", False)),
            "has_trailing_spaces": bool(prof.get("has_trailing_spaces", False)),
            "has_mixed_case": bool(prof.get("has_mixed_case", False)),
            "has_embedded_units": bool(prof.get("has_embedded_units", False)),
            "sentinel_values": list(prof.get("sentinel_values") or []),
            "normalization_gap": prof.get("normalization_gap", 0.0) or 0.0,
        }

    # Build null_like_strings from profiler output
    null_like_strings = {}
    for col_name, prof in column_profiles.items():
        null_like = prof.get("null_like_values", [])
        if null_like:
            null_like_strings[col_name] = null_like

    # Build type_mismatches — columns where dtype is object but inferred is typed
    type_mismatches = {}
    for col_name, col_data in columns.items():
        declared = col_data["declared_dtype"]
        inferred = col_data["inferred_type"]
        if declared == "object" and inferred in ("float", "int", "date", "boolean"):
            type_mismatches[col_name] = {
                "declared": declared,
                "inferred": inferred,
                "parseable_pct": 95.0,  # conservative default from profiler threshold
                "date_format": col_data.get("date_format"),
            }

    # Build key_candidates. Legacy dataset_profile_context exposed
    # metrics.potential_key_columns; profile_table keeps metrics flat, so derive
    # from per-column is_unique + zero nulls when the metrics list is absent.
    key_candidates = []
    potential_keys = metrics.get("potential_key_columns")
    if potential_keys is None:
        potential_keys = [
            name for name, prof in column_profiles.items()
            if prof.get("is_unique") and (prof.get("null_pct") or 0.0) == 0.0
        ]
    for key_col in potential_keys:
        key_candidates.append((key_col,))

    # For exploration_context: use suggested_keys or grain_analysis
    if not key_candidates and "grain_analysis" in profile_ctx:
        best_grain = profile_ctx["grain_analysis"].get("best_grain", [])
        if best_grain:
            key_candidates.append(tuple(best_grain))

    # Build anomalies. Same story: prefer legacy metrics lists, else derive from
    # per-column flags (is_constant / null_pct).
    all_null_cols = metrics.get("all_null_columns")
    if all_null_cols is None:
        all_null_cols = [
            name for name, prof in column_profiles.items()
            if (prof.get("non_null_count") == 0) or (prof.get("null_pct") or 0.0) >= 1.0
        ]
    constant_cols = metrics.get("constant_columns")
    if constant_cols is None:
        constant_cols = [name for name, prof in column_profiles.items() if prof.get("is_constant")]

    anomalies = []
    for col_name in all_null_cols:
        anomalies.append(f"\'{col_name}\' is entirely null")
    for col_name in constant_cols:
        anomalies.append(f"\'{col_name}\' is constant")

    # Detect snapshot columns from profiler freshness
    snapshot_columns = []
    freshness = profile_ctx.get("freshness")
    if freshness and isinstance(freshness, dict):
        fresh_col = freshness.get("column")
        if fresh_col and "snapshot" in fresh_col.lower():
            snapshot_columns.append(fresh_col)

    return {
        "summary": {
            "row_count": metrics.get("row_count", 0),
            "column_count": metrics.get("column_count", 0),
        },
        "columns": columns,
        "null_like_strings": null_like_strings,
        "type_mismatches": type_mismatches,
        "key_candidates": key_candidates,
        "anomalies": anomalies,
        "snapshot_columns": snapshot_columns,
    }
