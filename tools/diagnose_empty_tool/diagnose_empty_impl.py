"""Empty output diagnostic tool implementation.

Entry point: diagnose_empty_context(result_df, upstreams={...}, keys=[...], ...)
Diagnoses why a DataFrame has zero or unexpectedly few rows by analyzing
upstream DataFrames, key overlap, filter boundaries, null columns, and type mismatches.
Returns a standard Anchor contract with findings, risks, and suggested_next_actions.
"""
from __future__ import annotations

from typing import Any

from odibi_anchor._utils.contract import (
    build_base_context,
    finalize_context,
    guard_dataframe_type,
    validate_output_format,
)


# ============================================================================
# Engine detection (inlined — tools are standalone, no src/ imports)
# ============================================================================


def _detect_engine(df: Any) -> str:
    """Detect Pandas vs Spark safely. Returns 'pandas' | 'spark' | 'unknown'."""
    try:
        import pandas as pd

        if isinstance(df, pd.DataFrame):
            return "pandas"
    except Exception:
        pass
    try:
        from pyspark.sql import DataFrame as SparkDF

        if isinstance(df, SparkDF):
            return "spark"
    except Exception:
        pass
    mod = getattr(type(df), "__module__", "") or ""
    if "pyspark.sql" in mod:
        return "spark"
    if "pandas" in mod:
        return "pandas"
    return "unknown"


# ============================================================================
# Row count helpers
# ============================================================================


def _get_count(df: Any, engine: str) -> int:
    """Get row count for either engine."""
    if engine == "pandas":
        return len(df)
    return df.count()


def _get_columns(df: Any, engine: str) -> list[str]:
    """Get column names for either engine."""
    if engine == "pandas":
        return list(df.columns)
    return df.columns


def _get_dtype(df: Any, column: str, engine: str) -> str:
    """Get dtype string for a column."""
    if engine == "pandas":
        return str(df[column].dtype)
    for field in df.schema.fields:
        if field.name == column:
            return str(field.dataType)
    return "unknown"


def _type_family(dtype: str) -> str:
    """Map a dtype string to a coarse join-compatibility family.

    Join keys only fail to overlap when their *families* differ (e.g. string
    '123' vs int 123). Different widths within a family — float64 vs int64,
    long vs double — coerce automatically in both pandas and Spark joins, so
    treating them as a "type mismatch" is a false positive (the same class of
    bug fixed in pre_join's key normalization).
    """
    d = dtype.lower()
    if any(t in d for t in (
        "int", "float", "double", "decimal", "long", "short", "byte", "number"
    )):
        return "numeric"
    if "bool" in d:
        return "boolean"
    if any(t in d for t in ("date", "time", "timestamp")):
        return "datetime"
    if any(t in d for t in ("str", "object", "char", "text", "utf")):
        return "string"
    return d  # unknown — compare raw


# ============================================================================
# Diagnostic checks
# ============================================================================


def _build_row_count_funnel(
    result_df: Any,
    upstreams: dict[str, Any],
    result_engine: str,
) -> dict[str, dict]:
    """Stage-by-stage row count funnel across all upstreams + result."""
    funnel: dict[str, dict] = {}
    for name, upstream_df in upstreams.items():
        eng = _detect_engine(upstream_df)
        count = _get_count(upstream_df, eng)
        cols = _get_columns(upstream_df, eng)
        funnel[name] = {"count": count, "columns": len(cols), "engine": eng}
    result_count = _get_count(result_df, result_engine)
    result_cols = _get_columns(result_df, result_engine)
    funnel["__result__"] = {
        "count": result_count,
        "columns": len(result_cols),
        "engine": result_engine,
    }
    return funnel


