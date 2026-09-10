"""Suggest validation rules from profile output.

Entry point: suggest_rules_context(profile_ctx=..., df=..., ...)
Generates validation rules in the exact format accepted by anchor("validate").

Usage:
    # From profile output:
    profile = anchor("profile_table", df, output_format="dict")
    suggested = anchor("suggest_rules", profile_ctx=profile)
    anchor("validate", df, rules=suggested["rules"])

    # From raw DataFrame (auto-profiles):
    suggested = anchor("suggest_rules", df=df)
"""
from __future__ import annotations

import logging
import math
from typing import Any, cast

from tools.table_profiler_tool.lib.profiler import profile_table
from tools.table_profiler_tool.lib.contract import serialize_profile

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
# Constants
# ============================================================================

ACCEPTED_VALUES_MAX_DISTINCT = 20
DATE_COLUMN_KEYWORDS = {"date", "time", "timestamp", "dt", "datetime", "created", "updated", "modified"}
AUDIT_COLUMN_PATTERNS = {"_extracted_at", "_source_file", "_row_hash", "_loaded_at", "_ingested_at"}

STRICTNESS_CONFIG = {
    "strict": {"null_tolerance": 0.0, "range_margin": 0.0, "min_null_pct_for_skip": 0.0},
    "standard": {"null_tolerance": 0.0, "range_margin": 0.10, "min_null_pct_for_skip": 0.0},
    "lenient": {"null_tolerance": 0.05, "range_margin": 0.50, "min_null_pct_for_skip": 0.05},
}

VALID_RULE_TYPES = frozenset({"not_null", "unique", "accepted_values", "range", "expression", "type_check"})


# ============================================================================
# Public API
# ============================================================================


