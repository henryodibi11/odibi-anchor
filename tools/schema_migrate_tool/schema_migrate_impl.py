"""Schema migration code generator.

Entry point: schema_migrate_context(schema_diff_ctx, target_table, ...)
Takes output from anchor("schema_diff") and generates executable DDL + PySpark
migration code for bringing a target table in line with a source schema.

Generates code only — never executes. Engineer reviews and applies.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
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
# Safe cast mapping
# ============================================================================

_SAFE_CASTS: dict[tuple[str, str], bool] = {
    # Widening — always safe
    ("int", "bigint"): True,
    ("int", "long"): True,
    ("int", "double"): True,
    ("int", "float"): True,
    ("int", "decimal"): True,
    ("short", "int"): True,
    ("short", "bigint"): True,
    ("short", "long"): True,
    ("byte", "short"): True,
    ("byte", "int"): True,
    ("byte", "bigint"): True,
    ("float", "double"): True,
    ("float", "decimal"): True,
    ("bigint", "double"): True,
    ("bigint", "float"): True,
    ("bigint", "decimal"): True,
    ("long", "double"): True,
    ("long", "float"): True,
    ("long", "decimal"): True,
    ("int", "string"): True,
    ("date", "timestamp"): True,
    ("bigint", "string"): True,
    ("long", "string"): True,
    ("double", "string"): True,
    ("float", "string"): True,
    ("boolean", "string"): True,
    ("date", "string"): True,
    ("timestamp", "string"): True,
    # Lossy — needs TRY_CAST
    ("string", "int"): False,
    ("string", "bigint"): False,
    ("string", "long"): False,
    ("string", "short"): False,
    ("string", "float"): False,
    ("string", "double"): False,
    ("string", "decimal"): False,
    ("string", "date"): False,
    ("string", "timestamp"): False,
    ("string", "boolean"): False,
    ("double", "int"): False,
    ("double", "bigint"): False,
    ("double", "float"): False,
    ("float", "int"): False,
    ("bigint", "int"): False,
    ("long", "int"): False,
    ("timestamp", "date"): False,
    ("decimal", "int"): False,
    ("decimal", "bigint"): False,
}


# ============================================================================
# Pandas → SQL type normalization
# ============================================================================

_PANDAS_TYPE_MAP: dict[str, str] = {
    # Numeric
    "int8": "byte",
    "int16": "short",
    "int32": "int",
    "int64": "bigint",
    "uint8": "short",
    "uint16": "int",
    "uint32": "bigint",
    "uint64": "bigint",
    "float16": "float",
    "float32": "float",
    "float64": "double",
    # Nullable integer (pandas)
    "int8": "byte",
    "int16": "short",
    "int32": "int",
    "int64": "bigint",
    # String / object
    "object": "string",
    "string": "string",
    "str": "string",
    # Boolean
    "bool": "boolean",
    "boolean": "boolean",
    # Datetime
    "datetime64[ns]": "timestamp",
    "datetime64[us]": "timestamp",
    "datetime64[ms]": "timestamp",
    "datetime64[s]": "timestamp",
    "datetime64": "timestamp",
    "timedelta64[ns]": "string",
    # Date (pandas doesn't have native date — usually object or datetime)
    "date": "date",
    # Category
    "category": "string",
}


# Spark repr → canonical SQL type name mapping
_SPARK_REPR_MAP: dict[str, str] = {
    "integertype": "int",
    "longtype": "bigint",
    "shorttype": "short",
    "bytetype": "byte",
    "floattype": "float",
    "doubletype": "double",
    "stringtype": "string",
    "booleantype": "boolean",
    "datetype": "date",
    "timestamptype": "timestamp",
    "timestampntztype": "timestamp",
    "binarytype": "binary",
    "nulltype": "void",
    "decimaltype": "decimal",
    "varchartype": "varchar",
    "chartype": "char",
}

# Regex to match Spark type repr: "TypeName()" or "TypeName(args)"
_SPARK_TYPE_RE = re.compile(
    r"^([A-Za-z]+Type)\((.*)\)$", re.DOTALL
)


def _parse_spark_repr(type_name: str) -> str | None:
    """Parse a Spark Python repr string to canonical SQL form.

    Returns None if the string is not a Spark repr.
    Examples:
        "IntegerType()" → "int"
        "LongType()" → "bigint"
        "DecimalType(10,2)" → "decimal(10,2)"
        "ArrayType(StringType(), True)" → "array<string>"
        "MapType(StringType(), IntegerType(), True)" → "map<string,int>"
        "StructType(...)" → "struct"
    """
    m = _SPARK_TYPE_RE.match(type_name.strip())
    if not m:
        return None

    type_class = m.group(1).lower()  # e.g. "integertype", "decimaltype"
    args = m.group(2).strip()        # e.g. "", "10,2", "StringType(), True"

    # Simple types (no args or just nullable flag)
    if type_class in _SPARK_REPR_MAP:
        base = _SPARK_REPR_MAP[type_class]
        # DecimalType with precision: DecimalType(10,2) → decimal(10,2)
        if type_class == "decimaltype" and args:
            # Strip trailing nullable bool if present
            clean_args = re.sub(r",\s*(True|False)\s*$", "", args)
            if clean_args:
                return f"decimal({clean_args})"
            return "decimal"
        # VarcharType/CharType with length
        if type_class in ("varchartype", "chartype") and args:
            clean_args = re.sub(r",\s*(True|False)\s*$", "", args)
            return f"{base}({clean_args})"
        return base

    # ArrayType(elementType, containsNull) → array<elementType>
    if type_class == "arraytype":
        # Extract element type (first arg before last comma+bool)
        inner = re.sub(r",\s*(True|False)\s*$", "", args)
        inner_canonical = _parse_spark_repr(inner)
        if inner_canonical:
            return f"array<{inner_canonical}>"
        return "array"

    # MapType(keyType, valueType, valueContainsNull) → map<key,value>
    if type_class == "maptype":
        # Split on top-level commas (not inside nested parens)
        parts = _split_top_level(args)
        if len(parts) >= 2:
            key_type = _parse_spark_repr(parts[0].strip()) or parts[0].strip()
            val_type = _parse_spark_repr(parts[1].strip()) or parts[1].strip()
            return f"map<{key_type},{val_type}>"
        return "map"

    # StructType(...) → struct (complex internals not parsed)
    if type_class == "structtype":
        return "struct"

    return None


def _split_top_level(s: str) -> list[str]:
    """Split a string by commas, but only at the top level (not inside parens)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _normalize_type(type_name: str) -> str:
    """Normalize a type name to canonical SQL/Spark form.

    Handles:
    - Pandas dtypes: int64, float64, object, datetime64[ns], etc.
    - Spark Python repr: IntegerType(), LongType(), DecimalType(10,2), etc.
    - SQL/Spark names: int, bigint, string, decimal(10,2), etc.
    """
    stripped = type_name.strip()

    # Try Spark repr first (most specific pattern)
    spark_result = _parse_spark_repr(stripped)
    if spark_result is not None:
        # Return base type for safe-cast lookup (strip precision)
        return spark_result.split("(")[0].split("<")[0]

    # Pandas / SQL fallback
    lowered = stripped.lower()
    if lowered in _PANDAS_TYPE_MAP:
        return _PANDAS_TYPE_MAP[lowered]
    base = lowered.split("(")[0].strip()
    if base in _PANDAS_TYPE_MAP:
        return _PANDAS_TYPE_MAP[base]
    return base


