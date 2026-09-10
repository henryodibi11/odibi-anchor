"""Explain Row tool implementation.

Entry point: explain_row_context(output_df, keys=..., values=..., upstream=...)
Traces how a specific row in an output DataFrame was constructed from upstream DataFrames.
Reports column origin, match type, join path, and suggested next actions.

Dual-engine: Spark for production, Pandas for testing.
"""
from __future__ import annotations

import logging
from typing import Any

from odibi_anchor._utils.contract import (
    build_base_context,
    finalize_context,
    guard_dataframe_type,
    validate_output_format,
)
from odibi_anchor._utils.engine_utils import detect_engine, is_pandas_df, is_spark_df
from odibi_anchor._utils.render_utils import (
    render_bullet_section,
    render_header_lines,
    render_metrics_lines,
    render_table,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Value classification
# ============================================================================


def _normalize_for_coercion(value: Any) -> str | None:
    """Normalize a value for coercion comparison (strip whitespace + lowercase).

    Returns None if value is None/NaN.
    """
    if value is None:
        return None
    # Handle NaN (float or numpy)
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower()


def _is_null(value: Any) -> bool:
    """Check if a value is null-like (None or NaN)."""
    if value is None:
        return True
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _classify_value_match(output_value: Any, upstream_value: Any) -> str:
    """Classify how output value relates to upstream value.

    Returns:
        "exact" — values are identical (including both null)
        "transformed" — same column exists but value differs
        "coerced" — values match after normalization (whitespace, case)
        "null_filled" — upstream is NULL, output has a value (COALESCE/default)
        "not_found" — should not be called for this case (handled externally)
    """
    out_null = _is_null(output_value)
    up_null = _is_null(upstream_value)

    # Both null → exact match
    if out_null and up_null:
        return "exact"

    # Upstream null, output has value → null was filled
    if up_null and not out_null:
        return "null_filled"

    # Output null, upstream has value → transformed (value was nulled out)
    if out_null and not up_null:
        return "transformed"

    # Direct equality check
    if output_value == upstream_value:
        return "exact"

    # Type-coerced equality (e.g., int 42 vs str "42")
    try:
        if str(output_value) == str(upstream_value):
            return "coerced"
    except (TypeError, ValueError):
        pass

    # Whitespace/case normalization
    out_norm = _normalize_for_coercion(output_value)
    up_norm = _normalize_for_coercion(upstream_value)
    if out_norm is not None and up_norm is not None and out_norm == up_norm:
        return "coerced"

    # Values differ → transformed
    return "transformed"


# ============================================================================
# Row extraction
# ============================================================================


def _extract_row(df: Any, keys: list[str], values: dict, engine: str) -> dict | None:
    """Extract a single row from a DataFrame by key values.

    Returns the row as a dict, or None if not found.
    """
    if engine == "pandas":
        import pandas as pd

        mask = pd.Series([True] * len(df), index=df.index)
        for key in keys:
            if key not in df.columns:
                return None
            mask = mask & (df[key] == values[key])

        matched = df[mask]
        if matched.empty:
            return None
        # Take first matching row
        row = matched.iloc[0]
        return {col: (None if _is_null(v) else v) for col, v in row.items()}

    elif engine == "spark":
        from pyspark.sql import functions as F

        filtered = df
        for key in keys:
            if key not in df.columns:
                return None
            filtered = filtered.filter(F.col(key) == values[key])

        rows = filtered.limit(1).collect()
        if not rows:
            return None
        row_dict = rows[0].asDict()
        return {col: (None if _is_null(v) else v) for col, v in row_dict.items()}

    return None


# ============================================================================
# Column origin tracing
# ============================================================================


def _find_matching_keys(
    upstream_df: Any, keys: list[str], values: dict, engine: str
) -> list[str]:
    """Find which key columns from the output exist in this upstream.

    Returns all keys found in upstream columns, or empty list.
    """
    if engine == "pandas":
        cols = set(upstream_df.columns)
    else:
        cols = set(upstream_df.columns)

    return [key for key in keys if key in cols]


def _trace_column_origins(
    output_row: dict,
    keys: list[str],
    key_values: dict,
    upstream: dict[str, Any],
    engine: str,
    sample_limit: int = 5,
) -> list[dict]:
    """For each column in output_row, find which upstream has a matching value.

    For each non-key column:
    1. Check each upstream DataFrame for a column with the same name
    2. Filter upstream by key match (join the upstream on key columns)
    3. Compare the upstream value to the output value
    4. Classify: exact_match, transformed, coerced, null_filled, not_found

    Returns list of:
        {"column": str, "output_value": any, "origin": str|None,
         "upstream_value": any, "match_type": str,
         "join_key": bool, "all_sources": list[dict]}
    """
    results = []
    all_columns = list(output_row.keys())

    for col in all_columns:
        output_value = output_row[col]
        is_join_key = col in keys

        # For key columns, mark them as keys and find origin
        col_result = {
            "column": col,
            "output_value": output_value,
            "origin": None,
            "upstream_value": None,
            "match_type": "not_found",
            "join_key": is_join_key,
            "all_sources": [],
        }

        # Search each upstream for this column
        for upstream_name, upstream_df in upstream.items():
            # Check if column exists in this upstream
            if engine == "pandas":
                up_cols = set(upstream_df.columns)
            else:
                up_cols = set(upstream_df.columns)

            if col not in up_cols:
                continue

            # Find join keys that connect to this upstream
            join_keys_found = _find_matching_keys(upstream_df, keys, key_values, engine)
            if not join_keys_found:
                # Try using the column itself if it's a key
                if is_join_key:
                    join_keys_found = [col]
                else:
                    continue

            # Extract the upstream row using all matching join keys
            join_values = {k: key_values.get(k, output_row.get(k)) for k in join_keys_found}
            upstream_row = _extract_row(
                upstream_df, join_keys_found, join_values, engine
            )
            if upstream_row is None:
                continue

            upstream_value = upstream_row.get(col)
            match_type = _classify_value_match(output_value, upstream_value)

            source_info = {
                "upstream": upstream_name,
                "value": upstream_value,
                "match_type": match_type,
                "join_key_used": join_keys_found[0] if len(join_keys_found) == 1 else join_keys_found,
            }
            col_result["all_sources"].append(source_info)

            # First match wins for primary origin
            if col_result["origin"] is None:
                col_result["origin"] = upstream_name
                col_result["upstream_value"] = upstream_value
                col_result["match_type"] = match_type

        # Trim all_sources to sample_limit
        col_result["all_sources"] = col_result["all_sources"][:sample_limit]

        results.append(col_result)

    return results


# ============================================================================
# Join path detection
# ============================================================================


def _detect_join_paths(
    output_row: dict,
    keys: list[str],
    upstream: dict[str, Any],
    engine: str,
) -> list[dict]:
    """Determine which key columns connect the output row to each upstream.

    Returns list of:
        {"upstream": str, "join_keys": list[str], "match_count": int}
    """
    paths = []
    for upstream_name, upstream_df in upstream.items():
        matching_keys = _find_matching_keys(upstream_df, keys, {}, engine)
        if not matching_keys:
            continue

        # Count how many rows match on these keys
        key_values = {k: output_row[k] for k in matching_keys if k in output_row}
        matched_row = _extract_row(upstream_df, matching_keys, key_values, engine)
        match_count = 1 if matched_row else 0

        paths.append({
            "upstream": upstream_name,
            "join_keys": matching_keys,
            "match_count": match_count,
        })

    return paths


# ============================================================================
# Summary and findings generation
# ============================================================================


def _build_summary(
    subject: str,
    column_lineage: list[dict],
    upstream: dict,
) -> str:
    """Build a human-readable summary string."""
    total = len(column_lineage)
    traced = sum(1 for c in column_lineage if c["match_type"] != "not_found")
    untraced = total - traced

    # Count by origin
    origin_counts: dict[str, int] = {}
    for c in column_lineage:
        if c["origin"]:
            origin_counts[c["origin"]] = origin_counts.get(c["origin"], 0) + 1

    # Count transformed
    transformed = [c for c in column_lineage if c["match_type"] == "transformed"]

    # Lead with a "so what" on how well this row is explained by its sources.
    if total and untraced == 0:
        verdict = "Every output column traces to an upstream source — lineage looks complete"
    elif total and untraced == total:
        verdict = "No columns matched upstream — this row's values look fully computed/derived"
    elif untraced:
        verdict = (
            f"{traced}/{total} columns trace upstream; {untraced} look computed or "
            "renamed — inspect those before trusting the lineage"
        )
    else:
        verdict = "Row lineage traced"

    parts = [verdict + ".", f"Row traced through {len(upstream)} upstream source(s)."]
    for name, count in origin_counts.items():
        parts.append(f"{count} columns from {name}")

    if transformed:
        tx_desc = ", ".join(
            f"{c['column']}: {c['upstream_value']}→{c['output_value']} from {c['origin']}"
            for c in transformed[:3]
        )
        parts.append(f"{len(transformed)} transformed ({tx_desc})")

    if untraced:
        parts.append(f"{untraced} columns have no upstream match")

    return ". ".join(parts) + "."


def _build_findings(column_lineage: list[dict]) -> list[str]:
    """Generate human-readable findings from column lineage."""
    findings = []

    # Transformed values
    for c in column_lineage:
        if c["match_type"] == "transformed":
            findings.append(
                f"{c['column']}: output={c['output_value']!r} but {c['origin']} "
                f"has {c['upstream_value']!r} — value was transformed or overwritten"
            )

    # Null-filled values
    for c in column_lineage:
        if c["match_type"] == "null_filled":
            findings.append(
                f"{c['column']}: output={c['output_value']!r} but {c['origin']} "
                f"has NULL — value was filled (COALESCE or default)"
            )

    # Untraced columns
    untraced = [c["column"] for c in column_lineage if c["match_type"] == "not_found"]
    if untraced:
        findings.append(
            f"{', '.join(untraced)} have no upstream match — likely computed columns"
        )

    # Better origin available (exact match exists in a later upstream)
    for c in column_lineage:
        if c["match_type"] in ("transformed", "null_filled", "coerced") and c["all_sources"]:
            exact_alternatives = [
                s for s in c["all_sources"]
                if s["match_type"] == "exact" and s["upstream"] != c["origin"]
            ]
            if exact_alternatives:
                alt = exact_alternatives[0]
                findings.append(
                    f"{c['column']}: attributed to {c['origin']} ({c['match_type']}) "
                    f"but {alt['upstream']} has exact match — "
                    f"consider upstream ordering"
                )

    # Origin distribution
    origin_counts: dict[str, int] = {}
    for c in column_lineage:
        if c["origin"]:
            origin_counts[c["origin"]] = origin_counts.get(c["origin"], 0) + 1
    if origin_counts:
        dist = ", ".join(f"{count} from {name}" for name, count in origin_counts.items())
        findings.append(f"Column origins: {dist}")

    return findings


def _build_risks(column_lineage: list[dict]) -> list[str]:
    """Generate risk statements from column lineage."""
    risks = []

    for c in column_lineage:
        if c["match_type"] == "transformed":
            risks.append(
                f"{c['column']} value mismatch ({c['upstream_value']!r}→{c['output_value']!r}): "
                f"investigate transformation logic or check if wrong upstream row was joined"
            )
        elif c["match_type"] == "null_filled":
            risks.append(
                f"{c['column']} null-fill: verify the default {c['output_value']!r} "
                f"is correct business logic"
            )

    return risks


def _build_suggested_actions(
    column_lineage: list[dict],
    keys: list[str],
    upstream: dict,
) -> list[str]:
    """Build suggested next anchor() actions."""
    actions = []
    key_str = str(keys)

    # For transformed columns, suggest diff
    for c in column_lineage:
        if c["match_type"] == "transformed" and c["origin"]:
            actions.append(
                f"Investigate {c['column']} transformation: "
                f"anchor('diff', {c['origin']}_df, output_df, keys={key_str}, columns=['{c['column']}'])"
            )
            break  # Just suggest one

    # Suggest pre_join for duplicate key check
    if upstream:
        first_upstream = next(iter(upstream.keys()))
        actions.append(
            f"Check for duplicate upstream keys: "
            f"anchor('pre_join', output_df, {first_upstream}_df, keys={key_str})"
        )

    # Suggest microscope for suspicious columns
    transformed = [c for c in column_lineage if c["match_type"] == "transformed"]
    if transformed:
        col = transformed[0]["column"]
        actions.append(
            f"Profile the suspicious column: anchor('microscope', output_df, '{col}')"
        )

    # Suggest case_file for further investigation
    if transformed:
        col = transformed[0]["column"]
        actions.append(
            f"Find pattern in mismatches: "
            f"anchor('case_file', output_df, column='{col}')"
        )

    return actions


# ============================================================================
# Markdown rendering
# ============================================================================


def render_explain_row_report(ctx: dict) -> str:
    """Render explain_row context dict as markdown report."""
    lines = render_header_lines(ctx, "Explain Row")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    # Column lineage table
    lineage = ctx.get("column_lineage", [])
    if lineage:
        lines.extend(["", "## Column Lineage", ""])
        headers = ["Column", "Output Value", "Origin", "Upstream Value", "Match Type", "Key"]
        rows = []
        for c in lineage:
            rows.append([
                c["column"],
                repr(c["output_value"]),
                c["origin"] or "—",
                repr(c["upstream_value"]) if c["upstream_value"] is not None else "—",
                c["match_type"],
                "✓" if c.get("join_key") else "",
            ])
        lines.extend(render_table(rows, headers))

    # Join paths
    join_paths = ctx.get("join_paths", [])
    if join_paths:
        lines.extend(["", "## Join Paths", ""])
        headers = ["Upstream", "Join Keys", "Match Count"]
        rows = [[p["upstream"], ", ".join(p["join_keys"]), str(p["match_count"])] for p in join_paths]
        lines.extend(render_table(rows, headers))

    # Findings
    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))

    # Risks
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))

    # Suggested next actions
    lines.extend(render_bullet_section(
        ctx.get("suggested_next_actions", []), "## Suggested Next Actions"
    ))

    return "\n".join(lines)


