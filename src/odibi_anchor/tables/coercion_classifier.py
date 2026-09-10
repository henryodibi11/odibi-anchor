"""Coercion classifier — classify WHY string values differ between DataFrames.

Answers "what percentage of these mismatches are actually the same value
rendered differently?" by classifying each mismatch into categories like
whitespace, case, unicode, numeric_representation, date_format, or genuine.

Usage:
    from odibi_anchor.tables.coercion_classifier import (
        coercion_check_context,
        render_coercion_report,
    )

    ctx = coercion_check_context(old_df, new_df, keys=["id"])
    report = render_coercion_report(ctx)

    # Or inline:
    report = coercion_check_context(
        old_df, new_df, keys=["id"], output_format="markdown"
    )
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any

from odibi_anchor._utils.contract import (
    build_base_context,
    finalize_context,
    validate_output_format,
)
from odibi_anchor._utils.engine_utils import detect_engine
from odibi_anchor._utils.render_utils import (
    render_bullet_section,
    render_header_lines,
    render_metrics_lines,
    render_table,
)

# ── Constants ────────────────────────────────────────────────────────────────
_REPR_MAJORITY_THRESHOLD = 0.5  # Min representation % to classify a coercion type

_INVISIBLE_CHARS = frozenset("\u200b\u200c\u200d\ufeff\u00ad")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y%m%d",
)

_SUGGESTED_FIXES: dict[str, str] = {
    "whitespace": "TRIM() or strip whitespace before comparing",
    "case": "UPPER()/LOWER() both sides before comparing",
    "whitespace+case": "TRIM() + UPPER()/LOWER() — strip whitespace, then normalize case",
    "unicode": "Strip zero-width characters (U+200B, U+FEFF) before comparing",
    "numeric_representation": "CAST both sides AS INT (or FLOAT) before comparing",
    "date_format": "Parse both sides to DATE before comparing",
    "genuine": "Values are genuinely different — no coercion fix available",
}

_REPRESENTATION_CATEGORIES = frozenset(
    {"whitespace", "case", "whitespace+case", "unicode", "numeric_representation", "date_format"}
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _strip_invisible(s: str) -> str:
    """Remove zero-width / invisible unicode characters."""
    return "".join(ch for ch in s if ch not in _INVISIBLE_CHARS)


def _same_date_different_format(a: str, b: str) -> bool:
    """Try parsing both strings with multiple date formats and compare .date()."""
    parsed_a = None
    parsed_b = None
    for fmt in _DATE_FORMATS:
        if parsed_a is None:
            try:
                parsed_a = datetime.strptime(a.strip(), fmt).date()
            except ValueError:
                pass
        if parsed_b is None:
            try:
                parsed_b = datetime.strptime(b.strip(), fmt).date()
            except ValueError:
                pass
        if parsed_a is not None and parsed_b is not None:
            break
    if parsed_a is None or parsed_b is None:
        return False
    return parsed_a == parsed_b


def _classify_pair(old_val: str, new_val: str) -> str:
    """Classify a single value pair into a mismatch category.

    Categories checked in priority order:
        identical, whitespace, case, unicode, numeric_representation,
        date_format, genuine.

    Args:
        old_val: String value from the old DataFrame.
        new_val: String value from the new DataFrame.

    Returns:
        Category string.
    """
    if old_val == new_val:
        return "identical"

    # Whitespace: differ only in leading/trailing/internal whitespace
    if " ".join(old_val.split()) == " ".join(new_val.split()):
        return "whitespace"

    # Case: differ only in capitalization
    if old_val.lower() == new_val.lower():
        return "case"

    # Combined whitespace + case: differ in both padding and capitalization
    if " ".join(old_val.split()).lower() == " ".join(new_val.split()).lower():
        return "whitespace+case"

    # Unicode: differ only in invisible chars or normalization
    stripped_old = _strip_invisible(old_val)
    stripped_new = _strip_invisible(new_val)
    if stripped_old == stripped_new:
        return "unicode"
    if unicodedata.normalize("NFC", stripped_old) == unicodedata.normalize(
        "NFC", stripped_new
    ):
        return "unicode"

    # Numeric representation: same number, different format
    try:
        if float(old_val) == float(new_val):
            return "numeric_representation"
    except (ValueError, OverflowError):
        pass

    # Date format: same date, different format
    if _same_date_different_format(old_val, new_val):
        return "date_format"

    return "genuine"


# ── Main context function ────────────────────────────────────────────────────


def coercion_check_context(
    old_df: Any,
    new_df: Any,
    keys: list[str],
    columns: list[str] | None = None,
    *,
    subject: str | None = None,
    sample_limit: int = 20,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Classify why string values differ between two DataFrames.

    Joins old_df and new_df on *keys* (inner join), then for each
    column classifies every mismatch into a category.

    Args:
        old_df: Previous/source DataFrame.
        new_df: Current/target DataFrame.
        keys: Key columns for the inner join.
        columns: Columns to check. None = all non-key common columns.
        subject: Human-readable label. Auto-generated if omitted.
        sample_limit: Max samples per column.
        output_format: ``"dict"`` or ``"markdown"``.

    Returns:
        Context dict or markdown string.

    Raises:
        ValueError: If output_format is invalid.
    """
    validate_output_format(output_format)
    engine = detect_engine(old_df)

    if engine != "pandas":
        raise NotImplementedError(
            f"coercion_check_context only supports pandas for now (got {engine})"
        )

    import pandas as pd

    # Determine columns to check
    common_cols = [c for c in old_df.columns if c in set(new_df.columns)]
    key_set = set(keys)
    if columns is None:
        check_cols = [c for c in common_cols if c not in key_set]
    else:
        check_cols = [c for c in columns if c not in key_set]

    # Inner join
    merged = old_df.merge(new_df, on=keys, suffixes=("__old", "__new"), how="inner")

    column_results: dict[str, dict[str, Any]] = {}
    all_samples: dict[str, list[dict[str, str]]] = {}
    total_mismatches = 0
    total_representation = 0
    total_genuine = 0
    category_totals: Counter[str] = Counter()

    for col in check_cols:
        old_col = f"{col}__old"
        new_col = f"{col}__new"
        if old_col not in merged.columns or new_col not in merged.columns:
            continue

        raw_old = merged[old_col]
        raw_new = merged[new_col]

        # Only compare rows where BOTH values are non-null.
        # Null→value and value→null are null transitions, not coercion.
        both_present = raw_old.notna() & raw_new.notna()
        s_old = raw_old[both_present].astype(str)
        s_new = raw_new[both_present].astype(str)

        # Find rows where string representations differ
        diff_mask = s_old != s_new
        diff_old = s_old[diff_mask]
        diff_new = s_new[diff_mask]

        if len(diff_old) == 0:
            column_results[col] = {
                "total_mismatches": 0,
                "categories": {},
                "dominant_category": "identical",
                "confidence": 1.0,
                "suggested_fix": "No mismatches found",
            }
            all_samples[col] = []
            continue

        # Classify each pair
        categories: Counter[str] = Counter()
        col_samples: list[dict[str, str]] = []
        for ov, nv in zip(diff_old, diff_new):
            cat = _classify_pair(ov, nv)
            categories[cat] += 1
            if len(col_samples) < sample_limit:
                col_samples.append({"old": ov, "new": nv, "category": cat})

        col_total = sum(categories.values())
        dominant = categories.most_common(1)[0][0]
        confidence = categories[dominant] / col_total

        col_repr = sum(
            v for k, v in categories.items() if k in _REPRESENTATION_CATEGORIES
        )

        total_mismatches += col_total
        total_representation += col_repr
        total_genuine += categories.get("genuine", 0)
        category_totals.update(categories)

        column_results[col] = {
            "total_mismatches": col_total,
            "categories": dict(categories),
            "dominant_category": dominant,
            "confidence": confidence,
            "suggested_fix": _SUGGESTED_FIXES.get(dominant, ""),
        }
        all_samples[col] = col_samples

    # Build summary
    repr_pct = (
        total_representation / total_mismatches if total_mismatches > 0 else 0.0
    )
    auto_subject = subject or f"coercion check ({len(check_cols)} columns)"
    summary = (
        f"Classified {total_mismatches} value mismatches across "
        f"{len(check_cols)} columns. "
        f"{repr_pct:.0%} are representation issues."
    )

    # Build findings
    findings: list[str] = []
    for col, cr in column_results.items():
        if cr["total_mismatches"] == 0:
            continue
        findings.append(
            f"{col}: {cr['total_mismatches']} mismatches, "
            f"dominant={cr['dominant_category']} "
            f"({cr['confidence']:.0%} confidence)"
        )

    # Build risks
    risks: list[str] = []
    if total_genuine > 0:
        risks.append(
            f"{total_genuine} genuine mismatches found — these are real "
            f"data differences, not formatting."
        )
    if repr_pct > _REPR_MAJORITY_THRESHOLD:
        risks.append(
            "Majority of mismatches are representation issues. "
            "Raw string comparison will over-report differences."
        )

    # Build suggested next actions
    actions: list[str] = []
    dominant_cats = {
        cr["dominant_category"]
        for cr in column_results.values()
        if cr["total_mismatches"] > 0
    }
    for cat in sorted(dominant_cats):
        fix = _SUGGESTED_FIXES.get(cat)
        if fix:
            actions.append(fix)
    if total_mismatches > 0:
        actions.append(
            'Run anchor("diff", old_df, new_df, keys=[...]) after coercion '
            "fixes to see remaining genuine differences."
        )

    ctx = build_base_context(
        kind="coercion_check_context",
        subject=auto_subject,
        summary=summary,
        metrics={
            "columns_checked": len(check_cols),
            "total_mismatches": total_mismatches,
            "total_representation": total_representation,
            "total_genuine": total_genuine,
            "representation_pct": repr_pct,
            "category_totals": dict(category_totals),
        },
        findings=findings,
        risks=risks,
        samples=all_samples,
        suggested_next_actions=actions,
        column_results=column_results,
    )

    return finalize_context(ctx, output_format, render_coercion_report)