def _normalize_type_for_ddl(type_name: str) -> str:
    """Normalize type name for DDL output.

    Returns the full SQL type including precision, suitable for
    ALTER TABLE statements. Handles Spark repr, pandas, and SQL types.
    """
    stripped = type_name.strip()

    # Try Spark repr first
    spark_result = _parse_spark_repr(stripped)
    if spark_result is not None:
        return spark_result.upper()

    # Pandas / SQL fallback
    lowered = stripped.lower()
    if lowered in _PANDAS_TYPE_MAP:
        return _PANDAS_TYPE_MAP[lowered].upper()
    base = lowered.split("(")[0].strip()
    if base in _PANDAS_TYPE_MAP:
        suffix = type_name[len(base):]
        return _PANDAS_TYPE_MAP[base].upper() + suffix
    return type_name


# ============================================================================
# Type defaults for ADD COLUMN
# ============================================================================

_TYPE_DEFAULTS: dict[str, str] = {
    "string": "NULL",
    "int": "0",
    "integer": "0",
    "bigint": "0",
    "long": "0",
    "short": "0",
    "byte": "0",
    "float": "0",
    "double": "0",
    "decimal": "0",
    "boolean": "false",
    "date": "NULL",
    "timestamp": "NULL",
    "binary": "NULL",
    "array": "NULL",
    "map": "NULL",
    "struct": "NULL",
}