def _key_overlap_analysis(
    upstreams: dict[str, Any],
    keys: list[str],
    sample_limit: int,
) -> list[dict]:
    """Analyze key overlap between all pairs of upstream DataFrames.

    For each pair, reports:
    - Total distinct keys in each side
    - Overlap count and percentage
    - Sample orphan keys from each side
    """
    results: list[dict] = []
    names = list(upstreams.keys())

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            name_a, name_b = names[i], names[j]
            df_a, df_b = upstreams[name_a], upstreams[name_b]
            eng_a, eng_b = _detect_engine(df_a), _detect_engine(df_b)

            # Check that both have the key columns
            cols_a = set(_get_columns(df_a, eng_a))
            cols_b = set(_get_columns(df_b, eng_b))
            common_keys = [k for k in keys if k in cols_a and k in cols_b]

            if not common_keys:
                results.append({
                    "pair": f"{name_a}↔{name_b}",
                    "common_keys": [],
                    "error": f"No shared key columns from {keys}",
                })
                continue

            overlap = _compute_key_overlap(
                df_a, df_b, common_keys, eng_a, eng_b, name_a, name_b, sample_limit
            )
            results.append(overlap)

    return results


def _compute_key_overlap(
    df_a: Any,
    df_b: Any,
    keys: list[str],
    eng_a: str,
    eng_b: str,
    name_a: str,
    name_b: str,
    sample_limit: int,
) -> dict:
    """Compute key overlap between two DataFrames."""
    if eng_a == "pandas" and eng_b == "pandas":
        return _key_overlap_pandas(df_a, df_b, keys, name_a, name_b, sample_limit)
    if eng_a == "spark" and eng_b == "spark":
        return _key_overlap_spark(df_a, df_b, keys, name_a, name_b, sample_limit)
    # Mixed engine — convert Spark to Pandas for comparison
    return _key_overlap_mixed(df_a, df_b, keys, eng_a, eng_b, name_a, name_b, sample_limit)


def _key_overlap_pandas(
    df_a, df_b, keys: list[str], name_a: str, name_b: str, sample_limit: int
) -> dict:
    """Compute key overlap for two Pandas DataFrames."""
    import pandas as pd

    if len(keys) == 1:
        k = keys[0]
        set_a = set(df_a[k].dropna().unique())
        set_b = set(df_b[k].dropna().unique())
    else:
        set_a = set(df_a[keys].dropna().apply(tuple, axis=1).unique())
        set_b = set(df_b[keys].dropna().apply(tuple, axis=1).unique())

    overlap = set_a & set_b
    only_a = set_a - set_b
    only_b = set_b - set_a
    total_union = len(set_a | set_b)
    overlap_pct = round(len(overlap) / total_union, 4) if total_union > 0 else 0.0

    return {
        "pair": f"{name_a}↔{name_b}",
        "common_keys": keys,
        "distinct_a": len(set_a),
        "distinct_b": len(set_b),
        "overlap_count": len(overlap),
        "overlap_pct": overlap_pct,
        "only_in_a_count": len(only_a),
        "only_in_b_count": len(only_b),
        "sample_only_a": sorted(list(only_a))[:sample_limit],
        "sample_only_b": sorted(list(only_b))[:sample_limit],
    }


def _key_overlap_spark(
    df_a, df_b, keys: list[str], name_a: str, name_b: str, sample_limit: int
) -> dict:
    """Compute key overlap for two Spark DataFrames."""
    from pyspark.sql import functions as F

    distinct_a = df_a.select(keys).dropna().distinct()
    distinct_b = df_b.select(keys).dropna().distinct()

    count_a = distinct_a.count()
    count_b = distinct_b.count()

    overlap_df = distinct_a.intersect(distinct_b)
    overlap_count = overlap_df.count()

    only_a_df = distinct_a.subtract(distinct_b)
    only_b_df = distinct_b.subtract(distinct_a)
    only_a_count = only_a_df.count()
    only_b_count = only_b_df.count()

    total_union = count_a + count_b - overlap_count
    overlap_pct = round(overlap_count / total_union, 4) if total_union > 0 else 0.0

    # Sample orphans
    sample_a = [row.asDict() for row in only_a_df.limit(sample_limit).collect()]
    sample_b = [row.asDict() for row in only_b_df.limit(sample_limit).collect()]

    return {
        "pair": f"{name_a}↔{name_b}",
        "common_keys": keys,
        "distinct_a": count_a,
        "distinct_b": count_b,
        "overlap_count": overlap_count,
        "overlap_pct": overlap_pct,
        "only_in_a_count": only_a_count,
        "only_in_b_count": only_b_count,
        "sample_only_a": sample_a,
        "sample_only_b": sample_b,
    }


