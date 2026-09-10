"""odibi_anchor.codebase.session_snapshot_context — Session continuity for AI agents.

Captures the current state of a working session — not what was done (that's
the conversation summary), but what the world looks like right now plus the
reasoning behind decisions. Think of it as a save-game for AI coding sessions.

The tool auto-scans codebase state (files, hashes, line counts) and packages
it with manually-provided reasoning (decisions, rejected alternatives, open
questions) into a structured dict that can be saved to disk and loaded at the
start of the next session.

Usage:
    from odibi_anchor.codebase import session_snapshot_context

    snapshot = session_snapshot_context(
        root="/path/to/project",
        decisions=["Used stdlib AST for zero deps"],
        next_steps=["Build session_snapshot_context"],
    )
    save_snapshot(snapshot, "/path/to/project/.session_snapshot.json")

    # Next session:
    prev = load_snapshot("/path/to/project/.session_snapshot.json")
    # Immediately know: test state, decisions, what changed

Dependencies: stdlib only (hashlib, json, os, pathlib, time).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


def session_snapshot_context(
    root: str | Path,
    *,
    mode: str = "full",
    summary: str | None = None,
    state: str | None = None,
    subject: str | None = None,
    decisions: list[str] | None = None,
    rejected_alternatives: list[str] | None = None,
    open_questions: list[str] | None = None,
    next_steps: list[str] | None = None,
    session_notes: str | None = None,
    test_state: dict[str, int] | None = None,
    previous_snapshot: dict[str, Any] | None = None,
    conventions: dict[str, str] | None = None,
    compact: bool = False,
    output_format: str = "dict",
    **kwargs: Any,
) -> "dict[str, Any] | str":
    """Capture session state (mode=full) or generate a handoff packet (mode=handoff).

    When mode='handoff', delegates to handoff_context for a compact transfer brief.

    Auto-scans the codebase for file state (names, sizes, hashes, line counts)
    and packages it with reasoning context into a structured snapshot that can
    be persisted and loaded in the next session.

    Args:
        root: Project root directory to scan.
        subject: Human label. Defaults to directory name.
        decisions: Key decisions made and why (e.g., "Chose X over Y because Z").
        rejected_alternatives: Approaches considered and rejected (prevents re-exploration).
        open_questions: Unresolved threads to carry forward.
        next_steps: Logical continuation points for the next session.
        session_notes: Free-form notes about the session state.
        test_state: Test results dict (e.g., {"passed": 416, "skipped": 1, "failed": 0}).
            If not provided, the snapshot will not include test state.
        previous_snapshot: A prior snapshot dict. When provided, the output includes
            a "changes_since" field showing which files were added/modified/removed.
        conventions: Project conventions as key-value pairs. Keys are convention
            names, values are the rules. These persist across sessions and prevent
            agents from re-inferring patterns by reading files.
            Example: {"file_naming": "{module}_context.py + render_{module}_report()",
                      "test_naming": "tests/{package}/test_{module}_context.py"}
        compact: If True, omit file_states from output (reduces token count by
            ~60% for large projects). The full scan still runs internally for
            metrics and diff computation; only the output payload is trimmed.
        output_format: "dict" or "markdown".

    Returns:
        Structured snapshot dict (or markdown string).

    Example:
        >>> snapshot = session_snapshot_context("/path/to/project",
        ...     decisions=["Used AST for zero deps"],
        ...     next_steps=["Write tests"],
        ... )
        >>> snapshot["kind"]
        'session_snapshot_context'
        >>> compact = session_snapshot_context("/path/to/project", compact=True)
        >>> "file_states" not in compact
        True
    """
    # ── Mode dispatch: handoff delegates to handoff_context ──
    if mode == "handoff":
        from odibi_anchor.planning.handoff_context import handoff_context
        return handoff_context(
            task=summary or subject or "session handoff",
            state=state or "in_progress",
            decisions=decisions,
            rejected_alternatives=rejected_alternatives,
            open_questions=open_questions,
            next_action=next_steps[0] if next_steps else None,
            subject=subject,
            output_format=output_format,
            **kwargs,
        )

    validate_output_format(output_format)

    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory: {root}")

    subject = subject or root.name

    # Auto-scan: file inventory with hashes
    file_states = _scan_file_states(root)

    # Compute metrics
    metrics = _compute_snapshot_metrics(file_states)

    # Diff against previous snapshot
    changes_since: dict[str, Any] | None = None
    if previous_snapshot and "file_states" in previous_snapshot:
        changes_since = _diff_snapshots(previous_snapshot["file_states"], file_states)

    # Summary
    summary_parts = [f"{subject}: {metrics['file_count']} files, {metrics['total_lines']} lines"]
    if test_state:
        summary_parts.append(f"{test_state.get('passed', 0)} tests passing")
    if changes_since:
        n_changes = (
            len(changes_since.get("added", []))
            + len(changes_since.get("modified", []))
            + len(changes_since.get("removed", []))
        )
        summary_parts.append(f"{n_changes} file(s) changed since last snapshot")
    summary = ". ".join(summary_parts) + "."

    # Build findings
    findings = _build_snapshot_findings(metrics, test_state, changes_since, decisions, conventions)
    risks = _build_snapshot_risks(test_state, open_questions)

    ctx: dict[str, Any] = {
        "kind": "session_snapshot_context",
        "subject": subject,
        "summary": summary,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "metrics": metrics,
        "test_state": test_state,
        "changes_since": changes_since,
        "decisions": decisions or [],
        "rejected_alternatives": rejected_alternatives or [],
        "open_questions": open_questions or [],
        "next_steps": next_steps or [],
        "session_notes": session_notes,
        "conventions": conventions or {},
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": (next_steps or []) + [
            "MUST: Run anchor('snapshot', mode='handoff', summary=task, state='in_progress', decisions=[...]) "
            "if passing work to another thread.",
            "MUST: Close post-gate debt with anchor('learning', 'capture|assess', ...); never invent content.",
        ],
    }

    # Include file_states only in non-compact mode
    if not compact:
        ctx["file_states"] = file_states

    if output_format == "markdown":
        return render_session_snapshot_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# File Scanning
# ---------------------------------------------------------------------------

def _scan_file_states(root: Path) -> dict[str, dict[str, Any]]:
    """Scan all tracked files and compute hashes + line counts."""
    states: dict[str, dict[str, Any]] = {}
    tracked_extensions = {".py", ".md", ".yaml", ".yml", ".toml", ".cfg", ".txt", ".json"}

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden dirs, __pycache__, .git
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d != "__pycache__" and d != "node_modules"
        ]
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()
            if ext not in tracked_extensions:
                continue

            rel = str(fpath.relative_to(root)).replace("\\", "/")
            try:
                content = fpath.read_bytes()
                text = content.decode("utf-8", errors="replace")
                line_count = text.count("\n") + 1
                content_hash = hashlib.sha256(content).hexdigest()[:12]
                size = len(content)
            except (OSError, PermissionError):
                continue

            states[rel] = {
                "lines": line_count,
                "size": size,
                "hash": content_hash,
            }

    return states


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _compute_snapshot_metrics(file_states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute summary metrics from file states."""
    total_lines = sum(f["lines"] for f in file_states.values())
    total_size = sum(f["size"] for f in file_states.values())
    py_files = [p for p in file_states if p.endswith(".py")]
    md_files = [p for p in file_states if p.endswith(".md")]

    return {
        "file_count": len(file_states),
        "total_lines": total_lines,
        "total_size_bytes": total_size,
        "py_file_count": len(py_files),
        "md_file_count": len(md_files),
    }


