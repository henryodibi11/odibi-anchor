"""_compliance.py — Compliance, status, and audit functions.

Extracted from agent_init.py Phase 3 (revamp spec).
Session compliance scoring, status dashboard, skills registry, audit history.
"""
import os as _os
import time as _time

from odibi_anchor._dispatcher._enforcement import closure_satisfied

from odibi_anchor._utils._session_state import (
    _SESSION_FILES_CHANGED,
    _SESSION_FILES_CREATED,
    _SESSION_TIMINGS,
    get_degraded as _get_degraded,
    _SESSION_STATE,
)
from odibi_anchor._utils.contract import build_base_context
from odibi_anchor.codebase._memory_db import (
    query_audits as _db_query_audits,
    audit_trend as _db_audit_trend,
)


def _compliance_audit() -> dict:
    """Compute an objective compliance score (0-15) from session timings.

    Analyzes _SESSION_TIMINGS chronologically against a 15-point checklist.
    Returns dict with score, gaps (deductions), and stats. Pure computation —
    no file I/O, no subprocess. Called automatically by anchor("learn").

    Scoring:
        Each of 15 checks is worth 1 point. Deductions are binary (0 or 1).
        Score = 15 - len(gaps).

    Checks 1-10: Original compliance checks (planning, ordering, coverage)
    Checks 11-15: Efficiency/quality checks (error ratio, retries, diversity,
                  duration, first-edit latency)
    """
    def _passed(t):
        # ``passed`` was added in delivery-v2.  Treat legacy timing records as
        # successful only when they did not raise; current dispatcher records
        # always carry the explicit semantic result.
        return t.get("error") is None and t.get("passed", True) is True

    closure_start = 0
    for index in range(len(_SESSION_TIMINGS) - 1, -1, -1):
        timing = _SESSION_TIMINGS[index]
        if closure_satisfied(timing):
            prior_gate = any(
                item["action"] in {"gate", "checkpoint"} and _passed(item)
                for item in _SESSION_TIMINGS[:index]
            )
            if prior_gate or timing["action"] == "checkpoint":
                closure_start = index + 1
                break
    # A new planned feature starts a fresh delivery window even when the prior
    # feature ended at a manual gate and its learn has not yet been recorded.
    # This prevents the prior feature's successful checks from masking failures.
    for index in range(closure_start, len(_SESSION_TIMINGS)):
        timing = _SESSION_TIMINGS[index]
        if timing["action"] != "task" or not _passed(timing):
            continue
        if any(
            item["action"] in {"gate", "checkpoint"} and _passed(item)
            for item in _SESSION_TIMINGS[closure_start:index]
        ):
            closure_start = index
    closure_timings = _SESSION_TIMINGS[closure_start:]
    actions_run = [t["action"] for t in closure_timings if _passed(t)]
    actions_set = set(actions_run)
    enclosing = _SESSION_STATE.checkpoint_in_progress
    enclosing_passed = bool(enclosing and enclosing.get("preflight_passed")
                            and enclosing.get("tests_passed")
                            and enclosing.get("gate_passed")
                            and enclosing.get("learn_started"))
    has_py_changes = any(f.endswith(".py") for f in _SESSION_FILES_CHANGED)
    has_any_changes = len(_SESSION_FILES_CHANGED) > 0
    total_actions = len(_SESSION_TIMINGS)

    gaps = []

    # 1. Skill/bootstrap: session exists (we're running, so bootstrap happened)
    # Score: always 1 (if audit runs, bootstrap ran)

    # 2. Planning before implementation
    planning_actions = {"task"}
    edit_actions = {"touched", "safe", "semantic"}
    first_planning_idx = next((i for i, t in enumerate(closure_timings) if t["action"] in planning_actions and _passed(t)), None)
    first_edit_idx = next((i for i, t in enumerate(closure_timings) if t["action"] in edit_actions and _passed(t)), None)

    if has_any_changes and first_planning_idx is None:
        gaps.append("No planning (task) before implementation")
    elif has_any_changes and first_planning_idx is not None and first_edit_idx is not None:
        if first_planning_idx > first_edit_idx:
            gaps.append("Planning ran AFTER first edit (must come before)")

    # 3. known_bad before .py edits
    if has_py_changes and "known_bad" not in actions_set:
        gaps.append("No known_bad check before .py edits")

    # 4. The changed-file registry is canonical proof of registration.
    files_count = len(_SESSION_FILES_CHANGED)

    # 5. preflight ran (for .py changes)
    if has_py_changes and "preflight" not in actions_set and not enclosing_passed:
        gaps.append("No preflight run despite .py file changes")

    # 6. Tests ran
    if has_py_changes and "test" not in actions_set and "checkpoint" not in actions_set and not enclosing_passed:
        gaps.append("No tests run (test or checkpoint) despite file changes")

    # 7. Gate passed
    gate_entries = [t for t in closure_timings if t["action"] == "gate" and _passed(t)]
    checkpoint_entries = [t for t in closure_timings if t["action"] == "checkpoint" and _passed(t)]
    if has_any_changes and not gate_entries and not checkpoint_entries and not enclosing_passed:
        gaps.append("No gate or checkpoint run despite file changes")

    # 8. Feature boundaries cannot be inferred from raw file count.

    # 9. Learn at session end — auto-pass (we're inside learn)

    # 10. (Reserved — covered by #4)

    # --- Efficiency & quality checks (11-15) ---

    # 11. Error-to-action ratio: thrashing indicator
    error_count = sum(1 for t in _SESSION_TIMINGS if t.get("error") is not None)
    if total_actions >= 5 and error_count / total_actions > 0.3:
        gaps.append(f"High error ratio ({error_count}/{total_actions} = {error_count/total_actions:.0%}) — indicates thrashing")

    # 12. Retry loops: ignore expected per-item registration actions.
    repeatable_actions = {"skill_loaded", "touched"}
    if len(_SESSION_TIMINGS) >= 3:
        consecutive = 1
        max_consecutive_action = None
        for i in range(1, len(_SESSION_TIMINGS)):
            action = _SESSION_TIMINGS[i]["action"]
            if action == _SESSION_TIMINGS[i-1]["action"]:
                consecutive += 1
                if consecutive >= 3 and action not in repeatable_actions:
                    max_consecutive_action = action
            else:
                consecutive = 1
        if max_consecutive_action:
            gaps.append(f"Retry loop detected: '{max_consecutive_action}' ran 3+ times consecutively")

    # 13. Tool diversity: editing without verifying
    if files_count > 2 and len(actions_set) < 3:
        gaps.append(f"Low tool diversity ({len(actions_set)} unique actions for {files_count} files) — editing without verifying")

    # 14. Session duration without checkpoint/status (drift risk)
    total_time_ms = sum(t["elapsed_ms"] for t in _SESSION_TIMINGS)
    status_or_checkpoint = {"status", "checkpoint"}
    has_orientation = bool(actions_set & status_or_checkpoint)
    if total_time_ms > 300_000 and not has_orientation:
        gaps.append(f"Long session ({total_time_ms/1000:.0f}s) without status or checkpoint — drift risk")

    # 15. First-edit latency: cowboy coding (edit before any read/plan action)
    read_plan_actions = {"task", "lookup", "map", "known_bad", "status"}
    first_read_idx = next((i for i, t in enumerate(closure_timings) if t["action"] in read_plan_actions and _passed(t)), None)
    if has_any_changes and first_edit_idx is not None:
        if first_read_idx is None:
            gaps.append("Edited files without any prior read/plan action (cowboy coding)")
        elif first_edit_idx < first_read_idx:
            gaps.append("First edit came before first read/plan action (cowboy coding)")

    # Compute score
    max_score = 15
    score = max(0, max_score - len(gaps))

    return {
        "score": score,
        "max_score": max_score,
        "gaps": gaps,
        "stats": {
            "total_actions": total_actions,
            "total_time_ms": round(total_time_ms, 1),
            "files_changed": files_count,
            "files_created": len(_SESSION_FILES_CREATED),
            "errors_encountered": error_count,
            "unique_actions": len(actions_set),
        },
        "rating": (
            "excellent" if score >= 13 else
            "good" if score >= 10 else
            "needs_improvement" if score >= 7 else
            "poor"
        ),
    }




