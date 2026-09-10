"""Rendering functions for validation summary reports.

Internal module — not part of the public API.
"""

from __future__ import annotations

from typing import Any


def render_validation_report(
    ctx: dict[str, Any],
    *,
    show_samples: bool = False,
) -> str:
    """Render a validation context dict as structured markdown.

    Designed for LLM prompts and human consumption. Produces a
    compact, token-efficient report with clear sections.

    Args:
        ctx: The dictionary returned by
            ``validation_summary_context()``.
        show_samples: If True, include sample failure rows.

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

    lines.append(f"# Validation: {ctx['subject']}")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Promotion safety
    safe = metrics["is_promotion_safe"]
    icon = "\u2705" if safe else "\u274c"
    rec = ctx.get("recommendation", "")
    lines.append(f"{icon} **Promotion:** {rec}")
    lines.append("")

    # Metrics table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Engine | {metrics['engine']} |")
    lines.append(f"| Total rows | {metrics['total_rows']:,} |")
    lines.append(
        f"| Rules evaluated | {metrics['rules_evaluated']} |"
    )
    lines.append(f"| Rules passed | {metrics['rules_passed']} |")
    lines.append(f"| Rules failed | {metrics['rules_failed']} |")
    lines.append(f"| Blockers | {metrics['blocker_count']} |")
    lines.append(f"| Warnings | {metrics['warning_count']} |")
    lines.append(
        f"| Rows with failures | "
        f"{metrics['rows_with_any_failure']:,} |"
    )
    lines.append(
        f"| Overall pass rate | "
        f"{metrics['overall_pass_rate']:.1%} |"
    )
    lines.append(
        f"| Promotion safe | {metrics['is_promotion_safe']} |"
    )
    lines.append("")

    # Failed rules detail
    failed_rules = [r for r in ctx["rules"] if not r["passed"]]
    if failed_rules:
        lines.append("## Failed Rules")
        lines.append("")
        lines.append(
            "| Rule | Severity | Failed | Rate "
            "| Fix |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for r in failed_rules:
            sev_icon = (
                "\U0001f6d1" if r["severity"] == "blocker"
                else "\u26a0\ufe0f"
                if r["severity"] == "warning"
                else "\u2139\ufe0f"
            )
            fix = r.get("fix_expr", "")
            fix_short = fix[:50] + "..." if len(fix) > 50 else fix
            lines.append(
                f"| {r['rule_id']} | {sev_icon} "
                f"{r['severity']} | "
                f"{r['failed_count']:,} | "
                f"{r['failed_rate']:.1%} | "
                f"`{fix_short}` |"
            )
        lines.append("")

    # Fix code section
    fix_exprs = [
        r.get("fix_expr", "")
        for r in failed_rules if r.get("fix_expr")
    ]
    if fix_exprs:
        lines.append("## Fix Code (copy-paste)")
        lines.append("")
        lines.append("```python")
        for expr in fix_exprs:
            lines.append(expr)
        lines.append("```")
        lines.append("")

    # Quarantine call
    quarantine_call = ctx.get("quarantine_call", "")
    if quarantine_call:
        lines.append("## Quarantine Call")
        lines.append("")
        lines.append("```python")
        lines.append(quarantine_call)
        lines.append("```")
        lines.append("")

    # Blockers
    if ctx["blockers"]:
        lines.append("## Blockers")
        lines.append("")
        for b in ctx["blockers"]:
            lines.append(f"* \U0001f6d1 {b}")
        lines.append("")

    # Warnings
    if ctx["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for w in ctx["warnings"]:
            lines.append(f"* \u26a0\ufe0f {w}")
        lines.append("")

    # Sample failures
    if show_samples:
        for r in failed_rules:
            samples = r.get("sample_failures", [])
            if samples:
                lines.append(
                    f"### Samples: {r['rule_id']}"
                )
                lines.append("")
                keys = list(samples[0].keys())
                lines.append("| " + " | ".join(keys) + " |")
                lines.append(
                    "| " + " | ".join(["---"] * len(keys))
                    + " |"
                )
                for row in samples[:5]:
                    vals = [
                        str(row.get(k, "")) for k in keys
                    ]
                    lines.append(
                        "| " + " | ".join(vals) + " |"
                    )
                lines.append("")

    # Suggested next actions
    actions = ctx.get("suggested_next_actions", [])
    if actions:
        lines.append("## Suggested Next Actions")
        lines.append("")
        for a in actions:
            lines.append(f"* {a}")
        lines.append("")

    return "\n".join(lines)


def render_validation_summary_report(
    ctx: dict[str, Any],
    *,
    show_samples: bool = False,
) -> str:
    """Render a validation summary context as markdown (alias for render_validation_report)."""
    return render_validation_report(ctx, show_samples=show_samples)
