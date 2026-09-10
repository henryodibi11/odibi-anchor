"""Schema diff context generator.

This module turns a raw schema comparison into a compact context packet for
humans and LLMs. It is intentionally standalone: no framework, no registry,
no persistence, and no hidden side effects.

Typical usage:
    from odibi_anchor.tables.schema_diff_context import (
        schema_diff_context,
        render_schema_diff_report,
    )

    # From DataFrames
    ctx = schema_diff_context(old_df, new_df)

    # From Unity Catalog table names (Spark only)
    ctx = schema_diff_context(
        "catalog.schema.old_table",
        "catalog.schema.new_table",
        spark=spark,
    )

    # Markdown for LLM prompts
    report = render_schema_diff_report(ctx)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from odibi_anchor._utils.engine_utils import detect_engine
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

Engine = Literal["auto", "pandas", "spark"]
OutputFormat = Literal["dict", "markdown"]


@dataclass(frozen=True)
class _SchemaField:
    """Internal schema field representation for deterministic comparison.

    Attributes:
        column: Column name.
        dtype: String representation of the data type.
        position: Zero-based ordinal position in the schema.
        nullable: Whether the column is nullable (Spark only; None for pandas).
    """

    column: str
    dtype: str
    position: int
    nullable: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly field dictionary."""
        result: dict[str, Any] = {
            "column": self.column,
            "dtype": self.dtype,
            "position": self.position,
        }
        if self.nullable is not None:
            result["nullable"] = self.nullable
        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def schema_diff_context(
    old_df: Any,
    new_df: Any,
    *,
    old_subject: str = "old_df",
    new_subject: str = "new_df",
    subject: str | None = None,
    engine: Engine = "auto",
    include_unchanged: bool = True,
    max_unchanged: int = 50,
    output_format: OutputFormat = "dict",
    spark: Any = None,
) -> dict[str, Any] | str:
    """Generate a compact schema-diff context packet.

    Accepts DataFrames (Pandas or Spark) or Unity Catalog table name
    strings. When passing table names, provide the ``spark`` session.

    Args:
        old_df: Earlier/reference input. Accepts a Pandas DataFrame,
            Spark DataFrame, or a fully qualified table name string
            (e.g., ``"catalog.schema.table"``).
        new_df: Later/current input. Same types as ``old_df``.
        old_subject: Human-readable label for the old/reference dataset.
        new_subject: Human-readable label for the new/current dataset.
        subject: Optional explicit subject override.
        engine: Execution engine: ``"auto"``, ``"pandas"``, or ``"spark"``.
        include_unchanged: If True, include unchanged column details.
        max_unchanged: Maximum unchanged columns in payload.
        output_format: ``"dict"`` or ``"markdown"``.
        spark: SparkSession. Required when passing table name strings.

    Returns:
        Dictionary or markdown string depending on ``output_format``.

    Raises:
        ValueError: If parameters are invalid.
        TypeError: If inputs don't match the selected engine.

    Example:
        >>> import pandas as pd
        >>> old = pd.DataFrame({"id": [1], "amount": [10.0]})
        >>> new = pd.DataFrame({"id": [1], "status": ["new"]})
        >>> ctx = schema_diff_context(old, new)
        >>> ctx["metrics"]["is_breaking_change"]
        True
    """
    if engine not in {"auto", "pandas", "spark"}:
        raise ValueError(
            "engine must be one of: 'auto', 'pandas', 'spark'"
        )
    if max_unchanged < 0:
        raise ValueError("max_unchanged must be >= 0")
    validate_output_format(output_format)

    # Resolve table name strings to DataFrames
    old_df, new_df, engine = _resolve_inputs(
        old_df, new_df, engine, spark
    )

    resolved_engine = _resolve_engine(old_df, new_df, engine)

    if resolved_engine == "pandas":
        ctx = _schema_diff_context_pandas(
            old_df, new_df,
            old_subject=old_subject,
            new_subject=new_subject,
            subject=subject,
            include_unchanged=include_unchanged,
            max_unchanged=max_unchanged,
        )
    elif resolved_engine == "spark":
        ctx = _schema_diff_context_spark(
            old_df, new_df,
            old_subject=old_subject,
            new_subject=new_subject,
            subject=subject,
            include_unchanged=include_unchanged,
            max_unchanged=max_unchanged,
        )
    else:
        raise ValueError(f"Unsupported engine: {resolved_engine}")

    if output_format == "markdown":
        return render_schema_diff_report(ctx)
    return ctx