# ── Markdown renderer ────────────────────────────────────────────────────────


def render_coercion_report(ctx: dict[str, Any]) -> str:
    """Render a coercion-check context dict as structured markdown.

    Args:
        ctx: The dictionary returned by ``coercion_check_context()``.

    Returns:
        Markdown-formatted string.
    """
    lines: list[str] = []

    lines.extend(render_header_lines(ctx, "Coercion Check"))
    lines.extend(
        render_metrics_lines(ctx["metrics"], exclude={"category_totals"})
    )

    # Category totals
    cat_totals = ctx["metrics"].get("category_totals", {})
    if cat_totals:
        rows = [[cat, str(count)] for cat, count in sorted(cat_totals.items())]
        lines.append("")
        lines.append("## Category Totals")
        lines.append("")
        lines.extend(render_table(rows, ["Category", "Count"]))

    # Column results
    column_results = ctx.get("column_results", {})
    if column_results:
        lines.append("")
        lines.append("## Column Results")
        for col, cr in column_results.items():
            if cr["total_mismatches"] == 0:
                continue
            lines.append("")
            lines.append(f"### {col}")
            lines.append(
                f"- **Mismatches:** {cr['total_mismatches']}  "
                f"**Dominant:** {cr['dominant_category']}  "
                f"**Confidence:** {cr['confidence']:.0%}"
            )
            lines.append(f"- **Suggested fix:** {cr['suggested_fix']}")
            if cr.get("categories"):
                cat_rows = [
                    [cat, str(cnt)]
                    for cat, cnt in sorted(cr["categories"].items())
                ]
                lines.extend(render_table(cat_rows, ["Category", "Count"]))

    # Samples
    samples = ctx.get("samples", {})
    for col, col_samples in samples.items():
        if not col_samples:
            continue
        lines.append("")
        lines.append(f"### Samples: {col}")
        lines.append("")
        sample_rows = [
            [s["old"], s["new"], s["category"]] for s in col_samples[:5]
        ]
        lines.extend(render_table(sample_rows, ["Old", "New", "Category"]))

    lines.extend(
        render_bullet_section(ctx.get("findings", []), "## Findings")
    )
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))
    lines.extend(
        render_bullet_section(
            ctx.get("suggested_next_actions", []),
            "## Suggested Next Actions",
        )
    )

    return "\n".join(lines)