def _key_overlap_mixed(
    df_a, df_b, keys: list[str], eng_a: str, eng_b: str,
    name_a: str, name_b: str, sample_limit: int,
) -> dict:
    """Handle mixed-engine overlap by converting Spark side to Pandas."""
    if eng_a == "spark":
        pdf_a = df_a.select(keys).dropna().distinct().toPandas()
    else:
        pdf_a = df_a[keys].dropna().drop_duplicates()

    if eng_b == "spark":
        pdf_b = df_b.select(keys).dropna().distinct().toPandas()
    else:
        pdf_b = df_b[keys].dropna().drop_duplicates()

    return _key_overlap_pandas(pdf_a, pdf_b, keys, name_a, name_b, sample_limit)


def _filter_boundary_analysis(
    upstreams: dict[str, Any],
    filter_expr: str,
    sample_limit: int,
) -> dict:
    """Analyze filter boundary — check if filter kills all rows in upstream.

    For Spark: uses .where(filter_expr)
    For Pandas: uses .query(filter_expr) with fallback to .eval()
    """
    results: dict[str, Any] = {"filter_expr": filter_expr, "per_upstream": {}}

    for name, df in upstreams.items():
        eng = _detect_engine(df)
        total = _get_count(df, eng)

        if total == 0:
            results["per_upstream"][name] = {
                "total": 0,
                "passing": 0,
                "all_fail": True,
                "error": None,
            }
            continue

        try:
            if eng == "spark":
                passing = df.where(filter_expr).count()
            else:
                try:
                    passing = len(df.query(filter_expr))
                except Exception:
                    # Fallback: try eval for simpler expressions
                    mask = df.eval(filter_expr)
                    passing = int(mask.sum())
        except Exception as e:
            results["per_upstream"][name] = {
                "total": total,
                "passing": None,
                "all_fail": None,
                "error": str(e)[:200],
            }
            continue

        results["per_upstream"][name] = {
            "total": total,
            "passing": passing,
            "pass_pct": round(passing / total, 4) if total > 0 else 0.0,
            "all_fail": passing == 0,
            "error": None,
        }

    return results


def _all_null_columns(
    upstreams: dict[str, Any],
) -> dict[str, list[str]]:
    """Detect columns that are entirely NULL in each upstream.

    All-NULL columns suggest schema drift or empty source — a common
    cause of empty output when filters/joins depend on those columns.
    """
    results: dict[str, list[str]] = {}

    for name, df in upstreams.items():
        eng = _detect_engine(df)
        null_cols: list[str] = []
        total = _get_count(df, eng)

        if total == 0:
            # All columns are vacuously null if no rows
            results[name] = _get_columns(df, eng)
            continue

        cols = _get_columns(df, eng)

        if eng == "pandas":
            for col in cols:
                if df[col].isna().all():
                    null_cols.append(col)
        else:
            from pyspark.sql import functions as F

            # Batch null check — count non-null for each column
            exprs = [F.count(F.col(c)).alias(c) for c in cols]
            counts = df.select(exprs).collect()[0]
            for col in cols:
                if counts[col] == 0:
                    null_cols.append(col)

        results[name] = null_cols

    return results