def suggest_rules_context(
    *args,
    profile_ctx: dict | None = None,
    df: Any = None,
    subject: str | None = None,
    strictness: str = "standard",
    include_types: list[str] | None = None,
    exclude_columns: list[str] | None = None,
    output_format: str = "dict",
    **kwargs,
) -> dict[str, Any] | str:
    """Generate validation rules from profile output or raw DataFrame.

    Args:
        profile_ctx: Output from anchor("profile_table"). If provided, skips re-profiling.
        df: Raw DataFrame. If provided without profile_ctx, profiles it first.
        subject: Table name for display. Inferred from profile_ctx if not given.
        strictness: "strict" (0% tolerance), "standard" (small margin), "lenient" (wide margin).
        include_types: Rule types to include. Default: all.
            Options: not_null, unique, accepted_values, range, expression, type_check.
        exclude_columns: Columns to skip (e.g., audit columns like _extracted_at).
        output_format: "dict" or "markdown".

    Returns:
        Dictionary with rules in anchor("validate") format, or markdown string.

    Raises:
        ValueError: If neither profile_ctx nor df is provided, or strictness is invalid.
    """
    validate_output_format(output_format)

    if strictness not in STRICTNESS_CONFIG:
        raise ValueError(f"strictness must be one of: {list(STRICTNESS_CONFIG.keys())}")

    if include_types is not None:
        invalid = set(include_types) - VALID_RULE_TYPES
        if invalid:
            raise ValueError(f"Invalid include_types: {invalid}. Valid: {sorted(VALID_RULE_TYPES)}")

    # Resolve profile context
    if profile_ctx is None and df is None:
        # Check positional args
        if args and isinstance(args[0], dict) and args[0].get("kind") in ("dataset_profile_context", "profile_table"):
            profile_ctx = args[0]
        elif args:
            df = args[0]
        else:
            raise ValueError("Must provide profile_ctx (from anchor('profile_table')) or df (raw DataFrame).")

    if profile_ctx is None:
        # profile_table returns a TableProfile dataclass; serialize it to the
        # dict contract (column_profiles, subject, ...) that suggest_rules
        # consumes — same shape as anchor('profile_table', df, output_format='dict').
        generated_profile = profile_table(df, subject=subject or "dataframe")
        profile_ctx = serialize_profile(generated_profile)
        if not isinstance(profile_ctx, dict):
            raise TypeError(
                "profile_ctx must be a dict (from anchor('profile_table', df, output_format='dict')). "
                "Got a non-dict value — re-run profiling with output_format='dict' "
                "— see anchor('help', 'suggest_rules')."
            )

    if not isinstance(profile_ctx, dict):
        raise TypeError(
            f"profile_ctx must resolve to a dict, got {type(profile_ctx).__name__}. "
            "Pass output from anchor('profile_table', df, output_format='dict') "
            "— see anchor('help', 'suggest_rules')."
        )
    profile_ctx = cast(dict[str, Any], profile_ctx)

    try:
        # Resolve subject
        if subject is None:
            raw_subject = profile_ctx.get("subject", "dataframe")
            subject = raw_subject if isinstance(raw_subject, str) else "dataframe"

        # Get column profiles
        column_profiles = profile_ctx.get("column_profiles", {})
        if not column_profiles:
            raise ValueError(
            "profile_ctx has no column_profiles — cannot infer rules. "
            "Ensure you pass a full profile context from anchor('profile_table', df, output_format='dict'). "
            "The profile must include the column_profiles key — see anchor('help', 'suggest_rules')."
        )

        # Apply exclude_columns
        exclude_set = set(exclude_columns or [])
        # Also auto-exclude audit columns
        for col in list(column_profiles.keys()):
            col_lower = col.lower()
            if col_lower in AUDIT_COLUMN_PATTERNS or col in exclude_set:
                exclude_set.add(col)

        active_profiles = {k: v for k, v in column_profiles.items() if k not in exclude_set}

        # Infer rules
        config = STRICTNESS_CONFIG[strictness]
        rules: list[dict[str, Any]] = []
        findings: list[str] = []
        risks: list[str] = []

        if _should_include("not_null", include_types):
            not_null_rules, not_null_findings = _infer_not_null(active_profiles, config)
            rules.extend(not_null_rules)
            findings.extend(not_null_findings)

        if _should_include("unique", include_types):
            unique_rules, unique_findings = _infer_unique(active_profiles, profile_ctx)
            rules.extend(unique_rules)
            findings.extend(unique_findings)

        if _should_include("accepted_values", include_types):
            av_rules, av_findings, av_risks = _infer_accepted_values(active_profiles, config)
            rules.extend(av_rules)
            findings.extend(av_findings)
            risks.extend(av_risks)

        if _should_include("range", include_types):
            range_rules, range_findings = _infer_range(active_profiles, config)
            rules.extend(range_rules)
            findings.extend(range_findings)

        if _should_include("expression", include_types):
            expr_rules, expr_findings = _infer_expressions(active_profiles)
            rules.extend(expr_rules)
            findings.extend(expr_findings)

        # Build metrics
        by_type: dict[str, int] = {}
        for r in rules:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1

        avg_confidence = (
            sum(r.get("_confidence", 0.5) for r in rules) / len(rules)
            if rules else 0.0
        )

        columns_covered = set()
        for r in rules:
            if "columns" in r:
                columns_covered.update(r["columns"])
            elif "column" in r:
                columns_covered.add(r["column"])

        if exclude_set:
            findings.append(f"Skipped {len(exclude_set)} columns: {', '.join(sorted(exclude_set))}")

        metrics = {
            "total_rules": len(rules),
            "by_type": by_type,
            "avg_confidence": round(avg_confidence, 2),
            "columns_covered": len(columns_covered),
            "columns_skipped": len(exclude_set),
            "strictness": strictness,
        }

        ctx = build_base_context(
            kind="suggest_rules",
            subject=subject,
            summary=_build_summary(subject, metrics),
            metrics=metrics,
        )
        ctx["rules"] = rules
        ctx["findings"] = findings
        ctx["risks"] = risks
        ctx["suggested_next_actions"] = [
            "Review rules and adjust: remove rules that don't match business expectations",
            "Run validation: anchor('validate', df, rules=suggested['rules'])",
            "Add domain-specific rules manually (business logic the profiler can't infer)",
        ]
    except (KeyError, TypeError, AttributeError) as exc:
        logger.exception("suggest_rules failed while inferring validation rules")
        ctx = build_base_context(
            kind="suggest_rules",
            subject=subject or "dataframe",
            summary=f"ERROR: failed to infer validation rules for {subject or 'dataframe'}",
            metrics={"error_type": type(exc).__name__},
            findings=[],
            risks=[f"{type(exc).__name__}: {exc}"],
            samples={},
            suggested_next_actions=[
                "Inspect profile_ctx structure and ensure column_profiles contains the expected nested fields.",
                "Re-run anchor('profile_table', df, output_format='dict') and retry anchor('suggest_rules', profile_ctx=profile_ctx).",
            ],
            rules=[],
            error=str(exc),
        )

    return finalize_context(ctx, output_format, render_suggest_rules_report)


# ============================================================================
# Rule inference engines
# ============================================================================


def _should_include(rule_type: str, include_types: list[str] | None) -> bool:
    """Check if a rule type should be included."""
    if include_types is None:
        return True
    return rule_type in include_types


