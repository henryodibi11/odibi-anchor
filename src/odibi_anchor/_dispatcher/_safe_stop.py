"""Truthful terminal transition for work that cannot reach delivery gate."""

from __future__ import annotations

from typing import Any


def safe_stop_task(
    *, status: str, reason: str, unavailable_evidence: list[str] | None,
    session_state: Any, session_timings: list[dict[str, Any]], learning_project_id: str,
    pre_candidate_producer: bool = False,
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
    if getattr(session_state, "terminal_status", None) == "completed":
        raise RuntimeError(
            "safe_stop is invalid for a task already closed as completed; report it as completed"
        )

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
    # Stopping directly needs a task that owes no learning: a read-only task that never
    # opened an obligation (assessing would close it as completed), or a workflow producer
    # before its candidate (assessing is refused because it would freeze the producer).
    # The workflow itself is untouched, so a fresh producer can take it over.
    read_only_unassessed = closed is None and (
        session_state.active_task_profile.execution_mode == "read_only" or pre_candidate_producer
    )
    if read_only_unassessed and pre_candidate_producer:
        _refuse_stranded_source_edits(session_state)
    if not read_only_unassessed and (closed is None or closed["status"] != "assessed"):
        raise RuntimeError(
            "safe_stop requires a closed structured-learning assessment; call "
            "anchor('learning', 'assess', outcome='nothing_reusable_learned') first"
        )

    from odibi_anchor._dispatcher._operating_protocol import latest_delivery_gate_passed

    if latest_delivery_gate_passed(
        session_timings, epoch=session_state.task_verification_epoch,
    ):
        raise RuntimeError(
            "safe_stop is invalid after a successful delivery gate; close the task with "
            "anchor('learning', 'assess', outcome='nothing_reusable_learned') instead"
        )

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
            "learning_assessed": not read_only_unassessed,
            "unavailable_evidence_count": len(normalized_evidence),
        },
        findings=[
            "Task stopped without a learning obligation; nothing was assessed." if read_only_unassessed
            else "Structured learning assessment is closed."
        ],
        risks=[f"Unavailable evidence: {item}" for item in normalized_evidence],
        samples={"unavailable_evidence": normalized_evidence},
        suggested_next_actions=[],
    )


def _refuse_stranded_source_edits(session_state: Any) -> None:
    """Keep a source producer open while its own edits differ from its start baseline.

    A stopped task's uncommitted edits cannot be adopted, and a fresh source producer needs
    a clean worktree, so stopping now would strand the workflow. Unknown state fails closed.
    """
    if session_state.active_task_profile.execution_mode != "source_change":
        return
    try:
        from odibi_anchor._repository_snapshot import capture_task_change_scope

        changed = list(capture_task_change_scope(
            session_state.task_repository_baseline, session_state.task_repository_write_fingerprints,
        ).changed_paths)
    except Exception as exc:  # any unreadable state must not permit the stop
        raise RuntimeError(
            f"safe_stop cannot verify this producer's source changes ({type(exc).__name__}); keep the "
            "task open and resume it later with anchor('task_rebind', task_window_id=...)"
        ) from exc
    if changed:
        shown = ", ".join(changed[:10])
        raise RuntimeError(
            f"safe_stop would strand uncommitted source changes: {shown}. A fresh producer cannot "
            "take over a dirty worktree and a stopped task's edits cannot be adopted. Restore these "
            "files to the task's start state, then stop; or keep the task open and resume it later "
            f"with anchor('task_rebind', task_window_id={session_state.task_window_id!r})."
        )
