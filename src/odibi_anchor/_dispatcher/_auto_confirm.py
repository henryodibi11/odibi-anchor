"""Auto-confirm wrappers — extracted from anchor() closures in agent_init.py.

Each function replaces a closure that captured anchor()'s locals. The captured
state is now passed explicitly as parameters.
"""
from __future__ import annotations


def record_surfaced_result(result, *, db_path=None, primary_keys=("entries",),
                           associated_key="associated_entries") -> int:
    """Record only IDs present in a dispatcher-visible final retrieval result."""
    if not isinstance(result, dict):
        return 0
    ids = []
    for key in (*primary_keys, associated_key):
        for entry in result.get(key, []) or []:
            if isinstance(entry, dict):
                entry_id = entry.get("id") or entry.get("memory_id")
                if entry_id:
                    ids.append(entry_id)
    if not ids:
        return 0
    from odibi_anchor.codebase._memory_db import record_surfaced
    from odibi_anchor.codebase.memory_context import _get_current_session_id
    return record_surfaced(db_path, entry_ids=ids,
                           session_id=_get_current_session_id() or "")


def memory_with_auto_confirm(
    root, args, kwargs, *, memory_context_fn, render_fn,
):
    """Run the read-only anchor("memory") query before optional rendering."""
    saved_format = kwargs.get("output_format", "dict")
    kwargs["output_format"] = "dict"
    kwargs.setdefault("surfaced", False)
    result = memory_context_fn(root, *args, **kwargs)
    if saved_format == "markdown":
        return render_fn(result)
    return result


def error_with_auto_confirm(
    root, args, kwargs, *, failure_pattern_fn, render_fn,
):
    """Run anchor("known_error") in structured form before optional rendering."""
    saved_format = kwargs.get("output_format", "dict")
    kwargs["output_format"] = "dict"
    result = failure_pattern_fn(args[0] if args else "", root=root, **kwargs)
    if saved_format == "markdown":
        return render_fn(result)
    return result


def known_bad_with_auto_confirm(
    root, args, kwargs, *, known_bad_fn, add_tag_fn, render_fn,
):
    """Run the read-only anchor("known_bad") query before optional rendering."""
    saved_format = kwargs.get("output_format", "dict")
    kwargs["output_format"] = "dict"
    result = known_bad_fn(root, *args, **kwargs)
    if saved_format == "markdown":
        return render_fn(result)
    return result


def inject_spec_tag(kwargs: dict, spec_name) -> dict:
    """Append spec:<name> to kwargs['tags'] when a spec is active (S-4).

    Used by anchor("save") so manual memories are linked to the originating spec,
    the same way anchor("learn") learnings are. Mutates and returns kwargs.
    """
    if spec_name:
        tags = list(kwargs.get("tags") or [])
        st = f"spec:{spec_name}"
        if st not in tags:
            tags.append(st)
        kwargs["tags"] = tags
    return kwargs


def _link_learnings_to_spec(result, spec_name, db_path) -> int:
    """Tag each newly-added learning with spec:<name> (S-4). Returns count tagged.

    Co-locating the tag with the entry (same DB) closes the spec→production loop:
    a future anchor("spec", "create", "<name>") resurfaces these via the spec tag.
    """
    if not spec_name or not isinstance(result, dict):
        return 0
    from odibi_anchor.codebase._memory_db import add_tag_to_entry
    tagged = 0
    for m in result.get("memories_added", []):
        eid = m.get("id") if isinstance(m, dict) else None
        if eid and add_tag_to_entry(db_path, entry_id=eid, tag=f"spec:{spec_name}"):
            tagged += 1
    return tagged


def _prepare_learn_events(args, kwargs, *, required: bool):
    """Normalize anchor("learn") arguments and validate required learning content."""
    if "session_events" not in kwargs and args:
        if len(args) != 1:
            raise TypeError('anchor("learn") accepts one event list or session_events=[...]')
        kwargs["session_events"] = args[0]
        args = ()
    elif "session_events" not in kwargs:
        kwargs["session_events"] = []

    events = kwargs["session_events"]
    if required and not events:
        raise RuntimeError(
            "BLOCKED: anchor(\"learn\") requires substantive session_events after file changes or learn debt.\n"
            "Provide: anchor(\"learn\", session_events=[{\"type\": \"discovery|decision|convention\", "
            "\"detail\": \"what you learned\"}])"
        )
    if required and isinstance(events, list):
        substantive = any(
            bool(event.get("memory_id")) if event.get("type") == "confirm" else
            bool(event.get("forgot")) if event.get("type") == "edit" else
            bool(event.get("tool")) if event.get("type") == "tool_call" else
            len(" ".join(str(event.get(key) or "") for key in (
                "detail", "content", "text", "fix", "result", "file", "forgot",
            )).strip()) >= 10
            for event in events
            if isinstance(event, dict)
        )
        if not substantive:
            raise RuntimeError(
                "BLOCKED: anchor(\"learn\") session_events contain minimal detail (<10 chars each).\n"
                "Provide substantive learnings using detail, content, or text."
            )
    return args


