"""Shared markdown rendering primitives for odibi_anchor tools.

All render_*_report() functions build markdown from a context dict.
This module provides the common building blocks so each tool only
implements its domain-specific sections.

Functions return lists of strings (lines) so callers can extend their
own lines list and join at the end. This matches the existing pattern.
"""

from __future__ import annotations

from typing import Any


def render_header_lines(ctx: dict, title_prefix: str) -> list[str]:
    """Standard header block: # {title_prefix}: {subject} + summary.

    Returns lines for:
        # {title_prefix}: {subject}

        **Summary:** {summary}

    """
    return [
        f"# {title_prefix}: {ctx['subject']}",
        "",
        f"**Summary:** {ctx['summary']}",
        "",
    ]


def render_metrics_lines(
    metrics: dict[str, Any],
    *,
    header: str = "## Metrics",
    exclude: set[str] | None = None,
) -> list[str]:
    """Render metrics as a markdown table.

    Returns lines for:
        ## Metrics

        | Metric | Value |
        | --- | --- |
        | key | formatted_value |
        ...
    """
    exclude = exclude or set()
    lines = [header, "", "| Metric | Value |", "| --- | --- |"]
    for k, v in metrics.items():
        if k in exclude:
            continue
        lines.append(f"| {k} | {v} |")
    return lines


def render_bullet_section(
    items: list[str],
    header: str,
    *,
    prefix: str = "- ",
) -> list[str]:
    """Render a bullet list section. Returns empty list if items is empty.

    Returns lines for:
        (blank line)
        ## {header}
        (blank line)
        - item1
        - item2
    """
    if not items:
        return []
    lines = ["", header, ""]
    for item in items:
        lines.append(f"{prefix}{item}")
    return lines


def render_numbered_section(
    items: list[str],
    header: str,
) -> list[str]:
    """Render a numbered list section. Returns empty list if items is empty.

    Returns lines for:
        (blank line)
        ## {header}
        (blank line)
        1. item1
        2. item2
    """
    if not items:
        return []
    lines = ["", header, ""]
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item}")
    return lines


def render_table(
    rows: list[list[str]],
    headers: list[str],
) -> list[str]:
    """Render a generic markdown table.

    Returns lines for:
        | H1 | H2 |
        | --- | --- |
        | v1 | v2 |
    """
    if not rows:
        return []
    sep = " | ".join("---" for _ in headers)
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + sep + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


def format_metric(value: Any) -> str:
    """Smart formatting for metric values.

    - int → comma-separated (1,234)
    - float → 1 decimal place
    - bool → ✅ / ❌
    - else → str
    """
    if isinstance(value, bool):
        return "✅" if value else "❌"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def format_status_badge(passed: bool) -> str:
    """Returns ✅ PASS or ❌ FAIL."""
    return "✅ PASS" if passed else "❌ FAIL"
