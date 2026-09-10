"""odibi_anchor.planning.handoff_context — Structured cross-thread handoff packets.

Bundles decisions, blockers, evidence chain summaries, artifacts, and continuation
instructions into a compact, structured packet that survives thread boundaries
with minimal signal loss.

Solves the "40% signal loss" problem: when an agent hands off work between threads
or sessions, narrative summaries lose structure. This tool produces a machine-readable
handoff that the receiving agent can parse immediately.

Usage:
    from odibi_anchor.planning import handoff_context

    ctx = handoff_context(
        task="Merge bronze pipeline into silver",
        state="in_progress",
        decisions=["Used SCD2 for customer dim", "Chose hash-based dedup"],
        blockers=["Waiting for source schema freeze"],
        evidence_chain=[
            {"tool": "quality_gate_context", "status": "pass", "summary": "Write-safe"},
            {"tool": "profile_table", "status": "done", "summary": "50K rows, 3 high-null cols"},
        ],
        artifacts=[
            {"path": "notebooks/bronze_to_silver.py", "role": "source", "note": "Main transform"},
        ],
        next_action="Run validation_summary_context on merged output",
        context_needed=["target schema", "SCD2 history table"],
        skip=["Source profiling — already done"],
    )

Dependencies: stdlib only.
"""

from __future__ import annotations

from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


def handoff_context(
    task: str,
    *,
    state: str = "in_progress",
    goal: str | None = None,
    decisions: list[str] | None = None,
    rejected_alternatives: list[str] | None = None,
    blockers: list[str] | None = None,
    evidence_chain: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    next_action: str | None = None,
    context_needed: list[str] | None = None,
    skip: list[str] | None = None,
    open_questions: list[str] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
) -> "dict[str, Any] | str":
    """Generate a structured cross-thread handoff packet.

    Compresses multi-tool evidence chains, decisions, blockers, and
    continuation instructions into a compact packet optimized for
    agent-to-agent or agent-to-human thread transitions.

    Args:
        task: What the work is about (one sentence).
        state: Current state of the work. One of: "not_started",
            "in_progress", "blocked", "ready_for_review", "complete".
        goal: Desired outcome (optional but recommended).
        decisions: Key decisions made and why.
        rejected_alternatives: Approaches tried and rejected.
        blockers: Current blockers with resolution path if known.
        evidence_chain: Compressed tool outputs. Each dict should have
            at minimum ``tool`` and ``summary``. Optional: ``status``.
        artifacts: File paths and their roles. Each dict should have
            ``path`` and ``role`` (source/target/test/config/output).
            Optional: ``note``.
        next_action: The exact next step the receiver should take.
        context_needed: What the receiver needs to load/read first.
        skip: What's already done and should NOT be repeated.
        open_questions: Unresolved questions to carry forward.
        subject: Human label. Defaults to first 50 chars of task.
        output_format: "dict" or "markdown".

    Returns:
        Structured handoff dict or markdown string.

    Raises:
        ValueError: If parameters are invalid.

    Example:
        >>> ctx = handoff_context("Build silver pipeline", state="blocked",
        ...     blockers=["Schema not finalized"], next_action="Wait for DBA")
        >>> ctx["kind"]
        'handoff_context'
        >>> ctx["state"]
        'blocked'
    """
    validate_output_format(output_format)

    valid_states = {"not_started", "in_progress", "blocked", "ready_for_review", "complete"}
    if state not in valid_states:
        raise ValueError(f"state must be one of {sorted(valid_states)}, got {state!r}")

    if not task or not task.strip():
        raise ValueError("task must be a non-empty string")

    subject = subject or task[:50].strip()
    decisions = decisions or []
    rejected_alternatives = rejected_alternatives or []
    blockers = blockers or []
    evidence_chain = evidence_chain or []
    artifacts = artifacts or []
    context_needed = context_needed or []
    skip = skip or []
    open_questions = open_questions or []

    # Validate evidence_chain entries
    for i, entry in enumerate(evidence_chain):
        if not isinstance(entry, dict):
            raise ValueError(f"evidence_chain[{i}] must be a dict")
        if "tool" not in entry or "summary" not in entry:
            raise ValueError(f"evidence_chain[{i}] must have 'tool' and 'summary' keys")

    # Validate artifacts entries
    for i, entry in enumerate(artifacts):
        if not isinstance(entry, dict):
            raise ValueError(f"artifacts[{i}] must be a dict")
        if "path" not in entry or "role" not in entry:
            raise ValueError(f"artifacts[{i}] must have 'path' and 'role' keys")

    # Build summary
    summary_parts = [f"{subject}: {state.replace('_', ' ')}"]
    if blockers:
        summary_parts.append(f"{len(blockers)} blocker(s)")
    if evidence_chain:
        summary_parts.append(f"{len(evidence_chain)} evidence step(s)")
    if next_action:
        summary_parts.append(f"next: {next_action[:60]}")
    summary = ". ".join(summary_parts) + "."

    # Build findings
    findings: list[str] = []
    if decisions:
        findings.append(f"{len(decisions)} decision(s) recorded.")
    if evidence_chain:
        passed = sum(1 for e in evidence_chain if e.get("status") in {"pass", "passed", "done"})
        findings.append(f"{passed}/{len(evidence_chain)} evidence steps completed successfully.")
    if artifacts:
        findings.append(f"{len(artifacts)} artifact(s) referenced.")

    # Build risks
    risks: list[str] = []
    if blockers:
        for b in blockers[:5]:
            risks.append(f"BLOCKER: {b}")
    if open_questions:
        for q in open_questions[:3]:
            risks.append(f"OPEN: {q}")
    if not next_action:
        risks.append("No explicit next action — receiver must determine continuation.")

    # Build suggested next actions
    suggested: list[str] = []
    if context_needed:
        suggested.append(f"Load context: {', '.join(context_needed[:5])}")
    if next_action:
        suggested.append(next_action)
    if blockers:
        suggested.append(f"MUST: Resolve blocker(s): {blockers[0]}")
    if open_questions:
        suggested.append(f"MUST: Address open question: {open_questions[0]}")

    # anchor() workflow hints for the receiving agent
    suggested.append(
        "MUST: Run anchor('task', ..., known_facts=[...]) with this handoff as evidence "
        "before starting work."
    )
    if evidence_chain:
        suggested.append(
            "MUST: Review evidence_chain entries — do NOT re-run tools that already passed."
        )
    # ── Graph wiring (audit fix) ──
    suggested.append("MUST: Run anchor(\"snapshot\", decisions=[...]) to persist session state.")
    suggested.append(
        "MUST: Close learning through anchor(\"learning\", \"assess\", ...); "
        "capture genuine observations first when they exist."
    )

    # Build metrics
    metrics = {
        "decision_count": len(decisions),
        "blocker_count": len(blockers),
        "evidence_step_count": len(evidence_chain),
        "artifact_count": len(artifacts),
        "open_question_count": len(open_questions),
        "skip_count": len(skip),
        "is_blocked": len(blockers) > 0,
        "is_actionable": next_action is not None and len(blockers) == 0,
    }

    ctx: dict[str, Any] = {
        "kind": "handoff_context",
        "subject": subject,
        "summary": summary,
        "state": state,
        "task": task,
        "goal": goal,
        "metrics": metrics,
        "decisions": decisions,
        "rejected_alternatives": rejected_alternatives,
        "blockers": blockers,
        "evidence_chain": evidence_chain,
        "artifacts": artifacts,
        "continuation": {
            "next_action": next_action,
            "context_needed": context_needed,
            "skip": skip,
        },
        "open_questions": open_questions,
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": suggested,
    }

    if output_format == "markdown":
        return render_handoff_report(ctx)
    return ctx