def _type_default(col_type: str) -> str:
    """Return a type-appropriate default for ADD COLUMN."""
    normalized = _normalize_type(col_type)
    return _TYPE_DEFAULTS.get(normalized, "NULL")


def _is_safe_cast(from_type: str, to_type: str) -> bool:
    """Determine if a cast is safe (widening) or lossy."""
    from_norm = _normalize_type(from_type)
    to_norm = _normalize_type(to_type)
    if from_norm == to_norm:
        return True
    return _SAFE_CASTS.get((from_norm, to_norm), False)


def _escape_col(col_name: str) -> str:
    """Escape column name with backticks for SQL."""
    return f"`{col_name}`"


# ============================================================================
# Code generators
# ============================================================================


def _generate_add_column_ddl(table: str, col_name: str, col_type: str) -> dict:
    """Generate ALTER TABLE ADD COLUMN statement."""
    default = _type_default(col_type)
    sql_type = _normalize_type_for_ddl(col_type)
    escaped = _escape_col(col_name)
    if default == "NULL":
        ddl = f"ALTER TABLE {table} ADD COLUMN {escaped} {sql_type};"
    else:
        ddl = f"ALTER TABLE {table} ADD COLUMN {escaped} {sql_type} DEFAULT {default};"
    pyspark = f"# Column '{col_name}' ({sql_type}) — add via DDL above or schema evolution"
    return {
        "column": col_name,
        "type": sql_type,
        "default": default,
        "ddl": ddl,
        "pyspark": pyspark,
    }


def _generate_cast_expression(
    table: str, col_name: str, from_type: str, to_type: str
) -> dict:
    """Generate safe CAST or TRY_CAST expression."""
    safe = _is_safe_cast(from_type, to_type)
    escaped = _escape_col(col_name)

    # Normalize for DDL and expression output
    sql_to_type = _normalize_type_for_ddl(to_type)

    if safe:
        sql_expr = f"CAST({escaped} AS {sql_to_type})"
        spark_expr = f".withColumn('{col_name}', F.col('{col_name}').cast('{sql_to_type}'))"
    else:
        sql_expr = f"TRY_CAST({escaped} AS {sql_to_type})"
        spark_expr = (
            f".withColumn('{col_name}', "
            f'F.expr("TRY_CAST(`{col_name}` AS {sql_to_type})")'
        )

    # Build UPDATE statement for in-place type migration
    update_ddl = (
        f"-- Type change: {col_name} ({from_type} -> {to_type})\n"
        f"ALTER TABLE {table} ALTER COLUMN {escaped} TYPE {sql_to_type};"
        if safe
        else (
            f"-- Type change: {col_name} ({from_type} -> {to_type}) — UNSAFE, verify first\n"
            f"-- Check for lossy values: SELECT {escaped}, {sql_expr} FROM {table} "
            f"WHERE {sql_expr} IS NULL AND {escaped} IS NOT NULL;\n"
            f"ALTER TABLE {table} ALTER COLUMN {escaped} TYPE {sql_to_type};"
        )
    )

    return {
        "column": col_name,
        "from": from_type,
        "to": to_type,
        "safe": safe,
        "sql_expr": sql_expr,
        "spark": spark_expr,
        "ddl": update_ddl,
    }


