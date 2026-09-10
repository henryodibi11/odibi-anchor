"""Write-safety card (W-1) — a compact, glanceable go/no-go verdict.

Renders a quality/pre-merge result into a 3-5 line card so a human reviewing a
write proposal (or an agent about to propose one) sees the verdict next to the
SQL, instead of having to read the full quality contract. Approval ≠ quality:
the card puts the quality signal where the decision is made.
"""
from __future__ import annotations


def render_write_safety_card(
    *,
    safe: bool,
    headline: str,
    signals: list[str],
    fix_hint: str | None = None,
) -> str:
    """Format a write-safety verdict card.

    Args:
        safe: True if the write is structurally safe.
        headline: One-line verdict summary.
        signals: Short bullet facts (checks passed, rows, failed checks).
        fix_hint: What to do next when not safe (omitted when safe).

    Returns:
        A compact multi-line string.
    """
    mark = "✅ SAFE" if safe else "❌ BLOCKED"
    lines = [f"WRITE SAFETY: {mark} — {headline}"]
    for s in signals:
        lines.append(f"  • {s}")
    if not safe and fix_hint:
        lines.append(f"  → {fix_hint}")
    return "\n".join(lines)
