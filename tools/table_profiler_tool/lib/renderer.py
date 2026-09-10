"""Renderer — convert TableProfile objects to human- and LLM-readable text.

Public API
----------
render_table_ai_summary(profile, max_cols=10) -> str
    Compact, token-efficient YAML-like summary for LLM context (~500 tokens).

render_table_profile_md(profile) -> str
    Full GFM markdown report with all profile sections.

Both functions are pure: they never mutate the profile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import TableProfile


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _pct(f: float) -> str:
    """Format a ratio [0-1] as a percentage string, e.g. 0.123 -> '12%'."""
    return f"{f:.0%}"


def _quality_emoji(score: float) -> str:
    """Return an emoji badge for the given quality score."""
    if score >= 0.90:
        return "✅"
    if score >= 0.70:
        return "⚠️"
    return "❌"


def _truncate(s: str, n: int) -> str:
    """Truncate *s* to at most *n* characters, appending '…' if truncated."""
    return s if len(s) <= n else s[: n - 1] + "…"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Build a GFM pipe-table string.

    Every row must have the same number of cells as *headers*.
    """
    ncols = len(headers)
    sep = " | ".join("---" for _ in range(ncols))
    header_row = " | ".join(str(h) for h in headers)
    lines = [f"| {header_row} |", f"| {sep} |"]
    for row in rows:
        cells = " | ".join(str(c) for c in row)
        lines.append(f"| {cells} |")
    return "\n".join(lines)


def _display_value(value: object) -> str:
    """Render enum-backed or composite values compactly for reports."""

    if isinstance(value, list):
        return "+".join(_display_value(v) for v in value)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _format_runner_ups(items: list, limit: int = 2) -> str:
    """Format competing hypotheses compactly for summaries."""

    parts: list[str] = []
    for item in items[:limit]:
        value = item.value if hasattr(item, "value") else item.get("value")
        confidence = item.confidence if hasattr(item, "confidence") else item.get("confidence", 0.0)
        parts.append(f"{_display_value(value)} ({confidence:.0%})")
    return "; ".join(parts)


def _format_grain_runner_ups(items: list[dict[str, object]], limit: int = 2) -> str:
    """Format runner-up grain candidates without flooding the summary."""

    parts: list[str] = []
    for item in items[:limit]:
        columns_raw = item.get("columns", [])
        cols = "+".join(str(v) for v in (columns_raw if isinstance(columns_raw, list) else [])) or "unknown"
        dup_rate = _pct(float(item.get("duplicate_rate", 0.0)))
        parts.append(f"{cols} (dup={dup_rate})")
    return "; ".join(parts)


def _collect_column_verification_notes(columns: list, limit: int = 3) -> list[str]:
    """Collect concise column-level ambiguity notes for agent-facing output."""

    notes: list[str] = []
    for column in columns:
        semantic = column.semantic_type_inference
        role = column.role_inference
        snippets: list[str] = []
        if semantic and (semantic.runner_ups or semantic.verification_hint or semantic.blocker_reason):
            runner_ups = _format_runner_ups(semantic.runner_ups, limit=2)
            if runner_ups:
                snippets.append(f"semantic alt={runner_ups}")
            if semantic.verification_hint:
                snippets.append(f"verify {semantic.verification_hint}")
        if role and (role.runner_ups or role.verification_hint or role.blocker_reason):
            runner_ups = _format_runner_ups(role.runner_ups, limit=2)
            if runner_ups:
                snippets.append(f"role alt={runner_ups}")
            if role.verification_hint:
                snippets.append(f"verify {role.verification_hint}")
        if snippets:
            notes.append(f"{column.name}: " + " | ".join(snippets[:2]))
        if len(notes) >= limit:
            break
    return notes


# ---------------------------------------------------------------------------
# AI Summary (~500 tokens, compact YAML-like)
# ---------------------------------------------------------------------------