def _generate_rename_ddl(table: str, old_name: str, new_name: str) -> dict:
    """Generate ALTER TABLE RENAME COLUMN statement."""
    ddl = (
        f"ALTER TABLE {table} RENAME COLUMN "
        f"{_escape_col(old_name)} TO {_escape_col(new_name)};"
    )
    spark_expr = f".withColumnRenamed('{old_name}', '{new_name}')"
    return {
        "from_column": old_name,
        "to_column": new_name,
        "ddl": ddl,
        "spark": spark_expr,
    }


def _generate_drop_ddl(table: str, col_name: str, generate: bool) -> dict:
    """Generate DROP COLUMN DDL or warning-only."""
    escaped = _escape_col(col_name)
    if generate:
        ddl = f"ALTER TABLE {table} DROP COLUMN {escaped};"
        action = "drop"
    else:
        ddl = f"-- WARNING: Column '{col_name}' was removed from source. Uncomment to drop:\n-- ALTER TABLE {table} DROP COLUMN {escaped};"
        action = "warning_only"
    return {
        "column": col_name,
        "action": action,
        "ddl": ddl,
    }


# ============================================================================
# Enhanced rename detection
# ============================================================================


def _enhanced_rename_detection(
    added: list[dict], removed: list[dict], threshold: float
) -> list[dict]:
    """Detect additional renames using SequenceMatcher beyond schema_diff's built-in detection.

    Only considers columns NOT already matched by schema_diff's likely_renames.
    """
    if not added or not removed or threshold <= 0:
        return []

    candidates: list[dict] = []
    matched_added: set[str] = set()
    matched_removed: set[str] = set()

    added_names = [a["column"] for a in added]
    removed_names = [r["column"] for r in removed]

    # Score all pairs by name similarity
    scored_pairs: list[tuple[float, str, str]] = []
    for old_name in removed_names:
        for new_name in added_names:
            ratio = SequenceMatcher(None, old_name.lower(), new_name.lower()).ratio()
            if ratio >= threshold:
                scored_pairs.append((ratio, old_name, new_name))

    # Greedy match — best similarity first
    scored_pairs.sort(reverse=True, key=lambda x: x[0])
    for ratio, old_name, new_name in scored_pairs:
        if old_name in matched_removed or new_name in matched_added:
            continue
        # Check type compatibility
        old_item = next((r for r in removed if r["column"] == old_name), None)
        new_item = next((a for a in added if a["column"] == new_name), None)
        dtype_match = (
            old_item is not None
            and new_item is not None
            and old_item.get("dtype") == new_item.get("dtype")
        )
        candidates.append({
            "old_column": old_name,
            "new_column": new_name,
            "similarity": round(ratio, 3),
            "dtype_match": dtype_match,
            "confidence": "high" if dtype_match and ratio >= 0.9 else "medium",
            "reason": "name_similarity",
        })
        matched_added.add(new_name)
        matched_removed.add(old_name)

    return candidates


# ============================================================================
# Summary builder
# ============================================================================


def _build_summary(metrics: dict, dry_run: bool) -> str:
    """Build a human-readable summary line."""
    parts: list[str] = []
    if metrics["columns_added"]:
        parts.append(f"{metrics['columns_added']} ADD COLUMN")
    if metrics["columns_retyped"]:
        parts.append(f"{metrics['columns_retyped']} TYPE CAST")
    if metrics["columns_renamed"]:
        parts.append(f"{metrics['columns_renamed']} RENAME")
    if metrics["columns_dropped"]:
        parts.append(f"{metrics['columns_dropped']} DROP")

    body = ", ".join(parts) if parts else "No operations"
    suffix = "Dry run — no changes applied." if dry_run else "Ready to apply."
    return f"Migration plan: {body}. {suffix}"


# ============================================================================
# Public API
# ============================================================================


