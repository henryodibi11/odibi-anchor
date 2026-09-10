"""Coerce fix tool — apply automatic fixes based on coerce_check classification.

Entry point: coerce_fix_context(df, coerce_ctx, ...)
Takes output from anchor("coerce_check") and applies the appropriate fix for each
classified column (whitespace, case, unicode, numeric_representation, date_format).
Skips "genuine" mismatches by default.

Pandas-first implementation (matching coerce_check engine support).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from odibi_anchor._utils.contract import (
    build_base_context,
    finalize_context,
    guard_dataframe_type,
    validate_output_format,
)
from odibi_anchor._utils.engine_utils import detect_engine
from odibi_anchor._utils.render_utils import (
    render_bullet_section,
    render_header_lines,
    render_metrics_lines,
    render_table,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad]")

_FIXABLE_CATEGORIES = frozenset(
    {"whitespace", "case", "whitespace+case", "unicode", "numeric_representation", "date_format"}
)

_OPERATION_LABELS: dict[str, str] = {
    "whitespace": "TRIM + collapse whitespace",
    "case_upper": "UPPER",
    "case_lower": "LOWER",
    "whitespace+case_upper": "TRIM + collapse whitespace + UPPER",
    "whitespace+case_lower": "TRIM + collapse whitespace + LOWER",
    "unicode": "strip zero-width chars + NFC normalize",
    "numeric_representation": "remove commas/spaces",
    "date_format": "parse to ISO date",
}


# ── Pandas fix functions ─────────────────────────────────────────────────────


def _fix_whitespace(series):
    """Strip leading/trailing whitespace and collapse internal runs."""
    import pandas as pd

    return (
        series.astype(str)
        .where(series.notna(), other=pd.NA)
        .str.strip()
        .str.replace(r"\s+", " ", regex=True)
    )


def _fix_case(series, target: str = "upper"):
    """Normalize case to upper or lower."""
    import pandas as pd

    s = series.astype(str).where(series.notna(), other=pd.NA)
    if target == "upper":
        return s.str.upper()
    return s.str.lower()


def _fix_unicode(series):
    """Strip zero-width characters and normalize to NFC."""
    import pandas as pd

    def _clean(val):
        if pd.isna(val):
            return val
        cleaned = _INVISIBLE_CHARS_RE.sub("", str(val))
        return unicodedata.normalize("NFC", cleaned)

    return series.map(_clean)


def _fix_numeric(series):
    """Normalize numeric strings: remove commas/spaces and trailing .0 suffix."""
    import pandas as pd

    def _normalize_numeric(val):
        if pd.isna(val):
            return val
        s = str(val)
        # Strip commas and spaces
        s = re.sub(r"[,\s]", "", s)
        # Remove trailing .0 for integer-like floats (e.g. "2026.0" → "2026")
        try:
            f = float(s)
            if f == int(f) and "." in s:
                s = str(int(f))
        except (ValueError, OverflowError):
            pass
        return s

    return series.map(_normalize_numeric).where(series.notna(), other=pd.NA)


def _fix_date(series, target_fmt: str = "%Y-%m-%d"):
    """Parse dates flexibly and format to target."""
    import pandas as pd

    def _parse_date(val):
        if pd.isna(val) or str(val).strip() == "":
            return val
        try:
            return pd.to_datetime(str(val)).strftime(target_fmt)
        except (ValueError, TypeError):
            return val  # Leave unparseable values unchanged

    return series.map(_parse_date)


# ── Fix dispatcher ───────────────────────────────────────────────────────────


def _apply_fix_pandas(
    series,
    category: str,
    case_target: str = "upper",
    date_target: str = "%Y-%m-%d",
):
    """Apply the appropriate fix function for a category.

    Args:
        series: Pandas Series to fix.
        category: The coerce_check classification category.
        case_target: "upper" or "lower" for case fixes.
        date_target: strftime format for date fixes.

    Returns:
        Fixed pandas Series.

    Raises:
        ValueError: If category is not fixable.
    """
    if category == "whitespace":
        return _fix_whitespace(series)
    elif category == "case":
        return _fix_case(series, target=case_target)
    elif category == "whitespace+case":
        return _fix_case(_fix_whitespace(series), target=case_target)
    elif category == "unicode":
        return _fix_unicode(series)
    elif category == "numeric_representation":
        return _fix_numeric(series)
    elif category == "date_format":
        return _fix_date(series, target_fmt=date_target)
    else:
        raise ValueError(
            f"Cannot fix category: {category!r}. "
            "Check column_results from anchor('coerce_check') for valid category names "
            "— see anchor('help', 'coerce_fix')."
        )


# ── Sampling ─────────────────────────────────────────────────────────────────


def _capture_samples(
    before_series, after_series, n: int = 5
) -> dict[str, list[str]]:
    """Capture before/after sample values where they differ.

    Args:
        before_series: Original Series values.
        after_series: Fixed Series values.
        n: Number of samples to capture.

    Returns:
        Dict with "before" and "after" lists.
    """
    import pandas as pd

    # Find rows that actually changed
    both_present = before_series.notna() & after_series.notna()
    before_str = before_series[both_present].astype(str)
    after_str = after_series[both_present].astype(str)
    changed_mask = before_str != after_str

    before_changed = before_str[changed_mask].head(n).tolist()
    after_changed = after_str[changed_mask].head(n).tolist()

    return {"before": before_changed, "after": after_changed}


# ── Main context function ────────────────────────────────────────────────────


def coerce_fix_context(
    *args,
    df=None,
    coerce_ctx: dict | None = None,
    columns: list[str] | None = None,
    case_target: str = "upper",
    date_target: str = "%Y-%m-%d",
    dry_run: bool = False,
    subject: str | None = None,
    sample_limit: int = 5,
    output_format: str = "dict",
    **kwargs,
) -> dict[str, Any] | str:
    """Apply coercion fixes based on coerce_check classification.

    Args:
        df: The DataFrame to fix (typically the 'new' side from coerce_check).
        coerce_ctx: Output from anchor("coerce_check"). Provides per-column classification.
        columns: Limit fixes to specific columns. Default: all non-genuine columns.
        case_target: Target case for case fixes. "upper" or "lower". Default "upper".
        date_target: Target date format string. Default "%Y-%m-%d" (ISO).
        dry_run: If True, return fix plan without applying. Default False.
        subject: Human-readable label. Auto-generated if omitted.
        sample_limit: Max before/after samples per column.
        output_format: "dict" or "markdown".

    Returns:
        Context dict or markdown string.

    Raises:
        ValueError: If output_format is invalid or required args missing.
    """
    validate_output_format(output_format)

    # ── Handle positional args ───────────────────────────────────────────
    if len(args) >= 1 and df is None:
        df = args[0]
    if len(args) >= 2 and coerce_ctx is None:
        coerce_ctx = args[1]

    # ── Validation ───────────────────────────────────────────────────────
    if df is None:
        raise ValueError("df is required — pass the DataFrame to fix.")
    guard_dataframe_type(df, "df")
    if coerce_ctx is None:
        raise ValueError(
            "coerce_ctx is required — pass output from anchor(\"coerce_check\")."
        )
    if not isinstance(coerce_ctx, dict):
        raise ValueError("coerce_ctx must be a dict (output from coerce_check).")
    if "column_results" not in coerce_ctx:
        raise ValueError(
            "coerce_ctx missing 'column_results' key. "
            "Ensure it is the output from anchor(\"coerce_check\")."
        )
    if case_target not in {"upper", "lower"}:
        raise ValueError(f"case_target must be 'upper' or 'lower', got {case_target!r}")

    engine = detect_engine(df)
    if engine != "pandas":
        raise NotImplementedError(
            f"coerce_fix currently supports pandas only (got {engine}). "
            "Spark support planned for future release."
        )

    # ── Determine columns to fix ─────────────────────────────────────────
    column_results = coerce_ctx["column_results"]

    if columns is not None:
        target_cols = [c for c in columns if c in column_results]
    else:
        target_cols = list(column_results.keys())

    # ── Build fix plan ───────────────────────────────────────────────────
    fixes_planned: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for col in target_cols:
        cr = column_results[col]
        dominant = cr.get("dominant_category", "genuine")

        if dominant == "identical" or cr.get("total_mismatches", 0) == 0:
            continue

        if dominant == "genuine":
            skipped.append({
                "column": col,
                "category": "genuine",
                "reason": "genuine differences — not a formatting issue",
            })
            continue

        if dominant not in _FIXABLE_CATEGORIES:
            skipped.append({
                "column": col,
                "category": dominant,
                "reason": f"unknown category: {dominant}",
            })
            continue

        operation = _OPERATION_LABELS.get(
            f"case_{case_target}" if dominant == "case" else dominant,
            dominant,
        )
        fixes_planned.append({
            "column": col,
            "category": dominant,
            "operation": operation,
        })

    # ── Dry run: return plan only ────────────────────────────────────────
    if dry_run:
        auto_subject = subject or "coerce_fix (dry run)"
        plan_summary = (
            f"Dry run: {len(fixes_planned)} columns to fix, "
            f"{len(skipped)} skipped."
        )
        ctx = build_base_context(
            kind="coerce_fix",
            subject=auto_subject,
            summary=plan_summary,
            metrics={
                "columns_to_fix": len(fixes_planned),
                "columns_skipped": len(skipped),
                "dry_run": True,
            },
            fixes_planned=fixes_planned,
            skipped=skipped,
            suggested_next_actions=[
                "Review the plan above.",
                "Re-run without dry_run=True to apply fixes.",
            ],
        )
        return finalize_context(ctx, output_format, render_coerce_fix_report)

    # ── Apply fixes ──────────────────────────────────────────────────────
    import pandas as pd

    fixed_df = df.copy()
    fixes_applied: list[dict[str, Any]] = []
    all_samples: dict[str, dict[str, Any]] = {}
    total_values_corrected = 0
    by_category: dict[str, int] = {}

    for fix_item in fixes_planned:
        col = fix_item["column"]
        category = fix_item["category"]
        operation = fix_item["operation"]

        if col not in fixed_df.columns:
            skipped.append({
                "column": col,
                "category": category,
                "reason": f"column '{col}' not found in DataFrame",
            })
            continue

        before = fixed_df[col].copy()

        # Apply the fix
        fixed_df[col] = _apply_fix_pandas(
            fixed_df[col],
            category=category,
            case_target=case_target,
            date_target=date_target,
        )

        after = fixed_df[col]

        # Count affected rows (where values actually changed)
        both_present = before.notna() & after.notna()
        before_str = before[both_present].astype(str)
        after_str = after[both_present].astype(str)
        rows_affected = int((before_str != after_str).sum())

        total_values_corrected += rows_affected
        by_category[category] = by_category.get(category, 0) + rows_affected

        fixes_applied.append({
            "column": col,
            "category": category,
            "operation": operation,
            "rows_affected": rows_affected,
        })

        # Capture samples
        sample_record = {
            "category": category,
            **_capture_samples(before, after, n=sample_limit),
        }
        all_samples[col] = sample_record

        logger.info(
            "coerce_fix: %s (%s) — %d rows affected",
            col, category, rows_affected,
        )

    # ── Build output contract ────────────────────────────────────────────
    auto_subject = subject or "coerce_fix"
    cols_fixed = len(fixes_applied)
    cols_skipped = len(skipped)

    # Detect "wrong side" scenario: fixes planned but nothing changed
    zero_effect_cols = [
        fa["column"] for fa in fixes_applied if fa["rows_affected"] == 0
    ]
    risks: list[str] = []
    if zero_effect_cols and total_values_corrected == 0:
        risks.append(
            "⚠ All fixes produced 0 changes. You may be fixing the wrong "
            "DataFrame. coerce_check compares old↔new — try passing the OTHER "
            "side to coerce_fix (the one that has the formatting issues)."
        )
    elif zero_effect_cols:
        for zc in zero_effect_cols:
            risks.append(
                f"'{zc}' had 0 rows affected — the formatting issue may be "
                f"on the other DataFrame. Try: coerce_fix(other_df, coerce_ctx, "
                f"columns=['{zc}'])"
            )

    # Build human-readable summary
    fix_descs = [
        f"{fa['column']} ({fa['category']}→{fa['operation']})"
        for fa in fixes_applied
    ]
    summary = (
        f"Fixed {cols_fixed} column(s): {', '.join(fix_descs)}. "
        f"{total_values_corrected:,} values corrected."
        if fixes_applied
        else "No fixes applied."
    )

    # Suggested next actions
    actions: list[str] = []
    if fixes_applied:
        actions.append(
            "Verify fixes: anchor('diff', old_df, fixed_df, keys=[...])"
        )
        actions.append(
            "Re-run coerce_check: anchor('coerce_check', old_df, fixed_df, "
            "keys=[...], columns=[...])"
        )
    if skipped:
        genuine_cols = [s["column"] for s in skipped if s["category"] == "genuine"]
        if genuine_cols:
            actions.append(
                f"{len(genuine_cols)} column(s) skipped (genuine): "
                f"manually review {genuine_cols}"
            )

    ctx = build_base_context(
        kind="coerce_fix",
        subject=auto_subject,
        summary=summary,
        metrics={
            "columns_fixed": cols_fixed,
            "columns_skipped_genuine": sum(
                1 for s in skipped if s["category"] == "genuine"
            ),
            "total_values_corrected": total_values_corrected,
            "by_category": by_category,
        },
        findings=[
            f"{fa['column']}: {fa['operation']} ({fa['rows_affected']} rows)"
            for fa in fixes_applied
        ],
        risks=risks,
        samples=all_samples,
        suggested_next_actions=actions,
        fixes_applied=fixes_applied,
        skipped=skipped,
        df=fixed_df,
    )

    return finalize_context(ctx, output_format, render_coerce_fix_report)


# ── Markdown renderer ────────────────────────────────────────────────────────


def render_coerce_fix_report(ctx: dict[str, Any]) -> str:
    """Render a coerce_fix context dict as structured markdown.

    Args:
        ctx: The dictionary returned by coerce_fix_context().

    Returns:
        Markdown-formatted string.
    """
    lines: list[str] = []

    lines.extend(render_header_lines(ctx, "Coerce Fix"))
    lines.extend(
        render_metrics_lines(ctx["metrics"], exclude={"by_category"})
    )

    # By-category breakdown
    by_cat = ctx["metrics"].get("by_category", {})
    if by_cat:
        rows = [[cat, str(count)] for cat, count in sorted(by_cat.items())]
        lines.append("")
        lines.append("## Fixes by Category")
        lines.append("")
        lines.extend(render_table(rows, ["Category", "Rows Affected"]))

    # Fixes applied
    fixes = ctx.get("fixes_applied", [])
    if fixes:
        lines.append("")
        lines.append("## Fixes Applied")
        lines.append("")
        fix_rows = [
            [f["column"], f["category"], f["operation"], str(f.get("rows_affected", "—"))]
            for f in fixes
        ]
        lines.extend(render_table(fix_rows, ["Column", "Category", "Operation", "Rows"]))

    # Planned (dry run)
    planned = ctx.get("fixes_planned", [])
    if planned:
        lines.append("")
        lines.append("## Fixes Planned (Dry Run)")
        lines.append("")
        plan_rows = [
            [p["column"], p["category"], p["operation"]]
            for p in planned
        ]
        lines.extend(render_table(plan_rows, ["Column", "Category", "Operation"]))

    # Skipped
    skipped = ctx.get("skipped", [])
    if skipped:
        lines.append("")
        lines.append("## Skipped")
        lines.append("")
        skip_rows = [
            [s["column"], s["category"], s["reason"]]
            for s in skipped
        ]
        lines.extend(render_table(skip_rows, ["Column", "Category", "Reason"]))

    # Samples
    samples = ctx.get("samples", {})
    if samples and any(s.get("before") for s in samples.values()):
        lines.append("")
        lines.append("## Before / After Samples")
        for col, col_samples in samples.items():
            before = col_samples.get("before", [])
            after = col_samples.get("after", [])
            if not before:
                continue
            lines.append("")
            lines.append(f"### {col} ({col_samples.get('category', '?')})")
            lines.append("")
            sample_rows = [
                [b, a] for b, a in zip(before, after)
            ]
            lines.extend(render_table(sample_rows, ["Before", "After"]))

    lines.extend(
        render_bullet_section(ctx.get("findings", []), "## Findings")
    )
    lines.extend(
        render_bullet_section(
            ctx.get("suggested_next_actions", []),
            "## Suggested Next Actions",
        )
    )

    return "\n".join(lines)