def _type_mismatch_detection(
    upstreams: dict[str, Any],
    keys: list[str],
) -> list[dict]:
    """Detect type mismatches on join keys across upstream DataFrames.

    Type mismatches (e.g., string '123' vs int 123) cause zero overlap
    in joins — one of the most common causes of empty output.
    """
    mismatches: list[dict] = []
    names = list(upstreams.keys())

    for key_col in keys:
        types_found: dict[str, str] = {}
        for name in names:
            df = upstreams[name]
            eng = _detect_engine(df)
            cols = _get_columns(df, eng)
            if key_col in cols:
                types_found[name] = _get_dtype(df, key_col, eng)

        # Flag only when type *families* differ (string vs numeric, etc.).
        # float64 vs int64 and other within-family width differences coerce
        # cleanly in joins, so they are not a real cause of empty output.
        unique_families = {_type_family(t) for t in types_found.values()}
        if len(unique_families) > 1:
            mismatches.append({
                "key_column": key_col,
                "types_by_source": types_found,
                "unique_types": sorted(set(types_found.values())),
            })

    return mismatches


# ============================================================================
# Cause ranking
# ============================================================================


def _rank_causes(
    funnel: dict[str, dict],
    key_overlaps: list[dict],
    filter_analysis: dict | None,
    null_columns: dict[str, list[str]],
    type_mismatches: list[dict],
    keys: list[str],
) -> tuple[str, str, list[str]]:
    """Rank the most likely cause of empty output.

    Priority order (highest severity first):
    1. Type mismatch on join keys
    2. Zero key overlap between upstreams
    3. Filter kills all rows
    4. All-NULL columns on key columns
    5. Empty upstream (zero rows in source)
    6. Unknown

    Returns:
        (dropout_stage, dropout_cause, findings)
    """
    findings: list[str] = []

    # Check for empty upstreams first
    empty_upstreams = [
        name for name, info in funnel.items()
        if name != "__result__" and info["count"] == 0
    ]
    if empty_upstreams:
        findings.append(
            f"Empty upstream(s): {', '.join(empty_upstreams)} have 0 rows"
        )

    # 1. Type mismatch
    if type_mismatches:
        mm = type_mismatches[0]
        types_str = ", ".join(
            f"{src}={t}" for src, t in mm["types_by_source"].items()
        )
        stage = f"type_mismatch:{mm['key_column']}"
        cause = "key_type_mismatch"
        findings.insert(
            0,
            f"Type mismatch on key '{mm['key_column']}': {types_str}",
        )
        return stage, cause, findings

    # 2. Zero key overlap
    for overlap in key_overlaps:
        if "error" in overlap:
            findings.append(f"{overlap['pair']}: {overlap['error']}")
            continue
        if overlap.get("overlap_count", -1) == 0:
            stage = f"join:{overlap['pair']}"
            cause = "zero_key_overlap"
            findings.insert(
                0,
                f"Zero key overlap between {overlap['pair']} "
                f"(A={overlap['distinct_a']}, B={overlap['distinct_b']})",
            )
            return stage, cause, findings
        elif overlap.get("overlap_pct", 1.0) < 0.05:
            findings.append(
                f"Low key overlap between {overlap['pair']}: "
                f"{overlap['overlap_pct']:.1%}"
            )

    # 3. Filter kills all rows
    if filter_analysis:
        for name, info in filter_analysis.get("per_upstream", {}).items():
            if info.get("all_fail"):
                stage = f"filter:{name}"
                cause = "filter_kills_all"
                findings.insert(
                    0,
                    f"Filter '{filter_analysis['filter_expr']}' matches 0 of "
                    f"{info['total']} rows in '{name}'",
                )
                return stage, cause, findings

    # 4. All-NULL on key columns
    for name, null_cols in null_columns.items():
        null_keys = [k for k in keys if k in null_cols]
        if null_keys:
            stage = f"null_keys:{name}"
            cause = "key_columns_all_null"
            findings.insert(
                0,
                f"Key column(s) {null_keys} are entirely NULL in '{name}'",
            )
            return stage, cause, findings

    # 5. Empty upstream
    if empty_upstreams:
        stage = f"empty_source:{empty_upstreams[0]}"
        cause = "empty_upstream"
        return stage, cause, findings

    # 6. Unknown
    stage = "unknown"
    cause = "undetermined"
    if not findings:
        findings.append(
            "No obvious cause detected — check transformation logic or "
            "multi-step pipeline stages not provided as upstreams"
        )
    return stage, cause, findings