def render_schema_diff_report(
    ctx: dict[str, Any],
    *,
    show_unchanged: bool = False,
) -> str:
    """Render a schema-diff context dict as structured markdown.

    Designed for LLM prompts and human consumption. Produces a compact,
    token-efficient report with clear sections.

    Args:
        ctx: The dictionary returned by ``schema_diff_context()``.
        show_unchanged: If True, include unchanged columns table.

    Returns:
        A markdown-formatted string.

    Raises:
        ValueError: If ``ctx`` is missing required keys.
    """
    required = {"kind", "subject", "summary", "metrics"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(
            f"Context dict missing required keys: {sorted(missing)}"
        )

    lines: list[str] = []
    metrics = ctx["metrics"]

    lines.append(f"# Schema Diff: {ctx['subject']}")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Metrics table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Engine | {metrics['engine']} |")
    lines.append(
        f"| Old column count | {metrics['old_column_count']} |"
    )
    lines.append(
        f"| New column count | {metrics['new_column_count']} |"
    )
    lines.append(f"| Added | {metrics['added_count']} |")
    lines.append(f"| Removed | {metrics['removed_count']} |")
    lines.append(
        f"| Type changed | {metrics['type_changed_count']} |"
    )
    lines.append(
        f"| Nullable changed | "
        f"{metrics.get('nullable_changed_count', 0)} |"
    )
    lines.append(f"| Unchanged | {metrics['unchanged_count']} |")
    lines.append(f"| Reordered | {metrics['reordered_count']} |")
    lines.append(
        f"| Compatibility | {metrics['compatibility']} |"
    )
    lines.append(
        f"| Breaking change | {metrics['is_breaking_change']} |"
    )
    lines.append("")

    # Write safety
    write_safety = ctx.get("write_safety")
    if write_safety:
        lines.append("## Write Safety")
        lines.append("")
        icon = "\u2705" if write_safety["can_write"] else "\u274c"
        lines.append(
            f"{icon} **{write_safety['recommendation']}**"
        )
        if write_safety["blockers"]:
            lines.append("")
            lines.append("**Blockers:**")
            for b in write_safety["blockers"]:
                lines.append(f"* \U0001f6d1 {b}")
        if write_safety["warnings"]:
            lines.append("")
            lines.append("**Warnings:**")
            for w in write_safety["warnings"]:
                lines.append(f"* \u26a0\ufe0f {w}")
        lines.append("")

    # Likely renames
    likely_renames = ctx.get("likely_renames", [])
    if likely_renames:
        lines.append("## Likely Renames")
        lines.append("")
        lines.append(
            "| Old Column | New Column | Confidence | Reason |"
        )
        lines.append("| --- | --- | --- | --- |")
        for r in likely_renames:
            lines.append(
                f"| {r['old_column']} | {r['new_column']} "
                f"| {r['confidence']} | {r['reason']} |"
            )
        lines.append("")

    # Added columns
    added = ctx.get("added", [])
    if added:
        lines.append("## Added Columns")
        lines.append("")
        lines.append("| Column | Dtype | Position |")
        lines.append("| --- | --- | --- |")
        for field in added:
            nullable_str = ""
            if field.get("nullable") is not None:
                nullable_str = (
                    " (nullable)" if field["nullable"]
                    else " (not null)"
                )
            lines.append(
                f"| {field['column']} "
                f"| {field['dtype']}{nullable_str} "
                f"| {field['position']} |"
            )
        lines.append("")

    # Removed columns
    removed = ctx.get("removed", [])
    if removed:
        lines.append("## Removed Columns")
        lines.append("")
        lines.append("| Column | Dtype | Fix |")
        lines.append("| --- | --- | --- |")
        for field in removed:
            fix = field.get("fix_expr", "")
            lines.append(
                f"| {field['column']} "
                f"| {field['dtype']} "
                f"| `{fix}` |"
            )
        lines.append("")

    # Type changes
    type_changed = ctx.get("type_changed", [])
    if type_changed:
        lines.append("## Type Changes")
        lines.append("")
        lines.append(
            "| Column | Old | New | Risk | Fix |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for c in type_changed:
            fix = c.get("fix_expr", "")
            lines.append(
                f"| {c['column']} "
                f"| {c['old_dtype']} "
                f"| {c['new_dtype']} "
                f"| {c['risk_level']} "
                f"| `{fix}` |"
            )
        lines.append("")

    # Nullable changes
    nullable_changed = ctx.get("nullable_changed", [])
    if nullable_changed:
        lines.append("## Nullable Changes")
        lines.append("")
        lines.append(
            "| Column | Old | New | Direction | Risk |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for nc in nullable_changed:
            lines.append(
                f"| {nc['column']} "
                f"| {nc['old_nullable']} "
                f"| {nc['new_nullable']} "
                f"| {nc['direction']} "
                f"| {nc['risk_level']} |"
            )
        lines.append("")

    # Reordered
    reordered = ctx.get("reordered", [])
    if reordered:
        lines.append("## Reordered Columns")
        lines.append("")
        lines.append("| Column | Old Position | New Position |")
        lines.append("| --- | --- | --- |")
        for change in reordered:
            lines.append(
                f"| {change['column']} "
                f"| {change['old_position']} "
                f"| {change['new_position']} |"
            )
        lines.append("")

    # Unchanged (optional)
    unchanged = ctx.get("unchanged", [])
    if show_unchanged and unchanged:
        lines.append("## Unchanged Columns")
        lines.append("")
        trunc = metrics.get("unchanged_truncated_count", 0)
        if trunc > 0:
            lines.append(
                f"*Showing {len(unchanged)} of "
                f"{metrics['unchanged_count']} "
                f"({trunc} truncated)*"
            )
            lines.append("")
        lines.append("| Column | Dtype | Position |")
        lines.append("| --- | --- | --- |")
        for field in unchanged:
            lines.append(
                f"| {field['column']} "
                f"| {field['dtype']} "
                f"| {field.get('old_position', '')} |"
            )
        lines.append("")

    # Risks
    risks = ctx.get("risks", [])
    if risks:
        lines.append("## Risks")
        lines.append("")
        for risk in risks:
            icon = (
                "\U0001f534" if risk["severity"] == "critical"
                else "\U0001f7e1"
            )
            lines.append(
                f"* {icon} **{risk['risk']}**: "
                f"{risk['message']}"
            )
        lines.append("")

    # Suggested next actions
    actions = ctx.get("suggested_next_actions", [])
    if actions:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for action in actions:
            lines.append(f"* {action}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input resolution (table names → DataFrames)
# ---------------------------------------------------------------------------


def _resolve_inputs(
    old_df: Any, new_df: Any, engine: Engine, spark: Any
) -> tuple[Any, Any, Engine]:
    """Resolve string table names to Spark DataFrames.

    When a string is passed, uses ``spark.table(name)`` which reads
    schema metadata without triggering any Spark actions.

    Args:
        old_df: DataFrame or table name string.
        new_df: DataFrame or table name string.
        engine: Engine setting from caller.
        spark: SparkSession (required when strings are passed).

    Returns:
        Tuple of (resolved_old, resolved_new, effective_engine).

    Raises:
        TypeError: If strings passed without a spark session.
    """
    old_is_str = isinstance(old_df, str)
    new_is_str = isinstance(new_df, str)

    if not old_is_str and not new_is_str:
        return old_df, new_df, engine

    if spark is None:
        # Try to get spark from globals (Databricks auto-provides it)
        import builtins
        spark = getattr(builtins, "spark", None)
        if spark is None:
            raise TypeError(
                "spark session required when passing table name "
                "strings. Pass spark=spark explicitly."
            )

    if old_is_str:
        old_df = spark.table(old_df)
    if new_is_str:
        new_df = spark.table(new_df)

    # Force Spark engine since we resolved from UC tables
    return old_df, new_df, "spark"


# ---------------------------------------------------------------------------
# Engine-specific implementations
# ---------------------------------------------------------------------------


def _schema_diff_context_pandas(
    old_df: Any,
    new_df: Any,
    *,
    old_subject: str,
    new_subject: str,
    subject: str | None,
    include_unchanged: bool,
    max_unchanged: int,
) -> dict[str, Any]:
    """Pandas implementation for schema_diff_context."""
    import pandas as pd

    if not isinstance(old_df, pd.DataFrame) or not isinstance(
        new_df, pd.DataFrame
    ):
        raise TypeError(
            "engine='pandas' requires both inputs to be "
            "pandas.DataFrame"
        )

    old_fields = _extract_schema_pandas(old_df)
    new_fields = _extract_schema_pandas(new_df)
    duplicate_findings = _duplicate_column_findings(old_df, new_df)

    return _build_context(
        old_fields=old_fields,
        new_fields=new_fields,
        old_subject=old_subject,
        new_subject=new_subject,
        subject=subject,
        engine="pandas",
        include_unchanged=include_unchanged,
        max_unchanged=max_unchanged,
        extra_findings=duplicate_findings,
    )


def _schema_diff_context_spark(
    old_df: Any,
    new_df: Any,
    *,
    old_subject: str,
    new_subject: str,
    subject: str | None,
    include_unchanged: bool,
    max_unchanged: int,
) -> dict[str, Any]:
    """Spark implementation using df.schema.fields (no actions)."""
    if detect_engine(old_df) != "spark" or detect_engine(
        new_df
    ) != "spark":
        raise TypeError(
            "engine='spark' requires both inputs to be "
            "pyspark.sql.DataFrame"
        )

    old_fields = _extract_schema_spark(old_df)
    new_fields = _extract_schema_spark(new_df)

    return _build_context(
        old_fields=old_fields,
        new_fields=new_fields,
        old_subject=old_subject,
        new_subject=new_subject,
        subject=subject,
        engine="spark",
        include_unchanged=include_unchanged,
        max_unchanged=max_unchanged,
        extra_findings=[],
    )


# ---------------------------------------------------------------------------
# Context builder (core logic)
# ---------------------------------------------------------------------------


def _build_context(
    *,
    old_fields: list[_SchemaField],
    new_fields: list[_SchemaField],
    old_subject: str,
    new_subject: str,
    subject: str | None,
    engine: str,
    include_unchanged: bool,
    max_unchanged: int,
    extra_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the final context packet from normalized schema fields."""
    old_by_name = {f.column: f for f in old_fields}
    new_by_name = {f.column: f for f in new_fields}

    old_columns = [f.column for f in old_fields]
    new_columns = [f.column for f in new_fields]
    old_set = set(old_columns)
    new_set = set(new_columns)

    added = [
        new_by_name[c].as_dict()
        for c in new_columns if c not in old_set
    ]
    removed = [
        old_by_name[c].as_dict()
        for c in old_columns if c not in new_set
    ]

    # Add fix_expr for removed columns
    for field in removed:
        field["fix_expr"] = f".drop('{field['column']}')"

    type_changed: list[dict[str, Any]] = []
    nullable_changed: list[dict[str, Any]] = []
    unchanged_fields: list[dict[str, Any]] = []
    reordered: list[dict[str, Any]] = []

    for column in old_columns:
        if column not in new_by_name:
            continue
        old_f = old_by_name[column]
        new_f = new_by_name[column]

        if old_f.dtype != new_f.dtype:
            old_family = _type_family(old_f.dtype)
            new_family = _type_family(new_f.dtype)
            risk = _type_change_risk(old_family, new_family)
            fix_expr = _build_fix_expr(
                column, new_f.dtype, engine
            )
            type_changed.append({
                "column": column,
                "old_dtype": old_f.dtype,
                "new_dtype": new_f.dtype,
                "old_type_family": old_family,
                "new_type_family": new_family,
                "old_position": old_f.position,
                "new_position": new_f.position,
                "risk_level": risk,
                "fix_expr": fix_expr,
                "fix_safe": risk == "medium",
            })
        else:
            unchanged_fields.append({
                "column": column,
                "dtype": old_f.dtype,
                "old_position": old_f.position,
                "new_position": new_f.position,
            })

        # Nullable change detection (Spark only)
        if (old_f.nullable is not None
                and new_f.nullable is not None
                and old_f.nullable != new_f.nullable):
            direction = (
                "tightened" if not new_f.nullable else "relaxed"
            )
            nullable_changed.append({
                "column": column,
                "old_nullable": old_f.nullable,
                "new_nullable": new_f.nullable,
                "direction": direction,
                "risk_level": (
                    "high" if direction == "tightened"
                    else "info"
                ),
            })

        if old_f.position != new_f.position:
            reordered.append({
                "column": column,
                "dtype": new_f.dtype,
                "old_position": old_f.position,
                "new_position": new_f.position,
            })

    # Rename detection
    likely_renames = _detect_likely_renames(
        added, removed, old_by_name, new_by_name
    )

    unchanged = (
        unchanged_fields[:max_unchanged]
        if include_unchanged else []
    )
    unchanged_truncated_count = (
        max(len(unchanged_fields) - len(unchanged), 0)
        if include_unchanged
        else len(unchanged_fields)
    )

    findings = _build_findings(
        added, removed, type_changed, reordered,
        nullable_changed
    )
    findings.extend(extra_findings)
    risks = _build_risks(
        removed, type_changed, reordered,
        nullable_changed, extra_findings
    )
    suggested = _build_suggested_next_actions(
        added, removed, type_changed, reordered,
        nullable_changed, likely_renames, extra_findings
    )

    compatibility = _compatibility_status(
        added, removed, type_changed, nullable_changed
    )
    has_breaking_nullable = any(
        nc["direction"] == "tightened" for nc in nullable_changed
    )
    is_breaking = bool(
        removed or type_changed or has_breaking_nullable
    )

    metrics = {
        "engine": engine,
        "old_column_count": len(old_fields),
        "new_column_count": len(new_fields),
        "added_count": len(added),
        "removed_count": len(removed),
        "type_changed_count": len(type_changed),
        "nullable_changed_count": len(nullable_changed),
        "unchanged_count": len(unchanged_fields),
        "unchanged_returned_count": len(unchanged),
        "unchanged_truncated_count": unchanged_truncated_count,
        "reordered_count": len(reordered),
        "net_column_delta": len(new_fields) - len(old_fields),
        "has_changes": bool(
            added or removed or type_changed or reordered
            or nullable_changed or extra_findings
        ),
        "is_breaking_change": is_breaking,
        "compatibility": compatibility,
    }

    # Write safety assessment
    write_safety = _assess_write_safety(
        added, removed, type_changed, nullable_changed,
        new_by_name, engine
    )

    return {
        "kind": "schema_diff_context",
        "subject": subject or f"{old_subject} -> {new_subject}",
        "old_subject": old_subject,
        "new_subject": new_subject,
        "summary": _summary(metrics),
        "metrics": metrics,
        "added": added,
        "removed": removed,
        "type_changed": type_changed,
        "nullable_changed": nullable_changed,
        "unchanged": unchanged,
        "reordered": reordered,
        "likely_renames": likely_renames,
        "findings": findings,
        "risks": risks,
        "write_safety": write_safety,
        "samples": {},
        "suggested_next_actions": suggested,
    }


# ---------------------------------------------------------------------------
# Schema extraction
# ---------------------------------------------------------------------------


def _extract_schema_pandas(df: Any) -> list[_SchemaField]:
    """Extract a deterministic schema from a Pandas DataFrame."""
    return [
        _SchemaField(
            column=str(col), dtype=str(dtype), position=pos,
        )
        for pos, (col, dtype) in enumerate(df.dtypes.items())
    ]


def _extract_schema_spark(df: Any) -> list[_SchemaField]:
    """Extract schema from Spark DataFrame (no actions triggered)."""
    return [
        _SchemaField(
            column=str(f.name),
            dtype=str(f.dataType),
            position=pos,
            nullable=bool(f.nullable),
        )
        for pos, f in enumerate(df.schema.fields)
    ]


# ---------------------------------------------------------------------------
# Engine resolution
# ---------------------------------------------------------------------------


def _resolve_engine(old_df: Any, new_df: Any, engine: Engine) -> str:
    """Resolve and validate the execution engine."""
    if engine in ("pandas", "spark"):
        return engine

    old_eng = detect_engine(old_df)
    new_eng = detect_engine(new_df)

    if old_eng == new_eng and old_eng != "unknown":
        return old_eng
    if old_eng != new_eng:
        raise TypeError(
            "old_df and new_df must use the same DataFrame engine"
        )
    raise TypeError(
        "Could not infer engine. Pass pandas or Spark DataFrames, "
        "or set engine explicitly."
    )


# ---------------------------------------------------------------------------
# Rename detection
# ---------------------------------------------------------------------------


def _normalize_column_name(name: str) -> str:
    """Normalize a column name for rename comparison.

    Converts to lowercase, replaces camelCase boundaries with
    underscores, and strips non-alphanumeric characters.
    """
    # camelCase → snake_case
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    # Remove non-alphanumeric (except underscore)
    s = re.sub(r"[^a-z0-9_]", "", s.lower())
    # Collapse multiple underscores
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _detect_likely_renames(
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    old_by_name: dict[str, _SchemaField],
    new_by_name: dict[str, _SchemaField],
) -> list[dict[str, Any]]:
    """Detect likely column renames using simple heuristics.

    Two-pass matching:
    1. Case-insensitive exact match (e.g., CustomerID → customerid)
    2. Normalized match (camelCase ↔ snake_case)

    Only flags obvious cases to avoid false positives. Does NOT
    reclassify the added/removed lists.
    """
    if not added or not removed:
        return []

    renames: list[dict[str, Any]] = []
    added_names = [a["column"] for a in added]
    removed_names = [r["column"] for r in removed]
    matched_added: set[str] = set()
    matched_removed: set[str] = set()

    # Pass 1: case-insensitive exact match
    for old_name in removed_names:
        if old_name in matched_removed:
            continue
        for new_name in added_names:
            if new_name in matched_added:
                continue
            if old_name.lower() == new_name.lower():
                old_field = old_by_name.get(old_name)
                new_field = new_by_name.get(new_name)
                dtype_match = (
                    old_field is not None
                    and new_field is not None
                    and old_field.dtype == new_field.dtype
                )
                renames.append({
                    "old_column": old_name,
                    "new_column": new_name,
                    "confidence": (
                        "high" if dtype_match else "medium"
                    ),
                    "reason": "case_change",
                    "dtype_match": dtype_match,
                })
                matched_added.add(new_name)
                matched_removed.add(old_name)
                break

    # Pass 2: normalized match (camelCase ↔ snake_case)
    for old_name in removed_names:
        if old_name in matched_removed:
            continue
        old_norm = _normalize_column_name(old_name)
        if not old_norm:
            continue
        for new_name in added_names:
            if new_name in matched_added:
                continue
            new_norm = _normalize_column_name(new_name)
            if old_norm == new_norm:
                old_field = old_by_name.get(old_name)
                new_field = new_by_name.get(new_name)
                dtype_match = (
                    old_field is not None
                    and new_field is not None
                    and old_field.dtype == new_field.dtype
                )
                renames.append({
                    "old_column": old_name,
                    "new_column": new_name,
                    "confidence": (
                        "high" if dtype_match else "medium"
                    ),
                    "reason": "naming_convention_change",
                    "dtype_match": dtype_match,
                })
                matched_added.add(new_name)
                matched_removed.add(old_name)
                break

    return renames


# ---------------------------------------------------------------------------
# Fix code generation
# ---------------------------------------------------------------------------


def _build_fix_expr(
    column: str, new_dtype: str, engine: str
) -> str:
    """Generate a fix expression for a type change.

    Returns a Spark-style .withColumn cast expression.
    """
    # Simplify Spark type string for cast
    cast_type = _dtype_to_cast_string(new_dtype)
    return (
        f".withColumn('{column}', "
        f"col('{column}').cast('{cast_type}'))"
    )


def _dtype_to_cast_string(dtype: str) -> str:
    """Convert a dtype string to a Spark SQL cast-compatible string.

    Maps Spark type objects (e.g., 'IntegerType()') to SQL type
    strings (e.g., 'int'), and passes pandas dtypes through.
    """
    d = dtype.lower()
    if "integertype" in d or d == "int64" or d == "int32":
        return "int"
    if "longtype" in d:
        return "bigint"
    if "shorttype" in d:
        return "smallint"
    if "bytetype" in d:
        return "tinyint"
    if "doubletype" in d or d == "float64":
        return "double"
    if "floattype" in d or d == "float32":
        return "float"
    if "stringtype" in d or d == "object":
        return "string"
    if "booleantype" in d or d == "bool":
        return "boolean"
    if "timestamptype" in d:
        return "timestamp"
    if "datetype" in d:
        return "date"
    if "binarytype" in d:
        return "binary"
    # DecimalType(10, 2) → decimal(10,2)
    decimal_match = re.search(
        r"decimal.*?(\d+).*?(\d+)", dtype, re.IGNORECASE
    )
    if decimal_match:
        p, s = decimal_match.group(1), decimal_match.group(2)
        return f"decimal({p},{s})"
    # Fallback: strip "Type()" suffix
    cleaned = re.sub(r"type\(\)", "", dtype, flags=re.IGNORECASE)
    return cleaned.strip().lower() or dtype


# ---------------------------------------------------------------------------
# Write safety assessment
# ---------------------------------------------------------------------------


def _assess_write_safety(
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    type_changed: list[dict[str, Any]],
    nullable_changed: list[dict[str, Any]],
    new_by_name: dict[str, _SchemaField],
    engine: str,
) -> dict[str, Any]:
    """Assess whether source can safely write to target.

    Interprets the diff as: old=source (what I have),
    new=target (where I'm writing).

    Returns:
        Dict with can_write, blockers, warnings, recommendation.
    """
    blockers: list[str] = []
    warnings: list[str] = []

    # Columns in target but not in source — if NOT NULL, blocker
    for field in added:
        if field.get("nullable") is False:
            blockers.append(
                f"Target column '{field['column']}' is NOT NULL "
                f"but missing from source."
            )
        elif field.get("nullable") is True:
            warnings.append(
                f"Target has new column '{field['column']}' "
                f"(nullable). Schema evolution or default needed."
            )

    # Columns in source but not in target
    if removed:
        col_names = [r["column"] for r in removed]
        warnings.append(
            f"Source has columns not in target: {col_names}. "
            f"Use mergeSchema=true or drop before write."
        )

    # Type changes
    for tc in type_changed:
        if tc["risk_level"] == "high":
            blockers.append(
                f"Column '{tc['column']}' type change "
                f"({tc['old_dtype']} -> {tc['new_dtype']}) "
                f"crosses type families. Cast required."
            )
        else:
            warnings.append(
                f"Column '{tc['column']}' precision change "
                f"({tc['old_dtype']} -> {tc['new_dtype']}). "
                f"May lose precision."
            )

    # Nullable tightening
    for nc in nullable_changed:
        if nc["direction"] == "tightened":
            blockers.append(
                f"Column '{nc['column']}' is NOT NULL in target "
                f"but nullable in source. NULLs will fail."
            )

    can_write = len(blockers) == 0
    if can_write and not warnings:
        recommendation = "Safe to write. No schema conflicts."
    elif can_write:
        recommendation = (
            "Write is possible but review warnings first."
        )
    else:
        recommendation = (
            "BLOCKED. Fix blockers before writing."
        )

    return {
        "can_write": can_write,
        "blockers": blockers,
        "warnings": warnings,
        "recommendation": recommendation,
    }


# ---------------------------------------------------------------------------
# Utility / classification functions
# ---------------------------------------------------------------------------


def _duplicate_column_findings(
    old_df: Any, new_df: Any
) -> list[dict[str, Any]]:
    """Return warning findings for duplicate Pandas column labels."""
    findings: list[dict[str, Any]] = []
    for label, df in (("old", old_df), ("new", new_df)):
        columns = [str(c) for c in df.columns]
        duplicates = sorted(
            {c for c in columns if columns.count(c) > 1}
        )
        if duplicates:
            findings.append({
                "severity": "warning",
                "check_type": "duplicate_columns",
                "column": None,
                "message": (
                    f"{label}_df has duplicate column labels; "
                    "schema comparison may be ambiguous."
                ),
                "details": {
                    "dataset": label, "columns": duplicates
                },
            })
    return findings


def _type_family(dtype: str) -> str:
    """Normalize dtype strings into broad type families.

    Container types (array, struct, map) checked first via startswith
    to avoid false matches from inner type names.
    """
    value = dtype.lower()
    # Container types first
    if value.startswith("array"):
        return "array"
    if value.startswith("struct"):
        return "struct"
    if value.startswith("map"):
        return "map"
    # Scalar types
    if any(t in value for t in (
        "int", "long", "short", "byte",
        "float", "double", "decimal",
    )):
        return "numeric"
    if any(t in value for t in (
        "str", "string", "object", "varchar", "char"
    )):
        return "string"
    if "bool" in value:
        return "boolean"
    if any(t in value for t in (
        "datetime", "timestamp", "date"
    )):
        return "datetime"
    if "category" in value:
        return "category"
    if any(t in value for t in ("binary", "bytes")):
        return "binary"
    return "unknown"


def _type_change_risk(old_family: str, new_family: str) -> str:
    """Classify risk: high if families differ, medium otherwise."""
    if old_family == new_family:
        return "medium"
    if "unknown" in {old_family, new_family}:
        return "medium"
    return "high"


def _compatibility_status(
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    type_changed: list[dict[str, Any]],
    nullable_changed: list[dict[str, Any]],
) -> str:
    """Return compatibility: breaking, additive, or unchanged."""
    has_breaking_nullable = any(
        nc["direction"] == "tightened" for nc in nullable_changed
    )
    if removed or type_changed or has_breaking_nullable:
        return "breaking"
    if added or nullable_changed:
        return "additive"
    return "unchanged"


def _summary(metrics: dict[str, Any]) -> str:
    """Build a one-line human-readable summary with conditional framing."""
    if not metrics["has_changes"]:
        return "Schemas are identical — no migration needed."

    added = metrics["added_count"]
    removed = metrics["removed_count"]
    type_changed = metrics["type_changed_count"]

    has_added = added > 0
    has_removed = removed > 0
    has_type_changed = type_changed > 0

    # Single-category changes get specific, actionable summaries
    if has_added and not has_removed and not has_type_changed:
        return (
            f"Additive schema change — {added} new column(s). "
            f"Safe to evolve without breaking downstream."
        )
    if has_removed and not has_added and not has_type_changed:
        return (
            f"Breaking schema change — {removed} column(s) removed. "
            f"Downstream consumers likely depend on these columns."
        )
    if has_type_changed and not has_added and not has_removed:
        return (
            f"Type migration needed — {type_changed} column(s) "
            f"changed types. Review for data loss risk."
        )

    # Mixed changes
    parts: list[str] = []
    if has_added:
        parts.append(f"{added} added")
    if has_removed:
        parts.append(f"{removed} removed")
    if has_type_changed:
        parts.append(f"{type_changed} type changes")
    if metrics["nullable_changed_count"]:
        parts.append(
            f"{metrics['nullable_changed_count']} nullable changed"
        )
    if metrics["reordered_count"]:
        parts.append(f"{metrics['reordered_count']} reordered")
    return (
        f"Mixed schema change: {', '.join(parts)}. "
        f"Compatibility: {metrics['compatibility']}."
    )


# ---------------------------------------------------------------------------
# Findings, risks, and suggested actions
# ---------------------------------------------------------------------------


def _build_findings(
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    type_changed: list[dict[str, Any]],
    reordered: list[dict[str, Any]],
    nullable_changed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build structured findings from schema changes."""
    findings: list[dict[str, Any]] = []
    for field in added:
        nullable_note = ""
        if field.get("nullable") is True:
            nullable_note = (
                " New nullable column is safe — existing readers "
                "will get nulls."
            )
        elif field.get("nullable") is False:
            nullable_note = (
                " Column is NOT NULL — writers must supply a value."
            )
        findings.append({
            "severity": "info",
            "check_type": "column_added",
            "column": field["column"],
            "message": (
                f"Column '{field['column']}' was added "
                f"with dtype {field['dtype']}.{nullable_note}"
            ),
            "details": field,
        })
    for field in removed:
        findings.append({
            "severity": "critical",
            "check_type": "column_removed",
            "column": field["column"],
            "message": (
                f"Column '{field['column']}' was removed. "
                f"If downstream queries or notebooks reference "
                f"'{field['column']}', they will fail after "
                f"this change."
            ),
            "details": field,
        })
    for c in type_changed:
        severity = (
            "critical" if c["risk_level"] == "high" else "warning"
        )
        findings.append({
            "severity": severity,
            "check_type": "type_changed",
            "column": c["column"],
            "message": (
                f"Column '{c['column']}' type changed "
                f"from {c['old_dtype']} to {c['new_dtype']}. "
                f"If this narrows precision, existing data "
                f"likely needs casting to avoid data loss."
            ),
            "details": c,
        })
    for nc in nullable_changed:
        severity = (
            "critical" if nc["direction"] == "tightened"
            else "info"
        )
        direction_note = (
            "Writes with NULL values will likely fail at runtime."
            if nc["direction"] == "tightened"
            else "Relaxed — existing readers are unaffected."
        )
        findings.append({
            "severity": severity,
            "check_type": "nullable_changed",
            "column": nc["column"],
            "message": (
                f"Column '{nc['column']}' nullable changed "
                f"from {nc['old_nullable']} to "
                f"{nc['new_nullable']} ({nc['direction']}). "
                f"{direction_note}"
            ),
            "details": nc,
        })
    for c in reordered:
        findings.append({
            "severity": "warning",
            "check_type": "column_reordered",
            "column": c["column"],
            "message": (
                f"Column '{c['column']}' moved from "
                f"position {c['old_position']} "
                f"to {c['new_position']}. If position-based "
                f"operations (e.g. union-by-position) are used, "
                f"this suggests a potential mismatch."
            ),
            "details": c,
        })
    return findings


def _build_risks(
    removed: list[dict[str, Any]],
    type_changed: list[dict[str, Any]],
    reordered: list[dict[str, Any]],
    nullable_changed: list[dict[str, Any]],
    extra_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build concise risk objects."""
    risks: list[dict[str, Any]] = []
    if removed:
        removed_cols = [f["column"] for f in removed]
        risks.append({
            "severity": "critical",
            "risk": "removed_columns",
            "message": (
                f"If downstream queries or notebooks reference "
                f"{removed_cols}, they will fail after this "
                f"change. Check consumers before applying."
            ),
            "columns": removed_cols,
        })
    if type_changed:
        high = [c["column"] for c in type_changed
                if c["risk_level"] == "high"]
        medium = [c["column"] for c in type_changed
                  if c["risk_level"] != "high"]
        tc_messages: list[str] = []
        for c in type_changed:
            tc_messages.append(
                f"If '{c['column']}' changed from "
                f"{c['old_dtype']} to {c['new_dtype']}, "
                f"existing data may need casting. Widening "
                f"(e.g. int→long) is safe; narrowing "
                f"(e.g. double→int) risks data loss."
            )
        risks.append({
            "severity": "critical" if high else "warning",
            "risk": "type_changed_columns",
            "message": " ".join(tc_messages),
            "high_risk_columns": high,
            "medium_risk_columns": medium,
        })
    tightened = [nc for nc in nullable_changed
                 if nc["direction"] == "tightened"]
    if tightened:
        tightened_cols = [nc["column"] for nc in tightened]
        risks.append({
            "severity": "critical",
            "risk": "nullable_tightened",
            "message": (
                f"If {tightened_cols} became NOT NULL, then "
                f"writes with NULL values will fail at runtime. "
                f"Filter or fill NULLs before writing."
            ),
            "columns": tightened_cols,
        })
    # Added nullable columns — informational, not a risk
    added_nullable = [
        f.get("column") for f in extra_findings
        if f.get("check_type") == "column_added"
        and f.get("details", {}).get("nullable") is True
    ]
    if not added_nullable:
        # Check if any added columns in the reordered list are nullable
        # (this section is only reached via _build_risks, so we note
        # that new nullable columns are safe as a low-severity item)
        pass
    if reordered:
        risks.append({
            "severity": "warning",
            "risk": "column_order_changed",
            "message": (
                "If position-based operations (e.g. "
                "union-by-position) are used, column order "
                "changes suggest a potential mismatch. "
                "Use name-based column selection instead."
            ),
            "columns": [c["column"] for c in reordered],
        })
    dup = [f for f in extra_findings
           if f.get("check_type") == "duplicate_columns"]
    if dup:
        risks.append({
            "severity": "warning",
            "risk": "duplicate_column_labels",
            "message": (
                "Duplicate column labels make comparison "
                "ambiguous. Resolve before writes."
            ),
            "details": [f["details"] for f in dup],
        })
    return risks


def _build_suggested_next_actions(
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    type_changed: list[dict[str, Any]],
    reordered: list[dict[str, Any]],
    nullable_changed: list[dict[str, Any]],
    likely_renames: list[dict[str, Any]],
    extra_findings: list[dict[str, Any]],
) -> list[str]:
    """Build practical next actions from the diff."""
    actions: list[str] = []
    if not any((
        added, removed, type_changed, reordered,
        nullable_changed, extra_findings
    )):
        return ["Schemas are identical — no migration needed."]

    removed_cols = [f["column"] for f in removed]

    if added and not removed and not type_changed:
        actions.append(
            "No migration needed — additive change is safe. "
            "Consider updating documentation/contracts."
        )
    if added:
        actions.append(
            "If columns added: Run anchor(\"validate\", df, "
            "rules=[...]) on new columns."
        )
    if removed:
        actions.append(
            f"Run anchor('impact', target='downstream_table') to "
            f"check which consumers use {removed_cols}."
        )
    if type_changed:
        actions.append(
            "Run anchor('evolve', df, target_schema=new_df, "
            "subject='table') to auto-generate migration code."
        )
    tightened = [nc for nc in nullable_changed
                 if nc["direction"] == "tightened"]
    if tightened:
        cols = [nc["column"] for nc in tightened]
        actions.append(
            f"Columns {cols} became NOT NULL. Filter or "
            f"fill NULLs before writing."
        )
    if likely_renames:
        pairs = [
            f"{r['old_column']}->{r['new_column']}"
            for r in likely_renames
        ]
        actions.append(
            f"Likely renames detected: {pairs}. Confirm "
            f"with .withColumnRenamed() instead of "
            f"drop+add."
        )
    if reordered:
        actions.append(
            "Use name-based column selection; avoid "
            "position-dependent operations."
        )
    if any(f.get("check_type") == "duplicate_columns"
           for f in extra_findings):
        actions.append(
            "Rename or remove duplicate column labels "
            "before relying on this comparison."
        )
    actions.append(
        "Paste this context into Genie/LLM debugging "
        "prompts instead of raw schemas."
    )

    # anchor() workflow hints for agent autonomy
    if removed or type_changed:
        actions.append(
            "MUST: Run anchor('impact', target='affected_file.py') to find "
            "downstream consumers that need updating."
        )
    if type_changed or tightened:
        actions.append(
            "MUST: Run anchor('quality', df, subject='...', keys=[...]) after "
            "applying schema fixes to verify write-safety."
        )
    # ── Graph wiring (audit fix) ──

    actions.append("MUST: Run anchor(\"contract\", df, candidate_key_columns=[...]) to document new schema.")

    actions.append("MUST: Run anchor(\"quality\", df, keys=[...]) before writing with new schema.")

    actions.append("SKILL: Load skills/data-reconciliation/SKILL.md for structured schema comparison.")

    return actions