def _infer_not_null(
    profiles: dict[str, dict], config: dict
) -> tuple[list[dict], list[str]]:
    """Infer not_null rules for columns with 0% nulls."""
    tolerance = config["null_tolerance"]
    not_null_cols = []
    for col, p in profiles.items():
        null_pct = p.get("null_pct", 0.0)
        if null_pct <= tolerance:
            not_null_cols.append(col)

    rules: list[dict] = []
    findings: list[str] = []
    if not_null_cols:
        rules.append({
            "type": "not_null",
            "columns": not_null_cols,
            "_confidence": 0.9,
            "_reason": f"0% null in profile (tolerance: {tolerance:.0%})",
        })
        findings.append(
            f"{len(not_null_cols)} columns are fully populated -> not_null rule"
        )
    return rules, findings


def _infer_unique(
    profiles: dict[str, dict], profile_ctx: dict
) -> tuple[list[dict], list[str]]:
    """Infer unique rules for candidate key columns."""
    potential_keys = profile_ctx.get("metrics", {}).get("potential_key_columns", [])

    unique_cols = []
    for col, p in profiles.items():
        if p.get("is_unique", False) or col in potential_keys:
            if p.get("null_pct", 1.0) == 0.0 and p.get("distinct_pct", 0.0) >= 1.0:
                unique_cols.append(col)

    rules: list[dict] = []
    findings: list[str] = []
    for col in unique_cols:
        rules.append({
            "type": "unique",
            "columns": [col],
            "_confidence": 0.85,
            "_reason": "100% distinct, 0% null — candidate key",
        })
    if unique_cols:
        findings.append(
            f"{len(unique_cols)} candidate key column(s): {', '.join(unique_cols)}"
        )
    return rules, findings


def _infer_accepted_values(
    profiles: dict[str, dict], config: dict
) -> tuple[list[dict], list[str], list[str]]:
    """Infer accepted_values rules for low-cardinality categoricals."""
    rules: list[dict] = []
    findings: list[str] = []
    risks: list[str] = []

    for col, p in profiles.items():
        distinct_count = p.get("distinct_count", 999)
        inferred_type = p.get("inferred_type", "")

        if distinct_count > ACCEPTED_VALUES_MAX_DISTINCT:
            continue
        if distinct_count < 2:
            continue
        if any(kw in inferred_type for kw in ("numeric", "integer", "float", "decimal")):
            continue

        top_values = p.get("top_values", [])
        if not top_values:
            continue

        values = [tv["value"] for tv in top_values if tv.get("value") is not None]
        if not values:
            continue

        # Only emit if we have reasonable coverage of distinct values
        if len(values) < distinct_count and distinct_count <= ACCEPTED_VALUES_MAX_DISTINCT:
            if len(values) < distinct_count * 0.8:
                continue

        rules.append({
            "type": "accepted_values",
            "column": col,
            "values": sorted(values, key=str),
            "_confidence": 0.7,
            "_reason": f"{distinct_count} distinct values, categorical",
        })

        if distinct_count > 10:
            risks.append(
                f"accepted_values for '{col}' has {distinct_count} values — may need updating if new values are introduced"
            )

    if rules:
        findings.append(
            f"{len(rules)} categorical column(s) with <={ACCEPTED_VALUES_MAX_DISTINCT} distinct values"
        )
    return rules, findings, risks


def _infer_range(
    profiles: dict[str, dict], config: dict
) -> tuple[list[dict], list[str]]:
    """Infer range rules for numeric columns."""
    margin = config["range_margin"]
    rules: list[dict] = []
    findings: list[str] = []

    for col, p in profiles.items():
        inferred_type = p.get("inferred_type", "")
        if not any(kw in inferred_type for kw in ("numeric", "integer", "float", "decimal")):
            continue

        stats = p.get("stats", {})
        min_val = stats.get("min")
        max_val = stats.get("max")

        if min_val is None or max_val is None:
            continue
        if not isinstance(min_val, (int, float)) or not isinstance(max_val, (int, float)):
            continue
        if math.isnan(min_val) or math.isnan(max_val):
            continue

        span = max_val - min_val
        if span == 0:
            continue

        range_min = min_val - abs(min_val) * margin if min_val != 0 else min_val - span * margin
        range_max = max_val + abs(max_val) * margin if max_val != 0 else max_val + span * margin

        if isinstance(min_val, int) and isinstance(max_val, int) and margin == 0:
            rule_min = int(min_val)
            rule_max = int(max_val)
        else:
            rule_min = round(range_min, 2)
            rule_max = round(range_max, 2)

        if min_val >= 0:
            rule_min = max(0, rule_min)

        rules.append({
            "type": "range",
            "column": col,
            "min": rule_min,
            "max": rule_max,
            "_confidence": 0.6,
            "_reason": f"observed range [{min_val}, {max_val}], {margin:.0%} margin applied",
        })

    if rules:
        findings.append(f"{len(rules)} numeric column(s) with range rules")
    return rules, findings