def _skills_registry(root, session_state=None, **kwargs):
    """List central Odibi Anchor skills and their session loading status."""
    import os
    from odibi_anchor._utils.contract import build_base_context

    output_format = kwargs.pop("output_format", "markdown")

    from odibi_anchor._dispatcher._boot import _resolve_skills_dir
    skills_dir = _resolve_skills_dir()

    from odibi_anchor._dispatcher._guidance import guidance_registry_metadata
    available_skills = []
    if os.path.isdir(skills_dir):
        # Preserve the historical empty-directory discovery behavior, while
        # treating any non-empty guidance tree as a registry that must validate.
        # In particular, a partially copied tree must not silently look empty.
        directory_entries = list(os.scandir(skills_dir))
        metadata_entries = guidance_registry_metadata(skills_dir) if directory_entries else ()
        for metadata in metadata_entries:
            entry = metadata["name"]
            skill_path = os.path.join(skills_dir, entry, "SKILL.md")
            description = ""
            if os.path.isfile(skill_path):
                # Read frontmatter for description
                try:
                    with open(skill_path, "r", encoding="utf-8") as f:
                        in_frontmatter = False
                        for line in f:
                            if line.strip() == "---":
                                if in_frontmatter:
                                    break
                                in_frontmatter = True
                                continue
                            if in_frontmatter and line.startswith("description:"):
                                description = line.split(":", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass  # SILENT-OK: skill description parsing is best-effort
            available_skills.append({**metadata, "description": description})

    # Check which skills have been "loaded" this session (via timing records)
    loaded_skills = set(getattr(session_state, "skills_loaded", ()))

    findings = []
    for s in available_skills:
        status = "✓ loaded" if s["name"] in loaded_skills else "○ not loaded"
        findings.append(f"{status} — **{s['name']}**: {s['description']}")

    ctx = build_base_context(
        kind="skills_registry",
        subject="skills",
        summary=f"{len(available_skills)} skills available, {len(loaded_skills)} loaded this session",
        metrics={
            "total_skills": len(available_skills),
            "loaded_count": len(loaded_skills),
            "loaded_skills": sorted(loaded_skills),
        },
        findings=findings,
        risks=[],
        samples={},
        suggested_next_actions=[
            "Load skills matching your current task from the list above.",
        ],
        skills=available_skills,
    )

    if output_format == "markdown":
        lines = ["# Available Skills\n"]
        lines.append(f"**{len(available_skills)} skills available, {len(loaded_skills)} loaded this session**\n")
        lines.append("| Skill | Status | Description |")
        lines.append("| --- | --- | --- |")
        for s in available_skills:
            status = "✓ loaded" if s["name"] in loaded_skills else "○"
            lines.append(f"| {s['name']} | {status} | {s['description']} |")
        if ctx.get("suggested_next_actions"):
            lines.append("\n## Next Actions\n")
            for a in ctx["suggested_next_actions"]:
                lines.append(f"- {a}")
        return "\n".join(lines)
    return ctx



def _build_status_suggestions(obligations):
    """Build next-action suggestions from current-session obligations."""
    suggestions = []

    # Priority 1: Pending obligations
    if obligations:
        suggestions.append(f"MUST: Run anchor('{obligations[0].split(' ')[0]}')")

    if not obligations:
        suggestions.append("Session clean — ready for delivery or next feature.")

    # Planning enforcement
    task_actions = [t for t in _SESSION_TIMINGS if t.get("action") == "task"]
    if not task_actions and _SESSION_FILES_CHANGED:
        suggestions.insert(0, 'MUST: Files changed but no planning detected. Run anchor("task") IMMEDIATELY.')
    elif not task_actions:
        suggestions.append('SHOULD: Run anchor("task") before starting work.')

    # Test enforcement
    test_actions = [t for t in _SESSION_TIMINGS if t.get("action") == "test"]
    py_files_changed = [f for f in _SESSION_FILES_CHANGED if f.endswith(".py")]
    if py_files_changed and not test_actions:
        suggestions.append(f'SHOULD: {len(py_files_changed)} .py file(s) changed but no tests run. Load skills/writing-tests/SKILL.md when authoring tests.')

    # Self-review hint (before gate)
    gate_actions = [t for t in _SESSION_TIMINGS if t.get("action") in ("gate", "checkpoint")]
    if _SESSION_FILES_CHANGED and not gate_actions:
        suggestions.append('SHOULD: Review complete diff before gate.')

    # Thread discipline
    if len(_SESSION_TIMINGS) >= 15:
        suggestions.append(f'ALERT: Session has {len(_SESSION_TIMINGS)} actions. Consider snapshot(mode="handoff") to fresh thread.')

    return suggestions or ["Session clean — ready for delivery or next feature."]




# D-008: data actions surfaced in anchor("status") (Data & Profiling + Composed Workflows).
_DATA_ACTIONS = frozenset({
    "profile_table", "microscope", "case_file", "quality", "validate", "duplicate",
    "diff", "schema_diff", "contract", "transform", "apply_transform", "apply_sql",
    "rollback", "coerce_check", "coerce_fix", "pre_join", "pre_merge",
    "diagnose_empty", "explain_row", "reconcile", "investigate", "debug",
    "trace_row", "evolve",
})


def _status(root, manifest, session_frame, session_context, *args, **kwargs) -> dict:
    """Session health dashboard — zero-parameter read-only introspection.

    Returns aggregated session state: files changed/created, action count,
    timing, pending obligations, estimated risk level, and memory stats.
    Designed to complete in <100ms (no file I/O, no subprocess, no AST parsing).

    Usage:
        anchor("status")
    """
    output_format = kwargs.pop("output_format", "dict")
    from odibi_anchor._utils._session_state import _SESSION_STATE

    # ── Session state (from singleton — no I/O) ──
    total_changed = len(_SESSION_FILES_CHANGED)
    total_created = len(_SESSION_FILES_CREATED)
    total_actions = len(_SESSION_TIMINGS)
    total_time_ms = round(sum(t["elapsed_ms"] for t in _SESSION_TIMINGS), 1)
    last_action = _SESSION_TIMINGS[-1] if _SESSION_TIMINGS else None

    # ── Lightweight obligation check (no full gate run) ──
    obligations_pending = []
    has_py_files = any(f.endswith(".py") for f in _SESSION_FILES_CHANGED)
    has_any_changes = total_changed > 0

    # Check timing records for fulfilled obligations
    actions_run = {t["action"] for t in _SESSION_TIMINGS if t.get("error") is None}

    # checkpoint internally runs preflight, test, gate, and learn — treat as satisfying all
    _checkpoint_ran = "checkpoint" in actions_run
    if has_py_files and "preflight" not in actions_run and not _checkpoint_ran:
        obligations_pending.append("preflight (MUST for .py changes)")
    if has_any_changes and "test" not in actions_run and not _checkpoint_ran:
        obligations_pending.append("test (MUST before gate)")
    if has_any_changes and "gate" not in actions_run and not _checkpoint_ran:
        obligations_pending.append("gate (MUST before delivery)")
    if has_any_changes and "learn" not in actions_run and not _checkpoint_ran:
        obligations_pending.append("learn (MUST after gate)")

    # ── Next required action (state-machine router) ──
    from odibi_anchor._dispatcher._protocol import STARTUP_SEQUENCE
    _next_required = None
    for _step in STARTUP_SEQUENCE:
        if _step not in actions_run:
            _next_required = f'anchor("{_step}")'
            break
    if _next_required is None and obligations_pending:
        _first_obligation = obligations_pending[0].split(" ")[0]
        _next_required = f'anchor("{_first_obligation}")'

    # Risk estimation
    if not has_any_changes:
        estimated_risk = "low"
    elif has_py_files and "preflight" not in actions_run:
        estimated_risk = "high"
    elif len(obligations_pending) >= 3:
        estimated_risk = "high"
    elif len(obligations_pending) >= 1:
        estimated_risk = "medium"
    else:
        estimated_risk = "low"

    # Routine status reports only current-session learning activity. Store-wide
    # memory diagnostics belong to explicit memory-governance actions.
    session_learnings = 0
    learn_timings = [
        timing for timing in _SESSION_TIMINGS
        if timing["action"] == "learn" and timing.get("error") is None
    ]
    session_learnings = len(learn_timings)

    # ── Running compliance score ──
    task_actions = [t for t in _SESSION_TIMINGS if t.get("action") == "task"]
    test_actions = [t for t in _SESSION_TIMINGS if t.get("action") == "test"]
    py_files_changed = [f for f in _SESSION_FILES_CHANGED if f.endswith(".py")]
    assessment_actions = [
        t for t in _SESSION_TIMINGS
        if t.get("action") == "learning" and t.get("learning_command") == "assess"
    ]

    compliance_checks = 0
    compliance_score = 0
    if task_actions: compliance_score += 3  # Planning done
    compliance_checks += 3
    if test_actions and py_files_changed: compliance_score += 3  # Tests run
    elif not py_files_changed: compliance_score += 3  # No py files = no test needed
    compliance_checks += 3
    if len(_SESSION_TIMINGS) < 15: compliance_score += 2  # Thread not too long
    compliance_checks += 2
    if assessment_actions or not _SESSION_FILES_CHANGED:
        compliance_score += 2
    compliance_checks += 2

    # ── Historical compliance trend (from session_audits table) ──
    _compliance_trend = {}
    try:
        _compliance_trend = _db_audit_trend(project=str(root), limit=10)
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("compliance_trend", _exc)

    # ── Data-quality posture (D-008) ──
    from odibi_anchor._dispatcher._enforcement import data_quality_posture as _dqp
    _data_tools_used = sorted({
        t["action"] for t in _SESSION_TIMINGS
        if t.get("error") is None and t["action"] in _DATA_ACTIONS
    })
    _data_posture = _dqp(_SESSION_TIMINGS)

    # ── Build summary ──
    risk_badge = {"low": "✓", "medium": "⚠", "high": "✗"}[estimated_risk]
    summary = (
        f"{total_changed} files changed, "
        f"{len(obligations_pending)} obligations pending "
        f"(est. {estimated_risk} risk {risk_badge}), "
        f"{total_actions} actions in {total_time_ms}ms"
    )

    from odibi_anchor._utils.contract import build_base_context
    ctx = build_base_context(
        kind="session_status",
        subject="session",
        summary=summary,
        metrics={
            "files_changed": total_changed,
            "files_created": total_created,
            "total_actions": total_actions,
            "total_time_ms": total_time_ms,
            "last_action": last_action["action"] if last_action else None,
            "last_action_ms": last_action["elapsed_ms"] if last_action else None,
            "obligations_pending": len(obligations_pending),
            "estimated_risk": estimated_risk,
            "session_learnings": session_learnings,
            "candidate_entries": None,
            "tag_stats": {},
            "file_history": {},
            "memory_diagnostics": "deferred_to_explicit_governance",
            "compliance_score": f"{compliance_score}/{compliance_checks}",
            "compliance_trend": _compliance_trend if _compliance_trend else None,
            "manifest_state": "loaded" if manifest else "missing",
            "next_required_action": _next_required,
            "degraded_count": len(_get_degraded()),
            "frame_findings": len(getattr(session_frame, "findings", []) or []) if session_frame is not None else 0,
            "frame_risks": len(getattr(session_frame, "risks", []) or []) if session_frame is not None else 0,
            "data_tools_used": _data_tools_used,
        },
        findings=(
            ([f"Pending: {o}" for o in obligations_pending] if obligations_pending else ["All obligations fulfilled"])
            + (["DATA: " + _data_posture] if _data_posture else [])
        ),
        risks=[f"Risk: {estimated_risk} — {len(obligations_pending)} pending obligations"] if estimated_risk != "low" else [],
        samples={
            "files_changed": sorted(_SESSION_FILES_CHANGED),
            "files_created": sorted(_SESSION_FILES_CREATED),
        },
        suggested_next_actions=_build_status_suggestions(obligations_pending),
    )
    _task_context_present = _SESSION_STATE.active_task_profile is not None
    ctx["runtime"] = {
        "active_project": _SESSION_STATE.active_project,
        "active_problem": _SESSION_STATE.active_problem,
        "project_root": _SESSION_STATE.project_root,
        "artifact_root": _SESSION_STATE.artifact_root,
        "target_root": _SESSION_STATE.target_root,
        "session_id": _SESSION_STATE.session_id,
        "task_context_present": _task_context_present,
        "task_window_id": _SESSION_STATE.task_window_id if _task_context_present else None,
        "task_mode": _SESSION_STATE.active_task_mode if _task_context_present else None,
        "task_repository_baseline_present": (
            _task_context_present and _SESSION_STATE.task_repository_baseline is not None
        ),
        "loaded_source_revision": getattr(_SESSION_STATE, "runtime_loaded_revision", None),
        "current_source_revision": getattr(_SESSION_STATE, "runtime_current_revision", None),
        "loaded_source_fingerprint": getattr(
            _SESSION_STATE, "runtime_loaded_fingerprint", None,
        ),
        "current_source_fingerprint": getattr(
            _SESSION_STATE, "runtime_current_fingerprint", None,
        ),
        "source_revision_stale": getattr(_SESSION_STATE, "runtime_source_stale", False),
    }
    if ctx["runtime"]["source_revision_stale"]:
        ctx["risks"].append(
            "The dispatcher is stale relative to the current source revision; restart or "
            "rebootstrap before claiming current-source verification."
        )
    if _SESSION_STATE.active_problem and _SESSION_STATE.artifact_root:
        try:
            from odibi_anchor._dispatcher._problem import problem_action
            ctx["active_problem"] = problem_action(
                _SESSION_STATE.artifact_root,
                "resume",
                _SESSION_STATE.active_problem,
                output_format="dict",
            )
        except (FileNotFoundError, ValueError):
            ctx["active_problem"] = None

    # ── Degraded-subsystem warning (AXI #6: surface swallowed failures) ──
    _degraded = _get_degraded()
    if _degraded:
        _components = sorted({d["component"] for d in _degraded})
        ctx["suggested_next_actions"].insert(
            0,
            f"WARNING: {len(_degraded)} non-fatal failure(s) this session in: "
            f"{', '.join(_components)}. The system kept running but may be using "
            "partial state — re-run the affected action or check the environment.",
        )

    # ── Cross-session drift warnings (from previous session health snapshot) ──
    try:
        from odibi_anchor._dispatcher._session_health import check_cross_session_drift as _check_drift
        _drift_warnings = _check_drift(str(root))
        if _drift_warnings:
            # Prepend drift warnings to suggested_next_actions
            for _dw in reversed(_drift_warnings[:3]):  # Cap at 3, reversed to maintain order after insert
                ctx["suggested_next_actions"].insert(0, _dw)
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("status_drift_check", _exc)

    # ── Incomplete prior session warning (H-009) ──
    # Detect a previous session that changed files but never recorded a passing
    # gate (e.g. crashed mid-session). Only surface on the first status call of
    # this session (status itself is the only timing recorded so far).
    try:
        import json as _json_h
        _health_path = _os.path.join(str(root), ".anchor_session_health.json")
        if _os.path.exists(_health_path) and not _SESSION_TIMINGS[1:]:
            with open(_health_path, "r", encoding="utf-8") as _hf:
                _health = _json_h.load(_hf)
            _prior_files = _health.get("files_changed") or _health.get("file_hashes") or {}
            _prior_gate_ok = bool(_health.get("gate_passed", _health.get("overall_pass")))
            if _prior_files and not _prior_gate_ok:
                ctx["suggested_next_actions"].insert(
                    0,
                    f"WARNING: Previous session changed {len(_prior_files)} file(s) but did "
                    "not record a passing gate. Run anchor(\"diff\") to inspect leftover changes "
                    "and use canonical anchor(\"learning\", \"capture|assess\", ...) to clear "
                    "structured debt before starting new work.",
                )
    except Exception:
        pass  # SILENT-OK: incomplete-session check is non-blocking

    if output_format == "markdown":
        lines = [f"# Session Status\n"]
        lines.append(f"**{summary}**\n")
        lines.append(f"**Session ID:** `{ctx['runtime']['session_id']}`  ")
        lines.append(
            f"**Task context present:** `{ctx['runtime']['task_context_present']}`  "
        )
        lines.append(
            "**Task repository baseline present:** "
            f"`{ctx['runtime']['task_repository_baseline_present']}`  "
        )
        if ctx["runtime"]["task_context_present"]:
            lines.append(f"**Task mode:** `{ctx['runtime']['task_mode']}`  ")
            lines.append(f"**Task window:** `{ctx['runtime']['task_window_id']}`  ")
        if ctx["runtime"]["active_project"]:
            lines.append(f"**Active project:** `{ctx['runtime']['active_project']}`  ")
            lines.append(f"**Artifact root:** `{ctx['runtime']['artifact_root']}`  ")
            lines.append(f"**Target root:** `{ctx['runtime']['target_root'] or 'none'}`\n")
        if ctx.get("active_problem"):
            _problem = ctx["active_problem"]
            lines.append(
                f"**Active problem:** `{_problem['problem_id']}` — stage "
                f"{_problem['stage']}/7 — {_problem['status']}  "
            )
            lines.append(f"**Problem next action:** {_problem['next_action']}\n")
        if _next_required:
            lines.append(f"**▶ Next required action:** `{_next_required}`\n")
        lines.append("## Metrics\n")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        for k, v in ctx["metrics"].items():
            lines.append(f"| {k} | {v} |")
        if ctx["findings"]:
            lines.append("\n## Obligations\n")
            for f in ctx["findings"]:
                lines.append(f"- {f}")
        if ctx["samples"]["files_changed"]:
            lines.append("\n## Changed Files\n")
            for f in ctx["samples"]["files_changed"]:
                lines.append(f"- {f}")
        if _compliance_trend and _compliance_trend.get("total_audits", 0) > 0:
            lines.append("\n## Compliance Trend\n")
            _ct = _compliance_trend
            _trend_icon = {"improving": "📈", "declining": "📉", "stable": "➡️"}.get(_ct["trend"], "❓")
            lines.append(f"**Avg: {_ct['avg_score']}/{_ct['max_score']}** | Trend: {_trend_icon} {_ct['trend']} | Sessions: {_ct['total_audits']}")
            if _ct.get("recent_scores"):
                lines.append(f"Recent scores: {_ct['recent_scores']}")
            if _ct.get("top_gaps"):
                lines.append("\n| Gap | Occurrences |")
                lines.append("| --- | --- |")
                for _g in _ct["top_gaps"]:
                    lines.append(f"| {_g['gap']} | {_g['count']} |")
        if session_frame is not None and getattr(session_frame, 'findings', None):
            _fbc = session_frame.findings_by_category()
            _high_risks = [r.message for r in session_frame.risks if r.severity == "high"]
            lines.append("\n## Frame Summary\n")
            lines.append(f"**Findings: {len(session_frame.findings)} total | Risks: {len(session_frame.risks)}**")
            lines.append("| Category | Count |")
            lines.append("| --- | --- |")
            for _cat, _cnt in sorted(_fbc.items(), key=lambda x: -x[1]):
                lines.append(f"| {_cat} | {_cnt} |")
            if _high_risks:
                lines.append("\n**High risks:**")
                for _r in _high_risks[:3]:
                    lines.append(f"- {_r}")
        if ctx.get("suggested_next_actions"):
            lines.append("\n## Next Actions\n")
            for a in ctx["suggested_next_actions"]:
                lines.append(f"- {a}")
        return "\n".join(lines)
    return ctx



def _audit_history(root, *args, **kwargs) -> dict | str:
    """Show session compliance audit history and trends.

    Usage:
        anchor("audit_history")              # Last 10 audits
        anchor("audit_history", limit=20)    # Last 20 audits
    """
    output_format = kwargs.pop("output_format", "markdown")
    limit = kwargs.get("limit", 10)

    trend = _db_audit_trend(project=str(root), framework_epoch="delivery-v2", limit=limit)
    audits = _db_query_audits(project=str(root), limit=limit)

    from odibi_anchor._utils.contract import build_base_context
    ctx = build_base_context(
        kind="audit_history",
        subject="compliance",
        summary=f"{trend['total_audits']} audits | avg {trend['avg_score']}/{trend['max_score']} | trend: {trend['trend']}",
        metrics={
            "avg_score": trend["avg_score"],
            "max_score": trend["max_score"],
            "total_audits": trend["total_audits"],
            "trend": trend["trend"],
            "recent_scores": trend["recent_scores"],
        },
        findings=[f"Top gap: {g['gap']} ({g['count']}x)" for g in trend.get("top_gaps", [])],
        risks=["Compliance trending down"] if trend["trend"] == "declining" else [],
        samples={"audits": audits[:5]},
        suggested_next_actions=[
            "Address top recurring gaps to improve compliance scores.",
        ],
    )
    if output_format == "markdown":
        lines = ["# Compliance Audit History\n"]
        lines.append(f"**{ctx['summary']}**\n")
        _trend_icon = {"improving": "📈", "declining": "📉", "stable": "➡️"}.get(trend["trend"], "❓")
        lines.append(f"Trend: {_trend_icon} {trend['trend']}\n")
        if trend.get("top_gaps"):
            lines.append("## Top Recurring Gaps\n")
            lines.append("| Gap | Occurrences |")
            lines.append("| --- | --- |")
            for g in trend["top_gaps"]:
                lines.append(f"| {g['gap']} | {g['count']} |")
        if audits:
            lines.append("\n## Recent Sessions\n")
            lines.append("| Date | Epoch | Score | Rating | Gaps | Files |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for a in audits[:10]:
                date = a["created"][:10] if a.get("created") else "?"
                gap_count = len(a.get("gaps", []))
                file_count = len(a.get("files_changed", []))
                epoch = a.get("framework_epoch") or "legacy"
                lines.append(f"| {date} | {epoch} | {a['score']}/{a['max_score']} | {a['rating']} | {gap_count} gaps | {file_count} files |")
        return "\n".join(lines)
    return ctx
