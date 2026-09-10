"""Compatibility-only historical learning recovery.

``learn_context`` remains callable only when old persisted debt technically
requires its historical event-payload behavior. Normal learning uses
``anchor("learning", "capture|assess", ...)``. The compatibility path processes
caller-supplied events and can append candidate entries or telemetry records.

Dependencies: stdlib only (json, time, pathlib).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format, build_base_context
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


_TELEMETRY_FILE = ".telemetry.jsonl"

_SUPPORTED_EVENT_TYPES = {
    "error", "edit", "confirm", "discovery", "pattern", "tool_call",
    "decision", "convention", "gotcha", "failure_pattern", "preference",
}


def learn_context(
    root: str | Path,
    *,
    session_events: list[dict[str, Any]] | None = None,
    tool_usage: list[dict[str, Any]] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
    db_path: str | None = None,
    project: str | None = None,
    problem_id: str | None = None,
) -> dict[str, Any] | str:
    """Process compatibility-only historical learning event payloads.

    Use this callable only when old persisted debt technically requires it.
    Normal learning uses ``anchor("learning", "capture|assess", ...)``. This
    compatibility behavior processes caller-supplied errors, edits, and tool
    usage, appends candidate entries, and logs tool-effectiveness telemetry.

    Args:
        root: Project root directory.
        session_events: List of event dicts describing what happened.
            Each event has a "type" key. Supported types:
            - "error": Resolved error. Fields: text, fix, resolved (bool).
            - "edit": Propagation discovery. Fields: file, forgot (list[str]).
            - "confirm": Compatibility-only blocked confirmation request. Fields: memory_id.
              This route never promotes; use governed typed-verifier or authenticated owner
              promotion commands instead.
            - "discovery": Novel insight. Fields: text/content, source (optional).
            - "pattern": Reusable pattern found. Fields: text/content, context (optional).
            - "tool_call": Tool usage record. Fields: tool, result, useful (bool).
            - "decision": Technical decision made. Fields: text/content, tags (optional).
            - "convention": Convention established. Fields: text/content, tags (optional).
            - "gotcha": Non-obvious pitfall found. Fields: text/content, tags (optional).
            - "failure_pattern": Failure mode to avoid. Fields: text/content, tags (optional).
            - "preference": User/project preference. Fields: text/content, tags (optional).
        tool_usage: Optional list of tool usage records for telemetry.
            Each dict: {"tool": str, "was_helpful": bool, "outcome": str}.
        subject: Human label. Defaults to directory name.
        output_format: "dict" or "markdown".
        problem_id: Optional active Problem Record ID used to tag distilled memories.

    Returns:
        Structured context dict with learning outcomes.
    """
    validate_output_format(output_format)

    # ── Input validation with helpful error messages ──
    if session_events is None:
        session_events = []
    if not isinstance(session_events, list):
        raise TypeError(
            f"session_events must be a list of dicts, got {type(session_events).__name__}.\n"
            f"Compatibility-only historical recovery example: anchor(\"learn\", session_events=["
            f'{{"type": "discovery", "detail": "what you learned"}}])'
        )
    for i, event in enumerate(session_events):
        if not isinstance(event, dict):
            raise TypeError(
                f"session_events[{i}] must be a dict, got {type(event).__name__}.\n"
                f"Expected: {{\"type\": \"discovery|decision|convention|gotcha\", "
                f"\"detail\": \"what happened\"}}"
            )
        if "type" not in event:
            supported = ", ".join(sorted(_SUPPORTED_EVENT_TYPES))
            # Accept 'detail'/'content' as the text field
            raise ValueError(
                f"session_events[{i}] is missing 'type' key.\n"
                f"Supported types: {supported}\n"
                f"Example: {{\"type\": \"discovery\", \"detail\": \"what you learned\"}}"
            )

    root = Path(root).resolve()
    subject = subject or root.name

    def _memory_tags(*base: str) -> list[str]:
        tags = list(base)
        if problem_id:
            tags.append(f"problem:{problem_id}")
        return tags

    memories_added: list[dict[str, Any]] = []
    memories_confirmed: list[dict[str, Any]] = []
    telemetry_logged: list[dict[str, Any]] = []
    findings: list[str] = []
    unknown_events_skipped = 0

    # Process events
    for event in session_events:
        event_type = event.get("type", "")

        if event_type == "error" and event.get("resolved"):
            # Check if this error pattern is already known
            from odibi_anchor.codebase.memory_context import memory_context as query_memory, append_memory
            existing = query_memory(
                str(root),
                error_text=event.get("text", ""),
                entry_type="gotcha",
                limit=1,
                surfaced=False,
                db_path=db_path,
                project=project,
            )
            if not existing.get("entries"):
                # Novel error — record it with provenance
                entry = append_memory(
                    str(root),
                    entry_type="gotcha",
                    content=f"Error: {event.get('text', '')[:100]} → Fix: {event.get('fix', '')}",
                    tags=_memory_tags("auto-learned", "error-fix"),
                    source="learn_context",
                    confidence=0.4,
                    evidence={
                        "error_text": event.get("text", "")[:200],
                        "fix": event.get("fix", ""),
                        "session": event.get("session_id", ""),
                    },
                    db_path=db_path,
                    project=project,
                )
                if entry.get("action") == "inserted":
                    memories_added.append(entry)

        elif event_type == "edit" and event.get("forgot"):
            # Propagation pattern discovered
            from odibi_anchor.codebase.memory_context import append_memory
            forgot_files = event.get("forgot", [])
            edited_file = event.get("file", "unknown")
            entry = append_memory(
                str(root),
                entry_type="gotcha",
                content=f"Editing {edited_file} also requires updating: {', '.join(forgot_files)}",
                related_files=[edited_file] + forgot_files,
                tags=_memory_tags("auto-learned", "propagation"),
                source="learn_context",
                confidence=0.5,
                evidence={
                    "edited_file": edited_file,
                    "forgot_files": forgot_files,
                },
                db_path=db_path,
                project=project,
            )
            if entry.get("action") == "inserted":
                    memories_added.append(entry)

        elif event_type == "confirm" and event.get("memory_id"):
            # Compatibility-only signal; runtime confirmation remains blocked.
            from odibi_anchor.codebase.memory_context import confirm_memory
            updated = confirm_memory(str(root), event["memory_id"], db_path=db_path)
            if updated and updated.get("action") == "promoted":
                memories_confirmed.append(updated)
                findings.append(f"Confirmed memory {event['memory_id']} (now {updated['status']})")
            elif updated:
                findings.append(
                    f"Memory {event['memory_id']} was not promoted: {updated.get('reason', 'insufficient evidence')}"
                )

        elif event_type == "discovery":
            # Novel insight or finding worth recording
            from odibi_anchor.codebase.memory_context import append_memory
            # Accept 'detail' (the documented/validated key) as well as text/content
            # so the form the gate tells agents to use actually records.
            text = event.get("text") or event.get("content") or event.get("detail") or ""
            if text:
                entry = append_memory(
                    str(root),
                    entry_type="discovery",
                    content=text[:200],
                    tags=_memory_tags("auto-learned", "discovery"),
                    source=event.get("source", "learn_context"),
                    confidence=0.5,
                    evidence={"text": text, "source": event.get("source", "")},
                    db_path=db_path,
                    project=project,
                )
                if entry.get("action") == "inserted":
                    memories_added.append(entry)

        elif event_type == "pattern":
            # Reusable pattern found during session
            from odibi_anchor.codebase.memory_context import append_memory
            text = event.get("text") or event.get("content") or event.get("detail") or ""
            if text:
                entry = append_memory(
                    str(root),
                    entry_type="pattern",
                    content=text[:200],
                    tags=_memory_tags("auto-learned", "pattern"),
                    source="learn_context",
                    confidence=0.5,
                    evidence={"text": text, "context": event.get("context", "")},
                    db_path=db_path,
                    project=project,
                )
                if entry.get("action") == "inserted":
                    memories_added.append(entry)

        elif event_type == "tool_call":
            # Record tool effectiveness as a memory
            from odibi_anchor.codebase.memory_context import append_memory
            tool_name = event.get("tool", "")
            useful = event.get("useful", True)
            result = event.get("result", "")
            if tool_name:
                entry = append_memory(
                    str(root),
                    entry_type="tool_call",
                    content=f"Tool '{tool_name}': {'useful' if useful else 'not useful'} — {result[:100]}",
                    tags=_memory_tags("auto-learned", "tool-effectiveness"),
                    source="learn_context",
                    confidence=0.6 if useful else 0.3,
                    evidence={"tool": tool_name, "useful": useful, "result": result[:200]},
                    db_path=db_path,
                    project=project,
                )
                if entry.get("action") == "inserted":
                    memories_added.append(entry)

        elif event_type in ("decision", "convention", "gotcha", "failure_pattern", "preference"):
            # General-purpose memory types — route to append_memory with matching entry_type
            from odibi_anchor.codebase.memory_context import append_memory
            text = event.get("text") or event.get("content") or event.get("detail") or ""
            if text:
                entry = append_memory(
                    str(root),
                    entry_type=event_type,
                    content=text[:200],
                    tags=list(event.get("tags") or _memory_tags("auto-learned", event_type))
                    + ([f"problem:{problem_id}"] if problem_id and event.get("tags") else []),
                    source=event.get("source", "learn_context"),
                    confidence=event.get("confidence", 0.5),
                    evidence={"text": text, "context": event.get("context", "")},
                    db_path=db_path,
                    project=project,
                )
                if entry.get("action") == "inserted":
                    memories_added.append(entry)

        else:
            # Unknown event type — warn but don't crash
            unknown_events_skipped += 1
            findings.append(
                f"WARN: Unknown event type '{event.get('type')}' — skipped. "
                f"Supported: {', '.join(sorted(_SUPPORTED_EVENT_TYPES))}. "
                f'Example: {{"type": "discovery", "detail": "what you learned"}}'
            )

    # Log telemetry
    if tool_usage:
        telemetry_path = root / _TELEMETRY_FILE
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        for usage in tool_usage:
            record = {
                "timestamp": timestamp,
                "tool": usage.get("tool", ""),
                "was_helpful": usage.get("was_helpful", True),
                "outcome": usage.get("outcome", ""),
            }
            telemetry_logged.append(record)
            with open(telemetry_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    # --- Named Trails: distill session into replayable trail ---
    trail_id = ""
    try:
        from odibi_anchor.codebase._memory_db import record_trail as _record_trail
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS, get_state as _get_trail_state
        _tstate = _get_trail_state()
        _task_goal = _tstate.get("task_goal", "")
        _sess_name = _tstate.get("session_name", "")

        # Only create trail if session has meaningful actions and a goal
        if _task_goal and len(_SESSION_TIMINGS) >= 3:
            # Distill action sequence into trail steps
            trail_steps = []
            for timing in _SESSION_TIMINGS:
                action = timing.get("action", "")
                if action in ("status", "memory", "audit_history", "new_session"):
                    continue  # Skip boilerplate actions
                trail_steps.append({
                    "action": action,
                    "detail": timing.get("detail", "")[:200],
                    "outcome": timing.get("outcome", "")[:200],
                })
            if len(trail_steps) >= 2:
                trail_id = _record_trail(
                    db_path,
                    name=_sess_name or "unnamed_trail",
                    description=f"Auto-captured from session: {_sess_name}",
                    task_goal=_task_goal,
                    tags=_tstate.get("tags", []),
                    steps=trail_steps[:20],  # Cap at 20 steps
                )
                if trail_id:
                    findings.append(f"Trail captured: '{_sess_name}' ({len(trail_steps)} steps).")
    except Exception:
        pass  # SILENT-OK: trail recording is best-effort

    metrics = {
        "events_processed": len(session_events),
        "memories_added": len(memories_added),
        "memories_confirmed": len(memories_confirmed),
        "telemetry_logged": len(telemetry_logged),
        "unknown_events_skipped": unknown_events_skipped,
        "co_occurrence_pairs_updated": 0,
        "trail_recorded": trail_id[:8] if trail_id else "",
    }

    if memories_added:
        findings.append(f"Auto-recorded {len(memories_added)} new memory entries.")
        for m in memories_added:
            findings.append(f"  New {m.get('type', '?')}: {m.get('content', '(deduped)')[:80]}")
    else:
        findings.append("No new patterns to record — all events matched existing knowledge.")

    if telemetry_logged:
        helpful = sum(1 for t in telemetry_logged if t["was_helpful"])
        findings.append(f"Logged {len(telemetry_logged)} tool usage records ({helpful} helpful).")

    summary = (
        f"Compatibility learning recovery: {len(memories_added)} new memories, "
        f"{len(memories_confirmed)} confirmed, "
        f"{len(telemetry_logged)} telemetry records"
    )

    learn_actions = [
        # ── Graph wiring (audit fix) ──
        'SHOULD: Run anchor("snapshot", decisions=[...], next_steps=[...]) to capture full session state.',
        'SHOULD: Run anchor("snapshot", mode="handoff", summary="task", state="...") if passing to next session.',
        'COULD: Run anchor("memory_stats") to review memory health.',
    ]
    if memories_added:
        learn_actions.append(
            "MUST: Review new memories for accuracy — call reject_memory(root, entry_id) on any false positives."
        )
    if memories_confirmed:
        learn_actions.append(
            "MUST: Confirmed memories will score higher in future known_bad_change_context checks."
        )
    learn_actions.append(
        "INFO: The next accepted task retrieves bounded task memory automatically; "
        "do not add a standalone startup query."
    )

    # Post-session retrospective trigger
    from odibi_anchor._utils._session_state import _SESSION_TIMINGS
    error_actions = [t for t in _SESSION_TIMINGS if t.get("action") in ("trace", "error")]
    total_actions = len(_SESSION_TIMINGS)
    if len(error_actions) >= 4 or total_actions >= 20:
        learn_actions.append(
            f"MUST: Session had {len(error_actions)} error/trace actions across {total_actions} total actions. "
            f'Run anchor("task", mode="retrospective") next session to analyze patterns.'
        )

    ctx = build_base_context(
        kind="learn_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=[],
        samples={},
        suggested_next_actions=learn_actions,
        memories_added=memories_added,
        memories_confirmed=memories_confirmed,
        telemetry_logged=telemetry_logged,
    )

    if output_format == "markdown":
        return render_learn_report(ctx)
    return ctx


def render_learn_report(ctx: dict[str, Any]) -> str:
    """Render compatibility-only historical learning recovery output as Markdown."""
    lines = render_header_lines(ctx, "Compatibility Learning Recovery")
    lines.extend(render_metrics_lines(ctx["metrics"]))
    lines.extend(render_bullet_section(ctx.get("findings", []), "## Learnings"))
    return "\n".join(lines)