def schema_migrate_context(
    *args,
    schema_diff_ctx: dict | None = None,
    target_table: str | None = None,
    drop_columns: bool = False,
    dry_run: bool = True,
    rename_threshold: float = 0.8,
    output_format: str = "dict",
    **kwargs,
) -> dict | str:
    """Generate migration code from schema_diff output.

    Args:
        schema_diff_ctx: Output from anchor("schema_diff"). If not provided,
            first positional arg is used.
        target_table: Fully qualified target table name.
        drop_columns: If True, generate DROP COLUMN DDL. Default False (warning only).
        dry_run: If True, return plan without executing. Default True.
        rename_threshold: Similarity threshold for additional rename detection (0-1).
        output_format: "dict" or "markdown".

    Returns:
        Standard Anchor contract dict or markdown string.

    Raises:
        ValueError: If schema_diff_ctx or target_table is missing/invalid.
    """
    validate_output_format(output_format)

    # Resolve positional args
    if schema_diff_ctx is None and args:
        schema_diff_ctx = args[0]
    if target_table is None and len(args) > 1 and isinstance(args[1], str):
        target_table = args[1]

    # Validation
    if schema_diff_ctx is None:
        raise ValueError(
            "schema_diff_ctx is required. Pass output from anchor('schema_diff')."
        )
    if not isinstance(schema_diff_ctx, dict):
        raise ValueError(
            f"schema_diff_ctx must be a dict, got {type(schema_diff_ctx).__name__}. "
            "Pass output from anchor('schema_diff', source_df, target_df) "
            "— see anchor('help', 'schema_migrate')."
        )
    if target_table is None or not target_table.strip():
        raise ValueError(
            "target_table is required (e.g., 'catalog.schema.table')."
        )

    try:
        # Extract schema_diff data
        added_cols = schema_diff_ctx.get("added", [])
        removed_cols = schema_diff_ctx.get("removed", [])
        type_changed = schema_diff_ctx.get("type_changed", [])
        existing_renames = schema_diff_ctx.get("likely_renames", [])

        # ── Renames ───────────────────────────────────────────────────────────
        # Start with schema_diff's built-in renames
        all_renames = list(existing_renames)

        # Columns already matched by schema_diff renames
        already_renamed_old = {r["old_column"] for r in existing_renames}
        already_renamed_new = {r["new_column"] for r in existing_renames}

        # Filter added/removed to exclude already-renamed columns
        unmatched_added = [
            a for a in added_cols if a["column"] not in already_renamed_new
        ]
        unmatched_removed = [
            r for r in removed_cols if r["column"] not in already_renamed_old
        ]

        # Enhanced detection on unmatched columns
        extra_renames = _enhanced_rename_detection(
            unmatched_added, unmatched_removed, rename_threshold
        )
        all_renames.extend(extra_renames)

        # Update unmatched lists after enhanced detection
        extra_renamed_old = {r["old_column"] for r in extra_renames}
        extra_renamed_new = {r["new_column"] for r in extra_renames}
        final_added = [
            a for a in unmatched_added if a["column"] not in extra_renamed_new
        ]
        final_removed = [
            r for r in unmatched_removed if r["column"] not in extra_renamed_old
        ]

        # ── Generate migration operations ────────────────────────────────────
        add_ops = [
            _generate_add_column_ddl(target_table, col["column"], col["dtype"])
            for col in final_added
        ]

        cast_ops = [
            _generate_cast_expression(
                target_table, col["column"], col["old_dtype"], col["new_dtype"]
            )
            for col in type_changed
        ]

        rename_ops = [
            _generate_rename_ddl(target_table, r["old_column"], r["new_column"])
            for r in all_renames
        ]

        drop_ops = [
            _generate_drop_ddl(target_table, col["column"], drop_columns)
            for col in final_removed
        ]

        # ── Metrics ───────────────────────────────────────────────────────────
        unsafe_casts = [c for c in cast_ops if not c["safe"]]
        metrics = {
            "columns_added": len(add_ops),
            "columns_removed": len(final_removed),
            "columns_retyped": len(cast_ops),
            "columns_renamed": len(rename_ops),
            "columns_dropped": len(drop_ops) if drop_columns else 0,
            "total_operations": len(add_ops) + len(cast_ops) + len(rename_ops) + len(drop_ops),
            "safe_casts": len(cast_ops) - len(unsafe_casts),
            "unsafe_casts": len(unsafe_casts),
            "dry_run": dry_run,
            "drop_columns_enabled": drop_columns,
        }

        summary = _build_summary(metrics, dry_run)

        # ── Findings ──────────────────────────────────────────────────────────
        findings: list[str] = []
        if add_ops:
            col_list = ", ".join(f"{a['column']} ({a['type']})" for a in add_ops)
            findings.append(f"{len(add_ops)} new column(s): {col_list}")
        if cast_ops:
            for c in cast_ops:
                safety = "safe (widening)" if c["safe"] else "UNSAFE (requires TRY_CAST)"
                findings.append(
                    f"Type change: {c['column']} ({c['from']} → {c['to']}) — {safety}"
                )
        if rename_ops:
            for r in rename_ops:
                findings.append(
                    f"Rename: '{r['from_column']}' → '{r['to_column']}'"
                )
        if final_removed:
            action_desc = "DROP generated" if drop_columns else "warning only (drop_columns=False)"
            col_list = ", ".join(r["column"] for r in final_removed)
            findings.append(
                f"{len(final_removed)} removed column(s): {col_list} — {action_desc}"
            )

        # ── Risks ─────────────────────────────────────────────────────────────
        risks: list[str] = []
        for c in unsafe_casts:
            risks.append(
                f"TRY_CAST on '{c['column']}' ({c['from']}→{c['to']}) may produce "
                f"NULLs for incompatible values — verify with anchor('microscope', df, '{c['column']}')"
            )
        if rename_ops:
            risks.append(
                "Rename detection is heuristic — verify each rename is correct before applying."
            )
        if not dry_run:
            risks.append("dry_run=False — migration code is marked ready to apply.")

        # ── Full scripts ──────────────────────────────────────────────────────
        ddl_lines: list[str] = []
        pyspark_lines: list[str] = [
            "from pyspark.sql import functions as F",
            "",
            f"# Migration for: {target_table}",
            "# Generated by anchor('schema_migrate')",
            "",
        ]

        # Renames first (before add/drop to avoid conflicts)
        if rename_ops:
            ddl_lines.append("-- Renames")
            pyspark_lines.append("# --- Renames ---")
            for r in rename_ops:
                ddl_lines.append(r["ddl"])
                pyspark_lines.append(f"df = df{r['spark']}")
            ddl_lines.append("")
            pyspark_lines.append("")

        # Type casts
        if cast_ops:
            ddl_lines.append("-- Type changes")
            pyspark_lines.append("# --- Type casts ---")
            for c in cast_ops:
                ddl_lines.append(c["ddl"])
                pyspark_lines.append(f"df = df{c['spark']}")
            ddl_lines.append("")
            pyspark_lines.append("")

        # Add columns
        if add_ops:
            ddl_lines.append("-- Add columns")
            for a in add_ops:
                ddl_lines.append(a["ddl"])
            ddl_lines.append("")

        # Drops
        if drop_ops:
            ddl_lines.append("-- Drop columns" if drop_columns else "-- Removed columns (warning only)")
            pyspark_lines.append("# --- Drops ---" if drop_columns else "# --- Removed (warning only) ---")
            for d in drop_ops:
                ddl_lines.append(d["ddl"])
                if drop_columns:
                    pyspark_lines.append(f"df = df.drop('{d['column']}')")
                else:
                    pyspark_lines.append(f"# df = df.drop('{d['column']}')")
            ddl_lines.append("")
            pyspark_lines.append("")

        full_ddl = "\n".join(ddl_lines).strip()
        full_pyspark = "\n".join(pyspark_lines).strip()

        # ── Suggested next actions ────────────────────────────────────────────
        suggested: list[str] = [
            "Review migration plan — especially rename and type cast operations",
        ]
        if unsafe_casts:
            cols = ", ".join(f"'{c['column']}'" for c in unsafe_casts)
            suggested.append(
                f"Verify unsafe casts: anchor('microscope', df, {cols}) to check for incompatible values"
            )
        suggested.append("Apply migration: run the DDL statements in your SQL editor")
        suggested.append(
            "Re-run schema_diff after migration: anchor('schema_diff', source_df, target_df) to confirm alignment"
        )

        # ── Build context ─────────────────────────────────────────────────────
        ctx = build_base_context(
            kind="schema_migrate",
            subject=target_table,
            summary=summary,
            metrics=metrics,
            findings=findings,
            risks=risks,
            samples={"full_ddl": full_ddl, "full_pyspark": full_pyspark},
            suggested_next_actions=suggested,
            migration_plan={
                "add_columns": add_ops,
                "type_casts": cast_ops,
                "renames": rename_ops,
                "drops": drop_ops,
            },
        )

    except (KeyError, TypeError, AttributeError) as exc:
        import logging as _logging
        _logging.getLogger(__name__).exception(
            "schema_migrate failed while building migration plan"
        )
        ctx = build_base_context(
            kind="schema_migrate",
            subject=target_table,
            summary=f"ERROR: failed to generate migration plan — {exc}",
            metrics={"error_type": type(exc).__name__},
            findings=[],
            risks=[f"{type(exc).__name__}: {exc}"],
            samples={},
            suggested_next_actions=[
                "Inspect schema_diff_ctx structure — ensure all entries are "
                "dicts with the expected keys (column, old_dtype, new_dtype, old_column, new_column).",
                "Re-run anchor('schema_diff', source_df, target_df, output_format='dict') "
                "and pass the output directly.",
            ],
        )
    return finalize_context(ctx, output_format, render_schema_migrate_report)


