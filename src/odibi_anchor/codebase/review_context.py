"""odibi_anchor.codebase.review_context — Pre-gate changeset review.

Produces a structured analysis of the session's total changeset: diffstat summary,
untested changes, acceptance criteria verification, and risk flags. Designed to be
the single command an agent runs before anchor("gate") to self-inspect their work.

Usage:
    ctx = review_context(root, session_diff=diff_data, test_timings=timings)
    # → dict with diffstat, findings, risks

Dependencies: stdlib only.
"""
from __future__ import annotations

from typing import Any

from odibi_anchor._utils.contract import build_base_context
from odibi_anchor._utils.render_utils import (
    render_header_lines,
    render_metrics_lines,
    render_bullet_section,
)


def review_context(
    root: str,
    *,
    frame: Any | None = None,
    session_diff: dict | None = None,
    test_timings: list[dict] | None = None,
    acceptance_criteria: list[str] | None = None,
    files_changed: set[str] | None = None,
    output_format: str = "markdown",
) -> dict | str:
    """Pre-gate changeset review — unified diff analysis + quality checks.

    Combines:
    1. Diffstat summary (files, adds, deletes, net)
    2. Untested change detection (.py files with no test run)
    3. Acceptance criteria verification (keyword match in diff)
    4. Risk flags (large files, many changes)

    Args:
        root: Project root path.
        frame: Optional session frame (for plan_context access).
        session_diff: Output from get_diff() — dict with metrics and samples.per_file.
        test_timings: List of test/test_focus timing records from session.
        acceptance_criteria: List of acceptance criteria strings to verify.
        files_changed: Set of changed file paths this session.
        output_format: "dict" or "markdown".

    Returns:
        Standard context dict or markdown string.
    """
    findings: list[str] = []
    risks: list[str] = []

    # ── Handle empty session ──
    if not session_diff or session_diff.get("metrics", {}).get("files_changed", 0) == 0:
        ctx = build_base_context(
            kind="review_context",
            subject="session",
            summary="Nothing to review — no files changed this session.",
            metrics={"files_changed": 0, "additions": 0, "deletions": 0, "net_lines": 0},
            findings=["No changes detected. Nothing to review."],
            risks=[],
            samples={},
            suggested_next_actions=[
                "Session has no changes — gate will pass trivially.",
            ],
        )
        if output_format == "markdown":
            return _render_review_report(ctx)
        return ctx

    # ── Extract diffstat ──
    diff_metrics = session_diff.get("metrics", {})
    files_count = diff_metrics.get("files_changed", 0)
    additions = diff_metrics.get("total_additions", 0)
    deletions = diff_metrics.get("total_deletions", 0)
    net_lines = diff_metrics.get("net_lines", additions - deletions)

    per_file = session_diff.get("samples", {}).get("per_file", {})
    if not per_file:
        per_file = session_diff.get("samples", {}).get("diffstat", {})

    # ── Untested changes detection ──
    changed_py = [f for f in (files_changed or per_file.keys()) if f.endswith(".py")]
    has_tests_run = bool(test_timings)
    if changed_py and not has_tests_run:
        findings.append(
            f"UNTESTED: {len(changed_py)} .py file(s) changed but no tests run this session."
        )
        risks.append("Changed .py files without test verification — gate may block.")

    # ── Acceptance criteria check ──
    criteria_results: list[dict] = []
    if acceptance_criteria:
        # Build a combined diff text for keyword matching
        all_diff_text = ""
        for path, file_diff in per_file.items():
            diff_content = file_diff.get("diff", "")
            all_diff_text += f" {path} {diff_content}"
        all_diff_text_lower = all_diff_text.lower()

        for criterion in acceptance_criteria:
            # Heuristic: extract keywords from criterion (words > 3 chars)
            keywords = [w.lower() for w in criterion.split() if len(w) > 3]
            matched = any(kw in all_diff_text_lower for kw in keywords)
            criteria_results.append({"criterion": criterion, "matched": matched})
            if not matched:
                findings.append(f"UNVERIFIED CRITERION: '{criterion}' — no matching change found in diff.")

        matched_count = sum(1 for r in criteria_results if r["matched"])
        total_criteria = len(criteria_results)
        if matched_count < total_criteria:
            risks.append(
                f"{total_criteria - matched_count}/{total_criteria} acceptance criteria "
                f"not matched in changeset."
            )

    # ── Risk flags ──
    large_files = [
        (path, info.get("additions", 0) + info.get("deletions", 0))
        for path, info in per_file.items()
        if (info.get("additions", 0) + info.get("deletions", 0)) > 100
    ]
    if large_files:
        for path, churn in large_files:
            risks.append(f"High churn: {path} ({churn} lines changed)")

    # ── Build per-file summary for samples ──
    file_summaries = {}
    for path, info in per_file.items():
        file_summaries[path] = {
            "status": info.get("status", "unknown"),
            "additions": info.get("additions", 0),
            "deletions": info.get("deletions", 0),
        }

    # ── Assemble context ──
    summary = (
        f"{files_count} file(s) changed: +{additions}/-{deletions} "
        f"(net {'+' if net_lines >= 0 else ''}{net_lines} lines)"
    )

    if not findings:
        findings.append("All checks passed — changeset looks clean.")

    ctx = build_base_context(
        kind="review_context",
        subject="session",
        summary=summary,
        metrics={
            "files_changed": files_count,
            "additions": additions,
            "deletions": deletions,
            "net_lines": net_lines,
            "py_files_changed": len(changed_py),
            "tests_run": bool(has_tests_run),
            "criteria_matched": sum(1 for r in criteria_results if r["matched"]) if criteria_results else None,
            "criteria_total": len(criteria_results) if criteria_results else None,
            **{
                key: diff_metrics.get(key)
                for key in (
                    "scope_source", "target_start_sha", "target_current_sha", "target_drift",
                )
                if key in diff_metrics
            },
        },
        findings=findings,
        risks=risks,
        samples={"per_file": file_summaries, "criteria": criteria_results or []},
        suggested_next_actions=[
            "MUST: Run anchor('gate') to finalize delivery.",
            "SHOULD: Run anchor('session_diff', target='file.py') for detailed per-file review.",
        ],
    )

    if output_format == "markdown":
        return _render_review_report(ctx)
    return ctx


def _render_review_report(ctx: dict) -> str:
    """Render review context as markdown report."""
    lines = render_header_lines(ctx, title_prefix="Changeset Review")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    # Per-file diffstat table
    per_file = ctx.get("samples", {}).get("per_file", {})
    if per_file:
        lines.append("\n## Diffstat\n")
        lines.append("| File | Status | +Lines | -Lines |")
        lines.append("| --- | --- | --- | --- |")
        for path, info in per_file.items():
            lines.append(
                f"| {path} | {info.get('status', '?')} | "
                f"+{info.get('additions', 0)} | -{info.get('deletions', 0)} |"
            )

    # Criteria results
    criteria = ctx.get("samples", {}).get("criteria", [])
    if criteria:
        lines.append("\n## Acceptance Criteria\n")
        for c in criteria:
            icon = "✅" if c["matched"] else "❌"
            lines.append(f"- {icon} {c['criterion']}")

    lines.extend(render_bullet_section(ctx.get("findings", []), "Findings"))
    lines.extend(render_bullet_section(ctx.get("risks", []), "Risks"))
    lines.extend(render_bullet_section(ctx.get("suggested_next_actions", []), "Next Actions"))

    return "\n".join(lines)
