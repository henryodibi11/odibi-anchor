"""odibi_anchor.profiling.dogfood_regression_context — Dog-food output regression tracking.

Stores baseline outputs from dog-food runs as JSON files, diffs subsequent
runs against baselines, and classifies changes as improvements, regressions,
or neutral changes.

Eliminates the manual visual-diff workflow for verifying tool output quality
across changes.

Usage:
    from odibi_anchor.profiling import dogfood_regression_context

    # First run: save baseline
    current = anchor("profile_table", df, subject="table", output_format="dict")
    ctx = dogfood_regression_context(current, save_as_baseline=True)

    # Later: compare against baseline
    ctx = dogfood_regression_context(current)
    if ctx["metrics"]["regressions_count"] > 0:
        print("REGRESSION detected!")

Dependencies: stdlib only (json, os, pathlib, datetime, re).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


def dogfood_regression_context(
    current_output: dict[str, Any],
    *,
    baseline_dir: str = ".dogfood_baselines",
    subject: str | None = None,
    save_as_baseline: bool = False,
    root: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Compare current tool output against a stored baseline.

    Diffs the current output against a previously saved baseline and
    classifies each difference as an improvement, regression, or neutral change.

    Args:
        current_output: The tool output dict to compare (must have 'kind' key).
        baseline_dir: Directory to store/read baselines (relative to root or absolute).
        subject: Override subject. Defaults to current_output["subject"].
        save_as_baseline: If True, saves current_output as the new baseline.
        root: Project root. If None, uses cwd.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).

    Example:
        >>> from odibi_anchor.profiling import dogfood_regression_context
        >>> current = anchor("profile_table", df, subject="table", output_format="dict")
        >>> ctx = dogfood_regression_context(current, save_as_baseline=True)
        >>> ctx["summary"]
        'No baseline found — saved as new baseline.'
    """
    validate_output_format(output_format)

    if not isinstance(current_output, dict):
        raise TypeError("current_output must be a dict")

    if "kind" not in current_output:
        raise ValueError("current_output must have a 'kind' key")

    root_path = Path(root).resolve() if root else Path.cwd()
    subject = subject or current_output.get("subject", "unknown")
    kind = current_output["kind"]

    # Resolve baseline directory
    baseline_path = Path(baseline_dir)
    if not baseline_path.is_absolute():
        baseline_path = root_path / baseline_dir

    # Generate baseline filename
    slug = _slugify(f"{kind}__{subject}")
    baseline_file = baseline_path / f"{slug}.json"

    # Load existing baseline if available
    baseline = _load_baseline(baseline_file)

    # Save as baseline if requested
    if save_as_baseline:
        _save_baseline(baseline_file, current_output)
        if baseline is None:
            summary = "No prior baseline — saved as new baseline."
            diffs = []
        else:
            diffs = _compute_diffs(baseline["output"], current_output)
            summary = (
                f"Saved new baseline. Previous had "
                f"{len(diffs)} diff(s) vs current."
            )

        metrics = {
            "improvements_count": sum(1 for d in diffs if d["verdict"] == "improvement"),
            "regressions_count": sum(1 for d in diffs if d["verdict"] == "regression"),
            "unchanged_count": 0,
            "baseline_age_hours": 0.0,
        }
    elif baseline is None:
        summary = "No baseline found — run with save_as_baseline=True to create one."
        diffs = []
        metrics = {
            "improvements_count": 0,
            "regressions_count": 0,
            "unchanged_count": 0,
            "baseline_age_hours": None,
        }
    else:
        # Compare current against baseline
        diffs = _compute_diffs(baseline["output"], current_output)
        age_hours = _baseline_age_hours(baseline)

        improvements = [d for d in diffs if d["verdict"] == "improvement"]
        regressions = [d for d in diffs if d["verdict"] == "regression"]
        unchanged = [d for d in diffs if d["verdict"] == "unchanged"]

        summary = (
            f"{len(improvements)} improvement(s), "
            f"{len(regressions)} regression(s), "
            f"{len(unchanged)} unchanged vs baseline"
        )

        metrics = {
            "improvements_count": len(improvements),
            "regressions_count": len(regressions),
            "unchanged_count": len(unchanged),
            "baseline_age_hours": round(age_hours, 1),
        }

    # Build findings
    findings = [summary]
    if diffs:
        for d in diffs[:5]:
            findings.append(
                f"[{d['verdict'].upper()}] {d['field']}: "
                f"{_truncate(str(d['baseline']))} → {_truncate(str(d['current']))}"
            )

    # Risks
    risks = []
    if metrics["regressions_count"] > 0:
        risks.append(
            f"{metrics['regressions_count']} regression(s) detected — "
            f"review before merging."
        )
    if metrics.get("baseline_age_hours") and metrics["baseline_age_hours"] > 168:
        risks.append("Baseline is >7 days old — consider refreshing.")

    # Suggested actions
    suggested = []

    # ── Graph wiring (audit fix) ──

    suggested.append("If regressions found: Run anchor(\"trace\", \"error details\") to diagnose.")

    suggested.append("If clean: Run anchor(\"gate\", actions_taken=[...]) to close workflow.")

    suggested.append(
        "At learning closure, capture only evidence-backed reusable regression findings; "
        "otherwise assess nothing_reusable_learned."
    )
    if baseline is None and not save_as_baseline:
        suggested.append("Run with save_as_baseline=True to create initial baseline.")
    if metrics["regressions_count"] > 0:
        suggested.append("Investigate regressions before merging changes.")
        suggested.append("If regressions are intentional, re-save baseline.")
    if metrics["improvements_count"] > 0 and not save_as_baseline:
        suggested.append("Save as new baseline to lock in improvements.")

    # anchor() workflow hints
    if metrics["regressions_count"] > 0:
        suggested.append(
            "MUST: Run anchor('preflight', changed_files=[...]) to confirm no syntax/type errors "
            "before investigating regressions."
        )
    suggested.append(
        "MUST: Run anchor('gate', actions_taken=[...]) after resolving any regressions."
    )

    ctx: dict[str, Any] = {
        "kind": "dogfood_regression_context",
        "subject": f"{kind}::{subject}",
        "summary": summary,
        "metrics": metrics,
        "findings": findings,
        "risks": risks,
        "diffs": diffs,
        "baseline_path": (
            str(baseline_file.relative_to(root_path))
            if baseline_file.is_relative_to(root_path)
            else str(baseline_file)
        ),
        "samples": {},
        "suggested_next_actions": suggested,
    }

    if output_format == "markdown":
        return render_dogfood_regression_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_dogfood_regression_report(ctx: dict[str, Any]) -> str:
    """Render dogfood_regression_context output as markdown."""
    lines = render_header_lines(ctx, "Dog-Food Regression")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    if ctx["diffs"]:
        lines.extend(["", "## Diffs", ""])
        for d in ctx["diffs"]:
            emoji = {"improvement": "✅", "regression": "❌", "unchanged": "➖"}.get(
                d["verdict"], "❓"
            )
            lines.append(
                f"- {emoji} **{d['field']}**: `{_truncate(str(d['baseline']))}` → "
                f"`{_truncate(str(d['current']))}` ({d['verdict']})"
            )
            if d.get("reason"):
                lines.append(f"  - Reason: {d['reason']}")

    lines.append(f"\n**Baseline path:** `{ctx['baseline_path']}`")

    lines.extend(render_bullet_section(ctx["risks"], "## Risks"))
    lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Suggested Actions"))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal: Baseline I/O
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a safe filename slug."""
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", text)
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("_").lower()


def _load_baseline(path: Path) -> dict[str, Any] | None:
    """Load a baseline file. Returns None if not found."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_baseline(path: Path, output: dict[str, Any]) -> None:
    """Save output as a baseline file with metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "saved_at_epoch": time.time(),
        "output": output,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, default=str)


def _baseline_age_hours(baseline: dict[str, Any]) -> float:
    """Calculate hours since baseline was saved."""
    saved_epoch = baseline.get("saved_at_epoch")
    if saved_epoch is None:
        return 0.0
    return (time.time() - saved_epoch) / 3600.0


# ---------------------------------------------------------------------------
# Internal: Diff Logic
# ---------------------------------------------------------------------------

# Fields to compare and their "better" direction
_METRIC_DIRECTIONS: dict[str, str] = {
    # Higher is better
    "completeness_pct": "higher",
    "uniqueness_pct": "higher",
    "test_coverage_pct": "higher",
    "compliant_count": "higher",
    # Lower is better
    "null_pct": "lower",
    "duplicate_pct": "lower",
    "violation_count": "lower",
    "coverage_gaps_count": "lower",
    "regressions_count": "lower",
    "risk_count": "lower",
}

# Fields to deep-compare
_COMPARE_FIELDS = ["metrics", "grain_analysis", "freshness", "risks", "findings"]


def _compute_diffs(
    baseline_output: dict[str, Any],
    current_output: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compute diffs between baseline and current output."""
    diffs = []

    for field in _COMPARE_FIELDS:
        baseline_val = baseline_output.get(field)
        current_val = current_output.get(field)

        if baseline_val is None and current_val is None:
            continue
        if baseline_val is None and current_val is not None:
            diffs.append({
                "field": field,
                "baseline": None,
                "current": _summarize_value(current_val),
                "verdict": "unchanged",
                "reason": "field added (not in baseline)",
            })
            continue
        if baseline_val is not None and current_val is None:
            diffs.append({
                "field": field,
                "baseline": _summarize_value(baseline_val),
                "current": None,
                "verdict": "regression",
                "reason": "field removed",
            })
            continue

        if isinstance(baseline_val, dict) and isinstance(current_val, dict):
            # Compare dict fields individually
            for key in set(list(baseline_val.keys()) + list(current_val.keys())):
                bv = baseline_val.get(key)
                cv = current_val.get(key)
                if bv != cv:
                    verdict = _classify_change(f"{field}.{key}", bv, cv)
                    diffs.append({
                        "field": f"{field}.{key}",
                        "baseline": bv,
                        "current": cv,
                        "verdict": verdict,
                        "reason": _explain_change(f"{field}.{key}", bv, cv, verdict),
                    })
        elif isinstance(baseline_val, list) and isinstance(current_val, list):
            if baseline_val != current_val:
                verdict = _classify_list_change(field, baseline_val, current_val)
                diffs.append({
                    "field": field,
                    "baseline": _summarize_value(baseline_val),
                    "current": _summarize_value(current_val),
                    "verdict": verdict,
                    "reason": _explain_list_change(field, baseline_val, current_val),
                })
        elif baseline_val != current_val:
            verdict = _classify_change(field, baseline_val, current_val)
            diffs.append({
                "field": field,
                "baseline": baseline_val,
                "current": current_val,
                "verdict": verdict,
                "reason": _explain_change(field, baseline_val, current_val, verdict),
            })

    return diffs