def render_handoff_report(ctx: dict[str, Any]) -> str:
    """Render a handoff context dict as markdown.

    Args:
        ctx: Dictionary from ``handoff_context()``.

    Returns:
        Markdown-formatted string.

    Raises:
        ValueError: If ctx is missing required keys.
    """
    required = {"kind", "subject", "state", "task", "summary"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    state = ctx["state"]
    icon = {
        "not_started": "\u23f3",
        "in_progress": "\U0001f6a7",
        "blocked": "\u274c",
        "ready_for_review": "\U0001f4cb",
        "complete": "\u2705",
    }.get(state, "\u2753")

    lines.append(f"# Handoff: {ctx['subject']}")
    lines.append("")
    lines.append(f"{icon} **State:** {state.replace('_', ' ').upper()}")
    lines.append("")
    lines.append(f"**Task:** {ctx['task']}")
    if ctx.get("goal"):
        lines.append(f"**Goal:** {ctx['goal']}")
    lines.append("")
    lines.append(f"**Summary:** {ctx['summary']}")
    lines.append("")

    # Decisions
    if ctx.get("decisions"):
        lines.append("## Decisions Made")
        lines.append("")
        for d in ctx["decisions"]:
            lines.append(f"* {d}")
        lines.append("")

    # Rejected alternatives
    if ctx.get("rejected_alternatives"):
        lines.append("## Rejected Alternatives")
        lines.append("")
        for r in ctx["rejected_alternatives"]:
            lines.append(f"* {r}")
        lines.append("")

    # Blockers
    if ctx.get("blockers"):
        lines.append("## Blockers")
        lines.append("")
        for b in ctx["blockers"]:
            lines.append(f"* \u274c {b}")
        lines.append("")

    # Evidence chain
    if ctx.get("evidence_chain"):
        lines.append("## Evidence Chain")
        lines.append("")
        lines.append("| Tool | Status | Summary |")
        lines.append("| --- | --- | --- |")
        for e in ctx["evidence_chain"]:
            status = e.get("status", "—")
            lines.append(f"| {e['tool']} | {status} | {e['summary']} |")
        lines.append("")

    # Artifacts
    if ctx.get("artifacts"):
        lines.append("## Artifacts")
        lines.append("")
        for a in ctx["artifacts"]:
            note = f" — {a['note']}" if a.get("note") else ""
            lines.append(f"* `{a['path']}` [{a['role']}]{note}")
        lines.append("")

    # Continuation
    cont = ctx.get("continuation", {})
    if cont.get("next_action") or cont.get("context_needed") or cont.get("skip"):
        lines.append("## Continuation")
        lines.append("")
        if cont.get("next_action"):
            lines.append(f"**Next action:** {cont['next_action']}")
        if cont.get("context_needed"):
            lines.append(f"**Load first:** {', '.join(cont['context_needed'])}")
        if cont.get("skip"):
            lines.append(f"**Skip (already done):** {', '.join(cont['skip'])}")
        lines.append("")

    # Open questions
    if ctx.get("open_questions"):
        lines.append("## Open Questions")
        lines.append("")
        for q in ctx["open_questions"]:
            lines.append(f"* {q}")
        lines.append("")

    return "\n".join(lines)