def learn_with_auto_confirm(
    root, args, kwargs, *,
    learn_fn, render_fn,
    session_files_changed, session_files_created, session_timings, session_state,
    learning_project_id,
    compliance_audit_fn, db_insert_audit_fn, capture_health_fn,
):
    """Run the compatibility-only legacy learn path with audit and health snapshot."""
    if any(name in kwargs for name in ("_checkpoint_capability", "_deferred", "no_commit")):
        raise ValueError("deferred learn mode is internal-only")
    saved_format = kwargs.get("output_format", "dict")
    kwargs["output_format"] = "dict"
    args = _prepare_learn_events(
        args, kwargs,
        required=bool(session_files_changed or session_state.prior_learn_debt),
    )
    checkpoint_marker = session_state.checkpoint_in_progress
    if (checkpoint_marker and checkpoint_marker.get("defer_learn") and
            checkpoint_marker.get("learn_phase") != "commit"):
        from odibi_anchor._utils.contract import build_base_context
        events = kwargs.get("session_events", args[0] if args else [])
        if not isinstance(events, list) or any(not isinstance(item, dict) or not item.get("type")
                                               for item in events):
            raise TypeError("checkpoint learning events must be typed dictionaries")
        return build_base_context(
            kind="learn", subject="checkpoint-validation",
            summary="Checkpoint learning input validated; persistence is deferred.",
            metrics={"memories_added": 0, "events_validated": len(events), "deferred": True},
            findings=[], risks=[], samples={}, suggested_next_actions=[],
        )
    # Capture the intended row before legacy work. A later cycle must never be
    # selected merely because it became active while learn_fn was running.
    from odibi_anchor.codebase.structured_learning_context import (
        active_learning_obligation, close_learning_obligation_legacy,
    )
    owner = {
        "project_id": learning_project_id,
        "task_window_id": session_state.task_window_id,
    }
    obligation = active_learning_obligation(**owner)
    result = learn_fn(root, *args, **kwargs)

    # Core legacy learning is intentionally at-least-once. Only after it has
    # succeeded may it close structured debt; no structured item is synthesized.
    if obligation is not None:
        closed = close_learning_obligation_legacy(obligation["obligation_id"], **owner)
        session_state.latest_closed_obligation_id = closed["obligation_id"]
        # Reconcile exactly as structured assessment does. If another cycle was
        # activated while core legacy work ran, it remains the active debt.
        active = active_learning_obligation(**owner)
        session_state.learning_obligation_id = (
            active["obligation_id"] if active is not None else None
        )
        session_state.prior_learn_debt = active is not None

    # ── S-4: link new learnings to the active spec (spec→production loop) ──
    try:
        _link_learnings_to_spec(
            result, getattr(session_state, "active_spec_name", None),
            kwargs.get("db_path"),
        )
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("learn_spec_tag", _exc)

    # ── Hard compliance check: flag sessions that skipped planning ──
    _task_ran = any(
        t["action"] == "task" and t["error"] is None
        for t in session_timings
    )
    if session_files_changed and not _task_ran:
        _violation_msg = (
            "** COMPLIANCE FAILURE: Files were modified without planning.\n"
            f"  Files changed: {sorted(session_files_changed)}\n"
            "  anchor(\"task\") was NEVER called this session.\n"
            "  This session's compliance score will be recorded as POOR.\n"
            "  EVERY task requires anchor(\"task\") — no exceptions."
        )
        # Inject violation into learn result so the agent sees it
        if isinstance(result, dict):
            result.setdefault("risks", []).insert(0, _violation_msg)
            result.setdefault("findings", []).insert(0, "VIOLATION: No planning before file modifications")
    # Auto-compliance audit: score session and persist to session_audits table
    try:
        if session_files_changed:  # Only audit sessions that modified files
            audit = compliance_audit_fn()
            _action_sequence = [t["action"] for t in session_timings if t["error"] is None]
            # Collect agent-provided learnings as strings
            _learnings_list = []
            _agent_events = kwargs.get("session_events", args[0] if args else [])
            if isinstance(_agent_events, list):
                for _evt in _agent_events[:5]:
                    if isinstance(_evt, dict):
                        _detail = _evt.get("detail", _evt.get("content", ""))
                        if _detail:
                            _learnings_list.append(str(_detail)[:200])
            # Write structured audit to dedicated table
            db_insert_audit_fn(
                project=str(root),
                score=audit["score"],
                max_score=audit["max_score"],
                rating=audit["rating"],
                gaps=audit["gaps"],
                files_changed=sorted(session_files_changed),
                files_created=sorted(session_files_created),
                actions=_action_sequence,
                total_actions=audit["stats"]["total_actions"],
                total_time_ms=audit["stats"]["total_time_ms"],
                errors=audit["stats"]["errors_encountered"],
                learnings=_learnings_list,
                framework_epoch="delivery-v2",
            )
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("learn_audit_persist", _exc)
    # ── Cross-session health snapshot (exit checkpoint) ──
    try:
        if session_files_changed:
            capture_health_fn(
                root,
                session_files_changed,
                session_timings,
            )
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("session_health_capture", _exc)
    # ── Auto-populate session notebook with learnings ──
    if session_state.notebook_path:
        try:
            from odibi_anchor._dispatcher._memory import append_learn_to_notebook
            _agent_events = kwargs.get("session_events", args[0] if args else [])
            append_learn_to_notebook(
                session_state.notebook_path, result,
                session_events=_agent_events if isinstance(_agent_events, list) else None,
            )
        except Exception:
            pass  # SILENT-OK: notebook append is non-blocking telemetry
    if saved_format == "markdown":
        return render_fn(result)
    return result