# ============================================================================
# Public entry point
# ============================================================================


def explain_row_context(
    *args,
    output_df=None,
    keys: list[str] | None = None,
    values: dict | None = None,
    upstream: dict | None = None,
    output_format: str = "dict",
    sample_limit: int = 5,
    **kwargs,
) -> dict | str:
    """Trace where each column value in an output row came from.

    Args:
        output_df: The output/result DataFrame containing the row to explain.
            Can also be passed as the first positional argument.
        keys: Key column(s) to identify the row (e.g., ["project_id"]).
        values: Key values to locate the row (e.g., {"project_id": "PROJ-2847"}).
        upstream: Dict of upstream DataFrames (e.g., {"source_a": df_a, "source_b": df_b}).
            Keys are names for display, values are DataFrames.
        output_format: "dict" or "markdown".
        sample_limit: Max upstream matches to show per column when multiple exist.

    Raises:
        ValueError: If row not found, keys missing, or upstream empty.
        TypeError: If output_df is not a DataFrame.

    Returns:
        Standard Anchor contract dict or markdown string.
    """
    validate_output_format(output_format)

    # Handle positional arg
    if output_df is None and args:
        output_df = args[0]
        args = args[1:]

    # ── Validation ──────────────────────────────────────────────────────
    if output_df is None:
        raise ValueError("output_df is required — pass a DataFrame to trace.")

    guard_dataframe_type(output_df, "output_df")

    if not keys:
        raise ValueError(
                "keys is required — provide key column(s) to identify the row. "
                "Usage: anchor('explain_row', df, keys=['id'], values=[123]) "
                "— see anchor('help', 'explain_row')."
            )

    if not values:
        raise ValueError(
                "values is required — provide key values matching the keys list. "
                "Usage: anchor('explain_row', df, keys=['id'], values=[123]) "
                "— see anchor('help', 'explain_row')."
            )

    if not upstream or not isinstance(upstream, dict):
        raise ValueError(
            "upstream is required — provide a dict of upstream DataFrames "
            "(e.g., {'source_a': df_a, 'source_b': df_b})."
        )

    # Validate all keys are in values
    missing_keys = [k for k in keys if k not in values]
    if missing_keys:
        raise ValueError(
            f"values dict is missing keys: {missing_keys}. "
            "Provide all key column values to locate the row "
            "— see anchor('help', 'explain_row')."
        )

    # ── Engine detection ────────────────────────────────────────────────
    engine = detect_engine(output_df)
    if engine == "unknown":
        raise TypeError(
            f"Cannot detect engine for output_df: {type(output_df).__name__}. "
            "Pass a Pandas or Spark DataFrame "
            "— see anchor('help', 'explain_row')."
        )

    # ── Extract the target row ──────────────────────────────────────────
    output_row = _extract_row(output_df, keys, values, engine)
    if output_row is None:
        key_desc = ", ".join(f"{k}={v!r}" for k, v in values.items())
        raise ValueError(
            f"Row not found in output_df for keys: {key_desc}. "
            "Verify the values dict matches actual row values in output_df "
            "— see anchor('help', 'explain_row')."
        )

    # ── Trace column origins ────────────────────────────────────────────
    column_lineage = _trace_column_origins(
        output_row, keys, values, upstream, engine, sample_limit
    )

    # ── Detect join paths ───────────────────────────────────────────────
    join_paths = _detect_join_paths(output_row, keys, upstream, engine)

    # ── Build metrics ───────────────────────────────────────────────────
    total_cols = len(column_lineage)
    traced_cols = sum(1 for c in column_lineage if c["match_type"] != "not_found")
    match_type_counts = {}
    for c in column_lineage:
        mt = c["match_type"]
        match_type_counts[mt] = match_type_counts.get(mt, 0) + 1

    sources_matched = len({c["origin"] for c in column_lineage if c["origin"]})

    metrics = {
        "output_columns": total_cols,
        "columns_traced": traced_cols,
        "columns_untraced": total_cols - traced_cols,
        "upstream_sources_matched": sources_matched,
        "exact_matches": match_type_counts.get("exact", 0),
        "transformed_values": match_type_counts.get("transformed", 0),
        "coerced_values": match_type_counts.get("coerced", 0),
        "null_filled_values": match_type_counts.get("null_filled", 0),
    }

    # ── Build subject ───────────────────────────────────────────────────
    subject = ", ".join(f"{k}={v!r}" for k, v in values.items())

    # ── Build summary, findings, risks, actions ─────────────────────────
    summary = _build_summary(subject, column_lineage, upstream)
    findings = _build_findings(column_lineage)
    risks = _build_risks(column_lineage)
    suggested_next_actions = _build_suggested_actions(column_lineage, keys, upstream)

    # ── Build upstream sample rows ──────────────────────────────────────
    upstream_rows = {}
    for name, up_df in upstream.items():
        join_keys_found = _find_matching_keys(up_df, keys, values, engine)
        if join_keys_found:
            join_values = {k: values.get(k) for k in join_keys_found}
            row = _extract_row(up_df, join_keys_found, join_values, engine)
            if row:
                upstream_rows[name] = row

    samples = {
        "output_row": output_row,
        "upstream_rows": upstream_rows,
    }

    # ── Assemble contract ───────────────────────────────────────────────
    ctx = build_base_context(
        kind="explain_row",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples=samples,
        suggested_next_actions=suggested_next_actions,
        column_lineage=column_lineage,
        join_paths=join_paths,
    )

    return finalize_context(ctx, output_format, render_explain_row_report)