# ============================================================================
# Suggestion engine
# ============================================================================


def _build_suggestions(
    dropout_cause: str,
    type_mismatches: list[dict],
    key_overlaps: list[dict],
    filter_analysis: dict | None,
    null_columns: dict[str, list[str]],
    keys: list[str],
) -> list[str]:
    """Build actionable suggested_next_actions based on diagnosed cause."""
    suggestions: list[str] = []

    if dropout_cause == "key_type_mismatch" and type_mismatches:
        mm = type_mismatches[0]
        col = mm["key_column"]
        sources = list(mm["types_by_source"].keys())
        suggestions.append(
            f"Fix type mismatch: cast '{col}' to a common type before joining "
            f"(e.g., .withColumn('{col}', F.col('{col}').cast('string')))"
        )
        if len(sources) >= 2:
            suggestions.append(
                f"Verify with: anchor('pre_join', {sources[0]}_df, {sources[1]}_df, "
                f"keys={keys})"
            )

    elif dropout_cause == "zero_key_overlap":
        suggestions.append(
            "Investigate key values: sample distinct keys from each upstream "
            "to understand the mismatch"
        )
        suggestions.append(
            "Check for ETL issues: one upstream may be filtered to a different "
            "date range or partition"
        )
        if keys:
            suggestions.append(
                f"Verify join keys: anchor('pre_join', left_df, right_df, "
                f"keys={keys})"
            )

    elif dropout_cause == "filter_kills_all" and filter_analysis:
        expr = filter_analysis["filter_expr"]
        suggestions.append(
            f"Check filter expression: '{expr}' — verify the column values "
            f"and data types match expectations"
        )
        suggestions.append(
            "Try relaxing the filter or examining the column with: "
            "anchor('microscope', df, 'column_name')"
        )

    elif dropout_cause == "key_columns_all_null":
        suggestions.append(
            "Key columns are entirely NULL — check upstream data load or "
            "schema drift"
        )
        suggestions.append(
            "Profile the upstream: anchor('explore', upstream_df) to check null rates"
        )

    elif dropout_cause == "empty_upstream":
        suggestions.append(
            "One or more upstream DataFrames have 0 rows — check the data "
            "source or preceding pipeline stages"
        )

    if not suggestions:
        suggestions.append(
            "Add intermediate pipeline stages as upstreams to narrow down "
            "where rows disappear"
        )
        suggestions.append(
            "Check for multi-step transformations not captured in provided upstreams"
        )

    return suggestions


# ============================================================================
# Markdown renderer
# ============================================================================