# ============================================================================
# Markdown renderer
# ============================================================================


def render_schema_migrate_report(ctx: dict) -> str:
    """Render schema_migrate context as markdown."""
    lines: list[str] = []

    lines.extend(render_header_lines(ctx, "Schema Migration"))
    lines.append("")
    lines.extend(render_metrics_lines(ctx))
    lines.append("")

    # Findings
    findings = ctx.get("findings", [])
    if findings:
        lines.extend(render_bullet_section(findings, "Operations"))
        lines.append("")

    # Risks
    risks = ctx.get("risks", [])
    if risks:
        lines.extend(render_bullet_section(risks, "Risks"))
        lines.append("")

    # Migration plan details
    plan = ctx.get("migration_plan", {})

    # Add columns table
    adds = plan.get("add_columns", [])
    if adds:
        lines.append("## Add Columns")
        lines.append("")
        rows = [[a["column"], a["type"], a["default"]] for a in adds]
        lines.extend(render_table(rows, ["Column", "Type", "Default"]))
        lines.append("")

    # Type casts table
    casts = plan.get("type_casts", [])
    if casts:
        lines.append("## Type Casts")
        lines.append("")
        rows = [
            [c["column"], c["from"], c["to"], "✓" if c["safe"] else "⚠ TRY_CAST"]
            for c in casts
        ]
        lines.extend(render_table(rows, ["Column", "From", "To", "Safety"]))
        lines.append("")

    # Renames table
    renames = plan.get("renames", [])
    if renames:
        lines.append("## Renames")
        lines.append("")
        rows = [[r["from_column"], r["to_column"]] for r in renames]
        lines.extend(render_table(rows, ["Old Name", "New Name"]))
        lines.append("")

    # Drops
    drops = plan.get("drops", [])
    if drops:
        lines.append("## Drops")
        lines.append("")
        rows = [[d["column"], d["action"]] for d in drops]
        lines.extend(render_table(rows, ["Column", "Action"]))
        lines.append("")

    # Full DDL
    samples = ctx.get("samples", {})
    if samples.get("full_ddl"):
        lines.append("## Generated DDL")
        lines.append("")
        lines.append("```sql")
        lines.append(samples["full_ddl"])
        lines.append("```")
        lines.append("")

    # Full PySpark
    if samples.get("full_pyspark"):
        lines.append("## Generated PySpark")
        lines.append("")
        lines.append("```python")
        lines.append(samples["full_pyspark"])
        lines.append("```")
        lines.append("")

    # Suggested actions
    suggested = ctx.get("suggested_next_actions", [])
    if suggested:
        lines.extend(render_bullet_section(suggested, "Next Steps"))
        lines.append("")

    return "\n".join(lines)