def _infer_expressions(
    profiles: dict[str, dict],
) -> tuple[list[dict], list[str]]:
    """Infer expression rules for date ordering patterns."""
    rules: list[dict] = []
    findings: list[str] = []

    date_cols = []
    for col, p in profiles.items():
        inferred_type = p.get("inferred_type", "")
        if inferred_type in ("datetime", "date", "timestamp"):
            date_cols.append(col)
        elif any(kw in col.lower() for kw in DATE_COLUMN_KEYWORDS):
            if inferred_type not in ("numeric", "integer", "float", "decimal"):
                date_cols.append(col)

    if len(date_cols) >= 2:
        pairs = _find_date_ordering_pairs(date_cols)
        for early_col, late_col in pairs:
            rules.append({
                "type": "expression",
                "expr": f"`{early_col}` <= `{late_col}`",
                "description": f"{early_col} before {late_col}",
                "_confidence": 0.75,
                "_reason": "date column pair with temporal ordering",
            })
            findings.append(f"Date ordering: {early_col} <= {late_col}")

    return rules, findings


def _find_date_ordering_pairs(date_cols: list[str]) -> list[tuple[str, str]]:
    """Find likely date ordering pairs from column names."""
    pairs: list[tuple[str, str]] = []
    col_lower_map = {c.lower(): c for c in date_cols}

    ordering_patterns = [
        ("start", "end"),
        ("begin", "end"),
        ("created", "updated"),
        ("created", "modified"),
        ("created", "completed"),
        ("created", "closed"),
        ("queue", "completion"),
        ("queued", "completed"),
        ("open", "close"),
        ("opened", "closed"),
        ("submitted", "approved"),
        ("submitted", "completed"),
        ("effective", "expiration"),
    ]

    for early_pattern, late_pattern in ordering_patterns:
        for col_lower, col_orig in col_lower_map.items():
            if early_pattern in col_lower:
                for other_lower, other_orig in col_lower_map.items():
                    if other_lower != col_lower and late_pattern in other_lower:
                        if _columns_related(col_lower, other_lower, early_pattern, late_pattern):
                            pairs.append((col_orig, other_orig))

    seen = set()
    unique_pairs = []
    for p in pairs:
        key = (p[0], p[1])
        if key not in seen:
            seen.add(key)
            unique_pairs.append(p)

    return unique_pairs


def _columns_related(col1: str, col2: str, pattern1: str, pattern2: str) -> bool:
    """Check if two columns are related (share context beyond the pattern)."""
    remainder1 = col1.replace(pattern1, "").strip("_")
    remainder2 = col2.replace(pattern2, "").strip("_")

    if remainder1 == remainder2:
        return True
    if not remainder1 or not remainder2:
        return True
    if remainder1 in remainder2 or remainder2 in remainder1:
        return True
    return False


# ============================================================================
# Summary builder
# ============================================================================


def _build_summary(subject: str, metrics: dict) -> str:
    """Build a one-line summary."""
    total = metrics["total_rules"]
    by_type = metrics["by_type"]
    parts = [f"{v} {k}" for k, v in sorted(by_type.items())]
    type_str = ", ".join(parts) if parts else "none"
    return f"Generated {total} validation rules from profile ({type_str})"


# ============================================================================
# Renderer
# ============================================================================


def render_suggest_rules_report(ctx: dict[str, Any]) -> str:
    """Render suggest_rules context as markdown with copy-paste code block."""
    lines = render_header_lines(ctx, "Suggest Rules")
    lines.extend(render_metrics_lines(ctx.get("metrics", {})))

    rules = ctx.get("rules", [])
    if rules:
        lines.append("")
        lines.append("## Suggested Rules")
        lines.append("")
        table_data = []
        for i, r in enumerate(rules, 1):
            rule_type = r["type"]
            target = ", ".join(r.get("columns", [r.get("column", "")]))
            confidence = r.get("_confidence", "—")
            reason = r.get("_reason", "—")
            table_data.append([str(i), rule_type, target, f"{confidence}", reason])

        lines.extend(render_table(
            table_data,
            ["#", "Type", "Column(s)", "Confidence", "Reason"],
        ))

        lines.append("")
        lines.append("## Ready-to-Use Rules")
        lines.append("")
        lines.append("```python")
        lines.append("# Copy-paste into anchor('validate', df, rules=rules)")
        lines.append("rules = [")
        for r in rules:
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            lines.append(f"    {clean!r},")
        lines.append("]")
        lines.append("")
        lines.append("# Run validation:")
        lines.append("anchor('validate', df, rules=rules)")
        lines.append("```")

    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))
    lines.extend(render_bullet_section(ctx.get("suggested_next_actions", []), "## Next Actions"))

    return "\n".join(lines)
