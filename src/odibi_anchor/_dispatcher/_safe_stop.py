"""Truthful terminal transition for work that cannot reach delivery gate."""

from __future__ import annotations

from typing import Any


def safe_stop_task(
    *, status: str, reason: str, unavailable_evidence: list[str] | None,
    session_state: Any, session_timings: list[dict[str, Any]], learning_project_id: str,
) -> dict[str, Any]:
    """Close blocked or failed work without implying successful verification."""
    if status not in {"blocked", "failed"}:
        raise ValueError("safe_stop status must be 'blocked' or 'failed'")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("safe_stop requires a non-empty reason")
    evidence = unavailable_evidence or []
    if not isinstance(evidence, list) or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        raise TypeError("unavailable_evidence must be a list of non-empty strings")
    if session_state.active_task_profile is None:
        raise RuntimeError("safe_stop requires an accepted task")

    from odibi_anchor.codebase.structured_learning_context import (
        active_learning_obligation,
        latest_closed_learning_obligation,
    )

    owner = {
        "project_id": learning_project_id,
        "task_window_id": session_state.task_window_id,
    }
    if active_learning_obligation(**owner) is not None:
        raise RuntimeError(
            "safe_stop requires learning assessment first; call "
            "anchor('learning', 'assess', outcome=...)"
        )
    closed = latest_closed_learning_obligation(**owner)
    if closed is None or closed["status"] != "assessed":
        raise RuntimeError("safe_stop requires a closed structured-learning assessment")

    from odibi_anchor._dispatcher._operating_protocol import latest_delivery_gate_passed

    if latest_delivery_gate_passed(
        session_timings, epoch=session_state.task_verification_epoch,
    ):
        raise RuntimeError("safe_stop is invalid after a successful delivery gate")

    normalized_reason = " ".join(reason.split())
    normalized_evidence = [" ".join(item.split()) for item in evidence]
    session_state.terminal_status = status
    session_state.terminal_basis = "safe_stop"
    session_state.terminal_reason = normalized_reason

    from odibi_anchor._utils.contract import build_base_context

    return build_base_context(
        kind="safe_stop",
        subject=session_state.task_goal or "accepted task",
        summary=f"Task closed as {status}: {normalized_reason}",
        metrics={
            "terminal_status": status,
            "gate_passed": False,
            "learning_assessed": True,
            "unavailable_evidence_count": len(normalized_evidence),
        },
        findings=["Structured learning assessment is closed."],
        risks=[f"Unavailable evidence: {item}" for item in normalized_evidence],
        samples={"unavailable_evidence": normalized_evidence},
        suggested_next_actions=[],
    )