def render_table_ai_summary(
    profile: "TableProfile",
    max_cols: int = 10,
) -> str:
    """Compact, token-efficient summary of a TableProfile for LLM context.

    Targets approximately 500 tokens.  Sections are omitted when empty.
    Never mutates *profile*.

    Args:
        profile:  TableProfile produced by profile_table().
        max_cols: Maximum number of columns to list.  When
                  ``profile.column_importance_ranking`` is populated the
                  ranked order is used; otherwise schema order is used.

    Returns:
        A plain-text YAML-like summary string.
    """
    lines: list[str] = []

    # Header
    profiled_at = (profile.profiled_at or "unknown")[:19]
    lines.append(f"TABLE: {profile.subject}")
    lines.append(
        f"SHAPE: {profile.row_count:,} rows × {profile.column_count} cols"
        f"  |  LEVEL: {profile.profiling_level}"
    )
    lines.append(
        f"PROFILED: {profiled_at}  |  DURATION: {profile.profiling_duration_ms:,} ms"
    )
    lines.append("")

    # Classification + quality
    conf_pct = f"{profile.classification_confidence:.0%}"
    emoji = _quality_emoji(profile.overall_quality_score)
    quality_label = profile.quality_summary or "—"
    lines.append(f"CLASS:   {profile.classification.value} ({conf_pct})")
    if profile.classification_inference and profile.classification_inference.runner_ups:
        lines.append(
            f"CLASS_ALT: {_format_runner_ups(profile.classification_inference.runner_ups)}"
        )
    if profile.classification_inference and profile.classification_inference.verification_hint:
        lines.append(
            f"CLASS_VERIFY: {profile.classification_inference.verification_hint}"
        )
    lines.append(
        f"QUALITY: {profile.overall_quality_score:.4f} [{quality_label}] {emoji}"
    )
    lines.append("")

    # Grain
    grain = profile.grain
    if grain:
        grain_str = "+".join(grain.best_grain) if grain.best_grain else "none detected"
        lines.append(
            f"GRAIN:   {grain_str}"
            f"  |  unique={grain.is_unique}"
            f"  |  dup_rate={_pct(grain.duplicate_rate)}"
        )
        if grain.null_exclusion_rate > 0:
            lines.append(f"         null_exclusion={_pct(grain.null_exclusion_rate)}")
        if grain.runner_up_grains:
            lines.append(
                f"         alternates={_format_grain_runner_ups(grain.runner_up_grains)}"
            )
        if grain.verification_hint:
            lines.append(f"         verify={grain.verification_hint}")

    # Duplicate forensics
    if profile.duplicate_forensics:
        df_ = profile.duplicate_forensics
        lines.append(
            f"DUPES:   verdict={df_.verdict}"
            f"  |  count={df_.duplicate_count:,}"
            f"  |  snapshot={df_.is_snapshot_pattern}"
        )
        if df_.concentration_column:
            lines.append(f"         concentrated_in={df_.concentration_column}")

    # Freshness
    if profile.freshness:
        fv = profile.freshness
        gap = (
            f"  |  GAP: {fv.gap_description}"
            if fv.gap_detected and fv.gap_description
            else ""
        )
        cadence = fv.cadence or "unknown"
        lines.append(
            f"FRESH:   {fv.freshness_column}"
            f"  |  {fv.staleness}"
            f"  |  cadence={cadence}{gap}"
        )
    else:
        lines.append("FRESH:   no temporal column detected")
    lines.append("")

    # Joins (if profiled)
    if profile.joins:
        lines.append(f"JOINS ({len(profile.joins)}):")
        for jp in profile.joins:
            compat = "✓" if jp.format_compatible else "⚠"
            lines.append(
                f"  {jp.source_column} → {jp.target_table}.{jp.target_column}"
                f"  |  {jp.cardinality}  |  overlap={_pct(jp.overlap_pct)}"
                f"  |  orphans={_pct(jp.orphan_pct)}  |  fmt={compat}"
                f"  |  use={jp.safe_join_type}"
            )
        lines.append("")

    # Columns — prefer importance ranking, fall back to schema order
    ranked_names = profile.column_importance_ranking or [c.name for c in profile.columns]
    col_map = {c.name: c for c in profile.columns}
    cols_to_show = [col_map[n] for n in ranked_names if n in col_map][:max_cols]

    lines.append(f"COLUMNS (top {len(cols_to_show)} of {profile.column_count}):")
    for c in cols_to_show:
        sem = (
            f"/{c.semantic_type.value}"
            if c.semantic_type and c.semantic_type.value != "unknown"
            else ""
        )
        lines.append(
            f"  {c.name:<40s} {c.role.value:<16s} {c.spark_type:<12s}"
            f" null={_pct(c.null_pct)}{sem}"
        )
    verification_notes = _collect_column_verification_notes(cols_to_show)
    if verification_notes:
        lines.append("VERIFY:")
        for note in verification_notes:
            lines.append(f"  {note}")
    lines.append("")

    # Issues
    if profile.format_issues:
        errors = [i for i in profile.format_issues if i.severity == "error"]
        warnings = [i for i in profile.format_issues if i.severity == "warning"]
        lines.append(
            f"ISSUES ({len(profile.format_issues)}:"
            f" {len(errors)} errors, {len(warnings)} warnings):"
        )
        for iss in sorted(profile.format_issues, key=lambda x: (x.severity, x.column)):
            lines.append(
                f"  [{iss.severity.upper():7s}] {iss.column:<35s}"
                f" {iss.issue_type}  ({_pct(iss.affected_pct)})"
            )
        lines.append("")

    # Findings
    if profile.findings:
        lines.append(f"FINDINGS ({len(profile.findings)}):")
        for finding in profile.findings[:5]:
            lines.append(f"  • {finding}")
        lines.append("")

    # Performance
    if profile.step_timings:
        total_ms = max(sum(profile.step_timings.values()), 1)
        hot = sorted(profile.step_timings.items(), key=lambda x: -x[1])[:3]
        hot_str = ", ".join(
            f"{name}={ms}ms({ms * 100 // total_ms}%)" for name, ms in hot if ms > 0
        )
        if hot_str:
            lines.append(f"PERF:    {hot_str}")

    # Degraded
    if profile.degraded_features:
        lines.append(f"DEGRADED: {', '.join(profile.degraded_features)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def render_table_profile_md(profile: "TableProfile") -> str:
    """Render a full GFM markdown profile report.  Never mutates *profile*.

    Sections included:
    - Title + Overview table
    - Grain analysis
    - Freshness
    - Column inventory table
    - Format & Cleanliness Issues  (omitted when empty)
    - Findings / Risks / Suggested Actions  (omitted when all are empty)
    - Degraded Features  (omitted when empty)

    Args:
        profile: TableProfile produced by profile_table().

    Returns:
        A GFM markdown string suitable for notebook rendering or docs.
    """
    parts: list[str] = []
    emoji = _quality_emoji(profile.overall_quality_score)
    profiled_at = (profile.profiled_at or "unknown")[:19]
    quality_label = profile.quality_summary or "—"

    # Title
    parts.append(f"# 📊 Table Profile: `{profile.subject}`\n")

    # Overview
    parts.append("## Overview\n")
    overview_rows: list[list[str]] = [
        ["Rows", f"{profile.row_count:,}"],
        ["Columns", str(profile.column_count)],
        [
            "Classification",
            f"{profile.classification.value} ({profile.classification_confidence:.0%})",
        ],
        ["Quality Score", f"{profile.overall_quality_score:.4f} [{quality_label}] {emoji}"],
        ["Profiling Level", profile.profiling_level],
        ["Profiled At", profiled_at],
        ["Duration", f"{profile.profiling_duration_ms:,} ms"],
    ]
    parts.append(_md_table(["Metric", "Value"], overview_rows))
    parts.append("")

    # Grain
    grain = profile.grain
    if grain:
        parts.append("## Grain\n")
        grain_str = (
            "`" + "+".join(grain.best_grain) + "`"
            if grain.best_grain
            else "_none detected_"
        )
        unique_label = "✅ unique" if grain.is_unique else "⚠️ not unique"
        grain_rows: list[list[str]] = [
            ["Best Grain", grain_str],
            ["Unique", unique_label],
            ["Duplicate Rate", _pct(grain.duplicate_rate)],
            ["Null Exclusion Rate", _pct(grain.null_exclusion_rate)],
            ["Verification", grain.verification_hint or "—"],
        ]
        parts.append(_md_table(["Property", "Value"], grain_rows))
        parts.append("")
        if grain.runner_up_grains:
            runner_up_rows = [
                [
                    "`" + "+".join(str(v) for v in item.get("columns", [])) + "`",
                    "✅ yes" if item.get("is_unique") else "⚠️ no",
                    _pct(float(item.get("duplicate_rate", 0.0))),
                    _pct(float(item.get("null_exclusion_rate", 0.0))),
                    _truncate(str(item.get("verification_hint", "—")), 90),
                ]
                for item in grain.runner_up_grains
            ]
            parts.append("**Runner-up grain candidates:**\n")
            parts.append(
                _md_table(
                    ["Candidate", "Unique", "Dup Rate", "Null Excl", "Verification"],
                    runner_up_rows,
                )
            )
            parts.append("")

    # Duplicate Forensics
    if profile.duplicate_forensics:
        df_ = profile.duplicate_forensics
        verdict_emoji = {
            "snapshot_design": "📸",
            "dedup_needed": "🔴",
            "partial_overlap": "🟡",
            "unknown": "❓",
        }.get(df_.verdict, "❓")
        parts.append(f"## Duplicate Forensics {verdict_emoji}\n")

        dup_rows: list[list[str]] = [
            ["Verdict", f"{verdict_emoji} {df_.verdict}"],
            ["Duplicate Count", f"{df_.duplicate_count:,}"],
            ["Duplicate Rate", _pct(df_.duplicate_rate)],
            ["Snapshot Pattern", "✅ yes" if df_.is_snapshot_pattern else "❌ no"],
        ]
        if df_.concentration_column:
            dup_rows.append(["Concentration Column", f"`{df_.concentration_column}`"])
        if df_.is_time_concentrated and df_.time_concentration_description:
            dup_rows.append(["Time Concentration", df_.time_concentration_description])
        if df_.is_source_concentrated and df_.source_concentration_description:
            dup_rows.append(["Source Concentration", df_.source_concentration_description])
        parts.append(_md_table(["Property", "Value"], dup_rows))
        parts.append("")

        if df_.concentration_values:
            parts.append("**Top concentration values:**\n")
            conc_headers = ["Value", "Dup Count", "Dup %"]
            conc_rows = [
                [
                    _truncate(str(v.get("value", "")), 40),
                    f"{v.get('dup_count', 0):,}",
                    _pct(v.get("dup_pct", 0.0)),
                ]
                for v in df_.concentration_values[:5]
            ]
            parts.append(_md_table(conc_headers, conc_rows))
            parts.append("")

        if df_.explanation:
            parts.append(f"> {df_.explanation}\n")
        parts.append("")

    # Freshness
    parts.append("## Freshness\n")
    if profile.freshness:
        fv = profile.freshness
        gap_label = (
            "⚠️ " + (fv.gap_description or "gap detected")
            if fv.gap_detected
            else "✅ none"
        )
        cadence = fv.cadence or "unknown"
        fresh_rows: list[list[str]] = [
            ["Column", f"`{fv.freshness_column}`"],
            ["Latest Value", fv.latest_value],
            ["Staleness", fv.staleness],
            ["Cadence", cadence],
            ["Gap Detected", gap_label],
        ]
        parts.append(_md_table(["Property", "Value"], fresh_rows))
    else:
        parts.append("_No temporal column detected._")
    parts.append("")

    # Joins (if profiled)
    if profile.joins:
        parts.append("## Join Readiness\n")
        join_headers = ["Source Column", "Target", "Cardinality", "Overlap%", "Orphans%", "Format", "Recommendation"]
        join_rows: list[list[str]] = []
        for jp in profile.joins:
            fmt_badge = "✅ compatible" if jp.format_compatible else f"⚠️ {jp.format_mismatch_description or 'mismatch'}"
            join_rows.append([
                f"`{jp.source_column}`",
                f"`{jp.target_table}.{jp.target_column}`",
                jp.cardinality,
                _pct(jp.overlap_pct),
                f"{jp.orphan_count:,} ({_pct(jp.orphan_pct)})",
                _truncate(fmt_badge, 40),
                f"**{jp.safe_join_type}** JOIN",
            ])
        parts.append(_md_table(join_headers, join_rows))
        parts.append("")

    # Columns
    parts.append("## Columns\n")
    col_headers = ["Column", "Type", "Role", "Semantic", "Null%", "Distinct%", "Length", "Flags"]
    col_rows: list[list[str]] = []
    for c in profile.columns:
        sem = c.semantic_type.value if c.semantic_type else "—"
        flags = ", ".join(c.quality_flags[:2]) if c.quality_flags else "—"
        length_str = (
            f"{c.min_length}–{c.max_length}"
            if c.min_length is not None and c.max_length is not None
            else "—"
        )
        col_rows.append(
            [
                f"`{c.name}`",
                c.spark_type,
                c.role.value,
                _truncate(sem, 20),
                _pct(c.null_pct),
                _pct(c.distinct_pct),
                length_str,
                _truncate(flags, 40),
            ]
        )
    parts.append(_md_table(col_headers, col_rows))
    parts.append("")

    ambiguity_rows: list[list[str]] = []
    if profile.classification_inference and (
        profile.classification_inference.runner_ups
        or profile.classification_inference.verification_hint
    ):
        ambiguity_rows.append([
            "table",
            "classification",
            _format_runner_ups(profile.classification_inference.runner_ups) or "—",
            profile.classification_inference.verification_hint or "—",
        ])

    for c in profile.columns:
        if c.semantic_type_inference and (
            c.semantic_type_inference.runner_ups
            or c.semantic_type_inference.verification_hint
        ):
            ambiguity_rows.append([
                f"`{c.name}`",
                "semantic",
                _truncate(_format_runner_ups(c.semantic_type_inference.runner_ups) or "—", 60),
                _truncate(c.semantic_type_inference.verification_hint or "—", 90),
            ])
        if c.role_inference and (c.role_inference.runner_ups or c.role_inference.verification_hint):
            ambiguity_rows.append([
                f"`{c.name}`",
                "role",
                _truncate(_format_runner_ups(c.role_inference.runner_ups) or "—", 60),
                _truncate(c.role_inference.verification_hint or "—", 90),
            ])
        if len(ambiguity_rows) >= 6:
            break

    if ambiguity_rows:
        parts.append("## Ambiguity & Verification\n")
        parts.append(
            _md_table(
                ["Target", "Inference", "Alternatives", "How to Verify"],
                ambiguity_rows,
            )
        )
        parts.append("")

    # Issues
    if profile.format_issues:
        errors = [i for i in profile.format_issues if i.severity == "error"]
        warnings = [i for i in profile.format_issues if i.severity == "warning"]
        parts.append(
            f"## Format & Cleanliness Issues"
            f" ({len(errors)} errors, {len(warnings)} warnings)\n"
        )
        iss_headers = ["Severity", "Column", "Issue Type", "Affected%", "Examples"]
        iss_rows: list[list[str]] = []
        for iss in sorted(profile.format_issues, key=lambda x: (x.severity, x.column)):
            sev_badge = (
                "🔴 error" if iss.severity == "error"
                else "🟡 warning" if iss.severity == "warning"
                else "🔵 info"
            )
            examples_str = (
                ", ".join(str(e) for e in iss.examples[:2]) if iss.examples else "—"
            )
            iss_rows.append(
                [
                    sev_badge,
                    f"`{iss.column}`",
                    iss.issue_type,
                    _pct(iss.affected_pct),
                    _truncate(examples_str, 50),
                ]
            )
        parts.append(_md_table(iss_headers, iss_rows))
        parts.append("")

    # Numeric Outliers
    if profile.outliers:
        active_outliers = [o for o in profile.outliers if o.outlier_count > 0]
        if active_outliers:
            parts.append("## Numeric Outliers\n")
            num_out_rows: list[list[str]] = []
            for op in active_outliers:
                extremes = ", ".join(
                    str(e.get("value", "")) for e in op.extreme_values[:2]
                ) or "—"
                num_out_rows.append([
                    f"`{op.column}`",
                    op.method,
                    f"[{op.lower_bound:.2f},\u2009{op.upper_bound:.2f}]",
                    f"{op.outlier_count:,} ({_pct(op.outlier_pct)})",
                    _truncate(extremes, 40),
                ])
            parts.append(_md_table(
                ["Column", "Method", "Fence", "Count", "Extreme Values"],
                num_out_rows,
            ))
            parts.append("")

    # Findings / Risks / Actions
    if profile.findings or profile.risks or profile.suggested_actions:
        parts.append("## Findings & Recommendations\n")
        if profile.findings:
            parts.append("**Findings:**\n")
            for item in profile.findings:
                parts.append(f"- {item}")
            parts.append("")
        if profile.risks:
            parts.append("**Risks:**\n")
            for r in profile.risks:
                parts.append(f"- ⚠️ {r}")
            parts.append("")
        if profile.suggested_actions:
            parts.append("**Suggested Actions:**\n")
            for a in profile.suggested_actions:
                parts.append(f"- 💡 {a}")
            parts.append("")

    # Performance breakdown (only steps with measurable duration)
    if profile.step_timings:
        parts.append("## ⏱️ Performance Breakdown\n")
        total_ms = max(sum(profile.step_timings.values()), 1)
        perf_rows: list[list[str]] = []
        for step_name, ms in profile.step_timings.items():
            if ms == 0:
                continue
            pct = ms / total_ms
            bar_len = int(pct * 20)
            bar = "█" * bar_len + "░" * (20 - bar_len)
            perf_rows.append([
                step_name,
                f"{ms:,} ms",
                f"{pct:.0%}",
                f"`{bar}`",
            ])
        skipped = sum(1 for ms in profile.step_timings.values() if ms == 0)
        perf_rows.append(["**TOTAL**", f"**{total_ms:,} ms**", "**100%**", ""])
        parts.append(_md_table(["Step", "Duration", "%", "Bar"], perf_rows))
        if skipped:
            parts.append(f"\n_{skipped} step(s) completed in <1ms (hidden)_")
        parts.append("")

    # Degraded
    if profile.degraded_features:
        parts.append("## ⚠️ Degraded Features\n")
        deg_rows: list[list[str]] = [
            [feat, _truncate(profile.degradation_reasons.get(feat, "—"), 80)]
            for feat in profile.degraded_features
        ]
        parts.append(_md_table(["Feature", "Reason"], deg_rows))
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public: Microscope renderer
# ---------------------------------------------------------------------------


def render_microscope_md(result: dict) -> str:
    """Render microscope dict as a GFM markdown report.

    Args:
        result: Dict from microscope() with Anchor standard contract shape.

    Returns:
        GFM markdown string.
    """
    parts: list[str] = []
    metrics = result.get("metrics", {})
    samples = result.get("samples", {})

    # Header
    col_type = metrics.get("column_type_category", "unknown").upper()
    role = metrics.get("role", "")
    sem_type = metrics.get("semantic_type", "")
    badge_parts = [col_type]
    if role:
        badge_parts.append(f"role={role}")
    if sem_type:
        badge_parts.append(f"type={sem_type}")
    parts.append(f"# 🔬 Microscope: {result.get('subject', '?')}")
    parts.append(f"**{' | '.join(badge_parts)}**")
    parts.append("")
    parts.append(f"> {result.get('summary', '')}")
    parts.append("")

    # Key metrics table
    key_metrics = [
        ("Rows", f"{metrics.get('row_count', 0):,}"),
        ("Nulls", f"{metrics.get('null_count', 0):,} ({_pct(metrics.get('null_pct', 0))})"),
        ("Distinct", f"{metrics.get('distinct_count', 0):,} ({_pct(metrics.get('distinct_pct', 0))})"),
        ("Unique", str(metrics.get('is_unique', False))),
        ("Constant", str(metrics.get('is_constant', False))),
    ]
    # Type-specific metrics
    if metrics.get("mean") is not None:
        key_metrics.extend([
            ("Min", str(metrics.get('min', ''))),
            ("Max", str(metrics.get('max', ''))),
            ("Mean", f"{metrics.get('mean', 0):.4f}"),
            ("Median", str(metrics.get('median', ''))),
            ("Std", f"{metrics.get('std', 0):.4f}"),
        ])
        if metrics.get("p25") is not None:
            key_metrics.extend([
                ("P25 / P75", f"{metrics.get('p25', 0):.2f} / {metrics.get('p75', 0):.2f}"),
                ("P95 / P99", f"{metrics.get('p95', 0):.2f} / {metrics.get('p99', 0):.2f}"),
            ])
        if metrics.get("skewness") is not None:
            key_metrics.append(("Skewness", f"{metrics.get('skewness', 0):.2f}"))
        if metrics.get("zero_count", 0) > 0:
            key_metrics.append((
                "Zero Count",
                f"{metrics.get('zero_count', 0):,} ({_pct(metrics.get('zero_pct', 0))})",
            ))
        if metrics.get("outlier_count", 0) > 0:
            key_metrics.append((
                "Outliers",
                f"{metrics.get('outlier_count', 0):,} ({_pct(metrics.get('outlier_pct', 0))})"
                f" — fence [{metrics.get('outlier_lower_bound', 0):.2f}, {metrics.get('outlier_upper_bound', 0):.2f}]",
            ))
    if metrics.get("length_min") is not None:
        key_metrics.extend([
            ("Len Min", str(metrics.get('length_min', ''))),
            ("Len Max", str(metrics.get('length_max', ''))),
            ("Len Mean", f"{metrics.get('length_mean', 0):.1f}"),
        ])

    parts.append("## Key Metrics")
    parts.append("")
    parts.append(_md_table(
        ["Metric", "Value"],
        [[m, v] for m, v in key_metrics],
    ))
    parts.append("")

    # Findings
    if result.get("findings"):
        parts.append("## Findings")
        parts.append("")
        for f in result["findings"]:
            parts.append(f"- {f}")
        parts.append("")

    # Top values
    if samples.get("top_values"):
        parts.append("## Top Values")
        parts.append("")
        rows = []
        for tv in samples["top_values"][:10]:
            rows.append([
                _truncate(str(tv.get('value', '')), 40),
                f"{tv.get('count', 0):,}",
                _pct(tv.get('pct', 0)),
            ])
        parts.append(_md_table(["Value", "Count", "%"], rows))
        parts.append("")

    # Bottom values
    if samples.get("bottom_values"):
        parts.append("## Bottom Values")
        parts.append("")
        bv_rows = []
        for bv in samples["bottom_values"][:10]:
            bv_rows.append([
                _truncate(str(bv.get("value", "")), 40),
                f"{bv.get('count', 0):,}",
                _pct(bv.get("pct", 0)),
            ])
        parts.append(_md_table(["Value", "Count", "%"], bv_rows))
        parts.append("")

    # Outliers
    if samples.get("outliers"):
        parts.append("## Outliers")
        parts.append("")
        out_rows = []
        for ov in samples["outliers"]:
            out_rows.append([
                str(ov.get("value", "")),
                f"{ov.get('count', 0):,}",
                _pct(ov.get("pct", 0)),
            ])
        parts.append(_md_table(["Value", "Count", "%"], out_rows))
        parts.append("")

    # Histogram as GFM table — each bin renders as its own row
    if samples.get("histogram"):
        parts.append("## Distribution (Histogram)")
        parts.append("")
        bins = samples["histogram"]
        total_count = sum(h.get("count", 0) for h in bins)
        max_count = max(h.get("count", 0) for h in bins) or 1
        hist_rows: list[list[str]] = []
        for h in bins:
            count = h.get("count", 0)
            bar_len = int(16 * count / max_count)
            bar = "█" * bar_len if bar_len > 0 else "·"
            hist_rows.append([
                f"`[{h.get('bin_start', 0):.2f}, {h.get('bin_end', 0):.2f})`",
                bar,
                f"{count:,}",
                _pct(count / total_count) if total_count > 0 else "0%",
            ])
        parts.append(_md_table(["Bin", "▓", "Count", "%"], hist_rows))
        parts.append("")

    # Pattern fingerprints
    if samples.get("pattern_fingerprints"):
        parts.append("## Pattern Fingerprints")
        parts.append("")
        rows = []
        for pf in samples["pattern_fingerprints"][:10]:
            rows.append([
                f"`{_truncate(str(pf.get('pattern', '')), 30)}`",
                f"{pf.get('count', 0):,}",
                _pct(pf.get('pct', 0)),
            ])
        parts.append(_md_table(["Pattern", "Count", "%"], rows))
        parts.append("")

    # Risks
    if result.get("risks"):
        parts.append("## Risks")
        parts.append("")
        for r in result["risks"]:
            parts.append(f"- ⚠️ {r}")
        parts.append("")

    # Next actions
    if result.get("suggested_next_actions"):
        parts.append("## Suggested Next Actions")
        parts.append("")
        for a in result["suggested_next_actions"]:
            parts.append(f"- {a}")
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public: Case File renderer
# ---------------------------------------------------------------------------


def render_case_file_md(result: dict) -> str:
    """Render case_file dict as a GFM markdown report.

    Args:
        result: Dict from case_file() with Anchor standard contract shape.

    Returns:
        GFM markdown string.
    """
    parts: list[str] = []
    metrics = result.get("metrics", {})
    samples = result.get("samples", {})

    # Header
    parts.append(f"# 📁 Case File: {result.get('subject', '?')}")
    parts.append("")
    parts.append(f"> {result.get('summary', '')}")
    parts.append("")

    # Metrics
    parts.append("## Summary")
    parts.append("")
    parts.append(_md_table(
        ["Metric", "Value"],
        [
            ["Matched Rows", f"{metrics.get('matched_rows', 0):,}"],
            ["Match %", _pct(metrics.get('matched_pct', 0))],
            ["Total Rows", f"{metrics.get('total_rows', 0):,}"],
            ["Filter", str(metrics.get('filter_applied', ''))],
            ["Target Column", str(metrics.get('target_column', ''))],
        ],
    ))
    parts.append("")

    # Findings
    if result.get("findings"):
        parts.append("## Findings")
        parts.append("")
        for f in result["findings"]:
            parts.append(f"- {f}")
        parts.append("")

    # Co-occurrences
    if samples.get("co_occurrences"):
        parts.append("## Co-occurrence Patterns")
        parts.append("")
        rows = []
        for co in samples["co_occurrences"]:
            rows.append([
                str(co.get('column', '')),
                _truncate(str(co.get('value', '')), 30),
                f"{co.get('count', 0):,}",
                _pct(co.get('pct', 0)),
            ])
        parts.append(_md_table(["Column", "Value", "Count", "%"], rows))
        parts.append("")

    # Sample rows
    if samples.get("rows"):
        parts.append("## Sample Rows")
        parts.append("")
        sample_rows = samples["rows"]
        if sample_rows:
            headers = list(sample_rows[0].keys())
            rows = []
            for row in sample_rows[:10]:
                rows.append([_truncate(str(row.get(h, '')), 25) for h in headers])
            parts.append(_md_table(headers, rows))
        parts.append("")

    # Risks
    if result.get("risks"):
        parts.append("## Risks")
        parts.append("")
        for r in result["risks"]:
            parts.append(f"- ⚠️ {r}")
        parts.append("")

    # Next actions
    if result.get("suggested_next_actions"):
        parts.append("## Suggested Next Actions")
        parts.append("")
        for a in result["suggested_next_actions"]:
            parts.append(f"- {a}")
        parts.append("")

    return "\n".join(parts)