# ---------------------------------------------------------------------------
# Snapshot Diffing
# ---------------------------------------------------------------------------

def _diff_snapshots(
    old_states: dict[str, dict[str, Any]],
    new_states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compare two file state dicts to find what changed."""
    old_keys = set(old_states.keys())
    new_keys = set(new_states.keys())

    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    modified = sorted(
        p for p in old_keys & new_keys
        if old_states[p]["hash"] != new_states[p]["hash"]
    )

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged_count": len(old_keys & new_keys) - len(modified),
    }


# ---------------------------------------------------------------------------
# Findings & Risks
# ---------------------------------------------------------------------------

def _build_snapshot_findings(
    metrics: dict[str, Any],
    test_state: dict[str, int] | None,
    changes_since: dict[str, Any] | None,
    decisions: list[str] | None,
    conventions: dict[str, str] | None = None,
) -> list[str]:
    """Build findings for the snapshot."""
    findings: list[str] = []

    findings.append(
        f"{metrics['file_count']} tracked files "
        f"({metrics['py_file_count']} Python, {metrics['md_file_count']} Markdown)."
    )

    if test_state:
        passed = test_state.get("passed", 0)
        failed = test_state.get("failed", 0)
        if failed == 0:
            findings.append(f"All {passed} tests passing.")
        else:
            findings.append(f"TEST FAILURES: {failed} failed, {passed} passed.")

    if changes_since:
        added = len(changes_since.get("added", []))
        modified = len(changes_since.get("modified", []))
        removed = len(changes_since.get("removed", []))
        if added + modified + removed > 0:
            findings.append(
                f"Changes since last snapshot: {added} added, "
                f"{modified} modified, {removed} removed."
            )
        else:
            findings.append("No file changes since last snapshot.")

    if decisions:
        findings.append(f"{len(decisions)} decision(s) recorded.")

    if conventions:
        findings.append(f"{len(conventions)} convention(s) documented.")

    return findings


def _build_snapshot_risks(
    test_state: dict[str, int] | None,
    open_questions: list[str] | None,
) -> list[str]:
    """Build risks for the snapshot."""
    risks: list[str] = []

    if test_state and test_state.get("failed", 0) > 0:
        risks.append(f"{test_state['failed']} test(s) failing — fix before continuing.")

    if open_questions:
        risks.append(f"{len(open_questions)} open question(s) need resolution.")

    return risks


# ---------------------------------------------------------------------------
# Save / Load Helpers
# ---------------------------------------------------------------------------

def save_snapshot(snapshot: dict[str, Any], path: str | Path, *, root: str | Path | None = None) -> str:
    """Save a snapshot dict to a JSON file.

    Args:
        snapshot: The snapshot dict from session_snapshot_context().
        path: File path to write (typically .session_snapshot.json).
            If relative and root is provided, resolved against root.
        root: Optional project root. When provided, relative paths are
            resolved against this directory instead of the current working
            directory.

    Returns:
        The absolute path where the snapshot was written.

    Raises:
        OSError: If the file cannot be written.
    """
    path = Path(path)
    if not path.is_absolute() and root is not None:
        path = Path(root).resolve() / path
    else:
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, default=str)
    return str(path)


def load_snapshot(path: str | Path) -> dict[str, Any] | None:
    """Load a previously saved snapshot from disk.

    Args:
        path: File path to read.

    Returns:
        The snapshot dict, or None if the file doesn't exist.
    """
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_session_snapshot_report(ctx: dict[str, Any]) -> str:
    """Render a session snapshot as a markdown report.

    Args:
        ctx: Dictionary from session_snapshot_context().

    Returns:
        Human-readable markdown string.

    Raises:
        ValueError: If required keys are missing.
    """
    required = {"kind", "subject", "summary", "metrics"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []

    # Header
    lines.append(f"# Session Snapshot: {ctx['subject']}")
    lines.append("")
    lines.append(f"> {ctx['summary']}")
    lines.append("")
    if ctx.get("timestamp"):
        lines.append(f"*Captured: {ctx['timestamp']}*")
        lines.append("")

    # Metrics
    metrics = ctx["metrics"]
    lines.append("## Codebase State")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Files tracked | {metrics['file_count']} |")
    lines.append(f"| Total lines | {metrics['total_lines']} |")
    lines.append(f"| Python files | {metrics['py_file_count']} |")
    lines.append(f"| Docs (md) | {metrics['md_file_count']} |")
    lines.append("")

    # Test state
    test_state = ctx.get("test_state")
    if test_state:
        lines.append("## Test State")
        lines.append("")
        passed = test_state.get("passed", 0)
        failed = test_state.get("failed", 0)
        skipped = test_state.get("skipped", 0)
        status = "PASSING" if failed == 0 else "FAILING"
        lines.append(f"**{status}** — {passed} passed, {failed} failed, {skipped} skipped")
        lines.append("")

    # Changes since
    changes = ctx.get("changes_since")
    if changes:
        lines.append("## Changes Since Last Snapshot")
        lines.append("")
        if changes.get("added"):
            lines.append(f"**Added ({len(changes['added'])}):**")
            for f in changes["added"][:10]:
                lines.append(f"- `{f}`")
            lines.append("")
        if changes.get("modified"):
            lines.append(f"**Modified ({len(changes['modified'])}):**")
            for f in changes["modified"][:10]:
                lines.append(f"- `{f}`")
            lines.append("")
        if changes.get("removed"):
            lines.append(f"**Removed ({len(changes['removed'])}):**")
            for f in changes["removed"][:10]:
                lines.append(f"- `{f}`")
            lines.append("")

    # Decisions
    decisions = ctx.get("decisions", [])
    if decisions:
        lines.append("## Decisions Made")
        lines.append("")
        for d in decisions:
            lines.append(f"- {d}")
        lines.append("")

    # Conventions
    conventions = ctx.get("conventions", {})
    if conventions:
        lines.append("## Conventions")
        lines.append("")
        for key, value in conventions.items():
            lines.append(f"- **{key}:** {value}")
        lines.append("")

    # Rejected
    rejected = ctx.get("rejected_alternatives", [])
    if rejected:
        lines.append("## Rejected Alternatives")
        lines.append("")
        for r in rejected:
            lines.append(f"- {r}")
        lines.append("")

    # Open questions
    questions = ctx.get("open_questions", [])
    if questions:
        lines.append("## Open Questions")
        lines.append("")
        for q in questions:
            lines.append(f"- {q}")
        lines.append("")

    # Next steps
    next_steps = ctx.get("next_steps", [])
    if next_steps:
        lines.append("## Next Steps")
        lines.append("")
        for i, s in enumerate(next_steps, 1):
            lines.append(f"{i}. {s}")
        lines.append("")

    # Notes
    notes = ctx.get("session_notes")
    if notes:
        lines.append("## Session Notes")
        lines.append("")
        lines.append(notes)
        lines.append("")

    return "\n".join(lines)