def _render_markdown(ctx: dict) -> str:
    """Render the diagnose_empty context dict as readable markdown."""
    lines: list[str] = []
    lines.append(f"# Empty Output Diagnosis: {ctx['subject']}")
    lines.append("")
    lines.append(f"**{ctx['summary']}**")
    lines.append("")

    # Metrics
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    for k, v in ctx["metrics"].items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # Funnel
    if "funnel" in ctx:
        lines.append("## Row Count Funnel")
        lines.append("")
        lines.append("| Stage | Rows | Columns |")
        lines.append("| --- | --- | --- |")
        for name, info in ctx["funnel"].items():
            display_name = "**RESULT**" if name == "__result__" else name
            lines.append(f"| {display_name} | {info['count']:,} | {info['columns']} |")
        lines.append("")

    # Findings
    if ctx["findings"]:
        lines.append("## Findings")
        lines.append("")
        for f in ctx["findings"]:
            lines.append(f"- {f}")
        lines.append("")

    # Risks
    if ctx["risks"]:
        lines.append("## Risks")
        lines.append("")
        for r in ctx["risks"]:
            lines.append(f"- ⚠ {r}")
        lines.append("")

    # Key overlap details
    if ctx.get("samples", {}).get("key_overlaps"):
        lines.append("## Key Overlap Details")
        lines.append("")
        for ov in ctx["samples"]["key_overlaps"]:
            if "error" in ov:
                lines.append(f"- {ov['pair']}: {ov['error']}")
            else:
                lines.append(
                    f"- **{ov['pair']}**: overlap={ov['overlap_count']:,} "
                    f"({ov['overlap_pct']:.1%}), "
                    f"only_A={ov['only_in_a_count']:,}, "
                    f"only_B={ov['only_in_b_count']:,}"
                )
        lines.append("")

    # Type mismatches
    if ctx.get("samples", {}).get("type_mismatches"):
        lines.append("## Type Mismatches")
        lines.append("")
        for mm in ctx["samples"]["type_mismatches"]:
            types_str = ", ".join(
                f"{src}={t}" for src, t in mm["types_by_source"].items()
            )
            lines.append(f"- `{mm['key_column']}`: {types_str}")
        lines.append("")

    # Null columns
    if ctx.get("samples", {}).get("all_null_columns"):
        lines.append("## All-NULL Columns")
        lines.append("")
        for name, cols in ctx["samples"]["all_null_columns"].items():
            if cols:
                lines.append(f"- **{name}**: {', '.join(cols)}")
        lines.append("")

    # Suggested next actions
    if ctx["suggested_next_actions"]:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for i, action in enumerate(ctx["suggested_next_actions"], 1):
            lines.append(f"{i}. {action}")
        lines.append("")

    return "\n".join(lines)


# ============================================================================
# Main entry point
# ============================================================================


