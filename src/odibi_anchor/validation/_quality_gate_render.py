"""Quality gate report rendering."""

from __future__ import annotations

from typing import Any


def render_quality_gate_report(
    ctx: dict[str, Any],
    *,
    show_samples: bool = True,
) -> str:
    """Render a quality gate context dict as markdown.

    Produces a compact report for LLM prompts or human review.

    Args:
        ctx: Dictionary from ``quality_gate_context()``.
        show_samples: If True, include sample evidence rows.

    Returns:
        Markdown-formatted string.

    Raises:
        ValueError: If ctx is missing required keys.
    """
    required = {"kind", "subject", "status", "metrics"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(
            f"Missing required keys: {sorted(missing)}"
        )

    lines: list[str] = []
    m = ctx["metrics"]
    status = ctx["status"]

    icon = (
        "\u2705" if status == "pass"
        else "\u274c" if status == "fail"
        else "\u26a0\ufe0f"
    )
    lines.append(f"# Quality Gate: {ctx['subject']}")
    lines.append("")
    lines.append(f"{icon} **Status:** {status.upper()}")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Metrics table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total rows | {m['total_rows']:,} |")
    lines.append(
        f"| Checks run | {m['checks_run']} |"
    )
    lines.append(
        f"| Checks passed | {m['checks_passed']} |"
    )
    lines.append(
        f"| Checks failed | {m['checks_failed']} |"
    )
    lines.append(
        f"| Checks warned | {m['checks_warned']} |"
    )
    safe_icon = (
        "\u2705" if m["is_write_safe"] else "\u274c"
    )
    lines.append(
        f"| Write safe | {safe_icon} "
        f"{m['is_write_safe']} |"
    )
    lines.append("")

    # Fix impact
    fix_impact = ctx.get("fix_impact")
    if fix_impact and fix_impact["estimated_rows_dropped"] > 0:
        lines.append("## Fix Impact")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        lines.append(
            f"| Rows before | "
            f"{fix_impact['rows_before']:,} |"
        )
        lines.append(
            f"| Est. rows after | "
            f"{fix_impact['estimated_rows_after']:,} |"
        )
        lines.append(
            f"| Est. rows dropped | "
            f"{fix_impact['estimated_rows_dropped']:,} |"
        )
        lines.append(
            f"| Drop % | "
            f"{fix_impact['drop_pct']:.1f}% |"
        )
        lines.append("")

    # Per-check results
    check_results = ctx.get("checks", [])
    failed = [
        c for c in check_results
        if c["status"] == "fail"
    ]
    warned = [
        c for c in check_results
        if c["status"] == "warn"
    ]

    if failed:
        lines.append("## Failed Checks (Blockers)")
        lines.append("")
        lines.append(
            "| Check | Detail | Fix |"
        )
        lines.append("| --- | --- | --- |")
        for c in failed:
            fix = c.get("fix_expr", "")
            fix_short = (
                fix[:40] + "..."
                if len(fix) > 40
                else fix
            )
            lines.append(
                f"| {c['check_id']} | "
                f"{c['detail']} | "
                f"`{fix_short}` |"
            )
        lines.append("")

    if warned:
        lines.append("## Warnings")
        lines.append("")
        for c in warned:
            lines.append(
                f"* {c['check_id']}: {c['detail']}"
            )
        lines.append("")

    # Fix all code
    fix_all = ctx.get("fix_all_expr", "")
    if fix_all:
        lines.append("## Fix Code (copy-paste)")
        lines.append("")
        lines.append("```python")
        lines.append(fix_all)
        lines.append("```")
        lines.append("")

    # Samples
    if show_samples:
        for c in check_results:
            samples = c.get("samples", [])
            if samples and c["check_id"] != "completeness":
                lines.append(
                    f"### Samples: {c['check_id']}"
                )
                lines.append("")
                cols = list(samples[0].keys())
                header = (
                    "| " + " | ".join(cols) + " |"
                )
                sep = (
                    "| "
                    + " | ".join(
                        "---" for _ in cols
                    )
                    + " |"
                )
                lines.append(header)
                lines.append(sep)
                for row in samples:
                    vals = " | ".join(
                        str(row.get(c2, ""))
                        for c2 in cols
                    )
                    lines.append(f"| {vals} |")
                lines.append("")

    # Recommendation
    rec = ctx.get("recommendation", "")
    if rec:
        lines.append(f"**Recommendation:** {rec}")
        lines.append("")

    # Suggested actions
    actions = ctx.get("suggested_next_actions", [])
    if actions:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for a in actions:
            lines.append(f"* {a}")
        lines.append("")

    return "\n".join(lines)