def _classify_change(field: str, baseline_val: Any, current_val: Any) -> str:
    """Classify a change as improvement, regression, or unchanged."""
    # Check known metric directions
    field_key = field.split(".")[-1]
    direction = _METRIC_DIRECTIONS.get(field_key)

    if direction and isinstance(baseline_val, (int, float)) and isinstance(current_val, (int, float)):
        if direction == "higher":
            if current_val > baseline_val:
                return "improvement"
            elif current_val < baseline_val:
                return "regression"
        elif direction == "lower":
            if current_val < baseline_val:
                return "improvement"
            elif current_val > baseline_val:
                return "regression"
        return "unchanged"

    # For risks: fewer is better
    if "risk" in field.lower():
        if isinstance(baseline_val, list) and isinstance(current_val, list):
            if len(current_val) < len(baseline_val):
                return "improvement"
            elif len(current_val) > len(baseline_val):
                return "regression"

    return "unchanged"


def _classify_list_change(field: str, baseline_val: list, current_val: list) -> str:
    """Classify a list change."""
    if "risk" in field.lower():
        if len(current_val) < len(baseline_val):
            return "improvement"
        elif len(current_val) > len(baseline_val):
            return "regression"
    if "finding" in field.lower():
        if len(current_val) > len(baseline_val):
            return "improvement"  # More findings = more detail
    return "unchanged"


def _explain_change(field: str, bv: Any, cv: Any, verdict: str) -> str:
    """Generate a human-readable explanation of a change."""
    if verdict == "improvement":
        return f"value improved from {_truncate(str(bv))} to {_truncate(str(cv))}"
    elif verdict == "regression":
        return f"value regressed from {_truncate(str(bv))} to {_truncate(str(cv))}"
    return f"value changed (neutral)"


def _explain_list_change(field: str, bv: list, cv: list) -> str:
    """Explain a list change."""
    added = len(cv) - len(bv)
    if added > 0:
        return f"{added} item(s) added"
    elif added < 0:
        return f"{abs(added)} item(s) removed"
    return "items changed (same count)"


def _summarize_value(val: Any) -> Any:
    """Summarize a value for display in diffs."""
    if isinstance(val, list):
        if len(val) > 3:
            return f"[{len(val)} items]"
        return val
    if isinstance(val, dict):
        return f"{{{len(val)} keys}}"
    return val


def _truncate(s: str, maxlen: int = 60) -> str:
    """Truncate a string for display."""
    if len(s) <= maxlen:
        return s
    return s[:maxlen - 3] + "..."