def diagnose_empty_context(
    *args,
    result_df=None,
    upstreams: dict[str, Any] | None = None,
    keys: list[str] | None = None,
    filter_expr: str | None = None,
    subject: str = "output",
    output_format: str = "dict",
    sample_limit: int = 10,
    **kwargs,
) -> dict | str:
    """Diagnose why a DataFrame has zero or unexpectedly few rows.

    Args:
        result_df: The empty or sparse output DataFrame.
        upstreams: Dict of upstream DataFrames: {"source": df1, "dim": df2}.
        keys: Expected join/grain key columns (used for overlap analysis).
        filter_expr: The filter expression applied (if known). Used for
            boundary analysis.
        subject: Name for the result DataFrame in output.
        output_format: "dict" or "markdown".
        sample_limit: Max samples per diagnostic check.

    Returns:
        Standard Anchor contract dict (or markdown string if output_format="markdown").

    Raises:
        ValueError: If result_df is None or upstreams is empty.
    """
    # ── Resolve positional args ────────────────────────────────────────────
    if result_df is None and args:
        result_df = args[0]
        args = args[1:]
    if upstreams is None and args:
        upstreams = args[0]

    # ── Validate inputs ───────────────────────────────────────────────────
    if result_df is None:
        raise ValueError(
            "result_df is required — pass the output DataFrame: "
            "anchor('diagnose_empty', result_df, upstreams={'source': src_df}) "
            "— see anchor('help', 'diagnose_empty')."
        )
    if not upstreams:
        raise ValueError(
            "upstreams dict is required (at least one upstream DataFrame). "
            "Pass: anchor('diagnose_empty', df, upstreams={'source': src_df}) "
            "— see anchor('help', 'diagnose_empty')."
        )
    validate_output_format(output_format)
    guard_dataframe_type(result_df, "result_df")

    keys = keys or []
    result_engine = _detect_engine(result_df)

    # ── 1. Row count funnel ───────────────────────────────────────────────
    funnel = _build_row_count_funnel(result_df, upstreams, result_engine)
    result_count = funnel["__result__"]["count"]
    upstream_counts = {
        name: info["count"]
        for name, info in funnel.items()
        if name != "__result__"
    }

    # ── 2. Key overlap analysis ───────────────────────────────────────────
    key_overlaps: list[dict] = []
    if keys and len(upstreams) >= 2:
        key_overlaps = _key_overlap_analysis(upstreams, keys, sample_limit)

    # ── 3. Filter boundary analysis ──────────────────────────────────────
    filter_analysis: dict | None = None
    if filter_expr:
        filter_analysis = _filter_boundary_analysis(
            upstreams, filter_expr, sample_limit
        )

    # ── 4. All-NULL column detection ─────────────────────────────────────
    null_columns = _all_null_columns(upstreams)

    # ── 5. Type mismatch detection ───────────────────────────────────────
    type_mismatches: list[dict] = []
    if keys:
        type_mismatches = _type_mismatch_detection(upstreams, keys)

    # ── 6. Cause ranking ─────────────────────────────────────────────────
    dropout_stage, dropout_cause, findings = _rank_causes(
        funnel, key_overlaps, filter_analysis, null_columns, type_mismatches, keys
    )

    # ── Build suggestions ─────────────────────────────────────────────────
    suggestions = _build_suggestions(
        dropout_cause, type_mismatches, key_overlaps,
        filter_analysis, null_columns, keys,
    )

    # ── Build risks ───────────────────────────────────────────────────────
    risks: list[str] = []
    if type_mismatches:
        mm = type_mismatches[0]
        risks.append(
            f"Type mismatch on join key '{mm['key_column']}' — "
            f"cast to common type before joining"
        )
    if any(
        ov.get("overlap_pct", 1.0) < 0.1
        for ov in key_overlaps
        if "error" not in ov
    ):
        risks.append(
            "Very low key overlap between upstream sources — "
            "join will produce few/no rows"
        )
    if filter_analysis:
        for name, info in filter_analysis.get("per_upstream", {}).items():
            if info.get("all_fail"):
                risks.append(
                    f"Filter '{filter_analysis['filter_expr']}' eliminates all "
                    f"rows in '{name}'"
                )

    # ── Build summary ─────────────────────────────────────────────────────
    upstream_str = ", ".join(
        f"{name}={info['count']:,}"
        for name, info in funnel.items()
        if name != "__result__"
    )
    summary = (
        f"EMPTY OUTPUT: result has {result_count:,} rows. "
        f"Upstreams: {upstream_str}. "
        f"Cause: {dropout_cause.replace('_', ' ')} "
        f"(stage: {dropout_stage})."
    )

    # ── Build samples ─────────────────────────────────────────────────────
    samples: dict[str, Any] = {}
    if key_overlaps:
        samples["key_overlaps"] = key_overlaps
    if type_mismatches:
        samples["type_mismatches"] = type_mismatches
    if any(v for v in null_columns.values()):
        samples["all_null_columns"] = {
            k: v for k, v in null_columns.items() if v
        }
    if filter_analysis:
        samples["filter_analysis"] = filter_analysis

    # ── Build metrics ─────────────────────────────────────────────────────
    metrics = {
        "result_count": result_count,
        "result_column_count": funnel["__result__"]["columns"],
        "upstream_counts": upstream_counts,
        "dropout_stage": dropout_stage,
        "dropout_cause": dropout_cause,
    }

    # ── Assemble contract ─────────────────────────────────────────────────
    ctx = build_base_context(
        kind="diagnose_empty",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples=samples,
        suggested_next_actions=suggestions,
        funnel=funnel,
    )

    return finalize_context(ctx, output_format, _render_markdown)
